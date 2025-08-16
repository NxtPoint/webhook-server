# upload_app.py
import os, json, time
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response
from sqlalchemy import create_engine, text

# -----------------------------
# Config
# -----------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required")
if not OPS_KEY:
    raise RuntimeError("OPS_KEY env var is required")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
app = Flask(__name__)

# -----------------------------
# Helpers
# -----------------------------
def _guard():
    return request.args.get("key") == OPS_KEY

def _forbid():
    return Response("Forbidden", status=403)

def seconds_to_ts(base_dt, s):
    if s is None:
        return None
    try:
        return base_dt + timedelta(seconds=float(s))
    except Exception:
        return None

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
    if s in ("1","true","t","yes","y"): return True
    if s in ("0","false","f","no","n"): return False
    return None

def _get_json_from_sources():
    """
    Intake order:
      1) ?name=<relative/or absolute>  (tries /mnt/data/<name> then <app_root>/<name>)
      2) ?url=<direct-json-url>
      3) POST file field 'file'
      4) raw JSON body
    """
    # ---- Option B: name= with dual-path fallback ----
    name = request.args.get("name")
    if name:
        candidates = []
        if os.path.isabs(name):
            candidates = [name]
        else:
            app_root = os.getcwd()
            candidates = [f"/mnt/data/{name}", os.path.join(app_root, name)]
        last_err = None
        for path in candidates:
            try:
                with open(path, "rb") as f:
                    return json.load(f)
            except FileNotFoundError as e:
                last_err = e
                continue
        raise FileNotFoundError(f"File not found. Tried: {', '.join(candidates)}")

    # URL source
    url = request.args.get("url")
    if url:
        import requests
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        return r.json()

    # Multipart upload
    if "file" in request.files:
        return json.load(request.files["file"].stream)

    # Raw JSON body
    if request.data:
        return json.loads(request.data.decode("utf-8"))

    raise ValueError("No JSON supplied (use ?name=, ?url=, multipart file, or raw body).")

# -----------------------------
# DB init / views
# -----------------------------
def init_db(engine):
    from db_init import run_init
    run_init(engine)

def init_views(engine):
    from db_views import run_views
    run_views(engine)

# -----------------------------
# Mapping helpers
# -----------------------------
def _resolve_session_uid(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    return str(
        payload.get("session_uid")
        or meta.get("session_uid")
        or meta.get("video_uid")
        or meta.get("file_name")
        or f"session_{int(time.time())}"
    )

def _resolve_fps(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("fps","frame_rate","frames_per_second"):
        if k in payload and payload[k] is not None: return _float(payload[k])
        if k in meta and meta[k] is not None: return _float(meta[k])
    return None

def _resolve_session_date(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_date","date","recorded_at"):
        raw = payload.get(k) if k in payload else meta.get(k)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z","+00:00")).astimezone(timezone.utc)
            except Exception:
                return None
    return None

def _base_dt_for_session(session_date):
    return session_date if session_date else datetime(1970,1,1,tzinfo=timezone.utc)

# -----------------------------
# Ingestion (SportAI v2)
# -----------------------------
def ingest_result_v2(conn, payload, replace=False):
    session_uid = _resolve_session_uid(payload)
    fps = _resolve_fps(payload)
    session_date = _resolve_session_date(payload)
    base_dt = _base_dt_for_session(session_date)
    meta = payload.get("meta") or payload.get("metadata") or {}
    meta_json = json.dumps(meta)

    # Replace-mode: wipe session rows
    if replace:
        conn.execute(text("DELETE FROM dim_session WHERE session_uid = :u"), {"u": session_uid})

    # Upsert session
    conn.execute(text("""
        INSERT INTO dim_session (session_uid, fps, session_date, meta)
        VALUES (:u, :fps, :sdt, CAST(:m AS JSONB))
        ON CONFLICT (session_uid)
        DO UPDATE SET
          fps = COALESCE(EXCLUDED.fps, dim_session.fps),
          session_date = COALESCE(EXCLUDED.session_date, dim_session.session_date),
          meta = COALESCE(EXCLUDED.meta, dim_session.meta)
    """), {"u": session_uid, "fps": fps, "sdt": session_date, "m": meta_json})

    session_id = conn.execute(
        text("SELECT session_id FROM dim_session WHERE session_uid = :u"),
        {"u": session_uid}
    ).scalar_one()

    # Raw snapshot
    conn.execute(text("""
        INSERT INTO raw_result (session_id, payload_json, created_at)
        VALUES (:sid, CAST(:p AS JSONB), now() AT TIME ZONE 'utc')
    """), {"sid": session_id, "p": json.dumps(payload)})

    # Players & nested swings
    players = payload.get("players") or []
    uid_to_player_id = {}
    for p in players:
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or "")
        if not puid:
            continue
        full_name = p.get("full_name") or p.get("name")
        handed = p.get("handedness")
        age = p.get("age")
        utr = _float(p.get("utr"))
        metrics = p.get("metrics") or {}
        covered_distance = _float(metrics.get("covered_distance"))
        fastest_sprint = _float(metrics.get("fastest_sprint"))
        fastest_sprint_ts = _float(metrics.get("fastest_sprint_timestamp_s"))
        activity_score = _float(metrics.get("activity_score"))
        swing_type_distribution = p.get("swing_type_distribution")
        location_heatmap = p.get("location_heatmap") or p.get("heatmap")

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

        for s in p.get("swings") or []:
            _insert_swing(conn, session_id, pid, s, base_dt)

    # Optional top-level swings
    for s in payload.get("swings") or []:
        puid = str(s.get("player_id") or s.get("sportai_player_uid") or s.get("player_uid") or "")
        pid = uid_to_player_id.get(puid)
        _insert_swing(conn, session_id, pid, s, base_dt)

    # Rallies
    rallies = payload.get("rallies") or []
    for i, r in enumerate(rallies, start=1):
        if isinstance(r, dict):
            start_s = _float(r.get("start_ts") or r.get("start"))
            end_s   = _float(r.get("end_ts") or r.get("end"))
        else:
            try:
                start_s = _float(r[0]); end_s = _float(r[1])
            except Exception:
                start_s, end_s = None, None
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

    # Helper to map timestamp -> rally_id
    def rally_id_for_ts(ts_s):
        if ts_s is None:
            return None
        row = conn.execute(text("""
            SELECT rally_id FROM dim_rally
            WHERE session_id = :sid AND :s BETWEEN start_s AND end_s
            ORDER BY rally_number LIMIT 1
        """), {"sid": session_id, "s": ts_s}).fetchone()
        return row[0] if row else None

    # Bounces
    for b in payload.get("ball_bounces") or []:
        s = _float(b.get("timestamp_s") or b.get("ts") or b.get("t"))
        x = _float(b.get("x")); y = _float(b.get("y"))
        btype = b.get("type") or b.get("bounce_type")
        hitter_uid = str(b.get("player_id") or b.get("sportai_player_uid") or "") if b.get("player_id") or b.get("sportai_player_uid") else None
        hitter_pid = uid_to_player_id.get(hitter_uid) if hitter_uid else None
        conn.execute(text("""
            INSERT INTO fact_bounce (session_id, hitter_player_id, rally_id, bounce_s, bounce_ts, x, y, bounce_type)
            VALUES (:sid, :pid, :rid, :s, :ts, :x, :y, :bt)
        """), {"sid": session_id, "pid": hitter_pid, "rid": rally_id_for_ts(s),
               "s": s, "ts": seconds_to_ts(base_dt, s), "x": x, "y": y, "bt": btype})

    # Ball positions
    for p in payload.get("ball_positions") or []:
        s = _float(p.get("timestamp_s") or p.get("ts") or p.get("t"))
        x = _float(p.get("x")); y = _float(p.get("y"))
        conn.execute(text("""
            INSERT INTO fact_ball_position (session_id, ts_s, ts, x, y)
            VALUES (:sid, :ss, :ts, :x, :y)
        """), {"sid": session_id, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": x, "y": y})

    # Player positions
    for puid, arr in (payload.get("player_positions") or {}).items():
        pid = uid_to_player_id.get(str(puid))
        for p in arr or []:
            s = _float(p.get("timestamp_s") or p.get("ts") or p.get("t"))
            x = _float(p.get("x")); y = _float(p.get("y"))
            conn.execute(text("""
                INSERT INTO fact_player_position (session_id, player_id, ts_s, ts, x, y)
                VALUES (:sid, :pid, :ss, :ts, :x, :y)
            """), {"sid": session_id, "pid": pid, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": x, "y": y})

    # Optional blocks
    for t in payload.get("team_sessions") or []:
        conn.execute(text("INSERT INTO team_session (session_id, data) VALUES (:sid, CAST(:d AS JSONB))"),
                     {"sid": session_id, "d": json.dumps(t)})

    for h in payload.get("highlights") or []:
        conn.execute(text("INSERT INTO highlight (session_id, data) VALUES (:sid, CAST(:d AS JSONB))"),
                     {"sid": session_id, "d": json.dumps(h)})

    if "bounce_heatmap" in payload:
        conn.execute(text("""
            INSERT INTO bounce_heatmap (session_id, heatmap)
            VALUES (:sid, CAST(:h AS JSONB))
            ON CONFLICT (session_id) DO UPDATE SET heatmap = EXCLUDED.heatmap
        """), {"sid": session_id, "h": json.dumps(payload.get("bounce_heatmap"))})

    if "confidences" in payload:
        conn.execute(text("""
            INSERT INTO session_confidences (session_id, data)
            VALUES (:sid, CAST(:d AS JSONB))
            ON CONFLICT (session_id) DO UPDATE SET data = EXCLUDED.data
        """), {"sid": session_id, "d": json.dumps(payload.get("confidences"))})

    if "thumbnail_crops" in payload:
        conn.execute(text("""
            INSERT INTO thumbnail (session_id, crops)
            VALUES (:sid, CAST(:c AS JSONB))
            ON CONFLICT (session_id) DO UPDATE SET crops = EXCLUDED.crops
        """), {"sid": session_id, "c": json.dumps(payload.get("thumbnail_crops"))})

    return {"session_uid": session_uid}

def _insert_swing(conn, session_id, player_id, s, base_dt):
    suid = s.get("id") or s.get("swing_uid") or s.get("uid")
    start_s = _float(s.get("start_ts") or s.get("start") or s.get("start_s"))
    end_s   = _float(s.get("end_ts") or s.get("end") or s.get("end_s"))
    bh_s    = _float(s.get("ball_hit_timestamp") or s.get("ball_hit_ts") or s.get("ball_hit_s"))
    bh = s.get("ball_hit_location") or {}
    bhx = _float(bh.get("x")) if isinstance(bh, dict) else None
    bhy = _float(bh.get("y")) if isinstance(bh, dict) else None
    bspeed = _float(s.get("ball_speed"))
    bpd    = _float(s.get("ball_player_distance"))
    in_rally = _bool(s.get("is_in_rally"))
    serve = _bool(s.get("serve"))
    serve_type = s.get("serve_type")
    meta = {k:v for k,v in s.items() if k not in {
        "id","swing_uid","uid","player_id","sportai_player_uid",
        "start_ts","start","start_s","end_ts","end","end_s",
        "ball_hit_timestamp","ball_hit_ts","ball_hit_s","ball_hit_location",
        "ball_speed","ball_player_distance","is_in_rally","serve","serve_type"
    }}
    meta_json = json.dumps(meta) if meta else None

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
        "sid": session_id, "pid": player_id, "suid": str(suid) if suid else None,
        "ss": start_s, "es": end_s, "bhs": bh_s,
        "sts": seconds_to_ts(base_dt, start_s), "ets": seconds_to_ts(base_dt, end_s),
        "bh_ts": seconds_to_ts(base_dt, bh_s),
        "bhx": bhx, "bhy": bhy, "bs": bspeed, "bpd": bpd,
        "inr": in_rally, "srv": serve, "stype": serve_type, "meta": meta_json
    })

# -----------------------------
# Endpoints
# -----------------------------
@app.get("/")
def root():
    return jsonify({"service": "NextPoint Upload/Ingester v2", "status": "ok"})

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
    if not q.lower().startswith("select"): return Response("Only SELECT is allowed", status=400)
    if "limit" not in q.lower(): q = f"{q.rstrip(';')} LIMIT 200"
    with engine.connect() as conn:
        rows = conn.execute(text(q)).mappings().all()
        data = [dict(r) for r in rows]
    return jsonify({"ok": True, "rows": len(data), "data": data})

@app.route("/ops/ingest-file", methods=["GET","POST"])
def ops_ingest_file():
    if not _guard(): return _forbid()
    replace = str(request.args.get("replace","0")).strip().lower() in ("1","true","yes","y")
    try:
        payload = _get_json_from_sources()
        # ensure schema/migrations present before ingest
        init_db(engine)
        with engine.begin() as conn:
            res = ingest_result_v2(conn, payload, replace=replace)
        return jsonify({"ok": True, **res, "replace": replace})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")))
