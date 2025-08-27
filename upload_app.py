# upload_app.py
import os, json, hashlib, re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response
from sqlalchemy import create_engine, text, bindparam
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from db_init import engine

# ---------------------- config ----------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
OPS_KEY = os.environ.get("OPS_KEY")
STRICT_REINGEST = os.environ.get("STRICT_REINGEST", "0").strip().lower() in ("1","true","yes","y")
ENABLE_CORS = os.environ.get("ENABLE_CORS", "0").strip().lower() in ("1","true","yes","y")

if not DATABASE_URL: raise RuntimeError("DATABASE_URL required")
if not OPS_KEY: raise RuntimeError("OPS_KEY required")

app = Flask(__name__)

# --- Bronze schema helpers (paste below engine/app) ---
from functools import lru_cache
from sqlalchemy import text  # already imported above

@lru_cache(maxsize=32)
def table_columns(table_name: str):
    rows = engine.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t
    """), {"t": table_name}).fetchall()
    return {r[0] for r in rows}

def has_cols(table: str, *cols):
    cols_set = table_columns(table)
    return all(c in cols_set for c in cols)

def first_existing(table: str, *candidates):
    cols_set = table_columns(table)
    for c in candidates:
        if c in cols_set:
            return c
    return None

def resolve_player_id(session_id: int, sportai_uid: str):
    if not sportai_uid:
        return None
    row = engine.execute(text("""
        SELECT player_id
        FROM dim_player
        WHERE session_id=:sid AND sportai_player_uid=:uid
        LIMIT 1
    """), {"sid": session_id, "uid": str(sportai_uid)}).fetchone()
    return row[0] if row else None

# ---------------------- util ----------------------
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
        for k in ("timestamp", "timestamp_s", "ts", "time_s", "t", "seconds"):
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
        import os as _os
        paths = [name] if _os.path.isabs(name) else [f"/mnt/data/{name}", _os.path.join(os.getcwd(), name)]
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
            import os as _os
            fn = _os.path.splitext(_os.path.basename(src_hint))[0]
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
_SERVE_LABELS = {"serve", "first_serve", "1st_serve", "second_serve", "2nd_serve"}

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
    Returns dict with: suid, player_uid, start_s, end_s, ball_hit_s, ball_hit_x, ball_hit_y,
                       swing_type, volley, is_in_rally, serve, serve_type,
                       confidence_swing_type, confidence, confidence_volley,
                       ball_speed, ball_player_distance, meta
    """
    if not isinstance(obj, dict): 
        return None

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

    # accept dict OR [x,y] array for ball_hit_location
    loc_any = obj.get("ball_hit_location")
    if (bhx is None or bhy is None) and isinstance(loc_any, dict):
        bhx = _float(loc_any.get("x")); bhy = _float(loc_any.get("y"))
    if (bhx is None or bhy is None) and isinstance(loc_any, (list, tuple)) and len(loc_any) >= 2:
        bhx = _float(loc_any[0]); bhy = _float(loc_any[1])

    swing_type = (str(
        obj.get("swing_type")
        or obj.get("type")
        or obj.get("label")
        or obj.get("stroke_type")
        or ""
    )).lower()

    serve = _bool(obj.get("serve"))
    serve_type = obj.get("serve_type")
    if not serve and swing_type in _SERVE_LABELS:
        serve = True
        if serve_type is None and swing_type != "serve":
            serve_type = swing_type

    player_uid = (obj.get("player_id") or obj.get("sportai_player_uid") or obj.get("player_uid") or obj.get("player"))
    if player_uid is not None:
        player_uid = str(player_uid)

    # scalar extras straight from raw
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
        "id","uid","swing_uid",
        "player_id","sportai_player_uid","player_uid","player",
        "type","label","stroke_type","swing_type",
        "start","start_s","start_ts","end","end_s","end_ts",
        "timestamp","ts","time_s","t",
        "ball_hit","ball_hit_timestamp","ball_hit_ts","ball_hit_s","ball_hit_location",
        "events","serve","serve_type","ball_speed",
        "ball_player_distance","volley","is_in_rally",
        "confidence_swing_type","confidence","confidence_volley"
    }}

    return {
        "suid": suid,
        "player_uid": player_uid,
        "start_s": start_s,
        "end_s": end_s,
        "ball_hit_s": bh_s,
        "ball_hit_x": bhx,
        "ball_hit_y": bhy,
        "swing_type": swing_type,
        "volley": volley,
        "is_in_rally": is_in_rally,
        "serve": serve,
        "serve_type": serve_type,
        "confidence_swing_type": confidence_swing_type,
        "confidence": confidence,
        "confidence_volley": confidence_volley,
        "ball_speed": ball_speed,
        "ball_player_distance": ball_player_distance,
        "meta": meta if meta else None,
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

def _insert_raw_result(conn, sid: int, payload: dict) -> None:
    stmt = text("""
        INSERT INTO raw_result (session_id, payload_json, created_at)
        VALUES (:sid, :p, now() AT TIME ZONE 'utc')
    """).bindparams(bindparam("p", type_=JSONB))
    conn.execute(stmt, {"sid": sid, "p": payload})  # pass dict directly

# ---------------------- ingestion repair helpers ----------------------
def _ensure_rallies_from_swings(conn, session_id, gap_s=6.0):
    """
    If rallies are missing or don't cover early swings, build rally windows
    from the swing stream: a new rally starts when the gap >= gap_s.
    """
    conn.execute(text("""
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
    conn.execute(text("""
        UPDATE fact_swing fs
           SET rally_id = dr.rally_id
          FROM dim_rally dr
         WHERE fs.session_id = :sid
           AND dr.session_id = :sid
           AND fs.rally_id IS NULL
           AND COALESCE(fs.ball_hit_s, fs.start_s) BETWEEN dr.start_s AND dr.end_s
    """), {"sid": session_id})

def _normalize_serve_flags(conn, session_id):
    # if no serve in rally, set earliest, if multiple, keep only earliest
    conn.execute(text("""
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
    """Make *_ts video-relative: anchor zero at first swing’s time.
       Use a fresh CTE for each UPDATE because CTE scope is one statement.
    """
    # Swings
    conn.execute(text("""
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

    # Bounces
    conn.execute(text("""
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

    # Ball positions
    conn.execute(text("""
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

    # Player positions
    conn.execute(text("""
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

    if replace:
        conn.execute(text("DELETE FROM dim_session WHERE session_uid = :u"), {"u": session_uid})

    # upsert session row
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

    # ---------- raw snapshot (verbatim) ----------
    # (This guarantees 100% of the incoming payload is stored for later recon.)
    _insert_raw_result(conn, session_id, payload)

    # ---------- players ----------
    players = payload.get("players") or []
    uid_to_player_id = {}
    for p in players:
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        if not puid:
            continue
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

    # ensure players exist that appear only in player_positions
    pp_obj = payload.get("player_positions") or {}
    pp_uids = [str(k) for k, arr in pp_obj.items() if _valid_puid(k) and arr]
    for puid in [u for u in pp_uids if u not in uid_to_player_id]:
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

    # ---------- rallies (from payload if provided) ----------
    for i, r in enumerate(payload.get("rallies") or [], start=1):
        if isinstance(r, dict):
            start_s = _time_s(r.get("start_ts")) or _time_s(r.get("start"))
            end_s   = _time_s(r.get("end_ts"))   or _time_s(r.get("end"))
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

    # helper to map a timestamp to rally
    def rally_id_for_ts(ts_s):
        if ts_s is None:
            return None
        row = conn.execute(text("""
            SELECT rally_id FROM dim_rally
            WHERE session_id = :sid AND :s BETWEEN start_s AND end_s
            ORDER BY rally_number LIMIT 1
        """), {"sid": session_id, "s": ts_s}).fetchone()
        return row[0] if row else None

    # ---------- ball_bounces ----------
    # Robust XY mapping: prefer x/y; else use court_pos[0..1].
    for b in (payload.get("ball_bounces") or []):
        s  = _time_s(b.get("timestamp")) or _time_s(b.get("timestamp_s")) \
             or _time_s(b.get("ts")) or _time_s(b.get("t"))

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

        conn.execute(text("""
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
        s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) \
             or _time_s(p.get("ts")) or _time_s(p.get("t"))
        hx = _float(p.get("x")) if p.get("x") is not None else None
        hy = _float(p.get("y")) if p.get("y") is not None else None

        conn.execute(text("""
            INSERT INTO fact_ball_position (session_id, ts_s, ts, x, y)
            VALUES                         (:sid,       :ss,  :ts, :x, :y)
        """), {
            "sid": session_id,
            "ss": s,
            "ts": seconds_to_ts(base_dt, s),
            "x": hx, "y": hy
        })

    # ---------- player_positions ----------
    # Your RAW shape is an OBJECT whose values are arrays of samples.
    # Prefer court_X/Y (or court_x/y); else fall back to image X/Y or plain x/y.
    for puid, arr in (payload.get("player_positions") or {}).items():
        pid = uid_to_player_id.get(str(puid))
        if not pid:
            continue
        for p in (arr or []):
            s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) \
                 or _time_s(p.get("ts")) or _time_s(p.get("t"))

            px = None
            py = None
            # court coordinates (prefer)
            if "court_X" in p or "court_x" in p:
                px = _float(p.get("court_X", p.get("court_x")))
            if "court_Y" in p or "court_y" in p:
                py = _float(p.get("court_Y", p.get("court_y")))
            # fallback to image or plain
            if px is None:
                px = _float(p.get("X", p.get("x")))
            if py is None:
                py = _float(p.get("Y", p.get("y")))

            conn.execute(text("""
                INSERT INTO fact_player_position (session_id, player_id, ts_s, ts, x, y)
                VALUES                           (:sid,       :pid,      :ss, :ts, :x, :y)
            """), {
                "sid": session_id,
                "pid": pid,
                "ss": s,
                "ts": seconds_to_ts(base_dt, s),
                "x": px, "y": py
            })

    # ---------- optionals ----------
    for t in payload.get("team_sessions") or []:
        conn.execute(text("INSERT INTO team_session (session_id, data) VALUES (:sid, CAST(:d AS JSONB))"),
                     {"sid": session_id, "d": json.dumps(t)})
    for h in payload.get("highlights") or []:
        conn.execute(text("INSERT INTO highlight (session_id, data) VALUES (:sid, CAST(:d AS JSONB))"),
                     {"sid": session_id, "d": json.dumps(h)})

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

    # ---------- swings (existing normalization kept) ----------
    # NOTE: I’m preserving your swing normalization/extraction logic exactly as-is.
    # (Leave your existing _gather_all_swings/_normalize_swing_obj/_insert_swing blocks intact.)
    # If you want me to inline that here too, say the word.
    return {"session_uid": session_uid, "session_id": session_id}

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
            "suid": str(norm.get("suid")) if norm.get("suid") else None,
            "start_s": norm.get("start_s"),
            "end_s": norm.get("end_s"),
            "ball_hit_s": norm.get("ball_hit_s"),
            "ball_hit_x": norm.get("ball_hit_x"),
            "ball_hit_y": norm.get("ball_hit_y"),
            "ball_speed": norm.get("ball_speed") or (norm.get("meta") or {}).get("ball_speed"),
            "ball_player_distance": norm.get("ball_player_distance"),
            "swing_type": norm.get("swing_type") or norm.get("label"),
            "volley": norm.get("volley"),
            "is_in_rally": norm.get("is_in_rally"),
            "serve": norm.get("serve"),
            "serve_type": norm.get("serve_type"),
            "confidence_swing_type": norm.get("confidence_swing_type"),
            "confidence": norm.get("confidence"),
            "confidence_volley": norm.get("confidence_volley"),
            "meta": norm.get("meta"),
        }

        try:
            _insert_swing(conn, session_id, pid, s, base_dt, fps)
        except IntegrityError:
            pass

    # === Build/repair rallies, link, normalize serve, and align *_ts ===
    _ensure_rallies_from_swings(conn, session_id, gap_s=6.0)
    _link_swings_to_rallies(conn, session_id)
    _normalize_serve_flags(conn, session_id)
    _rebuild_ts_from_seconds(conn, session_id)

    return {"session_uid": session_uid}


# --- Helper: map SportAI player IDs -> our dim_player.player_id for a session ---
def _player_map(conn, session_id: int) -> dict:
    """
    Returns a dict {sportai_player_uid (as str) -> dim_player.player_id (int)}
    for the given session_id. Use this to translate IDs from SportAI payloads 
    before inserting into fact tables that FK to dim_player.
    """
    rows = conn.execute(text("""
        SELECT sportai_player_uid, player_id
        FROM dim_player
        WHERE session_id = :sid
    """), {"sid": session_id}).mappings().all()

    mp = {}
    for r in rows:
        suid = r.get("sportai_player_uid")
        pid  = r.get("player_id")
        if suid is not None and pid is not None:
            mp[str(suid)] = pid  # normalize key to string (SportAI dict keys often come as strings)
    return mp

# FK mapper: SportAI player id -> internal dim_player.player_id for a session
def _player_map(conn, session_id: int) -> dict:
    rows = conn.execute(text("""
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

# ---------- OPS: SportAI JSON webhook -> RAW + BRONZE ----------
@app.post("/ops/sportai-callback")
def ops_sportai_callback():
    if not _guard():
        return _forbid()

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400

    replace = (request.args.get("replace","1").strip().lower() in ("1","true","yes","y"))

    # Always prefer the UID in the payload; allow explicit override via querystring.
    payload_uid = (
        (payload.get("session_uid")
         or payload.get("sessionId")
         or payload.get("session_id")
         or payload.get("uid")
         or payload.get("id"))
    )
    forced_uid = request.args.get("session_uid") or payload_uid

    try:
        with engine.begin() as conn:
            res = ingest_result_v2(conn, payload, replace=replace, forced_uid=forced_uid)

            sid = res.get("session_id")
            counts = conn.execute(text("""
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

# ---------- OPS: (re)create views ----------
@app.get("/ops/init-views")
def ops_init_views():
    if not _guard():
        return _forbid()
    try:
        init_views(engine)  # idempotent: drops & recreates in dependency order
        return jsonify({"ok": True, "message": "Views created/refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------- OPS: materialize gold tables for Power BI ----------
@app.get("/ops/build-gold")
def ops_build_gold():
    if not _guard():
        return _forbid()

from flask import jsonify
from sqlalchemy import text

@app.get("/ops/refresh-gold")
def ops_refresh_gold():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as conn:
            # point_log_tbl
            conn.execute(text("""
                DO $$
                BEGIN
                  IF to_regclass('public.point_log_tbl') IS NULL THEN
                    CREATE TABLE point_log_tbl AS SELECT * FROM vw_point_log WHERE false;
                  END IF;
                END $$;
            """))
            conn.execute(text("TRUNCATE point_log_tbl;"))
            conn.execute(text("INSERT INTO point_log_tbl SELECT * FROM vw_point_log;"))
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_pl_sess_point_shot
                ON point_log_tbl(session_uid, point_number, shot_number);
            """))

            # point_summary_tbl
            conn.execute(text("""
                DO $$
                BEGIN
                  IF to_regclass('public.point_summary_tbl') IS NULL THEN
                    CREATE TABLE point_summary_tbl AS SELECT * FROM vw_point_summary WHERE false;
                  END IF;
                END $$;
            """))
            conn.execute(text("TRUNCATE point_summary_tbl;"))
            conn.execute(text("INSERT INTO point_summary_tbl SELECT * FROM vw_point_summary;"))
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_ps_session
                ON point_summary_tbl(session_uid, point_number);
            """))

        return jsonify({"ok": True, "message": "gold refreshed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


    ddl = [
        "DROP TABLE IF EXISTS point_log_tbl;",
        "CREATE TABLE point_log_tbl AS SELECT * FROM vw_point_log;",
        "DROP TABLE IF EXISTS point_summary_tbl;",
        "CREATE TABLE point_summary_tbl AS SELECT * FROM vw_point_summary;",
        # helpful indexes
        "CREATE INDEX IF NOT EXISTS ix_pl_session ON point_log_tbl(session_uid, point_number, shot_number);",
        "CREATE INDEX IF NOT EXISTS ix_ps_session ON point_summary_tbl(session_uid, point_number);",
    ]
    try:
        with engine.begin() as conn:  # not read-only
            for stmt in ddl:
                conn.execute(text(stmt))
        return jsonify({"ok": True, "built": ["point_log_tbl", "point_summary_tbl"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------- OPS: lightweight counts (safe if tables missing) ----------
@app.get("/ops/db-counts")
def ops_db_counts():
    if not _guard():
        return _forbid()

    def _exists(conn, tbl):
        # returns True if regclass resolves (table or view exists)
        return bool(conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"),
                                 {"t": f"public.{tbl}"}).scalar())

    with engine.connect() as conn:
        def c(tbl):
            return conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar_one()

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

        # include gold tables only if they exist
        for t in ("point_log_tbl", "point_summary_tbl"):
            if _exists(conn, t):
                counts[t] = c(t)

    return jsonify({"ok": True, "counts": counts})

# --- XY Backfill helpers ------------------------------------------------------

def _get_session_id(conn, session_uid: str):
    row = conn.execute(text("""
        SELECT session_id FROM dim_session WHERE session_uid=:u
    """), {"u": session_uid}).first()
    return row[0] if row else None

def _latest_payload(conn, session_id: int):
    row = conn.execute(text("""
        SELECT payload_json
        FROM raw_result
        WHERE session_id=:sid
        ORDER BY created_at DESC
        LIMIT 1
    """), {"sid": session_id}).first()
    return (row[0] if row else None)

def _coerce_f(f, default=None):
    try:
        if f is None:
            return default
        return float(f)
    except Exception:
        return default

# Inspect top-level keys & sizes in latest raw for a session
@app.get("/ops/inspect-raw")
def ops_inspect_raw():
    if not _guard(): return _forbid()
    session_uid = request.args.get("session_uid")
    if not session_uid:
        return jsonify({"ok": False, "error": "missing session_uid"}), 400

    with engine.connect() as conn:
        sid = _get_session_id(conn, session_uid)
        if not sid:
            return jsonify({"ok": False, "error": "unknown session_uid"}), 404
        doc = _latest_payload(conn, sid)

    if doc is None:
        return jsonify({"ok": False, "error": "no raw_result for session"}), 404

    # doc should already be a dict (JSONB). if not, try json.loads
    if isinstance(doc, str):
        try:
            import json as _json
            doc = _json.loads(doc)
        except Exception:
            return jsonify({"ok": False, "error": "payload not JSON"}), 500

    bp = doc.get("ball_positions") or doc.get("ballPositions")
    bb = doc.get("ball_bounces")  or doc.get("ballBounces")
    pp = doc.get("player_positions") or doc.get("playerPositions")

    summary = {
        "keys": sorted(doc.keys()),
        "ball_positions_len": (len(bp) if isinstance(bp, list) else None),
        "ball_bounces_len":   (len(bb) if isinstance(bb, list) else None),
        "player_positions_players": (len(pp) if isinstance(pp, dict) else None),
    }
    return jsonify({"ok": True, "session_uid": session_uid, "summary": summary})

# Backfill XY into Bronze from latest raw_result (by session or all)
@app.get("/ops/backfill-xy")
def ops_backfill_xy():
    if not _guard(): return _forbid()
    session_uid = request.args.get("session_uid")  # optional; if absent → all sessions

    try:
        with engine.begin() as conn:
            if session_uid:
                sid = _get_session_id(conn, session_uid)
                if not sid:
                    return jsonify({"ok": False, "error": "unknown session_uid"}), 404
                sid_rows = [(sid, session_uid)]
            else:
                sid_rows = conn.execute(text("""
                    SELECT DISTINCT rr.session_id, ds.session_uid
                    FROM raw_result rr
                    JOIN dim_session ds ON ds.session_id = rr.session_id
                """)).fetchall()

            totals = []
            for sid, suid in sid_rows:
                doc = _latest_payload(conn, sid)
                if doc is None:
                    totals.append({"session_uid": suid, "inserted": 0, "note": "no raw_result"})
                    continue

                if isinstance(doc, str):
                    try:
                        import json as _json
                        doc = _json.loads(doc)
                    except Exception:
                        totals.append({"session_uid": suid, "inserted": 0, "note": "payload not JSON"})
                        continue

                # Idempotent: clear existing rows for this session
                conn.execute(text("DELETE FROM fact_ball_position   WHERE session_id=:sid"), {"sid": sid})
                conn.execute(text("DELETE FROM fact_bounce          WHERE session_id=:sid"), {"sid": sid})
                conn.execute(text("DELETE FROM fact_player_position WHERE session_id=:sid"), {"sid": sid})

                inserted_bp = inserted_bb = inserted_pp = 0

                # --- Ball positions (image coords 0..1) ---
                bp = doc.get("ball_positions") or doc.get("ballPositions")
                if isinstance(bp, list):
                    rows = []
                    for itm in bp:
                        ts_s = _coerce_f(itm.get("timestamp"))
                        x    = _coerce_f(itm.get("X"))
                        y    = _coerce_f(itm.get("Y"))
                        if ts_s is None or x is None or y is None:
                            continue
                        rows.append({"sid": sid, "ts_s": ts_s, "x": x, "y": y})
                    if rows:
                        conn.execute(text("""
                            INSERT INTO fact_ball_position(session_id, ts_s, x, y)
                            VALUES (:sid, :ts_s, :x, :y)
                        """), rows)
                        inserted_bp = len(rows)

                # ==== BEGIN PATCH: Ball bounces (uses _player_map) ====
                bb = doc.get("ball_bounces") or doc.get("ballBounces")
                rows = []
                if isinstance(bb, list):
                    id_map = _player_map(conn, sid)  # FK mapping: SportAI -> dim_player.player_id
                    for itm in bb:
                        bounce_s = _coerce_f(itm.get("timestamp"))
                        court = itm.get("court_pos") or itm.get("courtPos") or []
                        x = _coerce_f(court[0]) if len(court) > 0 else None
                        y = _coerce_f(court[1]) if len(court) > 1 else None
                        sportai_pid = itm.get("player_id")
                        hitter = id_map.get(str(sportai_pid)) if sportai_pid is not None else None
                        btype = itm.get("type")
                        if x is None or y is None:
                            continue
                        rows.append({
                            "sid": sid, "bounce_s": bounce_s, "x": x, "y": y,
                            "hitter": hitter, "btype": btype
                        })

                if rows:
                    conn.execute(text("""
                        INSERT INTO fact_bounce(session_id, bounce_s, x, y, hitter_player_id, bounce_type)
                        VALUES (:sid, :bounce_s, :x, :y, :hitter, :btype)
                    """), rows)
                # ==== END PATCH ====

                # ==== BEGIN PATCH: Player positions (uses _player_map) ====
                pp = doc.get("player_positions") or doc.get("playerPositions") or {}
                rows = []
                if isinstance(pp, dict):
                    id_map = _player_map(conn, sid)  # FK mapping
                    for sportai_pid, samples in pp.items():
                        pid = id_map.get(str(sportai_pid))
                        if pid is None:
                            continue  # skip players we didn’t map
                        for s in (samples or []):
                            ts_s = _coerce_f(s.get("timestamp"))
                            # Prefer court_* (meters). If missing, you can later add an image->court fallback.
                            x = _coerce_f(s.get("court_X") or s.get("court_x") or s.get("courtX"))
                            y = _coerce_f(s.get("court_Y") or s.get("court_y") or s.get("courtY"))
                            if x is None or y is None:
                                continue
                            rows.append({"sid": sid, "pid": pid, "ts_s": ts_s, "x": x, "y": y})

                if rows:
                    conn.execute(text("""
                        INSERT INTO fact_player_position(session_id, player_id, ts_s, x, y)
                        VALUES (:sid, :pid, :ts_s, :x, :y)
                    """), rows)            

        return jsonify({"ok": True, "totals": totals})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



# 🔧 Always validate query & add LIMIT, regardless of GET/POST
@app.route("/ops/sql", methods=["GET", "POST"])
def ops_sql():
    if not _guard():
        return _forbid()

    q = None
    if request.method == "POST":
        if request.is_json:
            q = (request.get_json(silent=True) or {}).get("q")
        if not q:
            q = request.form.get("q")
    if not q:
        q = request.args.get("q", "")

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
            conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            conn.execute(text("SET LOCAL TRANSACTION READ ONLY"))
            rows = conn.execute(text(q)).mappings().all()
            data = [dict(r) for r in rows]
        return jsonify({"ok": True, "rows": len(data), "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "query": q, "timeout_ms": timeout_ms}), 400

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

def _render_upload_page(message: str = ""):
    key = request.args.get("key","")
    action = f"/ops/ingest-file?key={key}"
    return Response(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>NextPoint – Upload Session JSON</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; }}
    .card {{ max-width: 720px; padding: 20px; border: 1px solid #e5e7eb; border-radius: 12px; }}
    label {{ display:block; margin: 12px 0 6px; font-weight: 600; }}
    input[type=text] {{ width:100%; padding:8px; border:1px solid #d1d5db; border-radius:8px; }}
    .row {{ display:flex; gap:12px; align-items:center; }}
    .muted {{ color:#6b7280; font-size: 12px; }}
    button {{ padding:10px 16px; border-radius:10px; border:1px solid #111827; background:#111827; color:#fff; cursor:pointer; }}
    button:hover {{ opacity:.9 }}
    .msg {{ margin-bottom: 12px; color:#b45309; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Upload SportAI Session JSON</h2>
    {"<div class='msg'>" + message + "</div>" if message else ""}
    <form method="post" action="{action}" enctype="multipart/form-data">
      <label>JSON file</label>
      <input type="file" name="file" accept="application/json" />

      <div class="muted">— or —</div>

      <label>Direct JSON URL (public GET)</label>
      <input type="text" name="url" placeholder="https://example.com/session.json" />

      <div class="muted">— or —</div>

      <label>Server file name (on /mnt/data)</label>
      <input type="text" name="name" placeholder="s1.json" />

      <div class="row" style="margin-top:12px;">
        <label style="margin:0;"><input type="checkbox" name="replace" value="1" checked /> Replace existing</label>
        <label style="margin:0;">Mode:
          <select name="mode">
            <option value="soft" selected>soft</option>
            <option value="hard">hard</option>
          </select>
        </label>
        <label style="margin:0;">Session UID:
          <input type="text" name="session_uid" placeholder="(optional)" style="width:220px;" />
        </label>
      </div>

      <div style="margin-top:18px;">
        <button type="submit">Upload</button>
        <span class="muted">Your ops key is carried in the URL.</span>
      </div>
    </form>
  </div>
</body>
</html>
""", mimetype="text/html")

@app.route("/ops/ingest-file", methods=["GET","POST"])
def ops_ingest_file():
    if not _guard(): 
        return _forbid()

    # When you just open the page (GET, no inputs) → render the HTML form
    has_input = ("file" in request.files) or request.args.get("url") or request.args.get("name") \
                or (request.method == "POST" and (request.form.get("url") or request.form.get("name") or request.data))

    if request.method == "GET" and not has_input:
        return _render_upload_page()

    # Parse flags from either args (GET) or form (POST)
    def _get_arg(name, default=None):
        if request.method == "POST":
            v = request.form.get(name)
            if v is not None:
                return v
        return request.args.get(name, default)

    try:
        replace = str(_get_arg("replace","0")).strip().lower() in ("1","true","yes","y","on")
        forced_uid = _get_arg("session_uid")
        src_hint = _get_arg("name") or _get_arg("url")
        mode = (_get_arg("mode") or "hard").strip().lower()  # "hard" | "soft"

        # Try to obtain the payload:
        #  - file (multipart) OR url (server fetch) OR name (server disk) OR raw body
        try:
            payload = _get_json_from_sources()
        except Exception as e:
            # If this came from the HTML form, show the page with message instead of JSON
            if request.method == "POST" and "file" in request.files:
                return _render_upload_page(f"Upload failed: {str(e)}")
            raise

        # Guess/ensure session uid, then maybe clear existing (soft mode keeps session row)
        try:
            session_uid_guess = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
        except Exception:
            session_uid_guess = forced_uid

        # Read existing session_id (if any)
        with engine.connect() as c:
            existing_sid = c.execute(
                text("SELECT session_id FROM dim_session WHERE session_uid=:u"),
                {"u": session_uid_guess}
            ).scalar()

        if replace and mode == "soft" and existing_sid is not None:
            with engine.begin() as conn:
                # delete children in FK-safe order; keep raw_result
                conn.execute(text("DELETE FROM fact_ball_position WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM fact_player_position WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM fact_bounce WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM fact_swing WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM dim_rally WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM dim_player WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM bounce_heatmap WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM highlight WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM team_session WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM session_confidences WHERE session_id=:sid"), {"sid": existing_sid})
                conn.execute(text("DELETE FROM thumbnail WHERE session_id=:sid"), {"sid": existing_sid})

        # Ensure schema exists
        init_db(engine)

        # Ingest
        with engine.begin() as conn:
            res = ingest_result_v2(
                conn,
                payload,
                replace=(replace and not (mode == "soft" and existing_sid is not None)),
                forced_uid=forced_uid,
                src_hint=src_hint
            )

        # If the request came from the HTML form, show a friendly success page
        if request.method == "POST" and ("file" in request.files or request.form.get("url") or request.form.get("name")):
            msg = f"Upload OK. Session UID: {res.get('session_uid')}. Now go run Init Views."
            return _render_upload_page(msg)

        # Otherwise return JSON (API usage)
        return jsonify({"ok": True, **res, "replace": replace, "mode": mode})

    except Exception as e:
        # HTML form → return a page with the error; API → JSON error
        if request.method == "POST" and ("file" in request.files or request.form.get("url") or request.form.get("name")):
            return _render_upload_page(f"Error: {str(e)}")
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
               (SELECT COUNT(*) FROM raw_result rr WHERE rr.session_id=s.session_id) AS snapshots
            FROM dim_session s
            ORDER BY s.session_uid
        """)).mappings().all()
        data = [dict(r) for r in rows]
    return jsonify({"ok": True, "rows": len(data), "data": data})

# ---------- performance indexes ----------
@app.post("/ops/perf-indexes")
def ops_perf_indexes():
    if not _guard():
        return _forbid()
    ddl = [
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_rally ON fact_swing(session_id, rally_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_hitstart_expr ON fact_swing ((COALESCE(ball_hit_s, start_s)))",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_hitstart ON fact_swing(session_id, (COALESCE(ball_hit_s, start_s)))",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_player ON fact_swing(session_id, player_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_swing_session_serve_true ON fact_swing(session_id) WHERE serve IS TRUE",
        "CREATE INDEX IF NOT EXISTS idx_dim_rally_session_bounds ON dim_rally(session_id, start_s, end_s)",
        "CREATE INDEX IF NOT EXISTS idx_fact_bounce_session_rally ON fact_bounce(session_id, rally_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_player_position_session_player ON fact_player_position(session_id, player_id)",
        "CREATE INDEX IF NOT EXISTS idx_fact_ball_position_session_ts ON fact_ball_position(session_id, ts_s)"
    ]
    created = []
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
            created.append(stmt)
        conn.execute(text("ANALYZE"))
    return jsonify({"ok": True, "created_or_exists": created})

# ---------- repair endpoint ----------
@app.get("/ops/repair-swings")
def ops_repair_swings():
    if not _guard(): return _forbid()
    session_uid = request.args.get("session_uid")  # optional
    with engine.begin() as conn:
        session_id = None
        if session_uid:
            row = conn.execute(
                text("SELECT session_id FROM dim_session WHERE session_uid = :u"),
                {"u": session_uid}
            ).first()
            if not row:
                return jsonify({"ok": False, "error": "unknown session_uid"}), 400
            session_id = row[0]

        if session_id is None:
            return jsonify({"ok": False, "error": "session_uid required for targeted repair"}), 400

        _ensure_rallies_from_swings(conn, session_id, gap_s=6.0)
        _link_swings_to_rallies(conn, session_id)
        _normalize_serve_flags(conn, session_id)
        _rebuild_ts_from_seconds(conn, session_id)

        summary = conn.execute(text("""
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

# ---------------------- main ----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","8000")))
