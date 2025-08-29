# upload_app.py — NextPoint Upload/Ingester (restored & de-duped)
# ---------------------------------------------------------------
import os, json, time, re, hashlib
from datetime import datetime, timezone, timedelta
from typing import Dict

import requests
from flask import Flask, request, jsonify, Response, render_template, send_from_directory
from werkzeug.utils import secure_filename
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

# app already exists:
app = Flask(__name__, template_folder="templates", static_folder="static")
app.url_map.strict_slashes = False  # accept both /path and /path/

# ---- SportAI configuration ----
SPORTAI_BASE        = os.getenv("SPORT_AI_BASE", "https://api.sportai.app").rstrip("/")
SPORTAI_SUBMIT_PATH = os.getenv("SPORT_AI_SUBMIT_PATH", "/api/statistics").strip()
SPORTAI_STATUS_PATH = os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/{task_id}").strip()
SPORTAI_TOKEN       = os.getenv("SPORT_AI_TOKEN", "")

def _to_direct_dropbox(url: str) -> str:
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        p = urlparse(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        q["dl"] = "1"
        host = "dl.dropboxusercontent.com" if "dropbox.com" in p.netloc else p.netloc
        return urlunparse((p.scheme, host, p.path, p.params, urlencode(q), p.fragment))
    except Exception:
        return url

def _dbx_create_or_fetch_shared_link(token: str, path: str) -> str:
    # try to create, else list
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers=h, json={"path": path, "settings": {"audience": "public", "access": "viewer"}}, timeout=30
    )
    if r.status_code == 409:
        r = requests.post(
            "https://api.dropboxapi.com/2/sharing/list_shared_links",
            headers=h, json={"path": path, "direct_only": True}, timeout=30
        )
        r.raise_for_status()
        links = (r.json() or {}).get("links", [])
        if not links:
            raise RuntimeError("No shared link available")
        return links[0]["url"]
    r.raise_for_status()
    return (r.json() or {})["url"]

def _sportai_submit(video_url: str, email: str | None = None) -> str:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    url = f"{SPORTAI_BASE}{SPORTAI_SUBMIT_PATH}"
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"}
    payload = {"video_url": video_url, "sport": "tennis"}
    if email:
        payload["email"] = email
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json() or {}
    tid = data.get("task_id") or data.get("id") or (data.get("data") or {}).get("task_id")
    if not tid:
        raise RuntimeError(f"No task_id in SportAI response: {data}")
    return str(tid)

def _sportai_status(task_id: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    path = SPORTAI_STATUS_PATH.format(task_id=task_id)
    url = f"{SPORTAI_BASE}{path}"
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    j = r.json() or {}
    return {
        "status": j.get("status") or (j.get("data") or {}).get("status"),
        "result_url": j.get("result_url") or (j.get("data") or {}).get("result_url"),
        "raw": j,
    }

def _trigger_ingest(result_url: str):
    if not OPS_KEY:
        raise RuntimeError("OPS_KEY missing for ingest")
    base = (request.host_url or "").rstrip("/")
    from urllib.parse import urlencode
    q = urlencode({"key": OPS_KEY, "url": result_url, "replace": 1})
    ingest_url = f"{base}/ops/ingest-file?{q}"
    r = requests.get(ingest_url, timeout=120)
    r.raise_for_status()
    return r.json()


# =========================
#   Dropbox configuration
# =========================
DBX_APP_KEY     = os.getenv("DROPBOX_APP_KEY", "")
DBX_APP_SECRET  = os.getenv("DROPBOX_APP_SECRET", "")
DBX_REFRESH     = os.getenv("DROPBOX_REFRESH_TOKEN", "")
DBX_FOLDER      = os.getenv("DROPBOX_UPLOAD_FOLDER", "/incoming")  # shown on the page

def _dbx_access_token():
    """Exchange refresh token -> short-lived access token."""
    if not (DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH):
        return None, "Missing Dropbox env vars (DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN)."
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DBX_REFRESH,
            "client_id": DBX_APP_KEY,
            "client_secret": DBX_APP_SECRET,
        },
        timeout=30,
    )
    if r.ok:
        return r.json().get("access_token"), None
    return None, f"{r.status_code}: {r.text}"

@app.post("/upload/api/upload")
def api_upload_to_dropbox():
    """
    Accepts multipart/form-data:
      - file  : the video file (required)
      - email : optional (sent to SportAI payload if provided)
    Uploads to Dropbox, creates a shared link, converts to direct URL,
    submits to SportAI, and returns the task_id.
    """
    f = request.files.get("file")
    email = (request.form.get("email") or "").strip().lower()

    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file provided."}), 400

    token, err = _dbx_access_token()
    if not token:
        return jsonify({"ok": False, "error": f"Dropbox auth failed: {err}"}), 500

    # Upload to Dropbox
    clean = secure_filename(f.filename)
    ts = int(time.time())
    dest_path = f"{DBX_FOLDER.rstrip('/')}/{ts}_{clean}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Dropbox-API-Arg": json.dumps({"path": dest_path, "mode": "add", "autorename": True, "mute": False}),
        "Content-Type": "application/octet-stream",
    }
    up = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers=headers,
        data=f.read(),
        timeout=600,
    )
    if not up.ok:
        return jsonify({"ok": False, "error": f"Dropbox upload failed: {up.status_code} {up.text}"}), 502
    meta = up.json()

    # Create share link -> direct link
    try:
        share_url = _dbx_create_or_fetch_shared_link(token, meta.get("path_lower") or dest_path)
        video_url = _to_direct_dropbox(share_url)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Dropbox shared link error: {e}"}), 502

    # Submit to SportAI
    try:
        task_id = _sportai_submit(video_url, email=email)
    except Exception as e:
        # still return successful upload details; frontend can show “uploaded but not submitted”
        return jsonify({
            "ok": False,
            "stage": "sportai_submit",
            "upload": {"path": meta.get("path_display") or dest_path, "size": meta.get("size"), "name": meta.get("name", clean)},
            "share_url": share_url,
            "video_url": video_url,
            "error": str(e),
        }), 502

    return jsonify({
        "ok": True,
        "task_id": task_id,
        "share_url": share_url,
        "video_url": video_url,
        "upload": {"path": meta.get("path_display") or dest_path, "size": meta.get("size"), "name": meta.get("name", clean)},
    })


# =========================
#       App config
# =========================
DATABASE_URL     = os.environ.get("DATABASE_URL")
OPS_KEY          = os.environ.get("OPS_KEY", "")
STRICT_REINGEST  = os.environ.get("STRICT_REINGEST", "0").lower() in ("1","true","yes","y")
ENABLE_CORS      = os.environ.get("ENABLE_CORS", "0").lower() in ("1","true","yes","y")

# Keep bronze/raw equality strict by default
STRICT_BRONZE_RAW         = os.environ.get("STRICT_BRONZE_RAW", "1").lower() in ("1","true","yes","y")
PREFER_PAYLOAD_RALLIES    = os.environ.get("PREFER_PAYLOAD_RALLIES", "1").lower() in ("1","true","yes","y")
RALLY_GAP_S               = float(os.environ.get("RALLY_GAP_S", "6.0"))

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL required")

# One engine for everything
from db_init import engine  # noqa: E402

# =========================
#       Helpers
# =========================
def _guard() -> bool:
    """Allow ?key=… or header X-OPS-Key / Bearer."""
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

def _forbid():
    return Response("Forbidden", 403)

@app.after_request
def _maybe_cors(resp):
    if ENABLE_CORS:
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-OPS-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))

def _sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

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
    """Accept number-like or dict {timestamp|ts|time_s|t|seconds}."""
    if val is None: return None
    if isinstance(val, (int, float, str)): return _float(val)
    if isinstance(val, dict):
        for k in ("timestamp", "timestamp_s", "ts", "time_s", "t", "seconds", "s"):
            if k in val: return _float(val[k])
    return None

def seconds_to_ts(base_dt: datetime, s):
    if s is None: return None
    try: return base_dt + timedelta(seconds=float(s))
    except Exception: return None

def _quantize_time_to_fps(s, fps):
    if s is None or not fps: return s
    return round(round(float(s) * float(fps)) / float(fps), 5)

def _quantize_time(s, fps):
    if s is None: return None
    if fps: return _quantize_time_to_fps(s, fps)
    return round(float(s), 3)  # 1ms grid when fps unknown

# =========================
#  Health + diagnostics
# =========================
@app.get("/upload/api/status")
def upload_status():
    return jsonify({
        "ok": True,
        "dropbox_ready": bool(DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH),
        "sportai_ready": bool(os.getenv("SPORT_AI_TOKEN")),
        "target_folder": DBX_FOLDER,
    })

@app.get("/")
def root_ok():
    return jsonify({"service": "NextPoint Upload/Ingester v3", "ok": True})

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.get("/__routes")
def __routes_open():
    routes = [
        {"rule": r.rule, "endpoint": r.endpoint,
         "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
        for r in app.url_map.iter_rules()
    ]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if not _guard(): return _forbid()
    return __routes_open()

@app.get("/upload/api/task-status")
def api_task_status():
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        st = _sportai_status(task_id)
        return jsonify({"ok": True, **st})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.post("/upload/webhook")  # configure this in SportAI console
def upload_webhook():
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400

    result_url = payload.get("result_url") or (payload.get("data") or {}).get("result_url")
    task_id = payload.get("task_id") or (payload.get("data") or {}).get("task_id")
    if not result_url:
        return jsonify({"ok": False, "error": "Missing result_url"}), 400

    try:
        ing = _trigger_ingest(result_url)  # uses /ops/ingest-file with OPS_KEY
    except Exception as e:
        return jsonify({"ok": False, "task_id": task_id, "error": f"ingest failed: {e}"}), 502

    return jsonify({"ok": True, "task_id": task_id, "ingest": ing})

# =========================
#        Ingest core
# =========================

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

def _base_dt_for_session(dt):
    return dt if dt else datetime(1970,1,1,tzinfo=timezone.utc)

_SWING_TYPES   = {"swing","stroke","shot","hit","serve","forehand","backhand","volley","overhead","slice","drop","lob"}
_SERVE_LABELS  = {"serve","first_serve","1st_serve","second_serve","2nd_serve"}

def _extract_ball_hit_from_events(events):
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
    if not isinstance(obj, dict):
        return None

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
    if (bhx is None or bhy is None) and isinstance(loc_any, (list, tuple)) and len(loc_any) >= 2:
        bhx = _float(loc_any[0]); bhy = _float(loc_any[1])

    swing_type = (str(
        obj.get("swing_type") or obj.get("type") or obj.get("label") or obj.get("stroke_type") or ""
    )).lower()

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
    keys = ("swings", "strokes", "swing_events") if STRICT_BRONZE_RAW else ("swings", "strokes", "swing_events", "events")
    for key in keys:
        arr = container.get(key)
        if not isinstance(arr, list):
            continue
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

def _insert_raw_result(conn, sid: int, payload: dict) -> None:
    conn.execute(sql_text("""
        INSERT INTO raw_result (session_id, payload_json, created_at)
        VALUES (:sid, CAST(:p AS JSONB), now() AT TIME ZONE 'utc')
    """), {"sid": sid, "p": json.dumps(payload)})

def _ensure_rallies_from_swings(conn, session_id, gap_s=6.0):
    conn.execute(sql_text("""
    WITH sw AS (
      SELECT session_id, COALESCE(ball_hit_s, start_s) AS t, swing_id,
             ROW_NUMBER() OVER (ORDER BY COALESCE(ball_hit_s, start_s), swing_id) AS rn
      FROM fact_swing
      WHERE session_id = :sid AND COALESCE(ball_hit_s, start_s) IS NOT NULL
    ),
    seg AS (
      SELECT sw.*, CASE
        WHEN rn = 1 THEN 1
        WHEN (t - LAG(t) OVER (ORDER BY rn)) >= :gap THEN 1
        ELSE 0
      END AS new_seg
      FROM sw
    ),
    grp AS (
      SELECT session_id, t, SUM(new_seg) OVER (ORDER BY rn) AS grp_id
      FROM seg
    ),
    bounds AS (
      SELECT session_id, grp_id, MIN(t) AS start_s, MAX(t) AS end_s
      FROM grp
      GROUP BY session_id, grp_id
    )
    INSERT INTO dim_rally (session_id, rally_number, start_s, end_s)
    SELECT b.session_id, ROW_NUMBER() OVER (ORDER BY start_s), b.start_s, b.end_s
    FROM bounds b
    ON CONFLICT (session_id, rally_number) DO UPDATE
      SET start_s = EXCLUDED.start_s, end_s = EXCLUDED.end_s;
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
    # Swings
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
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
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
        )
        UPDATE fact_bounce b
           SET bounce_ts = make_timestamp(1970,1,1,0,0,0)
                           + make_interval(secs => GREATEST(0, COALESCE(b.bounce_s,0) - z.t0))
          FROM z
         WHERE b.session_id = z.session_id;
    """), {"sid": session_id})
    # Ball positions
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
        )
        UPDATE fact_ball_position bp
           SET ts = make_timestamp(1970,1,1,0,0,0)
                    + make_interval(secs => GREATEST(0, COALESCE(bp.ts_s,0) - z.t0))
          FROM z
         WHERE bp.session_id = z.session_id;
    """), {"sid": session_id})
    # Player positions
    conn.execute(sql_text("""
        WITH z AS (
          SELECT :sid AS session_id,
                 COALESCE((SELECT MIN(COALESCE(ball_hit_s, start_s))
                           FROM fact_swing WHERE session_id=:sid), 0) AS t0
        )
        UPDATE fact_player_position pp
           SET ts = make_timestamp(1970,1,1,0,0,0)
                    + make_interval(secs => GREATEST(0, COALESCE(pp.ts_s,0) - z.t0))
          FROM z
         WHERE pp.session_id = z.session_id;
    """), {"sid": session_id})

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
        "sid": session_id, "pid": player_id, "suid": s.get("suid"),
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

def ingest_result_v2(conn, payload: dict, replace=False, forced_uid=None, src_hint=None):
    # Session
    session_uid  = _resolve_session_uid(payload, forced_uid=forced_uid, src_hint=src_hint)
    fps          = _resolve_fps(payload)
    session_date = _resolve_session_date(payload)
    base_dt      = _base_dt_for_session(session_date)
    meta         = payload.get("meta") or payload.get("metadata") or {}
    meta_json    = json.dumps(meta)

    conn.execute(sql_text("""
        INSERT INTO dim_session (session_uid, fps, session_date, meta)
        VALUES (:u, :fps, :sdt, CAST(:m AS JSONB))
        ON CONFLICT (session_uid) DO UPDATE SET
          fps = COALESCE(EXCLUDED.fps, dim_session.fps),
          session_date = COALESCE(EXCLUDED.session_date, dim_session.session_date),
          meta = COALESCE(EXCLUDED.meta, dim_session.meta)
    """), {"u": session_uid, "fps": fps, "sdt": session_date, "m": meta_json})

    session_id = conn.execute(sql_text(
        "SELECT session_id FROM dim_session WHERE session_uid=:u"),
        {"u": session_uid}).scalar_one()

    if replace:
        conn.execute(sql_text("DELETE FROM fact_ball_position   WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM fact_player_position WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM fact_bounce          WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM fact_swing           WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM dim_rally            WHERE session_id=:sid"), {"sid": session_id})
        conn.execute(sql_text("DELETE FROM dim_player           WHERE session_id=:sid"), {"sid": session_id})

    # RAW snapshot
    _insert_raw_result(conn, session_id, payload)

    # Players
    players = payload.get("players") or []
    uid_to_player_id: Dict[str, int] = {}
    conn.execute(sql_text("ALTER TABLE IF EXISTS dim_player ADD COLUMN IF NOT EXISTS meta JSONB"))

    for p in players:
        puid = str(p.get("id") or p.get("sportai_player_uid") or p.get("uid") or p.get("player_id") or "")
        if not puid: continue
        full_name = p.get("full_name") or p.get("name")
        handed    = p.get("handedness")
        age       = p.get("age")
        utr       = _float(p.get("utr"))
        metrics   = p.get("metrics") or {}

        covered_distance  = _float(p.get("covered_distance") or metrics.get("covered_distance"))
        fastest_sprint    = _float(p.get("fastest_sprint")   or metrics.get("fastest_sprint"))
        fastest_sprint_ts = _float(p.get("fastest_sprint_timestamp") or
                                   p.get("fastest_sprint_timestamp_s") or
                                   metrics.get("fastest_sprint_timestamp") or
                                   metrics.get("fastest_sprint_timestamp_s"))
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
                :cd, :fs, :fst, :ascore, CAST(:dist AS JSONB), CAST(:lheat AS JSONB), CAST(:pmeta AS JSONB)
            )
            ON CONFLICT (session_id, sportai_player_uid) DO UPDATE SET
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

    # ensure players from player_positions
    pp_obj = payload.get("player_positions") or {}
    pp_uids = [str(k) for k, arr in pp_obj.items() if str(k) and arr]
    for puid in [u for u in pp_uids if u not in uid_to_player_id]:
        conn.execute(sql_text("""
            INSERT INTO dim_player (session_id, sportai_player_uid)
            VALUES (:sid, :puid) ON CONFLICT (session_id, sportai_player_uid) DO NOTHING
        """), {"sid": session_id, "puid": puid})
        pid = conn.execute(sql_text("""
            SELECT player_id FROM dim_player WHERE session_id=:sid AND sportai_player_uid=:p
        """), {"sid": session_id, "p": puid}).scalar_one()
        uid_to_player_id[puid] = pid

    # rallies from payload (if any)
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
            ON CONFLICT (session_id, rally_number) DO UPDATE SET
              start_s=COALESCE(EXCLUDED.start_s,dim_rally.start_s),
              end_s  =COALESCE(EXCLUDED.end_s,  dim_rally.end_s),
              start_ts=COALESCE(EXCLUDED.start_ts,dim_rally.start_ts),
              end_ts  =COALESCE(EXCLUDED.end_ts,  dim_rally.end_ts)
        """), {"sid": session_id, "n": i, "ss": start_s, "es": end_s,
               "sts": seconds_to_ts(base_dt, start_s), "ets": seconds_to_ts(base_dt, end_s)})

    def rally_id_for_ts(ts_s):
        if ts_s is None: return None
        row = conn.execute(sql_text("""
            SELECT rally_id FROM dim_rally
            WHERE session_id=:sid AND :s BETWEEN start_s AND end_s
            ORDER BY rally_number LIMIT 1
        """), {"sid": session_id, "s": ts_s}).fetchone()
        return row[0] if row else None

    # ball bounces
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
            INSERT INTO fact_bounce (session_id, hitter_player_id, rally_id, bounce_s, bounce_ts, x, y, bounce_type)
            VALUES (:sid, :pid, :rid, :s, :ts, :x, :y, :bt)
        """), {"sid": session_id, "pid": hitter_pid, "rid": rally_id_for_ts(s),
               "s": s, "ts": seconds_to_ts(base_dt, s), "x": bx, "y": by, "bt": btype})

    # ball positions
    for p in (payload.get("ball_positions") or []):
        s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
        hx = _float(p.get("x")) if p.get("x") is not None else None
        hy = _float(p.get("y")) if p.get("y") is not None else None
        conn.execute(sql_text("""
            INSERT INTO fact_ball_position (session_id, ts_s, ts, x, y)
            VALUES (:sid, :ss, :ts, :x, :y)
        """), {"sid": session_id, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": hx, "y": hy})

    # player positions
    for puid, arr in (payload.get("player_positions") or {}).items():
        pid = uid_to_player_id.get(str(puid))
        if not pid: continue
        for p in (arr or []):
            s  = _time_s(p.get("timestamp")) or _time_s(p.get("timestamp_s")) or _time_s(p.get("ts")) or _time_s(p.get("t"))
            px = _float(p.get("court_X", p.get("court_x"))) if ("court_X" in p or "court_x" in p) else _float(p.get("X", p.get("x")))
            py = _float(p.get("court_Y", p.get("court_y"))) if ("court_Y" in p or "court_y" in p) else _float(p.get("Y", p.get("y")))
            conn.execute(sql_text("""
                INSERT INTO fact_player_position (session_id, player_id, ts_s, ts, x, y)
                VALUES (:sid, :pid, :ss, :ts, :x, :y)
            """), {"sid": session_id, "pid": pid, "ss": s, "ts": seconds_to_ts(base_dt, s), "x": px, "y": py})

    # swings
    seen = set()
    def _seen_key(pid, norm):
        if norm.get("suid"): return ("suid", str(norm["suid"]))
        return ("fb", pid, _quantize_time(norm.get("start_s"), fps), _quantize_time(norm.get("end_s"), fps))

    for norm in _gather_all_swings(payload):
        pid = uid_to_player_id.get(str(norm.get("player_uid") or "")) if norm.get("player_uid") else None
        k = _seen_key(pid, norm)
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

    # Build/link/normalize
    if not had_payload_rallies:
        _ensure_rallies_from_swings(conn, session_id, gap_s=RALLY_GAP_S)
    _link_swings_to_rallies(conn, session_id)
    _normalize_serve_flags(conn, session_id)
    _rebuild_ts_from_seconds(conn, session_id)

    return {"session_uid": session_uid, "session_id": session_id}

# =========================
#        OPS endpoints
# =========================
@app.get("/ops/db-ping")
def db_ping():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        now = conn.execute(sql_text("SELECT now() AT TIME ZONE 'utc'")).scalar_one()
    return jsonify({"ok": True, "now_utc": str(now)})

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

@app.get("/ops/refresh-gold")
def ops_refresh_gold():
    if not _guard(): return _forbid()
    try:
        with engine.begin() as conn:
            # point_log_tbl
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
            # point_summary_tbl
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
            "dim_player":  c("dim_player"),
            "dim_rally":   c("dim_rally"),
            "fact_swing":  c("fact_swing"),
            "fact_bounce": c("fact_bounce"),
            "fact_ball_position":   c("fact_ball_position"),
            "fact_player_position": c("fact_player_position"),
            "team_session": c("team_session"),
            "highlight":    c("highlight"),
            "bounce_heatmap": c("bounce_heatmap"),
            "session_confidences": c("session_confidences"),
            "thumbnail": c("thumbnail"),
            "raw_result": c("raw_result"),
        }
        for t in ("point_log_tbl", "point_summary_tbl"):
            exists = conn.execute(sql_text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{t}"}).scalar()
            if exists: counts[t] = c(t)
    return jsonify({"ok": True, "counts": counts})

@app.get("/ops/db-rollup")
def ops_db_rollup():
    if not _guard(): return _forbid()
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT
              ds.session_uid, ds.session_id,
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
        return Response("Only SELECT/CTE queries are allowed", 400)
    stripped = q.strip()
    if ";" in stripped[:-1]:
        return Response("Only a single statement is allowed", 400)
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
    if not session_uid: return jsonify({"ok": False, "error": "missing session_uid"}), 400
    with engine.connect() as conn:
        sid = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u": session_uid}).scalar()
        if not sid: return jsonify({"ok": False, "error": "unknown session_uid"}), 404
        doc = conn.execute(sql_text("""
            SELECT payload_json FROM raw_result
            WHERE session_id=:sid ORDER BY created_at DESC LIMIT 1
        """), {"sid": sid}).scalar()
    if doc is None: return jsonify({"ok": False, "error": "no raw_result for session"}), 404
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
                sid = conn.execute(sql_text("SELECT session_id FROM dim_session WHERE session_uid=:u"), {"u": session_uid}).scalar()
                if not sid: return jsonify({"ok": False, "error": "unknown session_uid"}), 404
                sid_rows = [(sid, session_uid)]
            else:
                sid_rows = conn.execute(sql_text("""
                    SELECT DISTINCT rr.session_id, ds.session_uid
                    FROM raw_result rr JOIN dim_session ds ON ds.session_id = rr.session_id
                """)).fetchall()

            totals = []
            for sid, suid in sid_rows:
                doc = conn.execute(sql_text("""
                    SELECT payload_json FROM raw_result
                    WHERE session_id=:sid ORDER BY created_at DESC LIMIT 1
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

                # Ball positions
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

                # Ball bounces
                id_map = {r["sportai_player_uid"]: r["player_id"] for r in
                          conn.execute(sql_text("SELECT sportai_player_uid, player_id FROM dim_player WHERE session_id=:sid"),
                                       {"sid": sid}).mappings()}
                bb = doc.get("ball_bounces") or doc.get("ballBounces")
                rows = []
                if isinstance(bb, list):
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

                # Player positions
                pp = doc.get("player_positions") or doc.get("playerPositions") or {}
                rows = []
                if isinstance(pp, dict):
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
        if raw_result_id:
            rr = conn.execute(sql_text("""
                SELECT rr.raw_result_id, rr.session_id, rr.payload_json::text AS payload_text
                FROM raw_result rr WHERE rr.raw_result_id=:rid
            """), {"rid": int(raw_result_id)}).mappings().first()
            if not rr: return jsonify({"ok": False, "error": "raw_result_id not found"}), 404
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
            if not sid: return jsonify({"ok": False, "error": "unknown session_uid"}), 404
            payload_text = conn.execute(sql_text("""
                SELECT payload_json::text FROM raw_result
                WHERE session_id=:sid ORDER BY created_at DESC LIMIT 1
            """), {"sid": sid}).scalar()
            if not payload_text:
                return jsonify({"ok": False, "error": "no raw_result for this session"}), 404

        try:
            payload = json.loads(payload_text)
        except Exception as e:
            return jsonify({"ok": False, "error": f"invalid payload_json: {e}"}), 500

        # RAW counters
        def _len_safe(x): return len(x) if isinstance(x, list) else 0
        players = payload.get("players") or []
        n_players_raw = len(players)

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
            if (bx is not None and by is not None) or (isinstance(cp,(list,tuple)) and len(cp)>=2 and cp[0] is not None and cp[1] is not None):
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
        elif isinstance(pp, list):
            n_pp_raw = len(pp)
            n_pp_xy_raw = sum(1 for r in pp if (r.get("court_x") is not None and r.get("court_y") is not None) or (r.get("x") is not None and r.get("y") is not None))
        else:
            n_pp_raw = n_pp_xy_raw = 0

        # BRONZE counters
        n_players_dim = conn.execute(sql_text("SELECT COUNT(*) FROM dim_player WHERE session_id=:sid"), {"sid": sid}).scalar()
        n_swings      = conn.execute(sql_text("SELECT COUNT(*) FROM fact_swing WHERE session_id=:sid"), {"sid": sid}).scalar()
        n_rallies     = conn.execute(sql_text("SELECT COUNT(*) FROM dim_rally WHERE session_id=:sid"), {"sid": sid}).scalar()
        n_bounces     = conn.execute(sql_text("SELECT COUNT(*) FROM fact_bounce WHERE session_id=:sid"), {"sid": sid}).scalar()
        n_bounces_xy  = conn.execute(sql_text("SELECT COUNT(*) FROM fact_bounce WHERE session_id=:sid AND x IS NOT NULL AND y IS NOT NULL"), {"sid": sid}).scalar()
        n_ballpos     = conn.execute(sql_text("SELECT COUNT(*) FROM fact_ball_position WHERE session_id=:sid"), {"sid": sid}).scalar()
        n_ballpos_xy  = conn.execute(sql_text("SELECT COUNT(*) FROM fact_ball_position WHERE session_id=:sid AND x IS NOT NULL AND y IS NOT NULL"), {"sid": sid}).scalar()
        n_pp          = conn.execute(sql_text("SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid"), {"sid": sid}).scalar()
        n_pp_xy       = conn.execute(sql_text("SELECT COUNT(*) FROM fact_player_position WHERE session_id=:sid AND x IS NOT NULL AND y IS NOT NULL"), {"sid": sid}).scalar()

    return jsonify({
        "ok": True, "session_uid": session_uid, "session_id": sid,
        "raw": {
            "players": n_players_raw, "swings": n_swings_raw, "rallies": n_rallies_raw,
            "ball_bounces": n_bounces_raw, "ball_bounces_xy": n_bounces_xy_raw,
            "ball_positions": n_ballpos_raw, "ball_positions_xy": n_ballpos_xy_raw,
            "player_positions": n_pp_raw, "player_positions_xy": n_pp_xy_raw
        },
        "bronze": {
            "players": n_players_dim, "swings": n_swings, "rallies": n_rallies,
            "ball_bounces": n_bounces, "ball_bounces_xy": n_bounces_xy,
            "ball_positions": n_ballpos, "ball_positions_xy": n_ballpos_xy,
            "player_positions": n_pp, "player_positions_xy": n_pp_xy
        }
    })

# =========================
#   Upload page + webhook
# =========================
def _normalize_dropbox_url(u: str) -> str:
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        if "dropbox.com" not in u: return u
        p = urlparse(u)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        q["dl"] = "1"
        host = "dl.dropboxusercontent.com" if "dropbox.com" in p.netloc else p.netloc
        return urlunparse((p.scheme, host, p.path, p.params, urlencode(q), p.fragment))
    except Exception:
        return u

def _render_upload_page():
    if not _guard(): return _forbid()
    key = request.args.get("key") or request.args.get("ops_key") or ""
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<title>Upload SportAI Session JSON</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 24px; }}
  .card {{ max-width: 720px; margin:auto; border:1px solid #e5e7eb; border-radius:12px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,.05); }}
  h2 {{ margin:0 0 12px 0; }}
  label {{ display:block; font-weight:600; margin-top:16px; margin-bottom:6px; }}
  input[type="text"] {{ width:100%; padding:10px; border:1px solid #d1d5db; border-radius:8px; }}
  .muted {{ color:#6b7280; font-size:12px; }}
  button {{ margin-top:18px; padding:10px 16px; background:#111827; color:#fff; border:none; border-radius:10px; cursor:pointer; }}
  .row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
  .row > * {{ flex: 1 1 auto; }}
  .chip {{ display:inline-block; padding:2px 8px; border-radius:9999px; background:#eef2ff; color:#3730a3; font-size:12px; }}
</style>
</head><body>
<div class="card">
  <h2>Upload SportAI Session JSON</h2>
  <div class="muted">File / Public JSON URL / Dropbox link (set to “Anyone with the link”).</div>
  <form action="/ops/ingest-file?key={key}" method="post" enctype="multipart/form-data">
    <label>JSON file</label><input type="file" name="file" accept="application/json"/>
    <label>Public JSON URL</label><input type="text" name="url" placeholder="https://example.com/session.json"/>
    <div class="muted">If your Dropbox link ends with <span class="chip">dl=0</span> we’ll convert to <span class="chip">dl=1</span>.</div>
    <div class="row">
      <div><label>Replace</label><label class="muted"><input type="checkbox" name="replace" value="1" checked /> Replace existing</label></div>
      <div><label>Mode</label><select name="mode"><option value="soft" selected>soft</option><option value="hard">hard</option></select></div>
      <div><label>Session UID (optional)</label><input type="text" name="session_uid" placeholder="71053b3e-..." /></div>
    </div>
    <button type="submit">Ingest</button>
    <div class="muted" style="margin-top:8px;">Your ops key is carried in the URL.</div>
  </form>
</div>
</body></html>"""
    return Response(html, mimetype="text/html")

@app.route("/ops/ingest-file", methods=["GET", "POST"])
def ops_ingest_file():
    if not _guard(): return _forbid()
    if request.method == "GET":
        return _render_upload_page()

    replace    = str(request.form.get("replace", "1")).lower() in ("1","true","yes","y","on")
    mode       = (request.form.get("mode") or "soft").strip().lower()
    forced_uid = request.form.get("session_uid") or None

    payload = None
    if "file" in request.files and request.files["file"].filename:
        try:
            payload = json.load(request.files["file"].stream)
        except Exception as e:
            return jsonify({"ok": False, "error": f"file not JSON: {e}"}), 400
    elif request.form.get("url"):
        url = _normalize_dropbox_url(request.form["url"])
        try:
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            return jsonify({"ok": False, "error": f"failed to fetch URL: {e}"}), 400
    elif request.form.get("name"):
        name = request.form["name"]
        try:
            with open(name, "rb") as f: payload = json.load(f)
        except Exception:
            try:
                with open(os.path.join("/mnt/data", name), "rb") as f: payload = json.load(f)
            except Exception as e2:
                return jsonify({"ok": False, "error": f"failed to read file: {e2}"}), 400
    else:
        try:
            payload = request.get_json(force=True, silent=False)
        except Exception:
            return jsonify({"ok": False, "error": "no JSON supplied"}), 400

    try:
        with engine.begin() as conn:
            res = ingest_result_v2(
                conn,
                payload,
                replace=replace or (mode == "hard"),
                forced_uid=forced_uid,
                src_hint=request.form.get("url"),
            )
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

# =========================
#   Mount UI blueprint
# =========================
try:
    from ui_app import ui_bp  # sessions/SQL UI, templates/upload.html, etc.
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# Print routes at boot
print("=== ROUTES ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:30s} -> {r.endpoint:24s} [{meth}]")
print("================")
