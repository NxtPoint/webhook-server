# upload_app.py
import os, json, hashlib
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

# ---------- config ----------
DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY      = os.environ.get("OPS_KEY")
if not DATABASE_URL: raise RuntimeError("DATABASE_URL required")
if not OPS_KEY:      raise RuntimeError("OPS_KEY required")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
app = Flask(__name__)

# ---------- utils ----------
def _guard():  return request.args.get("key") == OPS_KEY
def _deny():   return Response("Forbidden", status=403)
def _float(v):
    try:    return float(v) if v is not None else None
    except: return None

def _time_s(val):
    if val is None: return None
    if isinstance(val, (int,float,str)): return _float(val)
    if isinstance(val, dict):
        for k in ("timestamp","ts","time_s","t","seconds"):
            if k in val: return _float(val[k])
    return None

def _base_ts(dt): return dt if dt else datetime(1970,1,1,tzinfo=timezone.utc)
def _sec_to_ts(base, s): return base + timedelta(seconds=float(s)) if s is not None else None

def _get_json():
    # ?name (file), ?url, multipart file, or raw body
    name = request.args.get("name")
    if name:
        paths = [name] if os.path.isabs(name) else [f"/mnt/data/{name}", os.path.join(os.getcwd(), name)]
        for p in paths:
            try:
                with open(p, "rb") as f: return json.load(f)
            except FileNotFoundError: pass
        raise FileNotFoundError(f"file not found: {paths}")
    url = request.args.get("url")
    if url:
        import requests
        r = requests.get(url, timeout=90); r.raise_for_status()
        return r.json()
    if "file" in request.files:
        return json.load(request.files["file"].stream)
    if request.data:
        return json.loads(request.data.decode("utf-8"))
    raise ValueError("No JSON supplied (use ?name=, ?url=, multipart 'file', or raw body)")

# ---------- schema init (delegates to your db_init / db_views) ----------
def _init_db():
    from db_init import run_init
    run_init(engine)

def _init_views():
    from db_views import run_views
    run_views(engine)

# ---------- rally/serve repair helpers ----------
def _link_swings_to_rallies(conn, session_id=None):
    cond = "TRUE" if session_id is None else "fs.session_id=:sid"
    sql = f"""
      WITH t AS (
        SELECT fs.swing_id, dr.rally_id
        FROM fact_swing fs
        JOIN dim_rally dr
          ON dr.session_id=fs.session_id
         AND COALESCE(fs.ball_hit_s,fs.start_s) BETWEEN dr.start_s AND dr.end_s
        WHERE {cond}
          AND (fs.rally_id IS DISTINCT FROM dr.rally_id OR fs.rally_id IS NULL)
      )
      UPDATE fact_swing fs
         SET rally_id=t.rally_id
        FROM t
       WHERE fs.swing_id=t.swing_id;
    """
    conn.execute(text(sql), {"sid": session_id} if session_id else {})

def _normalize_serve_flags(conn, session_id=None):
    cond = "TRUE" if session_id is None else "fs.session_id=:sid"

    conn.execute(text("""
      DROP TABLE IF EXISTS _first_sw;
      CREATE TEMP TABLE _first_sw AS
      SELECT fs.session_id, fs.rally_id,
             MIN(COALESCE(fs.ball_hit_s,fs.start_s)) AS t0
      FROM fact_swing fs
      WHERE fs.rally_id IS NOT NULL
      GROUP BY fs.session_id, fs.rally_id;
    """))

    # set serve=TRUE on first swing in rallies that currently have none
    conn.execute(text(f"""
      WITH first_ids AS (
        SELECT fs.swing_id
        FROM fact_swing fs
        JOIN _first_sw f ON f.session_id=fs.session_id AND f.rally_id=fs.rally_id
        WHERE COALESCE(fs.ball_hit_s,fs.start_s)=f.t0
      ),
      rallies_without AS (
        SELECT fs.session_id, fs.rally_id
        FROM fact_swing fs
        WHERE {cond} AND fs.rally_id IS NOT NULL
        GROUP BY fs.session_id, fs.rally_id
        HAVING SUM( CASE WHEN COALESCE(fs.serve,false) THEN 1 ELSE 0 END )=0
      )
      UPDATE fact_swing fs
         SET serve=TRUE
       WHERE fs.swing_id IN (SELECT swing_id FROM first_ids)
         AND (fs.session_id,fs.rally_id) IN (SELECT session_id,rally_id FROM rallies_without);
    """), {"sid": session_id} if session_id else {})

    # if multiple serves are flagged in a rally, keep earliest only
    conn.execute(text(f"""
      WITH serves AS (
        SELECT fs.session_id, fs.rally_id, fs.swing_id,
               ROW_NUMBER() OVER (
                 PARTITION BY fs.session_id, fs.rally_id
                 ORDER BY COALESCE(fs.ball_hit_s,fs.start_s), fs.swing_id
               ) AS rn
        FROM fact_swing fs
        WHERE {cond} AND fs.rally_id IS NOT NULL AND COALESCE(fs.serve,false)
      )
      UPDATE fact_swing fs
         SET serve=FALSE
        FROM serves s
       WHERE fs.swing_id=s.swing_id AND s.rn>1;
    """), {"sid": session_id} if session_id else {})

# ---------- very small ingest (kept for convenience) ----------
def _resolve_session_uid(payload, forced_uid=None, src_hint=None):
    if forced_uid: return str(forced_uid)
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_uid","video_uid","video_id"):
        if payload.get(k): return str(payload[k])
        if meta.get(k):    return str(meta[k])
    fn = meta.get("file_name") or meta.get("filename")
    if not fn and src_hint:
        try: fn = os.path.splitext(os.path.basename(src_hint))[0]
        except: pass
    if fn: return str(fn)
    fp = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"sha1_{fp}"

def _resolve_fps(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("fps","frame_rate","frames_per_second"):
        v = payload.get(k) if k in payload else meta.get(k)
        if v is not None: return _float(v)
    return None

def _ingest(conn, payload, replace=False, forced_uid=None, src_hint=None):
    session_uid = _resolve_session_uid(payload, forced_uid, src_hint)
    fps   = _resolve_fps(payload)
    sdt   = None
    meta  = payload.get("meta") or payload.get("metadata") or {}
    try:
        raw = meta.get("session_date") or meta.get("date") or meta.get("recorded_at")
        if raw: sdt = datetime.fromisoformat(str(raw).replace("Z","+00:00")).astimezone(timezone.utc)
    except: pass
    base_ts = _base_ts(sdt)

    if replace:
        conn.execute(text("DELETE FROM dim_session WHERE session_uid=:u"), {"u": session_uid})

    conn.execute(text("""
      INSERT INTO dim_session (session_uid,fps,session_date,meta)
      VALUES (:u,:fps,:sdt, CAST(:m AS JSONB))
      ON CONFLICT (session_uid) DO UPDATE
      SET fps=COALESCE(EXCLUDED.fps,dim_session.fps),
          session_date=COALESCE(EXCLUDED.session_date,dim_session.session_date),
          meta=COALESCE(EXCLUDED.meta,dim_session.meta)
    """), {"u":session_uid,"fps":fps,"sdt":sdt,"m":json.dumps(meta)})

    sid = conn.execute(text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u":session_uid}).scalar_one()

    # snapshot
    conn.execute(text("INSERT INTO raw_result (session_id,payload_json,created_at) VALUES (:sid, CAST(:p AS JSONB), now() AT TIME ZONE 'utc')"),
                 {"sid":sid,"p":json.dumps(payload)})

    # rallies (accept [start,end] or dicts with start*/end*)
    for i, r in enumerate(payload.get("rallies") or [], start=1):
        if isinstance(r, dict):
            ss = _time_s(r.get("start_ts") or r.get("start"))
            es = _time_s(r.get("end_ts")   or r.get("end"))
        else:
            try: ss, es = _float(r[0]), _float(r[1])
            except: ss, es = None, None
        conn.execute(text("""
          INSERT INTO dim_rally (session_id,rally_number,start_s,end_s,start_ts,end_ts)
          VALUES (:sid,:n,:ss,:es,:sts,:ets)
          ON CONFLICT (session_id,rally_number) DO UPDATE
          SET start_s=COALESCE(EXCLUDED.start_s,dim_rally.start_s),
              end_s=COALESCE(EXCLUDED.end_s,dim_rally.end_s),
              start_ts=COALESCE(EXCLUDED.start_ts,dim_rally.start_ts),
              end_ts=COALESCE(EXCLUDED.end_ts,dim_rally.end_ts)
        """), {"sid":sid,"n":i,"ss":ss,"es":es,"sts":_sec_to_ts(base_ts,ss),"ets":_sec_to_ts(base_ts,es)})

    # swings (extremely permissive—expects upstream to fill player_id later if needed)
    for obj in (payload.get("swings") or payload.get("strokes") or []):
        ss = _time_s(obj.get("start_ts") or obj.get("start_s") or obj.get("start"))
        es = _time_s(obj.get("end_ts")   or obj.get("end_s")   or obj.get("end"))
        hs = _time_s(obj.get("ball_hit_timestamp") or obj.get("ball_hit_ts") or obj.get("ball_hit_s"))
        bh = obj.get("ball_hit") or {}
        bhx = _float((bh.get("location") or {}).get("x") if isinstance(bh, dict) else None) or _float(obj.get("ball_hit_x"))
        bhy = _float((bh.get("location") or {}).get("y") if isinstance(bh, dict) else None) or _float(obj.get("ball_hit_y"))
        serve = obj.get("serve")
        serve_type = obj.get("serve_type")
        meta_extra = {k:v for k,v in obj.items() if k not in {
            "start","start_s","start_ts","end","end_s","end_ts",
            "ball_hit_timestamp","ball_hit_ts","ball_hit_s","ball_hit","ball_hit_x","ball_hit_y",
            "serve","serve_type","player_id","sportai_player_uid","player_uid"
        }}
        conn.execute(text("""
          INSERT INTO fact_swing (
            session_id, player_id, sportai_swing_uid,
            start_s, end_s, ball_hit_s,
            start_ts, end_ts, ball_hit_ts,
            ball_hit_x, ball_hit_y, serve, serve_type, meta
          ) VALUES (
            :sid, NULL, :suid,
            :ss, :es, :hs,
            :sts, :ets, :hts,
            :bhx, :bhy, :srv, :stype, CAST(:m AS JSONB)
          )
        """), {
            "sid":sid,
            "suid": str(obj.get("id") or obj.get("swing_uid") or obj.get("uid") or ""),
            "ss": ss, "es": es, "hs": hs,
            "sts": _sec_to_ts(base_ts, ss), "ets": _sec_to_ts(base_ts, es), "hts": _sec_to_ts(base_ts, hs),
            "bhx": bhx, "bhy": bhy, "srv": serve, "stype": serve_type,
            "m": json.dumps(meta_extra) if meta_extra else None
        })

    # after ingest — link rallies + normalize serves
    _link_swings_to_rallies(conn, sid)
    _normalize_serve_flags(conn, sid)
    return {"session_uid": session_uid}

# ---------- root ----------
@app.get("/")
def root():
    return jsonify({"service": "NextPoint API", "status": "ok"})

# ---------- ops ----------
@app.get("/ops/db-ping")
def db_ping():
    if not _guard(): return _deny()
    with engine.connect() as c:
        now = c.execute(text("SELECT now() AT TIME ZONE 'utc'")).scalar_one()
    return jsonify({"ok": True, "now_utc": str(now)})

@app.get("/ops/init-db")
def ops_init_db():
    if not _guard(): return _deny()
    try:
        _init_db()
        return jsonify({"ok": True, "message": "DB initialized / migrated"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/init-views")
def ops_init_views():
    if not _guard(): return _deny()
    try:
        _init_views()
        return jsonify({"ok": True, "message": "Views created/refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ops/sql", methods=["GET","POST"])
def ops_sql():
    if not _guard(): return _deny()
    q = request.values.get("q","").strip()
    if not q: return jsonify({"ok": False, "error": "q required"}), 400
    if not (q.lower().startswith("select") or q.lower().startswith("with")):
        return Response("Only SELECT/CTE queries are allowed", status=400)
    if " limit " not in q.lower():
        q = f"{q.rstrip(';')} LIMIT 200"
    try:
        timeout_ms = int(request.args.get("timeout_ms","60000"))
    except: timeout_ms = 60000
    try:
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            conn.execute(text("SET LOCAL TRANSACTION READ ONLY"))
            rows = conn.execute(text(q)).mappings().all()
            return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "query": q}), 400

@app.get("/ops/list-sessions")
def ops_list_sessions():
    if not _guard(): return _deny()
    with engine.connect() as conn:
        rows = conn.execute(text("""
          SELECT s.session_uid,
                 (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id=s.session_id) AS players,
                 (SELECT COUNT(*) FROM dim_rally dr  WHERE dr.session_id=s.session_id) AS rallies,
                 (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id=s.session_id) AS swings,
                 (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=s.session_id) AS ball_bounces,
                 (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=s.session_id) AS ball_positions,
                 (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=s.session_id) AS player_positions,
                 (SELECT COUNT(*) FROM raw_result rr WHERE rr.session_id=s.session_id) AS snapshots
          FROM dim_session s
          ORDER BY s.session_uid
        """)).mappings().all()
    return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})

@app.get("/ops/delete-session")
def ops_delete_session():
    if not _guard(): return _deny()
    uid = request.args.get("session_uid")
    if not uid: return jsonify({"ok": False, "error": "session_uid required"}), 400
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dim_session WHERE session_uid=:u"), {"u": uid})
    return jsonify({"ok": True, "deleted_session_uid": uid})

@app.route("/ops/ingest-file", methods=["GET","POST"])
def ops_ingest_file():
    if not _guard(): return _deny()
    replace  = str(request.args.get("replace","0")).lower() in ("1","true","yes","y")
    forced   = request.args.get("session_uid")
    src_hint = request.args.get("name") or request.args.get("url")
    try:
        payload = _get_json()
        _init_db()  # ensure tables exist
        with engine.begin() as conn:
            res = _ingest(conn, payload, replace=replace, forced_uid=forced, src_hint=src_hint)
        return jsonify({"ok": True, **res, "replace": replace})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/reconcile")
def ops_reconcile():
    if not _guard(): return _deny()
    forced_uid = request.args.get("session_uid")
    if not forced_uid:
        return jsonify({"ok": False, "error": "session_uid required"}), 400
    with engine.connect() as conn:
        row = conn.execute(text("SELECT session_id,fps FROM dim_session WHERE session_uid=:u"), {"u": forced_uid}).mappings().first()
        if not row:
            return jsonify({"ok": False, "error": "session not found"}), 404
        sid = row["session_id"]
        counts = conn.execute(text("""
          SELECT
            (SELECT COUNT(*) FROM dim_rally dr WHERE dr.session_id=:sid)            AS rallies,
            (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id=:sid)          AS swings,
            (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=:sid)           AS ball_bounces,
            (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=:sid)  AS ball_positions,
            (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=:sid)AS player_positions
        """), {"sid": sid}).mappings().first()
    return jsonify({"ok": True, "session_uid": forced_uid, "counts": dict(counts)})

@app.get("/ops/perf-indexes")
def ops_perf_indexes():
    if not _guard(): return _deny()
    ddl = [
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_rally ON fact_swing(session_id, rally_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_hitstart_expr ON fact_swing ((COALESCE(ball_hit_s, start_s)))",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_hitstart ON fact_swing(session_id, (COALESCE(ball_hit_s, start_s)))",
        "CREATE INDEX IF NOT EXISTS idx_dim_rally_session_bounds ON dim_rally(session_id, start_s, end_s)",
        "CREATE INDEX IF NOT EXISTS idx_fact_bounce_session_rally ON fact_bounce(session_id, rally_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_player_position_session_player ON fact_player_position(session_id, player_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_ball_position_session_ts ON fact_ball_position(session_id, ts_s)"
    ]
    with engine.begin() as conn:
        for s in ddl: conn.execute(text(s))
        conn.execute(text("ANALYZE"))
    return jsonify({"ok": True, "created_or_exists": ddl})

@app.get("/ops/link-swings-to-rallies")
def ops_link_swings_to_rallies():
    if not _guard(): return _deny()
    uid = request.args.get("session_uid")
    if not uid: return jsonify({"ok": False, "error": "session_uid required"}), 400
    with engine.begin() as conn:
        sid = conn.execute(text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u": uid}).scalar()
        if not sid: return jsonify({"ok": False, "error": "session not found"}), 404
        _link_swings_to_rallies(conn, sid)
    return jsonify({"ok": True, "session_uid": uid, "linked": True})

@app.get("/ops/repair-swings")
def ops_repair_swings():
    if not _guard(): return _deny()
    uid = request.args.get("session_uid")  # optional
    with engine.begin() as conn:
        sid = None
        if uid:
            sid = conn.execute(text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u": uid}).scalar()
            if not sid: return jsonify({"ok": False, "error": "session not found"}), 404
        _link_swings_to_rallies(conn, sid)
        _normalize_serve_flags(conn, sid)
        rows = conn.execute(text("""
          SELECT ds.session_uid,
                 COUNT(*) FILTER (WHERE fs.rally_id IS NOT NULL) AS swings_with_rally,
                 SUM(CASE WHEN COALESCE(fs.serve,false) THEN 1 ELSE 0 END) AS serve_swings
          FROM fact_swing fs
          JOIN dim_session ds ON ds.session_id=fs.session_id
          WHERE (:sid IS NULL OR fs.session_id=:sid)
          GROUP BY ds.session_uid
          ORDER BY ds.session_uid
        """), {"sid": sid}).mappings().all()
    return jsonify({"ok": True, "data": [dict(r) for r in rows]})

# ---------- local run ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")))
