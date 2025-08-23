import os, json, time, hashlib
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

# ---------------------- CONFIG ----------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")
STRICT_REINGEST = os.environ.get("STRICT_REINGEST", "0").strip().lower() in ("1","true","yes","y")
ENABLE_CORS = os.environ.get("ENABLE_CORS", "0").strip().lower() in ("1","true","yes","y")

if not DATABASE_URL: raise RuntimeError("DATABASE_URL required")
if not OPS_KEY: raise RuntimeError("OPS_KEY required")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
app = Flask(__name__)

# ---------------------- UTIL ----------------------
def _guard(): return request.args.get("key") == OPS_KEY
def _forbid(): return Response("Forbidden", status=403)

@app.after_request
def _maybe_cors(resp):
    if ENABLE_CORS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

def seconds_to_ts(base_dt, s):
    if s is None: return None
    try: return base_dt + timedelta(seconds=float(s))
    except Exception: return None

def _float(v):
    try: return float(v) if v is not None else None
    except Exception:
        try: return float(str(v))
        except Exception: return None

def _bool(v):
    if isinstance(v, bool): return v
    if v is None: return None
    s = str(v).strip().lower()
    return True if s in ("1","true","t","yes","y") else False if s in ("0","false","f","no","n") else None

def _time_s(val):
    """Accepts number-like or dict with {timestamp, ts, time_s, t, seconds}."""
    if val is None: return None
    if isinstance(val, (int,float,str)): return _float(val)
    if isinstance(val, dict):
        for k in ("timestamp","ts","time_s","t","seconds"):
            if k in val: return _float(val[k])
    return None

def _canonical_json(obj): return json.dumps(obj, sort_keys=True, separators=(",", ":"))
def _sha1_hex(s: str): return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ---------------------- INPUT HANDLER ----------------------
def _get_json_from_sources():
    """
    Sources precedence:
      1) ?name=foo.json (checks /mnt/data and cwd)
      2) ?url=<direct-json-url>
      3) multipart file
      4) raw JSON body
    """
    name = request.args.get("name")
    if name:
        paths = [name] if os.path.isabs(name) else [f"/mnt/data/{name}", os.path.join(os.getcwd(), name)]
        for p in paths:
            if os.path.exists(p):
                with open(p,"rb") as f: return json.load(f)
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

    raise ValueError("No JSON supplied")

# ---------------------- SESSION HELPERS ----------------------
def _resolve_session_uid(payload, forced_uid=None, src_hint=None):
    if forced_uid: return str(forced_uid)
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_uid","video_uid","video_id"):
        if payload.get(k): return str(payload[k])
        if meta.get(k): return str(meta[k])
    fn = meta.get("file_name") or meta.get("filename")
    if not fn and src_hint:
        try: fn = os.path.splitext(os.path.basename(src_hint))[0]
        except Exception: pass
    if fn: return str(fn)
    return "sha1_" + _sha1_hex(_canonical_json(payload))[:12]

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

# ---------------------- SWINGS ----------------------
def _extract_swing_meta(obj: dict):
    """Flatten useful fields from raw swing JSON into db-ready dict."""
    start_s = _time_s(obj.get("start_ts") or obj.get("start_s") or obj.get("start"))
    end_s   = _time_s(obj.get("end_ts")   or obj.get("end_s")   or obj.get("end"))
    bh_s    = _time_s(obj.get("ball_hit_timestamp") or obj.get("ball_hit_ts") or obj.get("ball_hit_s"))
    bhx = _float((obj.get("ball_hit_location") or {}).get("x"))
    bhy = _float((obj.get("ball_hit_location") or {}).get("y"))

    return {
        "suid": str(obj.get("id") or obj.get("uid") or obj.get("swing_uid") or ""),
        "player_uid": str(obj.get("player_id") or obj.get("sportai_player_uid") or obj.get("uid") or ""),
        "start_s": start_s,
        "end_s": end_s,
        "ball_hit_s": bh_s,
        "ball_hit_x": bhx,
        "ball_hit_y": bhy,
        "serve": _bool(obj.get("serve")),
        "serve_type": obj.get("serve_type"),
        "meta": json.dumps(obj, ensure_ascii=False),   # keep raw for reference
    }

# ---------------------- RALLY LINK ----------------------
def _link_swings_to_rallies(conn, session_id):
    conn.execute(text("""
        UPDATE fact_swing fs
           SET rally_id = dr.rally_id
          FROM dim_rally dr
         WHERE fs.session_id = :sid
           AND dr.session_id = :sid
           AND fs.rally_id IS NULL
           AND COALESCE(fs.ball_hit_s, fs.start_s) BETWEEN dr.start_s AND dr.end_s
    """), {"sid": session_id})

# ---------------------- INGEST CORE ----------------------
def ingest_result_v2(conn, payload, replace=False, forced_uid=None, src_hint=None):
    session_uid  = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
    fps          = _resolve_fps(payload)
    session_date = _resolve_session_date(payload)
    base_dt      = _base_dt_for_session(session_date)

    if replace:
        conn.execute(text("DELETE FROM dim_session WHERE session_uid=:u"), {"u": session_uid})

    # Insert session
    conn.execute(text("""
        INSERT INTO dim_session (session_uid, fps, session_date, meta)
        VALUES (:u,:fps,:sdt,CAST(:m AS JSONB))
        ON CONFLICT (session_uid) DO UPDATE SET
          fps=COALESCE(EXCLUDED.fps,dim_session.fps),
          session_date=COALESCE(EXCLUDED.session_date,dim_session.session_date),
          meta=COALESCE(EXCLUDED.meta,dim_session.meta)
    """), {"u":session_uid,"fps":fps,"sdt":session_date,"m":json.dumps(payload.get("meta") or {})})

    sid = conn.execute(text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u":session_uid}).scalar_one()

    # Raw snapshot
    conn.execute(text("""
        INSERT INTO raw_result (session_id,payload_json,created_at)
        VALUES (:sid,CAST(:p AS JSONB),now() AT TIME ZONE 'utc')
    """), {"sid":sid,"p":json.dumps(payload)})

    # Rallies
    for i,r in enumerate(payload.get("rallies") or [],1):
        try:
            start_s = _time_s(r.get("start") or r.get("start_s"))
            end_s   = _time_s(r.get("end") or r.get("end_s"))
        except AttributeError:
            start_s,end_s = _float(r[0]),_float(r[1])
        conn.execute(text("""
            INSERT INTO dim_rally(session_id,rally_number,start_s,end_s,start_ts,end_ts)
            VALUES(:sid,:n,:ss,:es,:sts,:ets)
            ON CONFLICT(session_id,rally_number) DO UPDATE
              SET start_s=EXCLUDED.start_s,end_s=EXCLUDED.end_s
        """),{"sid":sid,"n":i,"ss":start_s,"es":end_s,
              "sts":seconds_to_ts(base_dt,start_s),
              "ets":seconds_to_ts(base_dt,end_s)})

    # Swings
    for p in payload.get("players") or []:
        for sw in p.get("swings") or []:
            norm = _extract_swing_meta(sw)
            conn.execute(text("""
                INSERT INTO fact_swing(session_id,player_id,sportai_swing_uid,
                    start_s,end_s,ball_hit_s,
                    start_ts,end_ts,ball_hit_ts,
                    ball_hit_x,ball_hit_y,serve,serve_type,meta)
                VALUES(:sid,NULL,:suid,:ss,:es,:bhs,
                       :sts,:ets,:bhts,
                       :bhx,:bhy,:srv,:stype,CAST(:meta AS JSONB))
            """),{
                "sid":sid,"suid":norm["suid"],
                "ss":norm["start_s"],"es":norm["end_s"],"bhs":norm["ball_hit_s"],
                "sts":seconds_to_ts(base_dt,norm["start_s"]),
                "ets":seconds_to_ts(base_dt,norm["end_s"]),
                "bhts":seconds_to_ts(base_dt,norm["ball_hit_s"]),
                "bhx":norm["ball_hit_x"],"bhy":norm["ball_hit_y"],
                "srv":norm["serve"],"stype":norm["serve_type"],"meta":norm["meta"]
            })

    _link_swings_to_rallies(conn,sid)
    return {"session_uid":session_uid}
