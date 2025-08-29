# upload_app.py — entrypoint (uploads + SportAI + status)
import os, json, time
from datetime import timedelta, datetime, timezone

import requests
from flask import Flask, request, jsonify, Response
from werkzeug.utils import secure_filename

# single app for Render
app = Flask(__name__, template_folder="templates", static_folder="static")
app.url_map.strict_slashes = False

# ==== env / config
OPS_KEY = os.getenv("OPS_KEY", "")

DBX_APP_KEY     = os.getenv("DROPBOX_APP_KEY", "")
DBX_APP_SECRET  = os.getenv("DROPBOX_APP_SECRET", "")
DBX_REFRESH     = os.getenv("DROPBOX_REFRESH_TOKEN", "")
DBX_FOLDER      = os.getenv("DROPBOX_UPLOAD_FOLDER", "/wix-uploads")
SPORTAI_BASE        = os.getenv("SPORT_AI_BASE", "https://api.sportai.app").strip().rstrip("/")
SPORTAI_SUBMIT_PATH = os.getenv("SPORT_AI_SUBMIT_PATH", "/api/statistics").strip()
SPORTAI_STATUS_PATH = os.getenv("SPORT_AI_STATUS_PATH", "/api/statistics/{task_id}").strip()
SPORTAI_TOKEN       = os.getenv("SPORT_AI_TOKEN", "").strip()

# Try the configured base first, then a safe fallback.
ALT_SPORTAI_BASES = [SPORTAI_BASE, "https://sportai.app", "https://api.sportai.app"]
ALT_SPORTAI_BASES = list(dict.fromkeys([b.strip().rstrip("/") for b in ALT_SPORTAI_BASES if b]))

ENABLE_CORS = os.environ.get("ENABLE_CORS", "0").lower() in ("1","true","yes","y")

# --- Back-compat with the older deploy that used a single access token + /upload + field 'video'
DBX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN", "")

def _dbx_get_token():
    """Prefer legacy DROPBOX_ACCESS_TOKEN if present; otherwise mint from refresh token."""
    if DBX_ACCESS_TOKEN:
        return DBX_ACCESS_TOKEN, None
    return _dbx_access_token()  # existing function

# alias: accept old endpoint/field without breaking the new one
def _api_upload_compat():
    # Reuse the same handler; Flask will pass through to it
    return api_upload_to_dropbox()
app.add_url_rule("/upload", view_func=_api_upload_compat, methods=["POST", "OPTIONS"])

# DB engine (used by callback)
from db_init import engine  # noqa: E402

# import ingest core + ops blueprint
from ingest_app import ingest_bp, ingest_result_v2  # noqa: E402
app.register_blueprint(ingest_bp, url_prefix="")    # mounts all /ops/* ingest routes

# ==== small helpers
def _guard() -> bool:
    qk = request.args.get("key") or request.args.get("ops_key")
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = qk or hk
    return bool(OPS_KEY) and supplied == OPS_KEY

def _forbid(): return Response("Forbidden", 403)

@app.after_request
def _maybe_cors(resp):
    if ENABLE_CORS:
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-OPS-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

def _dbx_access_token():
    if not (DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH):
        return None, "Missing Dropbox env vars"
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "refresh_token",
              "refresh_token": DBX_REFRESH,
              "client_id": DBX_APP_KEY,
              "client_secret": DBX_APP_SECRET},
        timeout=30)
    if r.ok: return r.json().get("access_token"), None
    return None, f"{r.status_code}: {r.text}"

def _dbx_create_or_fetch_shared_link(token: str, path: str) -> str:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers=h, json={"path": path, "settings": {"audience": "public", "access": "viewer"}}, timeout=30)
    if r.status_code == 409:
        r = requests.post(
            "https://api.dropboxapi.com/2/sharing/list_shared_links",
            headers=h, json={"path": path, "direct_only": True}, timeout=30)
        r.raise_for_status()
        links = (r.json() or {}).get("links", [])
        if not links: raise RuntimeError("No Dropbox shared link available")
        return links[0]["url"]
    r.raise_for_status()
    return (r.json() or {})["url"]

def _to_direct_dropbox(url: str) -> str:
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        p = urlparse(url); q = dict(parse_qsl(p.query, keep_blank_values=True))
        q["dl"] = "1"
        host = "dl.dropboxusercontent.com" if "dropbox.com" in p.netloc else p.netloc
        return urlunparse((p.scheme, host, p.path, p.params, urlencode(q), p.fragment))
    except Exception:
        return url

def _sportai_submit(video_url: str, email: str | None = None) -> str:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")

    url = f"{SPORTAI_BASE}{SPORTAI_SUBMIT_PATH}"
    headers = {
        "Authorization": f"Bearer {SPORTAI_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "video_url": video_url,          # we still need to tell SportAI where the video is
        "only_in_rally_data": False,     # from their example
        "version": "stable",             # from their example
    }
    if email:
        payload["email"] = email

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    j = r.json() or {}

    # tolerate slight response shape differences
    task_id = j.get("task_id") or (j.get("data") or {}).get("task_id") or j.get("id")
    if not task_id:
        raise RuntimeError(f"No task_id in response: {j}")
    return str(task_id)


def _sportai_status(task_id: str) -> dict:
    if not SPORTAI_TOKEN:
        raise RuntimeError("SPORT_AI_TOKEN not set")
    url = f"{SPORTAI_BASE}{SPORTAI_STATUS_PATH.format(task_id=task_id)}"
    headers = {"Authorization": f"Bearer {SPORTAI_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=30); r.raise_for_status()
    j = r.json() or {}
    return {"status": j.get("status") or (j.get("data") or {}).get("status"),
            "result_url": j.get("result_url") or (j.get("data") or {}).get("result_url"),
            "raw": j}

# ==== public endpoints

@app.get("/")
def root_ok(): return jsonify({"service": "NextPoint Upload/Ingester v3", "ok": True})

@app.get("/healthz")
def healthz_ok(): return "OK", 200

@app.get("/__routes")
def __routes_open():
    routes = [{"rule": r.rule, "endpoint": r.endpoint,
               "methods": sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"})}
              for r in app.url_map.iter_rules()]
    routes.sort(key=lambda x: x["rule"])
    return jsonify({"ok": True, "count": len(routes), "routes": routes})

@app.get("/ops/routes")
def __routes_locked():
    if not _guard(): return _forbid()
    return __routes_open()

@app.get("/upload/api/status")
def upload_status():
    return jsonify({"ok": True,
                    "dropbox_ready": bool(DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH),
                    "sportai_ready": bool(SPORTAI_TOKEN),
                    "target_folder": DBX_FOLDER})

@app.get("/ops/dropbox-auth-test")
def ops_dropbox_auth_test():
    if not _guard(): return _forbid()
    tok, err = _dbx_get_token()
    if not tok:
        return jsonify({"ok": False, "error": f"Dropbox auth failed: {err}"}), 500
    mode = "access_token" if DBX_ACCESS_TOKEN else "refresh_flow"
    return jsonify({"ok": True, "mode": mode, "access_token_last4": tok[-4:]})

@app.get("/ops/net-test")
def ops_net_test():
    if not _guard(): return _forbid()
    out = {}
    for base in ALT_SPORTAI_BASES:
        try:
            u = f"{base}/"
            r = requests.get(u, timeout=10)
            out[base] = {"ok": r.ok, "code": r.status_code}
        except Exception as e:
            out[base] = {"ok": False, "error": str(e)}
    return jsonify({"ok": True, "tests": out})


@app.post("/upload/api/upload")
def api_upload_to_dropbox():
    # accept both 'file' (new) and 'video' (old)
    f = request.files.get("file") or request.files.get("video")
    email = (request.form.get("email") or "").strip().lower()
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file provided."}), 400

    tok, err = _dbx_get_token()  # was: _dbx_access_token()
    if not tok:
        return jsonify({"ok": False, "error": f"Dropbox auth failed: {err}"}), 500

    clean = secure_filename(f.filename)
    dest_path = f"{DBX_FOLDER.rstrip('/')}/{int(time.time())}_{clean}"

    headers = {
        "Authorization": f"Bearer {tok}",
        "Dropbox-API-Arg": json.dumps({"path": dest_path, "mode": "add", "autorename": True, "mute": False}),
        "Content-Type": "application/octet-stream",
    }
    up = requests.post("https://content.dropboxapi.com/2/files/upload", headers=headers, data=f.read(), timeout=600)
    if not up.ok:
        return jsonify({"ok": False, "error": f"Dropbox upload failed: {up.status_code} {up.text}"}), 502
    meta = up.json()

    # create a public link and force direct download (same outcome as the old app)
    share_url = _dbx_create_or_fetch_shared_link(tok, meta.get("path_lower") or dest_path)
    video_url = _to_direct_dropbox(share_url)

    # be liberal in what we send to SportAI so both old/new backends are happy
    try:
        task_id = _sportai_submit(video_url, email=email)
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"Upload ok, SportAI submit failed: {e}",
                        "upload": {"path": meta.get("path_display") or dest_path,
                                   "size": meta.get("size"),
                                   "name": meta.get("name", clean)}}), 502

    return jsonify({"ok": True,
                    "task_id": task_id,
                    "share_url": share_url,
                    "video_url": video_url,
                    "upload": {"path": meta.get("path_display") or dest_path,
                               "size": meta.get("size"),
                               "name": meta.get("name", clean)}})


@app.get("/upload/api/task-status")
def api_task_status():
    tid = request.args.get("task_id")
    if not tid: return jsonify({"ok": False, "error": "task_id required"}), 400
    try:
        return jsonify({"ok": True, **_sportai_status(tid)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

# add to upload_app.py
import socket
from urllib.parse import urlparse

@app.get("/ops/sportai-dns")
def ops_sportai_dns():
    if not _guard(): return _forbid()
    host = urlparse(SPORTAI_BASE).hostname
    try:
        ip = socket.gethostbyname(host)
        return jsonify({"ok": True, "host": host, "ip": ip})
    except Exception as e:
        return jsonify({"ok": False, "host": host, "error": str(e)}), 500


# SportAI → our callback (kept here per your request)
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

    from sqlalchemy import text as sql_text  # local import to avoid pulling lots into this file
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

        return jsonify({"ok": True,
                        "session_uid": res.get("session_uid"),
                        "session_id":  sid,
                        "bronze_counts": {"rallies": counts[0],
                                          "ball_bounces": counts[1],
                                          "ball_positions": counts[2],
                                          "player_positions": counts[3],
                                          "swings": counts[4]}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# try to mount your sessions/SQL UI, unchanged
try:
    from ui_app import ui_bp
    app.register_blueprint(ui_bp, url_prefix="/upload")
    print("Mounted ui_bp at /upload")
except Exception as e:
    print("ui_bp not mounted:", e)

# boot log
print("=== ROUTES ===")
for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
    meth = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
    print(f"{r.rule:30s} -> {r.endpoint:24s} [{meth}]")
print("================")
