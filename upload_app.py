# upload_app.py
import os, json, re, uuid, pathlib, hashlib
from datetime import datetime, timezone, timedelta

import requests
from flask import render_template, send_from_directory
from flask import (
    Flask, request, jsonify, Response, make_response, send_from_directory
)
from sqlalchemy import text as _sa_text
from sqlalchemy.exc import IntegrityError

from db_init import engine


# --------------------------------------------------------------------------------------
# Configuration (keeps your existing env names; no renames)
# --------------------------------------------------------------------------------------
sql_text = _sa_text  # compatibility alias used throughout

DATABASE_URL   = os.environ.get("DATABASE_URL")
OPS_KEY        = os.environ.get("OPS_KEY") or "270fb80a747d459eafded0ae67b9b8f6"
STRICT_REINGEST= os.environ.get("STRICT_REINGEST", "0").strip().lower() in ("1","true","yes","y")
ENABLE_CORS    = os.environ.get("ENABLE_CORS", "0").strip().lower() in ("1","true","yes","y")

# Enforce bronze mirrors raw (derivations only if payload has none)
STRICT_BRONZE_RAW = os.environ.get("STRICT_BRONZE_RAW", "1").strip().lower() in ("1","true","yes","y")
# Prefer payload rallies over derived rallies when present
PREFER_PAYLOAD_RALLIES = os.environ.get("PREFER_PAYLOAD_RALLIES", "1").strip().lower() in ("1","true","yes","y")

# Upload UI / Integrations (uses your established variable names)
MAX_UPLOAD_MB       = int(os.environ.get("MAX_UPLOAD_MB", "200"))

# Dropbox
DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
DROPBOX_TARGET_FOLDER= os.environ.get("DROPBOX_TARGET_FOLDER") or os.environ.get("DBX_TARGET_FOLDER") or "/uploads"

# SportAI
SPORT_AI_TOKEN        = os.environ.get("SPORT_AI_TOKEN", "")
SPORT_AI_RESULT_FIELD = os.environ.get("SPORT_AI_RESULT_FIELD", "result_json_url")  # field in status JSON with final JSON URL
SPORTAI_API_BASE      = os.environ.get("SPORTAI_API_BASE", "").rstrip("/")
SPORTAI_CREATE_URL    = os.environ.get("SPORT_AI_CREATE_URL") or (
    f"{SPORTAI_API_BASE}/v1/tasks" if SPORTAI_API_BASE else None
)
SPORTAI_STATUS_URL_TPL= os.environ.get("SPORT_AI_STATUS_URL_TEMPLATE") or (
    f"{SPORTAI_API_BASE}/v1/tasks/{{task_id}}" if SPORTAI_API_BASE else None
)

# Public base for webhook callback (used when creating SportAI task)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL required")


app = Flask(__name__, template_folder="templates", static_folder="static")
BASE_DIR = pathlib.Path(__file__).resolve().parent

# ====== DIAGNOSTIC & UI FALLBACK ROUTES (safe, read-only) ======
from flask import jsonify, render_template_string
from sqlalchemy import text

@app.get("/ops/routes")
def ops_routes():
    """
    List all registered routes so we can verify what's actually running in Render.
    Protected with the same guard as other /ops endpoints.
    """
    if not _guard():
        return _forbid()
    routes = sorted(
        {"rule": r.rule, "endpoint": r.endpoint, "methods": sorted(r.methods)}
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/upload/health")
def _ui_health():
    """Read-only DB ping so the UI path is always alive, even if the UI blueprint fails to load."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/upload/sessions")
def _ui_sessions_proxy():
    """
    Fallback dispatcher for the Sessions page: tries the UI blueprint handler; if that
    import fails you still get a readable error instead of a 404.
    """
    try:
        from ui_app import sessions as _sessions
        return _sessions()
    except Exception as e:
        return render_template_string(
            "<pre>UI /upload/sessions failed to dispatch:\n\n{{err}}</pre>", err=str(e)
        ), 500
# ====== END DIAGNOSTICS ======


# --------------------------------------------------------------------------------------
# CORS / guard helpers
# --------------------------------------------------------------------------------------
def _guard():
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return supplied == OPS_KEY

def _forbid(): return Response("Forbidden", status=403)

@app.after_request
def _maybe_cors(resp):
    if ENABLE_CORS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-OPS-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------
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
    if s in ("1","true","t","yes","y"): return True
    if s in ("0","false","f","no","n"):  return False
    return None

def _time_s(val):
    if val is None: return None
    if isinstance(val, (int, float, str)): return _float(val)
    if isinstance(val, dict):
        for k in ("timestamp","timestamp_s","ts","time_s","t","seconds","s"):
            if k in val: return _float(val[k])
    return None

def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

def _sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def _quantize_time_to_fps(s, fps):
    if s is None or not fps: return s
    return round(round(float(s) * float(fps)) / float(fps), 5)

_INVALID_PUIDS = {"", "none", "null", "nan"}
def _valid_puid(p):
    if p is None: return False
    s = str(p).strip().lower()
    return s not in _INVALID_PUIDS

def _quantize_time(s, fps):
    if s is None: return None
    if fps: return _quantize_time_to_fps(s, fps)
    return round(float(s), 3)  # stable 1ms grid

# --------------------------------------------------------------------------------------
# Session mappers
# --------------------------------------------------------------------------------------
def _resolve_session_uid(payload, forced_uid=None, src_hint=None):
    if forced_uid: return str(forced_uid)
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("session_uid","video_uid","video_id"):
        if payload.get(k): return str(payload[k])
        if meta.get(k):    return str(meta[k])
    fn = meta.get("file_name") or meta.get("filename")
    if not fn and src_hint:
        try:
            import os as _os
            fn = _os.path.splitext(_os.path.basename(src_hint))[0]
        except Exception:
            fn = None
    if fn: return str(fn)
    fp = _sha1_hex(_canonical_json(payload))[:12]
    return f"sha1_{fp}"

def _resolve_fps(payload):
    meta = payload.get("meta") or payload.get("metadata") or {}
    for k in ("fps","frame_rate","frames_per_second"):
        if payload.get(k) is not None: return _float(payload[k])
        if meta.get(k) is not None:    return _float(meta[k])
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

def _base_dt_for_session(dt): return dt if dt else datetime(1970,1,1,tzinfo=timezone.utc)

# --------------------------------------------------------------------------------------
# Swing normalization
# --------------------------------------------------------------------------------------
_SWING_TYPES  = {"swing","stroke","shot","hit","serve","forehand","backhand","volley","overhead","slice","drop","lob"}
_SERVE_LABELS = {"serve","first_serve","1st_serve","second_serve","2nd_serve"}

def _extract_ball_hit_from_events(events):
    if not isinstance(events, list): return (None,None,None)
    for ev in events:
        if not isinstance(ev, dict): continue
        label = (str(ev.get("type") or ev.get("label") or "")).lower()
        if label in {"ball_hit","contact","impact"}:
            ts = _time_s(ev.get("timestamp") or ev.get("ts") or ev.get("time_s") or ev.get("t"))
            loc = ev.get("location") or {}
            return ts, _float((loc or {}).get("x")), _float((loc or {}).get("y"))
    return (None,None,None)

def _normalize_swing_obj(obj):
    if not isinstance(obj, dict): return None
    suid   = obj.get("id") or obj.get("swing_uid") or obj.get("uid")
    start_s = _time_s(obj.get("start_ts")) or _time_s(obj.get("start_s")) or _time_s(obj.get("start"))
    end_s   = _time_s(obj.get("end_ts"))   or _time_s(obj.get("end_s"))   or _time_s(obj.get("end"))
    if start_s is None and end_s is None:
        only_ts = _time_s(obj.get("timestamp") or obj.get("ts") or obj.get("time_s") or obj.get("t"))
        if only_ts is not None: start_s = end_s = only_ts
    bh_s = _time_s(obj.get("ball_hit_timestamp") or obj.get("ball_hit_ts") or obj.get("ball_hit_s"))
    bhx = bhy = None
    if bh_s is None and isinstance(obj.get("ball_hit"), dict):
        bh_s = _time_s(obj["ball_hit"].get("timestamp"))
        loc  = obj["ball_hit"].get("location") or {}
        bhx  = _float(loc.get("x")); bhy = _float(loc.get("y"))
    if bh_s is None:
        ev_bh_s, ev_bhx, ev_bhy = _extract_ball_hit_from_events(obj.get("events"))
        bh_s = ev_bh_s
        bhx = bhx if bhx is not None else ev_bhx
        bhy = bhy if bhy is not None else ev_bhy
    loc_any = obj.get("ball_hit_location")
    if (bhx is None or bhy is None) and isinstance(loc_any, dict):
        bhx = _float(loc_any.get("x")); bhy = _float(loc_any.get("y"))
    if (bhx is None or bhy is None) and isinstance(loc_any, (list,tuple)) and len(loc_any) >= 2:
        bhx = _float(loc_any[0]); bhy = _float(loc_any[1])
    swing_type = (str(obj.get("swing_type") or obj.get("type") or obj.get("label") or obj.get("stroke_type") or "")).lower()
    serve = _bool(obj.get("serve"))
    serve_type = obj.get("serve_type")
    if not serve and swing_type in _SERVE_LABELS:
        serve = True
        if serve_type is None and swing_type != "serve":
            serve_type = swing_type
    player_uid = (obj.get("player_id") or obj.get("sportai_player_uid") or obj.get("player_uid") or obj.get("player"))
    if player_uid is not None: player_uid = str(player_uid)
    ball_speed            = _float(obj.get("ball_speed"))
    ball_player_distance  = _float(obj.get("ball_player_distance"))
    volley                = _bool(obj.get("volley"))
    is_in_rally           = _bool(obj.get("is_in_rally"))
    confidence_swing_type = _float(obj.get("confidence_swing_type"))
    confidence            = _float(obj.get("confidence"))
    confidence_volley     = _float(obj.get("confidence_volley"))
    if start_s is None and end_s is None and bh_s is None:
        return None
    meta = {k: v for k, v in obj.items() if k not in {
        "id","uid","swing_uid","player_id","sportai_player_uid","player_uid","player",
        "type","label","stroke_type","swing_type","start","start_s","start_ts","end","end_s","end_ts",
        "timestamp","ts","time_s","t","ball_hit","ball_hit_timestamp","ball_hit_ts","ball_hit_s",
        "ball_hit_location","events","serve","serve_type","ball_speed",
        "ball_player_distance","volley","is_in_rally","confidence_swing_type","confidence","confidence_volley"
    }}
    return {
        "suid": suid, "player_uid": player_uid,
        "start_s": start_s, "end_s": end_s, "ball_hit_s": bh_s,
        "ball_hit_x": bhx, "ball_hit_y": bhy,
        "swing_type": swing_type, "volley": volley, "is_in_rally": is_in_rally,
        "serve": serve, "serve_type": serve_type,
        "confidence_swing_type": confidence_swing_type,
        "confidence": confidence, "confidence_volley": confidence_volley,
        "ball_speed": ball_speed, "ball_player_distance": ball_player_distance,
        "meta": meta if meta else None,
    }

def _iter_candidate_swings_from_container(container):
    if not isinstance(container, dict): return
    keys = ("swings","strokes","swing_events") if STRICT_BRONZE_RAW else ("swings","strokes","swing_events","events")
    for key in keys:
        arr = container.get(key)
        if not isinstance(arr, list): continue
        if key == "events":
            for item in arr:
                lbl = str((item or {}).get("type") or (item or {}).get("label") or "").lower()
                if lbl and (lbl in _SWING_TYPES or "swing" in lbl or "stroke" in lbl):
                    norm = _normalize_swing_obj(item)
                    if norm: yield norm
        else:
            for item in arr:
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

# --------------------------------------------------------------------------------------
# DB helpers for RAW and rallies
# --------------------------------------------------------------------------------------
def _insert_raw_result(conn, sid: int, payload: dict) -> None:
    conn.execute(sql_text("""
        INSERT INTO raw_result (session_id, payload_json, created_at)
        VALUES (:sid, CAST(:p AS JSONB), now() AT TIME ZONE 'utc')
    """), {"sid": sid, "p": json.dumps(payload)})

def _fact_swing_ts_cols(conn):
    rows = conn.execute(sql_text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='fact_swing'
    """)).fetchall()
    cols = {r[0] for r in rows}
    ts_col     = 'ball_hit_s'  if 'ball_hit_s'  in cols else ('ts_s' if 'ts_s' in cols else None)
    ts_abs_col = 'ball_hit_ts' if 'ball_hit_ts' in cols else ('ts'   if 'ts'   in cols else None)
    return ts_col, ts_abs_col

def _ensure_rallies_from_swings(conn, session_id, gap_s=6.0):
    conn.execute(sql_text("""
    WITH sw AS (
      SELECT session_id,
             COALESCE(ball_hit_s, start_s) AS t,
             swing_id,
             ROW_NUMBER() OVER (ORDER BY COALESCE(ball_hit_s, start_s), swing_id) AS rn
      FROM fact_swing
      WHERE session_id = :sid AND COALESCE(ball_hit_s, start_s) IS NOT NULL
    ),
    seg AS (
      SELECT sw.*,
             CASE
               WHEN rn = 1 THEN 1
               WHEN (t - LAG(t) OVER (ORDER BY rn)) >= :gap THEN 1
               ELSE 0
             END AS new_seg
      FROM sw
    ),
    grp AS (
      SELECT session_id, t,
             SUM(new_seg) OVER (ORDER BY rn) AS grp_id
      FROM seg
    ),
    bounds AS (
      SELECT session_id, grp_id,
             MIN(t) AS start_s, MAX(t) AS end_s
      FROM grp
      GROUP BY session_id, grp_id
    ),
    upsert AS (
      INSERT INTO dim_rally (session_id, rally_number, start_s, end_s)
      SELECT b.session_id,
             ROW_NUMBER() OVER (ORDER BY start_s) AS rally_number,
             b.start_s, b.end_s
      FROM bounds b
      ON CONFLICT (session_id, rally_number) DO UPDATE
      SET start_s = EXCLUDED.start_s,
          end_s   = EXCLUDED.end_s
      RETURNING 1
    )
    SELECT 1;
    """), {"sid": session_id, "gap": float(gap_s)})

def _link_swings_to_rallies(conn, session_id):
    conn.execute(sql_text("""
        UPDATE fact_swing fs
           SET rally_id = dr.rally_id
          FROM dim_rally dr
         WHERE fs.session_id = :sid
           AND dr.session_id = :sid
           AND fs.rally_id IS NULL
           AND COALESCE(fs.ball_hit_s, fs.start_s) BETWEEN dr.start_s AND dr.end_s
    """), {"sid": session_id})

def _normalize_serve_flags(conn, session_id):
    conn.execute(sql_text("""
    DROP TABLE IF EXISTS _first_sw;
    CREATE TEMP TABLE _first_sw AS
    SELECT fs.session_id, fs.rally_id,
           MIN(COALESCE(fs.ball_hit_s, fs.start_s)) AS t0
      FROM fact_swing fs
     WHERE fs.session_id = :sid AND fs.rally_id IS NOT NULL
     GROUP BY fs.session_id, fs.rally_id;

    DROP TABLE IF EXISTS _first_sw_ids;
    CREATE TEMP TABLE _first_sw_ids AS
    SELECT fs.swing_id
      FROM fact_swing fs
      JOIN _first_sw f
        ON f.session_id = fs.session_id
       AND f.rally_id   = fs.rally_id
     WHERE COALESCE(fs.ball_hit_s, fs.start_s) = f.t0;

    WITH rallies_without_serve AS (
      SELECT fs.session_id, fs.rally_id
        FROM fact_swing fs
       WHERE fs.session_id = :sid AND fs.rally_id IS NOT NULL
       GROUP BY fs.session_id, fs.rally_id
      HAVING SUM(CASE WHEN COALESCE(fs.serve, FALSE) THEN 1 ELSE 0 END) = 0
    )
    UPDATE fact_swing fs
       SET serve = TRUE
     WHERE fs.swing_id IN (SELECT swing_id FROM _first_sw_ids)
       AND (fs.session_id, fs.rally_id) IN (SELECT session_id, rally_id FROM rallies_without_serve);

    WITH serves AS (
      SELECT fs.session_id, fs.rally_id, fs.swing_id,
             ROW_NUMBER() OVER (
               PARTITION BY fs.session_id, fs.rally_id
               ORDER BY COALESCE(fs.ball_hit_s, fs.start_s), fs.swing_id
             ) AS rn
      FROM fact_swing fs
     WHERE fs.session_id = :sid AND fs.rally_id IS NOT NULL AND COALESCE(fs.serve, FALSE)
    )
    UPDATE fact_swing fs
       SET serve = FALSE
      FROM serves s
     WHERE fs.swing_id = s.swing_id
       AND s.rn > 1;
    """), {"sid": session_id})

def _rebuild_ts_from_seconds(conn, session_id):
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE(
                   (SELECT MIN(COALESCE(ball_hit_s, start_s))
                      FROM fact_swing WHERE session_id=:sid),
                   0
                 ) AS t0
        )
        UPDATE fact_swing fs
           SET start_ts    = make_timestamp(1970,1,1,0,0,0)
                            + make_interval(secs => GREATEST(0, COALESCE(fs.start_s,0)    - z.t0)),
               end_ts      = make_timestamp(1970,1,1,0,0,0)
                            + make_interval(secs => GREATEST(0, COALESCE(fs.end_s,0)      - z.t0)),
               ball_hit_ts = make_timestamp(1970,1,1,0,0,0)
                            + make_interval(secs => GREATEST(0, COALESCE(fs.ball_hit_s,0) - z.t0))
          FROM z
         WHERE fs.session_id = z.session_id;
    """), {"sid": session_id})

    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE(
                   (SELECT MIN(COALESCE(ball_hit_s, start_s))
                      FROM fact_swing WHERE session_id=:sid),
                   0
                 ) AS t0
        )
        UPDATE fact_bounce b
           SET bounce_ts = make_timestamp(1970,1,1,0,0,0)
                           + make_interval(secs => GREATEST(0, COALESCE(b.bounce_s,0) - z.t0))
          FROM z
         WHERE b.session_id = z.session_id;
    """), {"sid": session_id})

    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE(
                   (SELECT MIN(COALESCE(ball_hit_s, start_s))
                      FROM fact_swing WHERE session_id=:sid),
                   0
                 ) AS t0
        )
        UPDATE fact_ball_position bp
           SET ts = make_timestamp(1970,1,1,0,0,0)
                    + make_interval(secs => GREATEST(0, COALESCE(bp.ts_s,0) - z.t0))
          FROM z
         WHERE bp.session_id = z.session_id;
    """), {"sid": session_id})

    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE(
                   (SELECT MIN(COALESCE(ball_hit_s, start_s))
                      FROM fact_swing WHERE session_id=:sid),
                   0
                 ) AS t0
        )
        UPDATE fact_player_position pp
           SET ts = make_timestamp(1970,1,1,0,0,0)
                    + make_interval(secs => GREATEST(0, COALESCE(pp.ts_s,0) - z.t0))
          FROM z
         WHERE pp.session_id = z.session_id;
    """), {"sid": session_id})

# --------------------------------------------------------------------------------------
# Ingestion (bronze mirrors raw; no transforms except "derive rallies if none")
# --------------------------------------------------------------------------------------
def _insert_swing(conn, session_id, player_id, s, base_dt, fps):
    q_start = _quantize_time(s.get("start_s"), fps)
    q_end   = _quantize_time(s.get("end_s"), fps)
    q_hit   = _quantize_time(s.get("ball_hit_s"), fps)

    conn.execute(sql_text("""
        INSERT INTO fact_swing (
            session_id, player_id, sportai_swing_uid,
            start_s, end_s, ball_hit_s,
            start_ts, end_ts, ball_hit_ts,
            ball_hit_x, ball_hit_y, ball_speed, ball_player_distance,
            swing_type, volley, is_in_rally, serve, serve_type,
            confidence_swing_type, confidence, confidence_volley, meta
        ) VALUES (
            :sid, :pid, :suid,
            :ss, :es, :bhs,
            :sts, :ets, :bh_ts,
            :bhx, :bhy, :bs, :bpd,
            :sw_type, :vol, :inr, :srv, :srv_type,
            :cst, :conf, :cv, CAST(:meta AS JSONB)
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
        "sw_type": s.get("swing_type"),
        "vol": s.get("volley"),
        "inr": s.get("is_in_rally"),
        "srv": s.get("serve"),
        "srv_type": s.get("serve_type"),
        "cst": s.get("confidence_swing_type"),
        "conf": s.get("confidence"),
        "cv": s.get("confidence_volley"),
        "meta": json.dumps(s.get("meta")) if s.get("meta") else None
    })

def ingest_result_v2(conn, payload, replace=False, forced_uid=None, src_hint=None):
    # ---------- session resolution ----------
    session_uid  = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
    fps          = _resolve_fps(payload)
    session_date = _resolve_session_date(payload)
    base_dt      = _base_dt_for_session(session_date)
    meta         = payload.get("meta") or payload.get("metadata") or {}
    meta_json    = json.dumps(meta)

    # Upsert session row
    conn.execute(sql_text("""
        INSERT INTO dim_session (session_uid, fps, session_date, meta)
        VALUES (:u, :fps, :sdt, CAST(:m AS JSONB))
        ON CONFLICT (session_uid)
        DO UPDATE SET
          fps = COALESCE(EXCLUDED.fps, dim_session.fps),
          session_date = COALESCE(EXCLUDED.session_date, dim_session.session_date),
          meta = COALESCE(EXCLUDED.meta, dim_session.meta)
    """), {"u": session_uid, "fps": fps, "sdt": session_date, "m": meta_json})

    session_id = conn.execute(
        sql_text("SELECT session_id FROM dim_session WHERE session_uid = :u"),
        {"u": session_uid}
    ).scalar_one()

    # If replace requested: clear children to avoid duplication (keep RAW history)
    if replace:
        conn.execute(sql_text("DELETE FROM fact_ball_position   WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM fact_player_position WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM fact_bounce          WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM fact_swing           WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM dim_rally            WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM dim_player           WHERE session_id=:sid"), {"sid": session_id})

    # ---------- raw snapshot (verbatim) ----------
    _insert_raw_result(conn, session_id, payload)

    # ---------- players ----------
    players = payload.get("players") or []
    uid_to_player_id = {}

    conn.execute(sql_text("ALTER TABLE IF EXISTS dim_player ADD COLUMN IF NOT EXISTS meta JSONB"))

    for p in players:
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        if not puid:
            continue

        full_name = p.get("full_name") or p.get("name")
        handed    = p.get("handedness")
        age       = p.get("age")
        utr       = _float(p.get("utr"))

        metrics = p.get("metrics") or {}

        covered_distance  = _float(p.get("covered_distance") or metrics.get("covered_distance"))
        fastest_sprint    = _float(p.get("fastest_sprint")   or metrics.get("fastest_sprint"))
        fastest_sprint_ts = _float(
            p.get("fastest_sprint_timestamp") or
            p.get("fastest_sprint_timestamp_s") or
            metrics.get("fastest_sprint_timestamp") or
            metrics.get("fastest_sprint_timestamp_s")
        )
        activity_score    = _float(p.get("activity_score")   or metrics.get("activity_score"))

        swing_type_distribution = p.get("swing_type_distribution")
        location_heatmap        = p.get("location_heatmap") or p.get("heatmap")

        player_meta = {k: v for k, v in p.items() if k not in {
            "id","sportai_player_uid","uid","player_id",
            "full_name","name","handedness","age","utr",
            "metrics","statistics","stats",
            "swing_type_distribution","location_heatmap","heatmap"
        }}

        conn.execute(sql_text("""
            INSERT INTO dim_player (
                session_id, sportai_player_uid, full_name, handedness, age, utr,
                covered_distance, fastest_sprint, fastest_sprint_timestamp_s,
                activity_score, swing_type_distribution, location_heatmap, meta
            ) VALUES (
                :sid, :puid, :nm, :hand, :age, :utr,
                :cd, :fs, :fst,
                :ascore, CAST(:dist AS JSONB), CAST(:lheat AS JSONB), CAST(:pmeta AS JSONB)
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
              location_heatmap = COALESCE(EXCLUDED.location_heatmap, dim_player.location_heatmap),
              meta = COALESCE(EXCLUDED.meta, dim_player.meta)
        """), {
            "sid": session_id, "puid": puid, "nm": full_name, "hand": handed, "age": age, "utr": utr,
            "cd": covered_distance, "fs": fastest_sprint, "fst": fastest_sprint_ts, "ascore": activity_score,
            "dist": json.dumps(swing_type_distribution) if swing_type_distribution is not None else None,
            "lheat": json.dumps(location_heatmap) if location_heatmap is not None else None,
            "pmeta": json.dumps(player_meta) if player_meta else None
        })

        pid = conn.execute(sql_text("""
            SELECT player_id FROM dim_player
            WHERE session_id = :sid AND sportai_player_uid = :puid
        """), {"sid": session_id, "puid": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # ensure players exist that appear only in player_positions
    pp_obj = payload.get("player_positions") or {}
    pp_uids = [str(k) for k, arr in pp_obj.items() if _valid_puid(k) and arr]
    for puid in [u for u in pp_uids if u not in uid_to_player_id]:
        conn.execute(sql_text("""
            INSERT INTO dim_player (session_id, sportai_player_uid)
            VALUES (:sid, :puid)
            ON CONFLICT (session_id, sportai_player_uid) DO NOTHING
        """), {"sid": session_id, "puid": puid})
        pid = conn.execute(sql_text("""
            SELECT player_id FROM dim_player
            WHERE session_id=:sid AND sportai_player_uid=:p
        """), {"sid": session_id, "p": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # ---------- rallies from payload (if provided) ----------
    payload_rallies = payload.get("rallies") or []
    had_payload_rallies = isinstance(payload_rallies, list) and len(payload_rallies) > 0

    for i, r in enumerate(payload_rallies, start=1):
        if isinstance(r, dict):
            start_s = _time_s(r.get("start_ts")) or _time_s(r.get("start"))
            end_s   = _time_s(r.get("end_ts"))   or _time_s(r.get("end"))
        else:
            try:
                start_s = _float(r[0]); end_s = _float(r[1])
            except Exception:
                start_s, end_s = None, None
        conn.execute(sql_text("""
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

    def rally_id_for_ts(ts_s):
        if ts_s is None: return None
        row = conn.execute(sql_text("""
            SELECT rally_id FROM dim_rally
            WHERE session_id = :sid AND :s BETWEEN start_s AND end_s
            ORDER BY rally_number LIMIT 1
        """), {"sid": session_id, "s": ts_s}).fetchone()
        return row[0] if row else None

    # ---------- ball_bounces ----------
    for b in (payload.get("ball_bounces") or []):
        s  = _time_s(b.get("timestamp")) or _time_s(b.get("timestamp_s")) or _time_s(b.get("ts")) or _time_s(b.get("t"))
        bx = _float(b.get("x")) if b.get("x") is not None else None
        by = _float(b.get("y")) if b.get("y") is not None else None
        if bx is None or by is None:
            cp = b.get("court_pos") or b.get("court_position")
            if isinstance(cp, (list, tuple)) and len(cp) >= 2:
                bx = _float(cp[0]); by = _float(cp[1])
        btype = b.get("type") or b.get("bounce_type")
        hitter_uid = b.get("player_id") or b.get("sportai_player_uid")
        hitter_uid = str(hitter_uid) if hitter_uid is not None else None
        hitter_pid = uid_to_player_id.get(hitter_uid) if hitter_uid else None

        conn.execute(sql_text("""
            INSERT INTO fact_bounce (session_id, hitter_player_id, rally_id,
                                     bounce_s, bounce_ts, x, y, bounce_type)
            VALUES                   (:sid,      :pid,             :rid,
                                     :s,        :ts,       :x, :y, :bt)
        """), {
            "sid": session_id,
            "pid": hitter_pid,
            "rid": rally_id_for_ts(s),
            "s": s,
            "ts": seconds_to_ts(base_dt, s),
            "x": bx, "y": by,
            "bt": btype
        })

    # ---------- ball_positions ----------
    for p in (payload.get("ball_positions") or []):
        s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
        hx = _float(p.get("x")) if p.get("x") is not None else None
        hy = _float(p.get("y")) if p.get("y") is not None else None
        conn.execute(sql_text("""
            INSERT INTO fact_ball_position (session_id, ts_s, ts, x, y)
            VALUES                         (:sid,       :ss,  :ts, :x, :y)
        """), {
            "sid": session_id, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": hx, "y": hy
        })

    # ---------- player_positions ----------
    for puid, arr in (payload.get("player_positions") or {}).items():
        pid = uid_to_player_id.get(str(puid))
        if not pid: continue
        for p in (arr or []):
            s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
            px = py = None
            if "court_X" in p or "court_x" in p:
                px = _float(p.get("court_X", p.get("court_x")))
            if "court_Y" in p or "court_y" in p:
                py = _float(p.get("court_Y", p.get("court_y")))
            if px is None: px = _float(p.get("X", p.get("x")))
            if py is None: py = _float(p.get("Y", p.get("y")))
            conn.execute(sql_text("""
                INSERT INTO fact_player_position (session_id, player_id, ts_s, ts, x, y)
                VALUES                           (:sid,       :pid,      :ss, :ts, :x, :y)
            """), {
                "sid": session_id, "pid": pid, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": px, "y": py
            })

    # === Build/repair rallies, link, normalize serve, and align *_ts ===
    if not (PREFER_PAYLOAD_RALLIES and had_payload_rallies):
        _ensure_rallies_from_swings(conn, session_id, gap_s=6.0)

    _link_swings_to_rallies(conn, session_id)
    _normalize_serve_flags(conn, session_id)
    _rebuild_ts_from_seconds(conn, session_id)

    return {"session_uid": session_uid, "session_id": session_id}

# --- Helper: map SportAI player IDs -> our dim_player.player_id for a session ---
def _player_map(conn, session_id: int) -> dict:
    rows = conn.execute(sql_text("""
        SELECT sportai_player_uid, player_id
        FROM dim_player
        WHERE session_id = :sid
    """), {"sid": session_id}).mappings().all()
    mp = {}
    for r in rows:
        suid = r.get("sportai_player_uid")
        pid  = r.get("player_id")
        if suid is not None and pid is not None:
            mp[str(suid)] = pid
    return mp

# --------------------------------------------------------------------------------------
# Basic root / ops
# --------------------------------------------------------------------------------------
@app.get("/")
def root():
    return jsonify({"service": "NextPoint Upload/Ingester v3", "status": "ok"})

@app.get("/ops/db-ping")
def db_ping():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        now = conn.execute(sql_text("SELECT now() AT TIME ZONE 'utc'")).scalar_one()
    return jsonify({"ok": True, "now_utc": str(now)})

# --------------------------------------------------------------------------------------
# Upload UI (video) + Dropbox + SportAI
# --------------------------------------------------------------------------------------
UPLOAD_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><title>Upload Match Video</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  html,body{margin:0;font-family:system-ui,Segoe UI,Arial,sans-serif;
    background:
      radial-gradient(1200px 600px at 60% -10%, #0ea5e9 0%, transparent 60%),
      radial-gradient(1000px 500px at -20% 10%, #22c55e 0%, transparent 55%),
      #0b1220 no-repeat center center fixed;
    background-size:cover;color:#fff}
  .overlay{background:rgba(0,0,0,.55);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}
  .card{width:100%;max-width:560px;background:rgba(16,185,129,.18);border:2px solid #22c55e;border-radius:16px;box-shadow:0 0 24px #22c55e}
  .pad{padding:22px}
  h2{margin:0 0 10px 0}
  input[type=email],input[type=file]{width:100%;padding:12px;margin:10px 0;border-radius:10px;border:none;font-size:1rem;background:#fff;color:#0b1220}
  button{background:#22c55e;color:#000;padding:12px 20px;border:none;border-radius:10px;font-size:1rem;cursor:pointer}
  .pill{display:inline-block;padding:6px 10px;border-radius:999px;border:1px solid #22c55e;background:rgba(0,0,0,.25);font-size:.85rem;margin-right:8px}
  .warn{color:#fca5a5;border-color:#fda4af}
  .progress{height:10px;background:#ffffff40;border-radius:8px;overflow:hidden;margin-top:10px}
  .fill{height:100%;width:0%;background:#22c55e;transition:width .25s}
  #status{margin-top:10px;white-space:pre-wrap;font-size:.95rem}
  code{background:rgba(0,0,0,.35);padding:6px 8px;border-radius:8px;border:1px solid #22c55e;display:block}
</style>
</head>
<body>
  <div class="overlay">
    <div class="card">
      <div class="pad">
        <h2>🎾 Upload Match Video</h2>

        <div>
          <span class="pill">Target: <b>{{target_folder}}</b></span>
          {% if not dropbox_ready %}<span class="pill warn">Dropbox not configured</span>{% endif %}
          {% if not sportai_ready %}<span class="pill warn">SportAI not configured</span>{% endif %}
          <span class="pill">Limit: {{max_mb}}MB</span>
        </div>

        <form id="f" enctype="multipart/form-data">
          <input type="email" name="email" placeholder="Your email" required/>
          <input type="file" name="video" accept=".mp4,.mov,.m4v" required/>
          <button type="submit">Upload & Analyze</button>
        </form>

        <div class="progress"><div id="p" class="fill"></div></div>
        <div id="status"></div>
        <div id="subs"></div>
      </div>
    </div>
  </div>

<script>
const form = document.getElementById('f');
const statusEl = document.getElementById('status');
const subsEl = document.getElementById('subs');
const fill = document.getElementById('p');

function setP(p){ fill.style.width = p + '%'; }
function log(m){ statusEl.textContent += (statusEl.textContent?'\\n':'') + m; }
function prettify(o){ try { return JSON.stringify(o, null, 2) } catch(e){ return String(o) } }

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  statusEl.textContent = ''; subsEl.textContent = ''; setP(5);
  try{
    const fd = new FormData(form);
    const f = form.querySelector('input[type=file]').files[0];
    if(!f){ log('❌ Pick a file'); setP(0); return }
    if(f.size > ({{max_mb}} * 1024 * 1024)){ log('❌ File too large'); setP(0); return }

    log('🚀 Uploading...');
    const res = await fetch('/upload', { method:'POST', body:fd });
    const data = await res.json().catch(()=>({}));
    if(!res.ok || data.ok===false){ log('❌ ' + (data.error || (res.status+' '+res.statusText))); setP(0); return }

    log('✅ Registered task: ' + (data.task_id || data.sportai_task_id));
    if(data.dropbox_url) log('📎 Source: ' + data.dropbox_url);
    setP(35);
    await poll(data.task_id || data.sportai_task_id);
  }catch(err){
    log('❌ ' + String(err));
    setP(0);
  }
});

async function poll(taskId){
  let tries=0, max=140; // ~12min @ 5s
  while(tries++ < max){
    const r = await fetch('/upload/task_status/'+taskId);
    const j = await r.json().catch(()=>({}));
    const st = j?.data?.task_status || 'unknown';
    const pct = Math.max(0, Math.min(100, Math.round((j?.data?.task_progress||0)*100)));
    setP(pct);
    subsEl.innerHTML = '<code>' + (JSON.stringify(j?.data?.subtask_progress||{}, null, 2)) + '</code>';

    if(st==='completed'){ setP(100); log('✅ Analysis complete. Ingested (if JSON provided).'); return }
    if(st==='failed'){ log('❌ Task failed'); return }
    log('🔄 ' + st + ' ('+pct+'%)');
    await new Promise(x=>setTimeout(x, 5000));
  }
  log('⚠️ Timeout while waiting for completion.');
}
</script>
</body></html>
"""

def _render_upload_html():
    """
    Render the upload UI. If templates/upload.html is missing or Jinja errors,
    serve a safe inline HTML fallback so we never 500 on /upload pages.
    """
    dropbox_ready = bool(
        os.environ.get("DROPBOX_ACCESS_TOKEN") or
        os.environ.get("DROPBOX_TOKEN")
    )
    sportai_ready = bool(
        os.environ.get("SPORT_AI_TOKEN") or
        os.environ.get("SPORTAI_TOKEN")
    )
    target_folder = os.environ.get("DROPBOX_TARGET_FOLDER", "/uploads")
    try:
        max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "200"))
    except Exception:
        max_upload_mb = 200

    # First try the real template: templates/upload.html
    try:
        return render_template(
            "upload.html",
            dropbox_ready=dropbox_ready,
            sportai_ready=sportai_ready,
            target_folder=target_folder,
            max_upload_mb=max_upload_mb,
        )
    except Exception as _e:
        # Fallback minimal inline page (works even if template/static files are missing)
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Upload Match Video</title>
  <style>
    html, body {{ margin:0; font-family:Arial,sans-serif; height:100%;
      background:#0b1d12; color:#fff; }}
    .overlay {{ min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }}
    .container {{ width:100%; max-width:520px; background:rgba(0,128,0,.15); border:2px solid #00ff80;
      border-radius:16px; padding:20px; box-shadow:0 0 20px #00ff80; }}
    h2 {{ margin:0 0 12px; }}
    .pill {{ display:inline-block; padding:4px 8px; border-radius:999px; border:1px solid #00ff80;
      background:rgba(0,0,0,.2); font-size:.85rem; margin:6px 6px 12px 0; }}
    .warn {{ color:#fca5a5; border-color:#fca5a5; }}
    input[type="email"], input[type="file"] {{ width:100%; padding:12px; margin:10px 0;
      border-radius:6px; border:none; font-size:1rem; }}
    button {{ background:#00ff80; color:#000; padding:12px 20px; border:none; border-radius:6px;
      font-size:1rem; cursor:pointer; }}
    button:hover {{ background:#00cc66; }}
    #status {{ background:rgba(255,255,255,.08); border:1px solid #00ff80; border-radius:10px;
      padding:12px; margin-top:14px; white-space:pre-wrap; }}
    .progress-bar {{ background:#ffffff40; border-radius:8px; margin-top:10px; height:12px; overflow:hidden; }}
    .progress-bar-fill {{ height:100%; width:0%; background:#00ff80; transition:width .3s; }}
  </style>
</head>
<body>
  <div class="overlay">
    <div class="container" data-max-mb="{max_upload_mb}">
      <h2>🎾 Upload Match Video</h2>
      {"<div class='pill warn'>Dropbox server credentials are not configured.</div>" if not dropbox_ready else ""}
      {"<div class='pill warn'>SPORT_AI_TOKEN is not configured.</div>" if not sportai_ready else ""}
      <div class="pill">Target folder: <strong>{target_folder}</strong></div>

      <form id="uploadForm" enctype="multipart/form-data">
        <input type="email" name="email" placeholder="Your email" required />
        <input type="file" name="video" accept=".mp4,.mov" required />
        <button type="submit">Upload to Our Dropbox & Analyze</button>
      </form>

      <div class="progress-bar"><div class="progress-bar-fill" id="progressFill"></div></div>
      <div id="status"></div>
    </div>
  </div>

  <script>
    const form = document.getElementById("uploadForm");
    const statusText = document.getElementById("status");
    const progressFill = document.getElementById("progressFill");
    const MAX_MB = parseInt(document.querySelector(".container").dataset.maxMb || "200", 10);
    const MAX_BYTES = MAX_MB * 1024 * 1024;

    function updateProgressBar(p) {{ progressFill.style.width = p + "%"; }}
    function updateStatus(m) {{
      statusText.textContent += (statusText.textContent ? "\\n" : "") + m;
    }}
    async function readJson(res) {{
      const txt = await res.text();
      try {{ return JSON.parse(txt); }} catch {{ return {{ _raw: txt }}; }}
    }}

    form.addEventListener("submit", async (e) => {{
      e.preventDefault();
      statusText.textContent = "🚀 Starting upload...";
      updateProgressBar(5);

      try {{
        const fd = new FormData(form);
        const f = form.querySelector('input[type="file"]').files[0];
        if (!f) {{ updateStatus("❌ Please select a video file."); updateProgressBar(0); return; }}
        if (f.size > MAX_BYTES) {{
          updateStatus(`❌ File is ${{Math.round(f.size/1024/1024)}}MB > ${{MAX_MB}}MB (server limit).`);
          updateProgressBar(0); return;
        }}

        const res = await fetch("/upload", {{ method: "POST", body: fd }});
        const data = await readJson(res);
        if (!res.ok || data?.error) {{
          const msg = data?.error || `HTTP ${{res.status}} ${{res.statusText}}: ${{(data?._raw||"").slice(0,400)}}`;
          updateStatus("❌ Upload Error: " + msg);
          updateProgressBar(0);
          return;
        }}

        const taskId = data?.task_id || data?.sportai_task_id;
        if (!taskId) {{ updateStatus("❌ No task id returned."); return; }}

        updateProgressBar(40);
        updateStatus("✅ Uploaded to Dropbox and registered with SportAI...");
        if (data.dropbox_url) updateStatus("📎 Source: " + data.dropbox_url);
        updateStatus("📡 Waiting for analysis to complete...");

        let attempts = 0, maxAttempts = 120, delay = 5000;
        const timer = setInterval(async () => {{
          attempts++;
          try {{
            const r = await fetch(`/upload/task_status/${{taskId}}`);
            const dj = await readJson(r);
            const status = dj?.data?.task_status || "unknown";
            const progress = typeof dj?.data?.task_progress === "number" ? dj.data.task_progress : 0;
            const pct = Math.max(0, Math.min(100, Math.round(progress*100)));
            updateProgressBar(pct);

            if (status === "completed") {{
              updateProgressBar(100);
              updateStatus("✅ Analysis complete! JSON downloaded & ingested.");
              clearInterval(timer);
            }} else if (status === "failed") {{
              updateStatus("❌ Analysis failed.");
              clearInterval(timer);
            }} else if (attempts >= maxAttempts) {{
              updateStatus("⚠️ Timeout. Try again later.");
              clearInterval(timer);
            }} else {{
              updateStatus(`🔄 Status: ${{status}} (${{pct}}%)`);
            }}
          }} catch (err) {{
            updateStatus("❌ Polling failed: " + String(err));
            clearInterval(timer);
          }}
        }}, delay);
      }} catch (err) {{
        updateStatus("❌ Final step failed: " + String(err));
      }}
    }});
  </script>
</body>
</html>"""
        return Response(html, mimetype="text/html")

# app already created like: app = Flask(__name__, template_folder="templates", static_folder="static")

try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    app.logger.info("UI blueprint mounted at /upload")
except Exception as e:
    app.logger.exception("Failed to mount UI blueprint: %s", e)

@app.get("/upload/sessions")
@app.get("/upload/index")
def upload_legacy_alias():
    return _render_upload_html()

@app.get("/upload/static/<path:filename>")
def upload_static(filename):
    base = os.path.join(app.root_path, "static", "upload")
    return send_from_directory(base, filename)

def _dbx_upload_bytes(path_in_dbx: str, blob: bytes) -> dict:
    h = {
        "Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}",
        "Content-Type": "application/octet-stream",
        "Dropbox-API-Arg": json.dumps({
            "path": path_in_dbx,
            "mode": "add", "autorename": True, "mute": False
        })
    }
    r = requests.post("https://content.dropboxapi.com/2/files/upload", headers=h, data=blob, timeout=300)
    r.raise_for_status()
    return r.json()

def _dbx_shared_link(path_in_dbx: str) -> str:
    h = {"Authorization": f"Bearer {DROPBOX_ACCESS_TOKEN}", "Content-Type": "application/json"}
    d = {"path": path_in_dbx, "settings": {"requested_visibility": "public"}}
    r = requests.post("https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings", headers=h, json=d, timeout=60)
    if r.status_code == 409:
        rr = requests.post("https://api.dropboxapi.com/2/sharing/list_shared_links", headers=h, json={"path": path_in_dbx}, timeout=60)
        rr.raise_for_status()
        links = rr.json().get("links", [])
        if not links:
            raise RuntimeError("Dropbox: no shared link")
        url = links[0]["url"]
    else:
        r.raise_for_status()
        url = r.json()["url"]
    # force direct download
    if url.endswith("?dl=0"): url = url[:-5]
    if not url.endswith("?dl=1"): url += "?dl=1"
    return url

@app.post("/upload")
def upload_post():
    try:
        f = request.files.get("video")
        email = request.form.get("email", "").strip()
        if not f or not f.filename:
            return jsonify({"ok": False, "error": "no file"}), 400
        if not DROPBOX_ACCESS_TOKEN:
            return jsonify({"ok": False, "error": "Dropbox not configured"}), 500
        if not SPORT_AI_TOKEN or not (SPORTAI_CREATE_URL or SPORTAI_API_BASE):
            return jsonify({"ok": False, "error": "SportAI not configured"}), 500

        blob = f.read()
        # path in dropbox
        today = datetime.utcnow().strftime("%Y/%m/%d")
        name  = f"{uuid.uuid4().hex}_{re.sub(r'[^A-Za-z0-9._-]+','_',f.filename)}"
        dbx_path = f"{DROPBOX_TARGET_FOLDER.rstrip('/')}/{today}/{name}"

        up = _dbx_upload_bytes(dbx_path, blob)
        src_url = _dbx_shared_link(up["path_display"])

        # create task with SportAI
        create_url = SPORTAI_CREATE_URL or f"{SPORTAI_API_BASE}/v1/tasks"
        headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "source_url": src_url,
            "metadata": {"email": email} if email else {},
        }
        if PUBLIC_BASE_URL:
            payload["webhook_url"] = f"{PUBLIC_BASE_URL}/ops/sportai-callback?key={OPS_KEY}"

        r = requests.post(create_url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        j = r.json()
        task_id = j.get("task_id") or j.get("id") or j.get("data", {}).get("task_id")

        return jsonify({"ok": True, "task_id": task_id, "dropbox_url": src_url})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/upload/task_status/<task_id>")
def upload_task_status(task_id):
    try:
        if not (SPORTAI_STATUS_URL_TPL or SPORTAI_API_BASE):
            return jsonify({"ok": False, "error": "SportAI status not configured"}), 500

        status_url = (SPORTAI_STATUS_URL_TPL or f"{SPORTAI_API_BASE}/v1/tasks/{{task_id}}").format(task_id=task_id)
        headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
        r = requests.get(status_url, headers=headers, timeout=60)
        r.raise_for_status()
        data = r.json()

        # If the status payload already includes a result JSON URL, fetch & ingest
        json_url = None
        if isinstance(data, dict):
            # common shapes: {"data": {...}} or flat
            node = data.get("data", data)
            if isinstance(node, dict):
                json_url = node.get(SPORT_AI_RESULT_FIELD)

        ingested = None
        if json_url:
            try:
                payload = requests.get(json_url, timeout=180).json()
                with engine.begin() as conn:
                    res = ingest_result_v2(conn, payload, replace=True, src_hint=json_url)
                    ingested = {"session_uid": res["session_uid"], "session_id": res["session_id"]}
            except Exception as ie:
                # non-fatal ingestion error: still return task status
                ingested = {"error": str(ie)}

        return jsonify({"ok": True, "data": data if isinstance(data, dict) else {"_raw": str(data)}, "ingested": ingested})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# --------------------------------------------------------------------------------------
# OPS: SportAI JSON webhook -> RAW + BRONZE (kept)
# --------------------------------------------------------------------------------------
@app.post("/ops/sportai-callback")
def ops_sportai_callback():
    if not _guard(): return _forbid()
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400

    replace = (request.args.get("replace","1").strip().lower() in ("1","true","yes","y"))
    payload_uid = (payload.get("session_uid") or payload.get("sessionId") or
                   payload.get("session_id") or payload.get("uid") or payload.get("id"))
    forced_uid = request.args.get("session_uid") or payload_uid

    try:
        with engine.begin() as conn:
            res = ingest_result_v2(conn, payload, replace=replace, forced_uid=forced_uid)
            sid = res.get("session_id")
            counts = conn.execute(sql_text("""
                SELECT
                  (SELECT COUNT(*) FROM dim_rally            WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_bounce          WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_ball_position   WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid),
                  (SELECT COUNT(*) FROM fact_swing           WHERE session_id=:sid)
            """), {"sid": sid}).fetchone()

        return jsonify({
            "ok": True,
            "session_uid": res.get("session_uid"),
            "session_id":  sid,
            "bronze_counts": {
                "rallies":          counts[0],
                "ball_bounces":     counts[1],
                "ball_positions":   counts[2],
                "player_positions": counts[3],
                "swings":           counts[4],
            }
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# --------------------------------------------------------------------------------------
# OPS: simple JSON ingest page (restored)
# --------------------------------------------------------------------------------------
INGEST_HTML = """<!DOCTYPE html><html><head>
<meta charset="utf-8"/><title>Ingest JSON</title>
<style>body{font-family:system-ui,Arial;margin:20px}textarea{width:100%;height:200px}</style>
</head><body>
<h3>Ingest JSON</h3>
<form method="post" enctype="multipart/form-data">
  URL: <input type="url" name="url" style="width:80%"/>
  <br><br>
  File: <input type="file" name="file" accept=".json"/>
  <br><br>
  <label><input type="checkbox" name="replace" value="1" checked/> Replace existing bronze for this session</label>
  <br><br>
  <button type="submit">Ingest</button>
</form>
</body></html>"""

@app.get("/ops/ingest-file")
def ingest_file_get():
    if not _guard(): return _forbid()
    return make_response(INGEST_HTML, 200, {"Content-Type": "text/html; charset=utf-8"})

@app.post("/ops/ingest-file")
def ingest_file_post():
    if not _guard(): return _forbid()
    try:
        replace = bool(request.form.get("replace"))
        url = (request.form.get("url") or "").strip()
        payload = None
        src_hint = None

        if url:
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            src_hint = url
            payload = r.json()
        elif "file" in request.files and request.files["file"].filename:
            payload = json.load(request.files["file"].stream)
            src_hint = request.files["file"].filename
        else:
            return jsonify({"ok": False, "error": "Provide url or file"}), 400

        with engine.begin() as conn:
            res = ingest_result_v2(conn, payload, replace=replace, src_hint=src_hint)
            sid = res["session_id"]
            ct = conn.execute(sql_text("""
                SELECT
                  (SELECT COUNT(*) FROM dim_rally            WHERE session_id=:sid) AS rallies,
                  (SELECT COUNT(*) FROM fact_bounce          WHERE session_id=:sid) AS ball_bounces,
                  (SELECT COUNT(*) FROM fact_ball_position   WHERE session_id=:sid) AS ball_positions,
                  (SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid) AS player_positions,
                  (SELECT COUNT(*) FROM fact_swing           WHERE session_id=:sid) AS swings
            """), {"sid": sid}).mappings().one()

        return jsonify({"ok": True, "session_uid": res["session_uid"], "session_id": sid, "bronze_counts": dict(ct)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# --------------------------------------------------------------------------------------
# OPS: misc endpoints (kept)
# --------------------------------------------------------------------------------------
@app.get("/ops/init-db")
def ops_init_db():
    if not _guard(): return _forbid()
    try:
        from db_init import run_init
        run_init(engine)
        return jsonify({"ok": True, "message": "DB initialized / migrated"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/init-views")
def ops_init_views():
    if not _guard(): return _forbid()
    try:
        from db_views import run_views
        run_views(engine)
        return jsonify({"ok": True, "message": "Views created/refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/routes")
def ops_routes():
    if not _guard():  # same auth as other /ops endpoints
        return _forbid()
    routes = sorted(
        {"rule": r.rule, "endpoint": r.endpoint, "methods": sorted(r.methods)}
        for r in app.url_map.iter_rules()
    )
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/refresh-gold")
def ops_refresh_gold():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as conn:
            conn.execute(sql_text("""
                DO $$ BEGIN
                  IF to_regclass('public.point_log_tbl') IS NULL THEN
                    CREATE TABLE point_log_tbl AS SELECT * FROM vw_point_log WHERE false;
                  END IF;
                END $$;
            """))
            conn.execute(sql_text("TRUNCATE point_log_tbl;"))
            conn.execute(sql_text("INSERT INTO point_log_tbl SELECT * FROM vw_point_log;"))
            conn.execute(sql_text("""
                CREATE INDEX IF NOT EXISTS ix_pl_sess_point_shot
                ON point_log_tbl(session_uid, point_number, shot_number);
            """))
            conn.execute(sql_text("""
                DO $$ BEGIN
                  IF to_regclass('public.point_summary_tbl') IS NULL THEN
                    CREATE TABLE point_summary_tbl AS SELECT * FROM vw_point_summary WHERE false;
                  END IF;
                END $$;
            """))
            conn.execute(sql_text("TRUNCATE point_summary_tbl;"))
            conn.execute(sql_text("INSERT INTO point_summary_tbl SELECT * FROM vw_point_summary;"))
            conn.execute(sql_text("""
                CREATE INDEX IF NOT EXISTS ix_ps_session
                ON point_summary_tbl(session_uid, point_number);
            """))
        return jsonify({"ok": True, "message": "gold refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/db-counts")
def ops_db_counts():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        def c(tbl): return conn.execute(sql_text(f"SELECT COUNT(*) FROM {tbl}")).scalar_one()
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
            "raw_result": c("raw_result"),
        }
        for t in ("point_log_tbl", "point_summary_tbl"):
            exists = conn.execute(sql_text("SELECT to_regclass(:t) IS NOT NULL"),
                                  {"t": f"public.{t}"}).scalar()
            if exists: counts[t] = c(t)
    return jsonify({"ok": True, "counts": counts})

@app.get("/ops/db-rollup")
def ops_db_rollup():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT
              ds.session_uid,
              ds.session_id,
              (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id = ds.session_id) AS n_players_dim,
              (SELECT COUNT(*) FROM dim_rally  dr WHERE dr.session_id = ds.session_id) AS n_rallies_dim,
              (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id = ds.session_id) AS n_swings,
              (SELECT COUNT(*) FROM fact_bounce fb WHERE fb.session_id = ds.session_id) AS n_bounces,
              (SELECT COUNT(*) FROM fact_bounce fb WHERE fb.session_id = ds.session_id AND fb.x IS NOT NULL AND fb.y IS NOT NULL) AS n_bounces_xy,
              (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id = ds.session_id) AS n_ballpos,
              (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id = ds.session_id AND bp.x IS NOT NULL AND bp.y IS NOT NULL) AS n_ballpos_xy,
              (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id = ds.session_id) AS n_pp,
              (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id = ds.session_id AND pp.x IS NOT NULL AND pp.y IS NOT NULL) AS n_pp_xy
            FROM dim_session ds
            ORDER BY ds.session_id DESC
            LIMIT 100
        """)).mappings().all()
    return jsonify({"ok": True, "rows": len(rows), "data": [dict(r) for r in rows]})

@app.route("/ops/sql", methods=["GET", "POST"])
def ops_sql():
    if not _guard(): return _forbid()

    q = None
    if request.method == "POST":
        if request.is_json:
            q = (request.get_json(silent=True) or {}).get("q")
        if not q: q = request.form.get("q")
    if not q: q = request.args.get("q", "")

    q = (q or "").strip()
    ql = q.lstrip().lower()
    if not (ql.startswith("select") or ql.startswith("with")):
        return Response("Only SELECT/CTE queries are allowed", status=400)

    stripped = q.strip()
    if ";" in stripped[:-1]:
        return Response("Only a single statement is allowed", status=400)
    if not re.search(r"\blimit\b", stripped, flags=re.IGNORECASE):
        q = f"{stripped.rstrip(';')}\nLIMIT 200"
    else:
        q = stripped.rstrip(';')

    try:
        timeout_ms = int(request.args.get("timeout_ms", "60000"))
    except Exception:
        timeout_ms = 60000

    try:
        with engine.begin() as conn:
            conn.execute(sql_text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            conn.execute(sql_text("SET LOCAL TRANSACTION READ ONLY"))
            rows = conn.execute(sql_text(q)).mappings().all()
            data = [dict(r) for r in rows]
        return jsonify({"ok": True, "rows": len(data), "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "query": q, "timeout_ms": timeout_ms}), 400

@app.get("/ops/inspect-raw")
def ops_inspect_raw():
    if not _guard(): return _forbid()
    session_uid = request.args.get("session_uid")
    if not session_uid:
        return jsonify({"ok": False, "error": "missing session_uid"}), 400

    with engine.connect() as conn:
        sid = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid=:u"),
                           {"u": session_uid}).scalar()
        if not sid: return jsonify({"ok": False, "error": "unknown session_uid"}), 404
        doc = conn.execute(sql_text("""
            SELECT payload_json
            FROM raw_result
            WHERE session_id=:sid
            ORDER BY created_at DESC
            LIMIT 1
        """), {"sid": sid}).scalar()

    if doc is None:
        return jsonify({"ok": False, "error": "no raw_result for session"}), 404
    if isinstance(doc, str):
        try: doc = json.loads(doc)
        except Exception: return jsonify({"ok": False, "error": "payload not JSON"}), 500

    bp = doc.get("ball_positions") or doc.get("ballPositions")
    bb = doc.get("ball_bounces")  or doc.get("ballBounces")
    pp = doc.get("player_positions") or doc.get("playerPositions")

    summary = {
        "keys": sorted(doc.keys() if isinstance(doc, dict) else []),
        "ball_positions_len": (len(bp) if isinstance(bp, list) else None),
        "ball_bounces_len":   (len(bb) if isinstance(bb, list) else None),
        "player_positions_players": (len(pp) if isinstance(pp, dict) else None),
    }
    return jsonify({"ok": True, "session_uid": session_uid, "summary": summary})

@app.get("/ops/backfill-xy")
def ops_backfill_xy():
    if not _guard(): return _forbid()
    session_uid = request.args.get("session_uid")

    try:
        with engine.begin() as conn:
            if session_uid:
                sid = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid=:u"),
                                   {"u": session_uid}).scalar()
                if not sid:
                    return jsonify({"ok": False, "error": "unknown session_uid"}), 404
                sid_rows = [(sid, session_uid)]
            else:
                sid_rows = conn.execute(sql_text("""
                    SELECT DISTINCT rr.session_id, ds.session_uid
                    FROM raw_result rr
                    JOIN dim_session ds ON ds.session_id = rr.session_id
                """)).fetchall()

            totals = []
            for sid, suid in sid_rows:
                doc = conn.execute(sql_text("""
                    SELECT payload_json
                    FROM raw_result
                    WHERE session_id=:sid
                    ORDER BY created_at DESC
                    LIMIT 1
                """), {"sid": sid}).scalar()
                if doc is None:
                    totals.append({"session_uid": suid, "inserted": 0, "note": "no raw_result"}); continue
                if isinstance(doc, str):
                    try: doc = json.loads(doc)
                    except Exception:
                        totals.append({"session_uid": suid, "inserted": 0, "note": "payload not JSON"}); continue

                conn.execute(sql_text("DELETE FROM fact_ball_position   WHERE session_id=:sid"), {"sid": sid})
                conn.execute(sql_text("DELETE FROM fact_bounce          WHERE session_id=:sid"), {"sid": sid})
                conn.execute(sql_text("DELETE FROM fact_player_position WHERE session_id=:sid"), {"sid": sid})

                inserted_bp = inserted_bb = inserted_pp = 0

                bp = doc.get("ball_positions") or doc.get("ballPositions")
                if isinstance(bp, list):
                    rows = []
                    for itm in bp:
                        ts_s = _float(itm.get("timestamp"))
                        x    = _float(itm.get("X"))
                        y    = _float(itm.get("Y"))
                        if ts_s is None or x is None or y is None: continue
                        rows.append({"sid": sid, "ts_s": ts_s, "x": x, "y": y})
                    if rows:
                        conn.execute(sql_text("""
                            INSERT INTO fact_ball_position(session_id, ts_s, x, y)
                            VALUES (:sid, :ts_s, :x, :y)
                        """), rows)
                        inserted_bp = len(rows)

                bb = doc.get("ball_bounces") or doc.get("ballBounces")
                rows = []
                if isinstance(bb, list):
                    id_map = _player_map(conn, sid)
                    for itm in bb:
                        bounce_s = _float(itm.get("timestamp"))
                        court = itm.get("court_pos") or itm.get("courtPos") or []
                        x = _float(court[0]) if len(court) > 0 else None
                        y = _float(court[1]) if len(court) > 1 else None
                        sportai_pid = itm.get("player_id")
                        hitter = id_map.get(str(sportai_pid)) if sportai_pid is not None else None
                        btype = itm.get("type")
                        if x is None or y is None: continue
                        rows.append({"sid": sid, "bounce_s": bounce_s, "x": x, "y": y,
                                     "hitter": hitter, "btype": btype})
                if rows:
                    conn.execute(sql_text("""
                        INSERT INTO fact_bounce(session_id, bounce_s, x, y, hitter_player_id, bounce_type)
                        VALUES (:sid, :bounce_s, :x, :y, :hitter, :btype)
                    """), rows)
                    inserted_bb = len(rows)

                pp = doc.get("player_positions") or doc.get("playerPositions") or {}
                rows = []
                if isinstance(pp, dict):
                    id_map = _player_map(conn, sid)
                    for sportai_pid, samples in pp.items():
                        pid = id_map.get(str(sportai_pid))
                        if pid is None: continue
                        for s in (samples or []):
                            ts_s = _float(s.get("timestamp"))
                            x = _float(s.get("court_X") or s.get("court_x") or s.get("courtX"))
                            y = _float(s.get("court_Y") or s.get("court_y") or s.get("courtY"))
                            if x is None or y is None: continue
                            rows.append({"sid": sid, "pid": pid, "ts_s": ts_s, "x": x, "y": y})
                if rows:
                    conn.execute(sql_text("""
                        INSERT INTO fact_player_position(session_id, player_id, ts_s, x, y)
                        VALUES (:sid, :pid, :ts_s, :x, :y)
                    """), rows)
                    inserted_pp = len(rows)

                totals.append({"session_uid": suid, "inserted": inserted_bp + inserted_bb + inserted_pp})

        return jsonify({"ok": True, "totals": totals})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/ops/reconcile")
def ops_reconcile():
    if not _guard(): return _forbid()

    session_uid = request.args.get("session_uid")
    raw_result_id = request.args.get("raw_result_id")

    with engine.begin() as conn:
        # Resolve session & payload
        if raw_result_id:
            rr = conn.execute(sql_text("""
                SELECT rr.raw_result_id, rr.session_id, rr.payload_json::text AS payload_text
                FROM raw_result rr
                WHERE rr.raw_result_id = :rid
            """), {"rid": int(raw_result_id)}).mappings().first()
            if not rr:
                return jsonify({"ok": False, "error": "raw_result_id not found"}), 404
            sid = rr["session_id"]
            suid = conn.execute(sql_text("SELECT session_uid FROM dim_session WHERE session_id=:sid"),
                                {"sid": sid}).scalar()
            session_uid = suid
            payload_text = rr["payload_text"]
        else:
            if not session_uid:
                return jsonify({"ok": False, "error": "session_uid or raw_result_id required"}), 400
            sid = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid=:u"),
                               {"u": session_uid}).scalar()
            if not sid:
                return jsonify({"ok": False, "error": "unknown session_uid"}), 404
            payload_text = conn.execute(sql_text("""
                SELECT payload_json::text
                FROM raw_result
                WHERE session_id=:sid
                ORDER BY created_at DESC
                LIMIT 1
            """), {"sid": sid}).scalar()
            if not payload_text:
                return jsonify({"ok": False, "error": "no raw_result for this session"}), 404

        try:
            payload = json.loads(payload_text)
        except Exception as e:
            return jsonify({"ok": False, "error": f"invalid payload_json: {e}"}), 500

        # RAW counts
        players = payload.get("players") or []
        n_players_raw = len(players)

        def _len_safe(x): return len(x) if isinstance(x, list) else 0
        n_swings_raw = 0
        n_swings_raw += _len_safe(payload.get("swings"))
        n_swings_raw += _len_safe(payload.get("strokes"))
        n_swings_raw += _len_safe(payload.get("hits"))
        n_swings_raw += _len_safe(payload.get("shots"))
        for p in players:
            n_swings_raw += _len_safe(p.get("swings"))
            n_swings_raw += _len_safe(p.get("strokes"))
            stats = p.get("statistics") or p.get("stats") or {}
            n_swings_raw += _len_safe(stats.get("swings"))
            n_swings_raw += _len_safe(stats.get("strokes"))

        rallies = payload.get("rallies") or []
        n_rallies_raw = len(rallies)

        ball_bounces = payload.get("ball_bounces") or []
        n_bounces_raw = len(ball_bounces)
        n_bounces_xy_raw = 0
        for b in ball_bounces:
            bx = b.get("x"); by = b.get("y")
            cp = b.get("court_pos") or b.get("court_position")
            if (bx is not None and by is not None) or (isinstance(cp, (list,tuple)) and len(cp) >= 2 and cp[0] is not None and cp[1] is not None):
                n_bounces_xy_raw += 1

        ball_positions = payload.get("ball_positions") or []
        n_ballpos_raw = len(ball_positions)
        n_ballpos_xy_raw = sum(1 for p in ball_positions if p.get("x") is not None and p.get("y") is not None)

        pp = payload.get("player_positions") or {}
        if isinstance(pp, dict):
            n_pp_raw = sum(len(v or []) for v in pp.values())
            def _has_xy(rec):
                return (rec.get("court_x") is not None and rec.get("court_y") is not None) or \
                       (rec.get("court_X") is not None and rec.get("court_Y") is not None) or \
                       (rec.get("x") is not None and rec.get("y") is not None) or \
                       (rec.get("X") is not None and rec.get("Y") is not None)
            n_pp_xy_raw = sum(sum(1 for r in (v or []) if _has_xy(r)) for v in pp.values())
            raw_player_uids = {str(k) for k in pp.keys()} | {str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "") for p in players if (p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id"))}
        elif isinstance(pp, list):
            n_pp_raw = len(pp)
            n_pp_xy_raw = sum(1 for r in pp if (r.get("court_x") is not None and r.get("court_y") is not None) or (r.get("x") is not None and r.get("y") is not None))
            raw_player_uids = {str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "") for p in players if (p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id"))}
        else:
            n_pp_raw = 0
            n_pp_xy_raw = 0
            raw_player_uids = {str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "") for p in players if (p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id"))}

        highlights = payload.get("highlights") or []
        n_highlights_raw = len(highlights)
        team_sessions = payload.get("team_sessions") or []
        n_team_sessions_raw = len(team_sessions)
        heatmap_present_raw = (payload.get("bounce_heatmap") is not None)

        # BRONZE counts
        n_players_dim = conn.execute(sql_text("SELECT COUNT(*) FROM dim_player WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        n_rallies_dim = conn.execute(sql_text("SELECT COUNT(*) FROM dim_rally WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        n_swings = conn.execute(sql_text("SELECT COUNT(*) FROM fact_swing WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        n_bounces = conn.execute(sql_text("SELECT COUNT(*) FROM fact_bounce WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        n_bounces_xy = conn.execute(sql_text("SELECT COUNT(*) FROM fact_bounce WHERE session_id=:sid AND x IS NOT NULL AND y IS NOT NULL"), {"sid": sid}).scalar() or 0
        n_ballpos = conn.execute(sql_text("SELECT COUNT(*) FROM fact_ball_position WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        n_ballpos_xy = conn.execute(sql_text("SELECT COUNT(*) FROM fact_ball_position WHERE session_id=:sid AND x IS NOT NULL AND y IS NOT NULL"), {"sid": sid}).scalar() or 0
        n_pp = conn.execute(sql_text("SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        n_pp_xy = conn.execute(sql_text("SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid AND x IS NOT NULL AND y IS NOT NULL"), {"sid": sid}).scalar() or 0
        n_highlights = conn.execute(sql_text("SELECT COUNT(*) FROM highlight WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        n_team_sessions = conn.execute(sql_text("SELECT COUNT(*) FROM team_session WHERE session_id=:sid"), {"sid": sid}).scalar() or 0
        heatmap_present = bool(conn.execute(sql_text("SELECT COUNT(*) FROM bounce_heatmap WHERE session_id=:sid"), {"sid": sid}).scalar() or 0)

        db_player_uids = {r[0] for r in conn.execute(sql_text("SELECT sportai_player_uid FROM dim_player WHERE session_id=:sid AND sportai_player_uid IS NOT NULL"), {"sid": sid}).fetchall()}
        players_diff = {
            "extra_in_db": sorted(list(db_player_uids - raw_player_uids)),
            "missing_in_db": sorted(list(raw_player_uids - db_player_uids)),
        }

        return jsonify({
            "ok": True,
            "session_uid": session_uid,
            "session_id": sid,
            "summary": {
                "db": {
                    "players": n_players_dim,
                    "rallies": n_rallies_dim,
                    "swings": n_swings,
                    "ball_bounces": n_bounces,
                    "ball_bounces_xy": n_bounces_xy,
                    "ball_positions": n_ballpos,
                    "ball_positions_xy": n_ballpos_xy,
                    "player_positions": n_pp,
                    "player_positions_xy": n_pp_xy,
                    "highlights": n_highlights,
                    "team_sessions": n_team_sessions,
                    "bounce_heatmap_present": heatmap_present
                },
                "payload": {
                    "players": n_players_raw,
                    "rallies": n_rallies_raw,
                    "swings": n_swings_raw,
                    "ball_bounces": n_bounces_raw,
                    "ball_bounces_xy": n_bounces_xy_raw,
                    "ball_positions": n_ballpos_raw,
                    "ball_positions_xy": n_ballpos_xy_raw,
                    "player_positions": n_pp_raw,
                    "player_positions_xy": n_pp_xy_raw,
                    "highlights": n_highlights_raw,
                    "team_sessions": n_team_sessions_raw,
                    "bounce_heatmap_present": bool(heatmap_present_raw)
                }
            },
            "deltas": {
                "players_vs_dim": n_players_raw - n_players_dim,
                "rallies_vs_dim": n_rallies_raw - n_rallies_dim,
                "swings": n_swings_raw - n_swings,
                "ball_bounces": n_bounces_raw - n_bounces,
                "ball_bounces_xy": n_bounces_xy_raw - n_bounces_xy,
                "ball_positions": n_ballpos_raw - n_ballpos,
                "ball_positions_xy": n_ballpos_xy_raw - n_ballpos_xy,
                "player_positions": n_pp_raw - n_pp,
                "player_positions_xy": n_pp_xy_raw - n_pp_xy,
                "highlights": n_highlights_raw - n_highlights,
                "team_sessions": n_team_sessions_raw - n_team_sessions,
                "bounce_heatmap_present": (1 if heatmap_present_raw else 0) - (1 if heatmap_present else 0),
            },
            "players": players_diff
        })

@app.get("/ops/delete-session")
def ops_delete_session():
    if not _guard(): return _forbid()
    uid = request.args.get("session_uid")
    if not uid:
        return jsonify({"ok": False, "error": "session_uid is required"}), 400
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM dim_session WHERE session_uid = :u"), {"u": uid})
    return jsonify({"ok": True, "deleted_session_uid": uid})

@app.get("/ops/list-sessions")
def ops_list_sessions():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT s.session_uid,
               (SELECT COUNT(*) FROM dim_player dp WHERE dp.session_id=s.session_id) AS players,
               (SELECT COUNT(*) FROM dim_rally dr WHERE dr.session_id=s.session_id) AS rallies,
               (SELECT COUNT(*) FROM fact_swing fs WHERE fs.session_id=s.session_id) AS swings,
               (SELECT COUNT(*) FROM fact_bounce b WHERE b.session_id=s.session_id) AS ball_bounces,
               (SELECT COUNT(*) FROM fact_ball_position bp WHERE bp.session_id=s.session_id) AS ball_positions,
               (SELECT COUNT(*) FROM fact_player_position pp WHERE pp.session_id=s.session_id) AS player_positions,
               (SELECT COUNT(*) FROM raw_result rr WHERE rr.session_id=s.session_id) AS snapshots
            FROM dim_session s
            ORDER BY s.session_uid
        """)).mappings().all()
        data = [dict(r) for r in rows]
    return jsonify({"ok": True, "rows": len(data), "data": data})

@app.post("/ops/perf-indexes")
def ops_perf_indexes():
    if not _guard(): return _forbid()
    ddl = [
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_rally ON fact_swing(session_id, rally_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_hitstart_expr ON fact_swing ((COALESCE(ball_hit_s, start_s)))",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_hitstart ON fact_swing(session_id, (COALESCE(ball_hit_s, start_s)))",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_player ON fact_swing(session_id, player_id)",
        "CREATE INDEX IF NOT EXISTS idx_dim_rally_session_bounds ON dim_rally(session_id, start_s, end_s)",
        "CREATE INDEX IF NOT EXISTS idx_fact_bounce_session_rally ON fact_bounce(session_id, rally_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_player_position_session_player ON fact_player_position(session_id, player_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_ball_position_session_ts ON fact_ball_position(session_id, ts_s)"
    ]
    created = []
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(sql_text(stmt))
            created.append(stmt)
        conn.execute(sql_text("ANALYZE"))
    return jsonify({"ok": True, "created_or_exists": created})

@app.get("/ops/repair-swings")
def ops_repair_swings():
    if not _guard(): return _forbid()
    session_uid = request.args.get("session_uid")
    if not session_uid:
        return jsonify({"ok": False, "error": "session_uid required"}), 400
    with engine.begin() as conn:
        row = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid = :u"),
                           {"u": session_uid}).first()
        if not row: return jsonify({"ok": False, "error": "unknown session_uid"}), 400
        session_id = row[0]

        doc = conn.execute(sql_text("""
            SELECT payload_json
            FROM raw_result
            WHERE session_id=:sid
            ORDER BY created_at DESC
            LIMIT 1
        """), {"sid": session_id}).scalar()

        has_payload_rallies = False
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except Exception:
                doc = None
        if isinstance(doc, dict):
            r = doc.get("rallies")
            has_payload_rallies = isinstance(r, list) and len(r) > 0

        if not (PREFER_PAYLOAD_RALLIES and has_payload_rallies):
            _ensure_rallies_from_swings(conn, session_id, gap_s=6.0)

        _link_swings_to_rallies(conn, session_id)
        _normalize_serve_flags(conn, session_id)
        _rebuild_ts_from_seconds(conn, session_id)

        summary = conn.execute(sql_text("""
            SELECT ds.session_uid,
                   COUNT(*) FILTER (WHERE fs.rally_id IS NOT NULL) AS swings_with_rally,
                   SUM(CASE WHEN COALESCE(fs.serve, FALSE) THEN 1 ELSE 0 END) AS serve_swings,
                   COUNT(DISTINCT fs.rally_id) AS rallies_linked
            FROM fact_swing fs
            JOIN dim_session ds ON ds.session_id = fs.session_id
            WHERE fs.session_id = :sid
            GROUP BY ds.session_uid
        """), {"sid": session_id}).mappings().all()

    return jsonify({"ok": True, "data": [dict(x) for x in summary]})

# --- attach UI pages (upload page, sessions list, task polling, etc.) ---
try:
    from ui_app import register_ui  # this function will add /upload, /upload/sessions, ...
    register_ui(app)
    app.logger.info("UI routes registered")
except Exception as e:
    app.logger.exception("Failed to register UI routes: %s", e)

# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")))
