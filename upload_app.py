# upload_app.py
import os, json, time, hashlib
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")
STRICT_REINGEST = os.environ.get("STRICT_REINGEST", "0").strip().lower() in ("1","true","yes","y")

if not DATABASE_URL: raise RuntimeError("DATABASE_URL required")
if not OPS_KEY: raise RuntimeError("OPS_KEY required")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
app = Flask(__name__)

# ---------------------- util ----------------------
def _guard(): return request.args.get("key") == OPS_KEY
def _forbid(): return Response("Forbidden", status=403)

def seconds_to_ts(base_dt, s):
    if s is None: return None
    try: return base_dt + timedelta(seconds=float(s))
    except Exception: return None

def _float(v):
    if v is None: return None
    try: return float(v)
    except Exception:
        try: return float(str(v))
        except Exception: return None

def _bool(v):
    if v is None: return None
    if isinstance(v, bool): return v
    s = str(v).strip().lower()
    return True if s in ("1","true","t","yes","y") else False if s in ("0","false","f","no","n") else None

def _time_s(val):
    """Accepts number-like or dict with timestamp/ts/time_s/t/seconds."""
    if val is None:
        return None
    if isinstance(val, (int, float, str)):
        return _float(val)
    if isinstance(val, dict):
        for k in ("timestamp", "ts", "time_s", "t", "seconds"):
            if k in val:
                return _float(val[k])
    return None

def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

def _sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def _get_json_from_sources():
    """
    Intake precedence:
      1) ?name=<relative/absolute> (tries /mnt/data/<name> then <app_root>/<name>)
      2) ?url=<direct-json-url>
      3) multipart file field 'file'
      4) raw JSON body
    """
    name = request.args.get("name")
    if name:
        paths = [name] if os.path.isabs(name) else [f"/mnt/data/{name}", os.path.join(os.getcwd(), name)]
        last_err = None
        for p in paths:
            try:
                with open(p, "rb") as f:
                    return json.load(f)
            except FileNotFoundError as e:
                last_err = e
        raise FileNotFoundError(f"File not found. Tried: {', '.join(paths)}")

    url = request.args.get("url")
    if url:
        import requests
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        return r.json()

    if "file" in request.files:
        return json.load(request.files["file"].stream)

    if request.data:
        return json.loads(request.data.decode("utf-8"))

    raise ValueError("No JSON supplied (use ?name=, ?url=, multipart file, or raw body).")

def init_db(engine):
    from db_init import run_init
    run_init(engine)

def init_views(engine):
    from db_views import run_views
    run_views(engine)

# -------- quantization helpers --------
def _quantize_time_to_fps(s, fps):
    if s is None or not fps:
        return s
    return round(round(float(s) * float(fps)) / float(fps), 5)

_INVALID_PUIDS = {"", "0", "none", "null", "nan"}
def _valid_puid(p):
    if p is None:
        return False
    s = str(p).strip().lower()
    return s not in _INVALID_PUIDS

def _quantize_time(s, fps):
    """Use fps if available, else stable 1ms grid to kill float jitter."""
    if s is None:
        return None
    if fps:
        return _quantize_time_to_fps(s, fps)
    return round(float(s), 3)  # 1ms

# ---------------------- mappers ----------------------
def _resolve_session_uid(payload, forced_uid=None, src_hint=None):
    if forced_uid:
        return str(forced_uid)

    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_uid", "video_uid", "video_id"):
        if payload.get(k): return str(payload[k])
        if meta.get(k):    return str(meta[k])

    fn = meta.get("file_name") or meta.get("filename")
    if not fn and src_hint:
        try:
            fn = os.path.splitext(os.path.basename(src_hint))[0]
        except Exception:
            pass
    if fn:
        return str(fn)

    fp = _sha1_hex(_canonical_json(payload))[:12]
    return f"sha1_{fp}"

def _resolve_fps(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("fps","frame_rate","frames_per_second"):
        if payload.get(k) is not None: return _float(payload[k])
        if meta.get(k) is not None: return _float(meta[k])
    return None

def _resolve_session_date(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_date","date","recorded_at"):
        raw = payload.get(k) if k in payload else meta.get(k)
        if raw:
            try: return datetime.fromisoformat(str(raw).replace("Z","+00:00")).astimezone(timezone.utc)
            except Exception: return None
    return None

def _base_dt_for_session(dt): return dt if dt else datetime(1970,1,1,tzinfo=timezone.utc)

# ---------- swing extraction (robust) ----------
_SWING_TYPES = {"swing","stroke","shot","hit","serve","forehand","backhand","volley","overhead","slice","drop","lob"}

def _extract_ball_hit_from_events(events):
    """Find ball-hit-like event in an events list."""
    if not isinstance(events, list): return (None, None, None)
    for ev in events:
        if not isinstance(ev, dict): continue
        label = (str(ev.get("type") or ev.get("label") or "")).lower()
        if label in {"ball_hit","contact","impact"}:
            ts = _time_s(ev.get("timestamp") or ev.get("ts") or ev.get("time_s") or ev.get("t"))
            loc = ev.get("location") or {}
            return ts, _float((loc or {}).get("x")), _float((loc or {}).get("y"))
    return (None, None, None)

def _normalize_swing_obj(obj):
    """
    Normalize a swing-like object.
    Returns dict with: suid, player_uid, start_s, end_s, ball_hit_s, ball_hit_x, ball_hit_y, serve, serve_type, meta
    """
    if not isinstance(obj, dict): return None

    suid = obj.get("id") or obj.get("swing_uid") or obj.get("uid")

    start_s = _time_s(obj.get("start_ts")) or _time_s(obj.get("start_s")) or _time_s(obj.get("start"))
    end_s   = _time_s(obj.get("end_ts"))   or _time_s(obj.get("end_s"))   or _time_s(obj.get("end"))
    if start_s is None and end_s is None:
        only_ts = _time_s(obj.get("timestamp") or obj.get("ts") or obj.get("time_s") or obj.get("t"))
        if only_ts is not None:
            start_s = end_s = only_ts

    bh_s = _time_s(obj.get("ball_hit_timestamp") or obj.get("ball_hit_ts") or obj.get("ball_hit_s"))
    bhx = bhy = None
    if bh_s is None and isinstance(obj.get("ball_hit"), dict):
        bh_s = _time_s(obj["ball_hit"].get("timestamp"))
        loc = obj["ball_hit"].get("location") or {}
        bhx = _float(loc.get("x")); bhy = _float(loc.get("y"))
    if bh_s is None:
        ev_bh_s, ev_bhx, ev_bhy = _extract_ball_hit_from_events(obj.get("events"))
        bh_s = ev_bh_s
        bhx = bhx if bhx is not None else ev_bhx
        bhy = bhy if bhy is not None else ev_bhy
    if (bhx is None or bhy is None) and isinstance(obj.get("ball_hit_location"), dict):
        bhx = _float(obj["ball_hit_location"].get("x")); bhy = _float(obj["ball_hit_location"].get("y"))

    label = (str(obj.get("type") or obj.get("label") or obj.get("stroke_type") or "")).lower()
    serve = _bool(obj.get("serve"))
    serve_type = obj.get("serve_type")
    if not serve and label in {"serve","first_serve","second_serve"}:
        serve = True
        if serve_type is None and label != "serve":
            serve_type = label

    player_uid = (obj.get("player_id") or obj.get("sportai_player_uid") or obj.get("player_uid") or obj.get("player"))
    if player_uid is not None:
        player_uid = str(player_uid)

    if start_s is None and end_s is None and bh_s is None:
        return None

    meta = {k: v for k, v in obj.items() if k not in {
        "id","uid","swing_uid",
        "player_id","sportai_player_uid","player_uid","player",
        "type","label","stroke_type",
        "start","start_s","start_ts","end","end_s","end_ts",
        "timestamp","ts","time_s","t",
        "ball_hit","ball_hit_timestamp","ball_hit_ts","ball_hit_s","ball_hit_location",
        "events","serve","serve_type"
    }}
    return {
        "suid": suid,
        "player_uid": player_uid,
        "start_s": start_s,
        "end_s": end_s,
        "ball_hit_s": bh_s,
        "ball_hit_x": bhx,
        "ball_hit_y": bhy,
        "serve": serve,
        "serve_type": serve_type,
        "meta": meta if meta else None,
        "label": label,
    }

def _iter_candidate_swings_from_container(container):
    if not isinstance(container, dict):
        return
    for key in ("swings","strokes","swing_events","events"):
        arr = container.get(key)
        if isinstance(arr, list):
            for item in arr:
                if key == "events":
                    lbl = str((item or {}).get("type") or (item or {}).get("label") or "").lower()
                    if lbl and (lbl in _SWING_TYPES or "swing" in lbl or "stroke" in lbl):
                        norm = _normalize_swing_obj(item)
                        if norm: yield norm
                else:
                    norm = _normalize_swing_obj(item)
                    if norm: yield norm

def _gather_all_swings(payload):
    for norm in _iter_candidate_swings_from_container(payload or {}):
        yield norm
    for p in (payload.get("players") or []):
        p_uid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        for norm in _iter_candidate_swings_from_container(p):
            if not norm.get("player_uid") and p_uid:
                norm["player_uid"] = p_uid
            yield norm
        stats = p.get("statistics") or p.get("stats") or {}
        for norm in _iter_candidate_swings_from_container(stats):
            if not norm.get("player_uid") and p_uid:
                norm["player_uid"] = p_uid
            yield norm

# ---------------------- ingest ----------------------
def _insert_swing(conn, session_id, player_id, s, base_dt, fps):
    q_start = _quantize_time(s.get("start_s"), fps)
    q_end   = _quantize_time(s.get("end_s"), fps)
    q_hit   = _quantize_time(s.get("ball_hit_s"), fps)

    conn.execute(text("""
        INSERT INTO fact_swing (
            session_id, player_id, sportai_swing_uid,
            start_s, end_s, ball_hit_s,
            start_ts, end_ts, ball_hit_ts,
            ball_hit_x, ball_hit_y, ball_speed, ball_player_distance,
            is_in_rally, serve, serve_type, meta
        ) VALUES (
            :sid, :pid, :suid,
            :ss, :es, :bhs,
            :sts, :ets, :bh_ts,
            :bhx, :bhy, :bs, :bpd,
            :inr, :srv, :stype, CAST(:meta AS JSONB)
        )
    """), {
        "sid": session_id,
        "pid": player_id,
        "suid": s.get("suid"),
        "ss": q_start, "es": q_end, "bhs": q_hit,
        "sts": seconds_to_ts(base_dt, q_start), "ets": seconds_to_ts(base_dt, q_end),
        "bh_ts": seconds_to_ts(base_dt, q_hit),
        "bhx": s.get("ball_hit_x"), "bhy": s.get("ball_hit_y"),
        "bs": s.get("ball_speed"), "bpd": s.get("ball_player_distance"),
        "inr": s.get("is_in_rally"), "srv": s.get("serve"), "stype": s.get("serve_type"),
        "meta": json.dumps(s.get("meta")) if s.get("meta") else None
    })

def ingest_result_v2(conn, payload, replace=False, forced_uid=None, src_hint=None):
    session_uid  = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
    fps          = _resolve_fps(payload)
    session_date = _resolve_session_date(payload)
    base_dt      = _base_dt_for_session(session_date)
    meta         = payload.get("meta") or payload.get("metadata") or {}
    meta_json    = json.dumps(meta)

    if replace:
        conn.execute(text("DELETE FROM dim_session WHERE session_uid = :u"), {"u": session_uid})

    conn.execute(text("""
        INSERT INTO dim_session (session_uid, fps, session_date, meta)
        VALUES (:u, :fps, :sdt, CAST(:m AS JSONB))
        ON CONFLICT (session_uid)
        DO UPDATE SET
          fps = COALESCE(EXCLUDED.fps, dim_session.fps),
          session_date = COALESCE(EXCLUDED.session_date, dim_session.session_date),
          meta = COALESCE(EXCLUDED.meta, dim_session.meta)
    """), {"u": session_uid, "fps": fps, "sdt": session_date, "m": meta_json})

    session_id = conn.execute(text("SELECT session_id FROM dim_session WHERE session_uid = :u"),
                              {"u": session_uid}).scalar_one()

    # raw snapshot
    conn.execute(text("""
        INSERT INTO raw_result (session_id, payload_json, created_at)
        VALUES (:sid, CAST(:p AS JSONB), now() AT TIME ZONE 'utc')
    """), {"sid": session_id, "p": json.dumps(payload)})

    # players
    players = payload.get("players") or []
    uid_to_player_id = {}
    for p in players:
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        if not puid: continue
        full_name = p.get("full_name") or p.get("name")
        handed    = p.get("handedness")
        age       = p.get("age")
        utr       = _float(p.get("utr"))
        metrics   = p.get("metrics") or {}
        covered_distance   = _float(metrics.get("covered_distance"))
        fastest_sprint     = _float(metrics.get("fastest_sprint"))
        fastest_sprint_ts  = _float(metrics.get("fastest_sprint_timestamp_s"))
        activity_score     = _float(metrics.get("activity_score"))
        swing_type_distribution = p.get("swing_type_distribution")
        location_heatmap   = p.get("location_heatmap") or p.get("heatmap")

        conn.execute(text("""
            INSERT INTO dim_player (
                session_id, sportai_player_uid, full_name, handedness, age, utr,
                covered_distance, fastest_sprint, fastest_sprint_timestamp_s,
                activity_score, swing_type_distribution, location_heatmap
            ) VALUES (
                :sid, :puid, :nm, :hand, :age, :utr,
                :cd, :fs, :fst, :ascore, CAST(:dist AS JSONB), CAST(:lheat AS JSONB)
            )
            ON CONFLICT (session_id, sportai_player_uid)
            DO UPDATE SET
              full_name = COALESCE(EXCLUDED.full_name, dim_player.full_name),
              handedness = COALESCE(EXCLUDED.handedness, dim_player.handedness),
              age = COALESCE(EXCLUDED.age, dim_player.age),
              utr = COALESCE(EXCLUDED.utr, dim_player.utr),
              covered_distance = COALESCE(EXCLUDED.covered_distance, dim_player.covered_distance),
              fastest_sprint = COALESCE(EXCLUDED.fastest_sprint, dim_player.fastest_sprint),
              fastest_sprint_timestamp_s = COALESCE(EXCLUDED.fastest_sprint_timestamp_s, dim_player.fastest_sprint_timestamp_s),
              activity_score = COALESCE(EXCLUDED.activity_score, dim_player.activity_score),
              swing_type_distribution = COALESCE(EXCLUDED.swing_type_distribution, dim_player.swing_type_distribution),
              location_heatmap = COALESCE(EXCLUDED.location_heatmap, dim_player.location_heatmap)
        """), {
            "sid": session_id, "puid": puid, "nm": full_name, "hand": handed, "age": age, "utr": utr,
            "cd": covered_distance, "fs": fastest_sprint, "fst": fastest_sprint_ts, "ascore": activity_score,
            "dist": json.dumps(swing_type_distribution) if swing_type_distribution is not None else None,
            "lheat": json.dumps(location_heatmap) if location_heatmap is not None else None
        })

        pid = conn.execute(text("""
            SELECT player_id FROM dim_player
            WHERE session_id = :sid AND sportai_player_uid = :puid
        """), {"sid": session_id, "puid": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # ensure players exist for any valid UIDs that only appear in player_positions
    pp = payload.get("player_positions") or {}
    pp_uids = [str(k) for k, arr in pp.items() if _valid_puid(k) and arr]
    missing_pp = [u for u in pp_uids if u not in uid_to_player_id]
    for puid in missing_pp:
        conn.execute(text("""
            INSERT INTO dim_player (session_id, sportai_player_uid)
            VALUES (:sid, :puid)
            ON CONFLICT (session_id, sportai_player_uid) DO NOTHING
        """), {"sid": session_id, "puid": puid})
        pid = conn.execute(text("""
            SELECT player_id FROM dim_player
            WHERE session_id=:sid AND sportai_player_uid=:p
        """), {"sid": session_id, "p": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # rallies
    for i, r in enumerate(payload.get("rallies") or [], start=1):
        if isinstance(r, dict):
            start_s = _time_s(r.get("start_ts")) or _time_s(r.get("start"))
            end_s   = _time_s(r.get("end_ts"))   or _time_s(r.get("end"))
        else:
            try: start_s = _float(r[0]); end_s = _float(r[1])
            except Exception: start_s, end_s = None, None
        conn.execute(text("""
            INSERT INTO dim_rally (session_id, rally_number, start_s, end_s, start_ts, end_ts)
            VALUES (:sid, :n, :ss, :es, :sts, :ets)
            ON CONFLICT (session_id, rally_number)
            DO UPDATE SET
              start_s = COALESCE(EXCLUDED.start_s, dim_rally.start_s),
              end_s   = COALESCE(EXCLUDED.end_s, dim_rally.end_s),
              start_ts= COALESCE(EXCLUDED.start_ts, dim_rally.start_ts),
              end_ts  = COALESCE(EXCLUDED.end_ts, dim_rally.end_ts)
        """), {"sid": session_id, "n": i, "ss": start_s, "es": end_s,
               "sts": seconds_to_ts(base_dt, start_s), "ets": seconds_to_ts(base_dt, end_s)})

    # helper for bounce->rally
    def rally_id_for_ts(ts_s):
        if ts_s is None: return None
        row = conn.execute(text("""
            SELECT rally_id FROM dim_rally
            WHERE session_id = :sid AND :s BETWEEN start_s AND end_s
            ORDER BY rally_number LIMIT 1
        """), {"sid": session_id, "s": ts_s}).fetchone()
        return row[0] if row else None

    # bounces
    for b in payload.get("ball_bounces") or []:
        s = _time_s(b.get("timestamp_s")) or _time_s(b.get("ts")) or _time_s(b.get("t"))
        x = _float(b.get("x")); y = _float(b.get("y"))
        btype = b.get("type") or b.get("bounce_type")
        hitter_uid = str(b.get("player_id") or b.get("sportai_player_uid") or "") if (b.get("player_id") or b.get("sportai_player_uid")) else None
        hitter_pid = uid_to_player_id.get(hitter_uid) if hitter_uid else None
        conn.execute(text("""
            INSERT INTO fact_bounce (session_id, hitter_player_id, rally_id, bounce_s, bounce_ts, x, y, bounce_type)
            VALUES (:sid, :pid, :rid, :s, :ts, :x, :y, :bt)
        """), {"sid": session_id, "pid": hitter_pid, "rid": rally_id_for_ts(s),
               "s": s, "ts": seconds_to_ts(base_dt, s), "x": x, "y": y, "bt": btype})

    # ball positions
    for p in payload.get("ball_positions") or []:
        s = _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
        x = _float(p.get("x")); y = _float(p.get("y"))
        conn.execute(text("""
            INSERT INTO fact_ball_position (session_id, ts_s, ts, x, y)
            VALUES (:sid, :ss, :ts, :x, :y)
        """), {"sid": session_id, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": x, "y": y})

    # player positions
    for puid, arr in (payload.get("player_positions") or {}).items():
        pid = uid_to_player_id.get(str(puid))
        for p in arr or []:
            s = _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
            x = _float(p.get("x")); y = _float(p.get("y"))
            conn.execute(text("""
                INSERT INTO fact_player_position (session_id, player_id, ts_s, ts, x, y)
                VALUES (:sid, :pid, :ss, :ts, :x, :y)
            """), {"sid": session_id, "pid": pid, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": x, "y": y})

    # optional blocks
    for t in payload.get("team_sessions") or []:
        conn.execute(text("INSERT INTO team_session (session_id, data) VALUES (:sid, CAST(:d AS JSONB))"),
                     {"sid": session_id, "d": json.dumps(t)})

    for h in payload.get("highlights") or []:
        conn.execute(text("INSERT INTO highlight (session_id, data) VALUES (:sid, CAST(:d AS JSONB))"),
                     {"sid": session_id, "d": json.dumps(h)})

    # UPDATE→INSERT (no unique index required)
    if "bounce_heatmap" in payload:
        h = json.dumps(payload.get("bounce_heatmap"))
        res = conn.execute(text("UPDATE bounce_heatmap SET heatmap = CAST(:h AS JSONB) WHERE session_id = :sid"),
                           {"sid": session_id, "h": h})
        if res.rowcount == 0:
            conn.execute(text("INSERT INTO bounce_heatmap (session_id, heatmap) VALUES (:sid, CAST(:h AS JSONB))"),
                         {"sid": session_id, "h": h})

    if "confidences" in payload:
        d = json.dumps(payload.get("confidences"))
        res = conn.execute(text("UPDATE session_confidences SET data = CAST(:d AS JSONB) WHERE session_id = :sid"),
                           {"sid": session_id, "d": d})
        if res.rowcount == 0:
            conn.execute(text("INSERT INTO session_confidences (session_id, data) VALUES (:sid, CAST(:d AS JSONB))"),
                         {"sid": session_id, "d": d})

    if "thumbnail_crops" in payload:
        c = json.dumps(payload.get("thumbnail_crops"))
        res = conn.execute(text("UPDATE thumbnail SET crops = CAST(:c AS JSONB) WHERE session_id = :sid"),
                           {"sid": session_id, "c": c})
        if res.rowcount == 0:
            conn.execute(text("INSERT INTO thumbnail (session_id, crops) VALUES (:sid, CAST(:c AS JSONB))"),
                         {"sid": session_id, "c": c})

    # --------- wide swing discovery (with in-memory dedupe) ----------
    seen = set()  # ('suid', <str>) or ('fb', pid, start_s, end_s)
    def _seen_key(pid, norm, fps):
        if norm.get("suid"): return ("suid", str(norm["suid"]))
        return ("fb", pid,
                _quantize_time(norm.get("start_s"), fps),
                _quantize_time(norm.get("end_s"), fps))

    for norm in _gather_all_swings(payload):
        pid = None
        if norm.get("player_uid"):
            pid = uid_to_player_id.get(str(norm["player_uid"]))
        k = _seen_key(pid, norm, fps)
        if k in seen:
            continue
        seen.add(k)

        s = {
            "suid": str(norm["suid"]) if norm.get("suid") else None,
            "start_s": norm.get("start_s"),
            "end_s": norm.get("end_s"),
            "ball_hit_s": norm.get("ball_hit_s"),
            "ball_hit_x": norm.get("ball_hit_x"),
            "ball_hit_y": norm.get("ball_hit_y"),
            "ball_speed": norm.get("ball_speed"),
            "ball_player_distance": norm.get("ball_player_distance"),
            "is_in_rally": norm.get("is_in_rally"),
            "serve": norm.get("serve"),
            "serve_type": norm.get("serve_type"),
            "meta": norm.get("meta"),
        }
        try:
            _insert_swing(conn, session_id, pid, s, base_dt, fps)
        except IntegrityError:
            pass

    return {"session_uid": session_uid}

# ---------------------- OPS ENDPOINTS ----------------------
@app.get("/")
def root():
    return jsonify({"service": "NextPoint Upload/Ingester v3", "status": "ok"})

@app.get("/ops/db-ping")
def db_ping():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        now = conn.execute(text("SELECT now() AT TIME ZONE 'utc'")).scalar_one()
    return jsonify({"ok": True, "now_utc": str(now)})

@app.get("/ops/init-db")
def ops_init_db():
    if not _guard(): return _forbid()
    try:
        init_db(engine)
        return jsonify({"ok": True, "message": "DB initialized / migrated"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/init-views")
def ops_init_views():
    if not _guard(): return _forbid()
    try:
        init_views(engine)
        return jsonify({"ok": True, "message": "Views created/refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/db-counts")
def ops_db_counts():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        def c(tbl): return conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar_one()
        counts = {
            "dim_session": c("dim_session"),
            "dim_player": c("dim_player"),
            "dim_rally": c("dim_rally"),
            "fact_swing": c("fact_swing"),
            "fact_bounce": c("fact_bounce"),
            "fact_ball_position": c("fact_ball_position"),
            "fact_player_position": c("fact_player_position"),
            "team_session": c("team_session"),
            "highlight": c("highlight"),
            "bounce_heatmap": c("bounce_heatmap"),
            "session_confidences": c("session_confidences"),
            "thumbnail": c("thumbnail"),
            "raw_result": c("raw_result")
        }
    return jsonify({"ok": True, "counts": counts})

@app.get("/ops/sql")
def ops_sql():
    if not _guard():
        return _forbid()
    q = request.args.get("q", "").strip()
    ql = q.lstrip().lower()

    # allow plain SELECT or CTEs (WITH/with recursive)
    if not (ql.startswith("select") or ql.startswith("with")):
        return Response("Only SELECT/CTE queries are allowed", status=400)

    stripped = q.strip()
    if ";" in stripped[:-1]:  # allow a single trailing semicolon only
        return Response("Only a single statement is allowed", status=400)

    if " limit " not in ql:
        q = f"{stripped.rstrip(';')} LIMIT 200"

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = 5000"))
        conn.execute(text("SET LOCAL TRANSACTION READ ONLY"))
        rows = conn.execute(text(q)).mappings().all()
        data = [dict(r) for r in rows]
    return jsonify({"ok": True, "rows": len(data), "data": data})

@app.get("/ops/reconcile")
def ops_reconcile():
    if not _guard():
        return _forbid()

    forced_uid = request.args.get("session_uid")
    src_hint = request.args.get("name") or request.args.get("url")

    try:
        payload = None
        try:
            if src_hint or "file" in request.files or request.data:
                payload = _get_json_from_sources()
        except Exception:
            payload = None

        with engine.connect() as conn:
            if payload is None:
                if not forced_uid:
                    return jsonify({"ok": False, "error": "Provide session_uid or supply a payload via ?name/?url/file/body"}), 400
                sid = conn.execute(text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u": forced_uid}).scalar()
                if sid is None:
                    return jsonify({"ok": False, "error": f"session_uid '{forced_uid}' not found"}), 404
                payload = conn.execute(text("""
                SELECT payload_json FROM raw_result
                WHERE session_id=:sid
                ORDER BY created_at DESC NULLS LAST
                LIMIT 1
                """), {"sid": sid}).scalar()
                if payload is None:
                    return jsonify({"ok": False, "error": f"No raw_result snapshot found for session_uid '{forced_uid}'"}), 404

            session_uid = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)

            row = conn.execute(text("SELECT session_id, fps FROM dim_session WHERE session_uid=:u"), {"u": session_uid}).mappings().first()
            if not row:
                return jsonify({"ok": False, "error": f"Session not found in DB: {session_uid}"}), 404
            sid, fps = row["session_id"], row["fps"]

            payload_players = set()
            for p in (payload.get("players") or []):
                for k in ("id","sportai_player_uid","uid","player_id"):
                    if k in p and p[k] is not None:
                        payload_players.add(str(p[k]))
                        break

            pp_payload = {}
            pp = payload.get("player_positions") or {}
            for puid, arr in pp.items():
                puid_s = str(puid)
                pp_payload[puid_s] = int(len(arr or []))
                if _valid_puid(puid_s) and arr:
                    payload_players.add(puid_s)

            payload_rallies        = len(payload.get("rallies") or [])
            payload_bounces        = len(payload.get("ball_bounces") or [])
            payload_ball_positions = len(payload.get("ball_positions") or [])

            rows = conn.execute(text("""
                SELECT player_id, sportai_player_uid 
                FROM dim_player WHERE session_id=:sid
            """), {"sid": sid}).mappings().all()
            uid_to_pid = {str(r["sportai_player_uid"]): r["player_id"] for r in rows}

            payload_swing_keys = set()
            for norm in _gather_all_swings(payload):
                puid = str(norm.get("player_uid") or "")
                pid = uid_to_pid.get(puid)
                if norm.get("suid"):
                    k = ("suid", str(norm["suid"]))
                else:
                    k = ("fb", pid,
                         _quantize_time(norm.get("start_s"), fps),
                         _quantize_time(norm.get("end_s"), fps))
                payload_swing_keys.add(k)

            db_rallies = conn.execute(text("SELECT COUNT(*) FROM dim_rally WHERE session_id=:sid"), {"sid": sid}).scalar_one()
            db_bounces = conn.execute(text("SELECT COUNT(*) FROM fact_bounce WHERE session_id=:sid"), {"sid": sid}).scalar_one()
            db_ball_positions = conn.execute(text("SELECT COUNT(*) FROM fact_ball_position WHERE session_id=:sid"), {"sid": sid}).scalar_one()
            db_swings = conn.execute(text("SELECT COUNT(*) FROM fact_swing WHERE session_id=:sid"), {"sid": sid}).scalar_one()

            db_players = set(conn.execute(text("""
                SELECT sportai_player_uid FROM dim_player WHERE session_id=:sid
            """), {"sid": sid}).scalars().all())

            pp_rows = conn.execute(text("""
                SELECT dp.sportai_player_uid AS puid, COUNT(*) AS cnt
                FROM fact_player_position f
                JOIN dim_player dp ON dp.player_id=f.player_id
                WHERE f.session_id=:sid
                GROUP BY dp.sportai_player_uid
            """), {"sid": sid}).mappings().all()
            pp_db = {str(r["puid"]): int(r["cnt"]) for r in pp_rows}

            db_rows = conn.execute(text("""
                SELECT player_id, sportai_swing_uid, start_s, end_s
                FROM fact_swing WHERE session_id=:sid
            """), {"sid": sid}).mappings().all()
            db_swing_keys = set()
            for r in db_rows:
                if r["sportai_swing_uid"]:
                    db_swing_keys.add(("suid", str(r["sportai_swing_uid"])))
                else:
                    db_swing_keys.add(("fb", r["player_id"],
                                       _quantize_time(r["start_s"], fps),
                                       _quantize_time(r["end_s"], fps)))

            players_missing_in_db = sorted(list(payload_players - db_players))[:20]
            players_extra_in_db   = sorted(list(db_players - payload_players))[:20]

            swings_missing_in_db = sorted(list(payload_swing_keys - db_swing_keys))[:50]
            swings_extra_in_db   = sorted(list(db_swing_keys - payload_swing_keys))[:50]

            all_puids = set(pp_payload.keys()) | set(pp_db.keys())
            pos_mismatch_sample = []
            for pu in sorted(all_puids):
                pv = pp_payload.get(pu, 0)
                dv = pp_db.get(pu, 0)
                if pv != dv:
                    pos_mismatch_sample.append({"player_uid": pu, "payload_points": pv, "db_points": dv})
                if len(pos_mismatch_sample) >= 20:
                    break

            return jsonify({
                "ok": True,
                "session_uid": session_uid,
                "summary": {
                    "payload": {
                        "rallies": payload_rallies,
                        "ball_bounces": payload_bounces,
                        "ball_positions": payload_ball_positions,
                        "players": len(payload_players),
                        "swings_distinct": len(payload_swing_keys),
                    },
                    "db": {
                        "rallies": db_rallies,
                        "ball_bounces": db_bounces,
                        "ball_positions": db_ball_positions,
                        "players": len(db_players),
                        "swings": db_swings,
                    }
                },
                "swings": {
                    "payload_distinct": len(payload_swing_keys),
                    "db": db_swings,
                    "delta": db_swings - len(payload_swing_keys),
                    "missing_in_db_sample": swings_missing_in_db,
                    "extra_in_db_sample": swings_extra_in_db
                },
                "players": {
                    "missing_in_db": players_missing_in_db,
                    "extra_in_db": players_extra_in_db
                },
                "positions_mismatch_sample": pos_mismatch_sample
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ops/ingest-file", methods=["GET","POST"])
def ops_ingest_file():
    if not _guard(): return _forbid()
    replace = str(request.args.get("replace","0")).strip().lower() in ("1","true","yes","y")
    forced_uid = request.args.get("session_uid")
    src_hint = request.args.get("name") or request.args.get("url")
    try:
        payload = _get_json_from_sources()

        # --- STRICT_REINGEST guard (require replace=1 for existing sessions) ---
        try:
            session_uid_guess = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
        except Exception:
            session_uid_guess = forced_uid
        if STRICT_REINGEST:
            with engine.connect() as c:
                sid = c.execute(text("SELECT session_id FROM dim_session WHERE session_uid=:u"),
                                {"u": session_uid_guess}).scalar()
            if sid is not None and not replace:
                return jsonify({
                    "ok": False,
                    "error": f"Session '{session_uid_guess}' already exists; re-ingest with &replace=1 to overwrite"
                }), 400
        # ----------------------------------------------------------------------

        init_db(engine)  # ensure migrations/ensures ran
        with engine.begin() as conn:
            res = ingest_result_v2(conn, payload, replace=replace, forced_uid=forced_uid, src_hint=src_hint)
        return jsonify({"ok": True, **res, "replace": replace})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/delete-session")
def ops_delete_session():
    if not _guard(): return _forbid()
    uid = request.args.get("session_uid")
    if not uid:
        return jsonify({"ok": False, "error": "session_uid is required"}), 400
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dim_session WHERE session_uid = :u"), {"u": uid})
    return jsonify({"ok": True, "deleted_session_uid": uid})

@app.get("/ops/list-sessions")
def ops_list_sessions():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT s.session_uid,
               (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id=s.session_id) AS players,
               (SELECT COUNT(*) FROM dim_rally dr WHERE dr.session_id=s.session_id) AS rallies,
               (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id=s.session_id) AS swings,
               (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=s.session_id) AS ball_bounces,
               (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=s.session_id) AS ball_positions,
               (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=s.session_id) AS player_positions,
               (SELECT COUNT(*) FROM highlight h WHERE h.session_id=s.session_id) AS highlights,
               (SELECT COUNT(*) FROM team_session t WHERE t.session_id=s.session_id) AS team_sessions,
               (SELECT COUNT(*) FROM raw_result rr WHERE rr.session_id=s.session_id) AS snapshots
            FROM dim_session s
            ORDER BY s.session_uid
        """)).mappings().all()
        data = [dict(r) for r in rows]
    return jsonify({"ok": True, "rows": len(data), "data": data})

# ---------------------- DASHBOARD API (read-only) ----------------------
def _get_session_row(conn, session_uid):
    return conn.execute(text("SELECT session_id, fps, session_date FROM dim_session WHERE session_uid=:u"),
                        {"u": session_uid}).mappings().first()

def _front_back_labels(conn, session_id):
    labels = {}
    rows = conn.execute(text("SELECT data FROM team_session WHERE session_id=:sid"), {"sid": session_id}).scalars().all()
    fronts, backs = set(), set()
    for d in rows:
        try:
            obj = d if isinstance(d, dict) else json.loads(d)
            for u in obj.get("team_front") or []:
                fronts.add(str(u))
            for u in obj.get("team_back") or []:
                backs.add(str(u))
        except Exception:
            pass
    for u in fronts:
        labels[str(u)] = "front"
    for u in backs:
        labels[str(u)] = "back"
    return labels

@app.get("/api/session/<session_uid>/summary")
def api_session_summary(session_uid):
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        srow = _get_session_row(conn, session_uid)
        if not srow: return jsonify({"ok": False, "error": "session not found"}), 404
        sid, fps, sdt = srow["session_id"], srow["fps"], srow["session_date"]

        labels = _front_back_labels(conn, sid)

        counts_row = conn.execute(text("""
            SELECT
              (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id=:sid) AS players,
              (SELECT COUNT(*) FROM dim_rally dr WHERE dr.session_id=:sid) AS rallies,
              (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id=:sid) AS swings,
              (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=:sid) AS ball_bounces,
              (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=:sid) AS ball_positions,
              (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=:sid) AS player_positions,
              (SELECT COUNT(*) FROM highlight h WHERE h.session_id=:sid) AS highlights
        """), {"sid": sid}).mappings().first()

        counts = {k: int(v) for k, v in counts_row.items()} if counts_row else {}

        players = conn.execute(text("""
            SELECT sportai_player_uid AS uid, full_name, handedness
            FROM dim_player WHERE session_id=:sid
            ORDER BY uid
        """), {"sid": sid}).mappings().all()
        plist = []
        for r in players:
            uid = str(r["uid"])
            display = f"{uid} ({labels.get(uid)})" if labels.get(uid) else uid
            plist.append({
                "uid": uid,
                "display": display,
                "full_name": r["full_name"],
                "handedness": r["handedness"],
            })

        return jsonify({
            "ok": True,
            "session_uid": session_uid,
            "fps": fps,
            "session_date": str(sdt) if sdt else None,
            "counts": counts,
            "players": plist
        })

@app.get("/api/session/<session_uid>/swings")
def api_session_swings(session_uid):
    if not _guard(): return _forbid()
    limit = max(1, min(int(request.args.get("limit", 200)), 1000))
    offset = max(0, int(request.args.get("offset", 0)))
    order = request.args.get("order", "ball_hit_s asc").strip().lower()
    allowed_order = {"ball_hit_s asc","ball_hit_s desc","start_s asc","start_s desc"}
    if order not in allowed_order:
        order = "ball_hit_s asc"
    player_uid_filter = request.args.get("player_uid")
    side = request.args.get("side")  # "front" / "back"

    with engine.connect() as conn:
        srow = _get_session_row(conn, session_uid)
        if not srow: return jsonify({"ok": False, "error": "session not found"}), 404
        sid = srow["session_id"]
        labels = _front_back_labels(conn, sid)

        base_sql = """
            SELECT fs.start_s, fs.end_s, fs.ball_hit_s, fs.ball_hit_x, fs.ball_hit_y,
                   fs.serve, fs.serve_type, dp.sportai_player_uid AS player_uid
            FROM fact_swing fs
            LEFT JOIN dim_player dp ON dp.player_id = fs.player_id
            WHERE fs.session_id=:sid
        """
        params = {"sid": sid}

        if player_uid_filter:
            base_sql += " AND dp.sportai_player_uid=:puid"
            params["puid"] = player_uid_filter

        rows = conn.execute(text(base_sql + f" ORDER BY {order} LIMIT :lim OFFSET :off"),
                            {**params, "lim": limit, "off": offset}).mappings().all()

        data = []
        for r in rows:
            uid = r["player_uid"]
            display = f"{uid} ({labels.get(uid)})" if uid and labels.get(uid) else uid
            if side and labels.get(uid) != side:
                continue
            data.append({
                "player_uid": uid,
                "player_display": display,
                "start_s": r["start_s"],
                "end_s": r["end_s"],
                "ball_hit_s": r["ball_hit_s"],
                "ball_hit_x": r["ball_hit_x"],
                "ball_hit_y": r["ball_hit_y"],
                "serve": r["serve"],
                "serve_type": r["serve_type"],
            })
        return jsonify({"ok": True, "rows": len(data), "data": data})

@app.get("/api/session/<session_uid>/rallies")
def api_session_rallies(session_uid):
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        srow = _get_session_row(conn, session_uid)
        if not srow: return jsonify({"ok": False, "error": "session not found"}), 404
        sid = srow["session_id"]
        rows = conn.execute(text("""
            SELECT r.rally_number, r.start_s, r.end_s,
                   (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=r.session_id AND b.rally_id=r.rally_id) AS bounces
            FROM dim_rally r
            WHERE r.session_id=:sid
            ORDER BY r.rally_number
        """), {"sid": sid}).mappings().all()
        return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})

@app.get("/api/session/<session_uid>/players")
def api_session_players(session_uid):
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        srow = _get_session_row(conn, session_uid)
        if not srow: return jsonify({"ok": False, "error": "session not found"}), 404
        sid = srow["session_id"]
        labels = _front_back_labels(conn, sid)
        rows = conn.execute(text("""
            SELECT sportai_player_uid AS uid, full_name, handedness
            FROM dim_player WHERE session_id=:sid
            ORDER BY uid
        """), {"sid": sid}).mappings().all()
        data = []
        for r in rows:
            uid = str(r["uid"])
            data.append({
                "uid": uid,
                "display": f"{uid} ({labels.get(uid)})" if labels.get(uid) else uid,
                "full_name": r["full_name"],
                "handedness": r["handedness"],
            })
        return jsonify({"ok": True, "rows": len(data), "data": data})

@app.get("/api/session/<session_uid>/bounces")
def api_session_bounces(session_uid):
    if not _guard(): return _forbid()
    limit = max(1, min(int(request.args.get("limit", 500)), 5000))
    offset = max(0, int(request.args.get("offset", 0)))
    with engine.connect() as conn:
        srow = _get_session_row(conn, session_uid)
        if not srow: return jsonify({"ok": False, "error": "session not found"}), 404
        sid = srow["session_id"]
        rows = conn.execute(text("""
            SELECT b.bounce_s, b.x, b.y, dp.sportai_player_uid AS player_uid, r.rally_number
            FROM fact_bounce b
            LEFT JOIN dim_player dp ON dp.player_id=b.hitter_player_id
            LEFT JOIN dim_rally  r ON r.rally_id=b.rally_id
            WHERE b.session_id=:sid
            ORDER BY b.bounce_s
            LIMIT :lim OFFSET :off
        """), {"sid": sid, "lim": limit, "off": offset}).mappings().all()
        return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})

@app.get("/api/session/<session_uid>/highlights")
def api_session_highlights(session_uid):
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        srow = _get_session_row(conn, session_uid)
        if not srow: return jsonify({"ok": False, "error": "session not found"}), 404
        sid = srow["session_id"]
        rows = conn.execute(text("SELECT data FROM highlight WHERE session_id=:sid ORDER BY 1"),
                            {"sid": sid}).scalars().all()
        out = []
        for d in rows:
            try:
                out.append(d if isinstance(d, dict) else json.loads(d))
            except Exception:
                pass
        return jsonify({"ok": True, "rows": len(out), "data": out})

# ---------------------- main ----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")))
