# ui_app.py
import os, time, json, requests, threading, collections
from threading import Thread
from flask import Blueprint, request, render_template, jsonify
from werkzeug.utils import secure_filename

ui_bp = Blueprint(
    "ui",
    __name__,
    static_folder="static",
    template_folder="templates"
)

# ---------------- Config ----------------
BASE_URL = os.getenv("BASE_URL", "https://api.nextpointtennis.com")
OPS_KEY  = os.getenv("OPS_KEY", "")

def _get_sportai_token():
    tok = os.getenv("SPORT_AI_TOKEN") or os.getenv("SPORTAI_TOKEN")
    return tok.strip() if tok else None

DBX_APP_KEY       = os.getenv("DROPBOX_APP_KEY", "")
DBX_APP_SECRET    = os.getenv("DROPBOX_APP_SECRET", "")
DBX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")
DROPBOX_TARGET_FOLDER = os.getenv("DROPBOX_TARGET_FOLDER", "/wix-uploads")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))

# Optional: also ship the JSON to your webhook
RESULT_WEBHOOK_URL = os.getenv("RESULT_WEBHOOK_URL")

# In-memory ingest log (for quick debugging in UI)
_INGEST_LOG = collections.deque(maxlen=50)
_INGEST_LOCK = threading.Lock()

def _log_ingest(event: dict):
    with _INGEST_LOCK:
        event["ts"] = int(time.time())
        _INGEST_LOG.appendleft(event)

# ---------------- Dropbox helpers ----------------
def _dbx_access_token():
    """
    Exchange refresh token for a short-lived Dropbox access token.
    Uses HTTP Basic auth (recommended).
    """
    if not (DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH_TOKEN):
        return None, "Dropbox credentials not configured (DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN)"
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": DBX_REFRESH_TOKEN},
        auth=(DBX_APP_KEY, DBX_APP_SECRET),
        timeout=60,
    )
    if r.status_code != 200:
        return None, f"Dropbox token error: {r.text}"
    return r.json().get("access_token"), None

def _dbx_ensure_folder(token: str, folder_path: str):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(
        "https://api.dropboxapi.com/2/files/create_folder_v2",
        headers=headers,
        json={"path": folder_path, "autorename": False},
        timeout=30,
    )
    if r.status_code in (200, 409):
        return None
    return f"Dropbox create_folder error: {r.text}"

def _dbx_get_temporary_link(token: str, path: str):
    """
    Returns a fresh, fetchable URL (preferred for server-to-server access).
    """
    r = requests.post(
        "https://api.dropboxapi.com/2/files/get_temporary_link",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"path": path},
        timeout=60,
    )
    if r.status_code != 200:
        return None, f"Dropbox get_temporary_link error: {r.text}"
    return r.json().get("link"), None  # e.g. https://dl.dropboxusercontent.com/...

def _dbx_upload_and_link(file_storage):
    """
    Uploads the file to Dropbox and returns a *temporary* direct link.
    This avoids cookie/expiry issues seen with shared links.
    """
    token, err = _dbx_access_token()
    if err:
        return None, err
    ferr = _dbx_ensure_folder(token, DROPBOX_TARGET_FOLDER)
    if ferr:
        return None, ferr

    filename = secure_filename(file_storage.filename or f"upload_{int(time.time())}.mp4")
    path = f"{DROPBOX_TARGET_FOLDER.rstrip('/')}/{int(time.time())}_{filename}"
    data = file_storage.read()

    up = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({
                "path": path, "mode": "add", "autorename": True, "mute": False
            }),
            "Content-Type": "application/octet-stream",
        },
        data=data,
        timeout=1200,
    )
    if up.status_code != 200:
        return None, f"Dropbox upload error: {up.text}"

    # Return a fresh temporary link (no need to add ?raw=1 etc.)
    return _dbx_get_temporary_link(token, path)

# ---------------- SportAI helpers ----------------
def _sportai_headers():
    token = _get_sportai_token()
    if not token:
        return None, "SPORT_AI_TOKEN not configured on server"
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, None

def _sportai_check(video_url):
    headers, err = _sportai_headers()
    if err:
        return False, err
    r = requests.post(
        "https://api.sportai.com/api/videos/check",
        json={"version": "latest", "video_urls": [video_url]},
        headers=headers,
        timeout=60,
    )
    if r.status_code != 200:
        return False, f"SportAI check HTTP {r.status_code}: {r.text}"
    try:
        ok = r.json()["data"][video_url]["video_ok"]
        return bool(ok), None
    except Exception as e:
        return False, f"video check parse error: {e}"

def _sportai_submit(video_url, *, processing_windows=None, only_in_rally_data=False, version=None):
    headers, err = _sportai_headers()
    if err:
        return None, err
    ver = version or os.getenv("SPORTAI_VERSION") or "latest"
    payload = {
        "video_url": video_url,
        "only_in_rally_data": bool(only_in_rally_data),
        "version": ver,
    }
    if processing_windows:
        payload["processing_windows"] = processing_windows
    r = requests.post(
        "https://api.sportai.com/api/statistics/tennis",
        json=payload,
        headers=headers,
        timeout=60,
    )
    if r.status_code not in (200, 201, 202):
        return None, f"SportAI submit HTTP {r.status_code}: {r.text}"
    try:
        return r.json()["data"]["task_id"], None
    except Exception as e:
        return None, f"parse error: {e}; body={r.text[:400]}"

def _sportai_status(task_id):
    """
    Supports both old (status/progress) and new (task_status/task_progress) keys.
    """
    headers, err = _sportai_headers()
    if err:
        return None, None, err
    r = requests.get(
        f"https://api.sportai.com/api/statistics/{task_id}/status",
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        return None, None, f"status HTTP {r.status_code}: {r.text}"
    j = r.json().get("data", {})
    status   = j.get("status") or j.get("task_status")
    progress = j.get("progress") or j.get("task_progress")
    return status, progress, None

# ---------------- Download, webhook, and ingest ----------------
def _post_to_ingester(local_path, forced_uid=None):
    if not OPS_KEY or not BASE_URL:
        _log_ingest({"stage": "ingest-skip", "reason": "missing OPS_KEY or BASE_URL"})
        return
    try:
        params = {"key": OPS_KEY, "replace": "1"}
        if forced_uid:
            params["session_uid"] = forced_uid  # ensure uniqueness per task

        with open(local_path, "rb") as fh:
            files = {"file": (os.path.basename(local_path), fh, "application/json")}
            resp = requests.post(
                f"{BASE_URL}/ops/ingest-file",
                params=params,
                files=files,
                timeout=300,
            )
        ok = False
        detail = None
        try:
            body = resp.json()
            ok = body.get("ok") is True
            detail = body
        except Exception:
            detail = resp.text
        _log_ingest({"stage": "ingest-post", "http": resp.status_code, "ok": ok, "detail": detail})
    except Exception as e:
        _log_ingest({"stage": "ingest-exception", "error": str(e)})

def _download_result_and_ingest(task_id, save_prefix):
    headers, err = _sportai_headers()
    if err:
        _log_ingest({"stage": "token-missing", "error": err})
        return

    meta = requests.get(
        f"https://api.sportai.com/api/statistics/{task_id}",
        headers=headers,
        timeout=60,
    )
    if meta.status_code != 200:
        _log_ingest({"stage": "meta", "http": meta.status_code, "body": meta.text[:300]})
        return

    result_url = meta.json().get("data", {}).get("result_url")
    if not result_url:
        _log_ingest({"stage": "no-result-url"})
        return

    res = requests.get(result_url, timeout=300)
    if res.status_code != 200:
        _log_ingest({"stage": "download", "http": res.status_code, "body": res.text[:300]})
        return

    os.makedirs("data", exist_ok=True)
    fn = os.path.join("data", f"{save_prefix}_{task_id}.json")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(res.text)

    _log_ingest({"stage": "saved", "file": fn, "bytes": len(res.content)})

    # Optional webhook push
    if RESULT_WEBHOOK_URL:
        try:
            w = requests.post(RESULT_WEBHOOK_URL, data=res.text, headers={"Content-Type": "application/json"}, timeout=30)
            _log_ingest({"stage": "webhook", "http": w.status_code})
        except Exception as e:
            _log_ingest({"stage": "webhook-exception", "error": str(e)})

    # Post into your ingester (upload_app.py) — keep rows unique per job
    _post_to_ingester(fn, forced_uid=task_id)

def _poll_and_download(task_id, save_prefix):
    for _ in range(120):  # ~10 minutes
        st, prog, err = _sportai_status(task_id)
        if st in ("done", "completed"):
            _log_ingest({"stage": "completed", "task_id": task_id})
            _download_result_and_ingest(task_id, save_prefix)
            break
        if st == "failed":
            _log_ingest({"stage": "failed", "task_id": task_id})
            break
        time.sleep(5)

# ---------------- Routes ----------------
@ui_bp.get("/")
def upload_page():
    return render_template(
        "upload.html",
        max_upload_mb=MAX_UPLOAD_MB,
        dropbox_ready=bool(DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH_TOKEN),
        sportai_ready=bool(_get_sportai_token()),
        target_folder=DROPBOX_TARGET_FOLDER,
    )

@ui_bp.post("")
@ui_bp.post("/")
def upload_and_analyze():
    if not _get_sportai_token():
        return jsonify({"error": "SPORT_AI_TOKEN not configured on server"}), 500

    email = (request.form.get("email") or "").strip().replace("@", "_at_")
    file = request.files.get("video")
    if not file or not file.filename:
        return jsonify({"error": "Please choose a .mp4/.mov file to upload."}), 400

    clen = request.content_length
    if clen and clen > MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"File exceeds server limit of {MAX_UPLOAD_MB} MB."}), 413

    # Upload to Dropbox and get a FRESH temporary link (server-to-server friendly)
    try:
        link, err = _dbx_upload_and_link(file)
    except Exception as e:
        return jsonify({"error": f"Dropbox upload raised: {e}"}), 502
    if err:
        return jsonify({"error": err}), 502

    # Optional: sanity check with SportAI
    ok, err = _sportai_check(link)
    if not ok:
        return jsonify({"error": err or "Video not accepted by SportAI"}), 400

    task_id, err = _sportai_submit(link, version="latest", only_in_rally_data=False)
    if not task_id:
        return jsonify({"error": err or "Submit to SportAI failed"}), 502

    prefix = f"sportai_{email or 'anon'}_{int(time.time())}"
    Thread(target=_poll_and_download, args=(task_id, prefix), daemon=True).start()

    return jsonify({
        "message": "OK. Uploaded to our Dropbox and submitted to SportAI (tennis/latest).",
        "dropbox_url": link,
        "task_id": task_id,
        "sportai_task_id": task_id,
    })

@ui_bp.get("/task_status/<task_id>")
def ui_task_status(task_id):
    """
    Always 200 so the front-end doesn't throw; includes any upstream error
    in the payload. Supports both old/new key names from SportAI.
    """
    st, prog, err = _sportai_status(task_id)
    if err:
        # Return a friendly payload, not a 502
        return jsonify({
            "data": {
                "task_id": task_id,
                "task_status": "error",
                "task_progress": 0.0
            },
            "error": err
        })
    mapped = "completed" if st in ("done", "completed") else ("failed" if st == "failed" else st or "queued")
    progress = float(prog) if isinstance(prog, (int, float)) else (1.0 if mapped in ("completed", "failed") else 0.0)
    return jsonify({"data": {"task_id": task_id, "task_status": mapped, "task_progress": progress}})

@ui_bp.get("/debug/sportai-status/<task_id>")
def debug_sportai_status(task_id):
    """
    Raw passthrough of SportAI status for troubleshooting.
    """
    headers, err = _sportai_headers()
    if err:
        return jsonify({"http": 0, "error": err})
    r = requests.get(f"https://api.sportai.com/api/statistics/{task_id}/status",
                     headers=headers, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = r.text
    return jsonify({"http": r.status_code, "body": body})

# -------- Debug helpers -------
@ui_bp.get("/debug/sportai-token")
def debug_sportai_token():
    return jsonify({"present": bool(_get_sportai_token())})

@ui_bp.get("/debug/dropbox")
def debug_dropbox():
    tok, err = _dbx_access_token()
    if err:
        return jsonify({"ok": False, "stage": "token", "error": err}), 500
    ferr = _dbx_ensure_folder(tok, DROPBOX_TARGET_FOLDER)
    if ferr:
        return jsonify({"ok": False, "stage": "ensure_folder", "error": ferr}), 500
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    r = requests.post(
        "https://api.dropboxapi.com/2/files/list_folder",
        headers=headers,
        json={"path": DROPBOX_TARGET_FOLDER, "limit": 10, "recursive": False},
        timeout=30,
    )
    if r.status_code != 200:
        return jsonify({"ok": False, "stage": "list_folder", "error": r.text}), 500
    return jsonify({"ok": True, "folder": DROPBOX_TARGET_FOLDER, "entries_seen": len(r.json().get("entries", []))})

@ui_bp.get("/debug/ingest-log")
def debug_ingest_log():
    with _INGEST_LOCK:
        return jsonify(list(_INGEST_LOG))

@ui_bp.get("/admin")
def admin_panel():
    # Renders the admin helper page with buttons/links to ops endpoints
    return render_template("admin.html")
