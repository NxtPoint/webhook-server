# upload_app.py
import os, json, time, hashlib
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")
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
def _insert_swing(conn, session_id, player_id, s, base_dt):
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
        "ss": s.get("start_s"), "es": s.get("end_s"), "bhs": s.get("ball_hit_s"),
        "sts": seconds_to_ts(base_dt, s.get("start_s")), "ets": seconds_to_ts(base_dt, s.get("end_s")),
        "bh_ts": seconds_to_ts(base_dt, s.get("ball_hit_s")),
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
    def _seen_key(pid, norm):
        if norm.get("suid"): return ("suid", str(norm["suid"]))
        return ("fb", pid, norm.get("start_s"), norm.get("end_s"))

    for norm in _gather_all_swings(payload):
        pid = None
        if norm.get("player_uid"):
            pid = uid_to_player_id.get(str(norm["player_uid"]))
        k = _seen_key(pid, norm)
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
            _insert_swing(conn, session_id, pid, s, base_dt)
        except IntegrityError:
            # ignore duplicates if legacy unique indexes exist
            pass

    return {"session_uid": session_uid}

# ---------------------- endpoints ----------------------
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
    if not _guard(): return _forbid()
    q = request.args.get("q","").strip()
    ql = q.lstrip().lower()
    # allow SELECT or CTEs that start with WITH
    if not (ql.startswith("select") or ql.startswith("with")):
        return Response("Only SELECT/CTE queries are allowed", status=400)
    if "limit" not in ql:
        q = f"{q.rstrip(';')} LIMIT 200"
    with engine.connect() as conn:
        rows = conn.execute(text(q)).mappings().all()
        data = [dict(r) for r in rows]
    return jsonify({"ok": True, "rows": len(data), "data": data})

@app.route("/ops/ingest-file", methods=["GET","POST"])
def ops_ingest_file():
    if not _guard(): return _forbid()
    replace = str(request.args.get("replace","0")).strip().lower() in ("1","true","yes","y")
    forced_uid = request.args.get("session_uid")
    src_hint = request.args.get("name") or request.args.get("url")
    try:
        payload = _get_json_from_sources()
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")))
