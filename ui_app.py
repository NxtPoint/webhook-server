# ui_app.py
import os, time, json, requests
from threading import Thread
from flask import Blueprint, request, render_template, jsonify
from werkzeug.utils import secure_filename

# ---------- Blueprint ----------
ui_bp = Blueprint(
    "ui",
    __name__,
    static_folder="static",      # serves /upload/static/...
    template_folder="templates"  # uses templates/upload.html
)

# ---------- Config (env) ----------
BASE_URL = os.getenv("BASE_URL", "https://api.nextpointtennis.com")
OPS_KEY  = os.getenv("OPS_KEY", "")

# SportAI
SPORTAI_TOKEN = os.getenv("SPORT_AI_TOKEN") or os.getenv("SPORTAI_TOKEN", "")

# Dropbox OAuth for YOUR Dropbox account (server will upload files here)
DBX_APP_KEY       = os.getenv("DROPBOX_APP_KEY", "")
DBX_APP_SECRET    = os.getenv("DROPBOX_APP_SECRET", "")
DBX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")

# Target folder inside your Dropbox to store uploads (must exist or we’ll create)
DROPBOX_TARGET_FOLDER = os.getenv("DROPBOX_TARGET_FOLDER", "/uploads")

# Max size for direct uploads from browser to our server (MB).
# Large files can still work, but your hosting proxy may cap body size.
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))

# ---------- Dropbox helpers ----------
def _dbx_access_token():
    """Refresh app access token for YOUR Dropbox account."""
    if not (DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH_TOKEN):
        return None, "Dropbox credentials not configured (DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN)"
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DBX_REFRESH_TOKEN,
            "client_id": DBX_APP_KEY,
            "client_secret": DBX_APP_SECRET,
        },
        timeout=30,
    )
    if r.status_code != 200:
        return None, f"Dropbox token error: {r.text}"
    return r.json().get("access_token"), None

def _dbx_ensure_folder(token: str, folder_path: str):
    """Create folder if missing (idempotent)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(
        "https://api.dropboxapi.com/2/files/create_folder_v2",
        headers=headers,
        json={"path": folder_path, "autorename": False},
        timeout=30,
    )
    if r.status_code in (200, 409):
        return None  # ok (already exists or created)
    return f"Dropbox create_folder error: {r.text}"

def _dbx_create_or_get_shared_link(token: str, path: str):
    """Create or fetch a shared link for an internal path; normalize to direct bytes."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sh = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers=headers,
        json={"path": path, "settings": {"requested_visibility": "public"}},
        timeout=30,
    )
    if sh.status_code == 409:
        ls = requests.post(
            "https://api.dropboxapi.com/2/sharing/list_shared_links",
            headers=headers,
            json={"path": path, "direct_only": True},
            timeout=30,
        )
        if ls.status_code != 200:
            return None, f"Dropbox link fetch error: {ls.text}"
        url = ls.json()["links"][0]["url"]
    elif sh.status_code != 200:
        return None, f"Dropbox link create error: {sh.text}"
    else:
        url = sh.json()["url"]

    # normalize to direct bytes
    url = url.replace("www.dropbox.com", "dl.dropboxusercontent.com").replace("?dl=0", "")
    if "?raw=1" not in url:
        url = url + ("&raw=1" if "?" in url else "?raw=1")
    return url, None

def _dbx_upload_and_link(file_storage):
    """
    Upload the received file to YOUR Dropbox under DROPBOX_TARGET_FOLDER/<filename>,
    then return a direct-download URL for SportAI.
    """
    token, err = _dbx_access_token()
    if err:
        return None, err

    # ensure folder exists
    ferr = _dbx_ensure_folder(token, DROPBOX_TARGET_FOLDER)
    if ferr:
        return None, ferr

    filename = secure_filename(file_storage.filename or f"upload_{int(time.time())}.mp4")
    path = f"{DROPBOX_TARGET_FOLDER.rstrip('/')}/{filename}"
    data = file_storage.read()

    up = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite", "mute": True}),
            "Content-Type": "application/octet-stream",
        },
        data=data,
        timeout=1200,  # up to 20 minutes for big files
    )
    if up.status_code != 200:
        return None, f"Dropbox upload error: {up.text}"

    return _dbx_create_or_get_shared_link(token, path)

# ---------- SportAI helpers ----------
def _sportai_check(video_url):
    r = requests.post(
        "https://api.sportai.com/api/videos/check",
        json={"version": "stable", "video_urls": [video_url]},
        headers={"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"},
        timeout=60,
    )
    if r.status_code != 200:
        return False, f"SportAI check HTTP {r.status_code}: {r.text}"
    try:
        ok = r.json()["data"][video_url]["video_ok"]
        return bool(ok), None
    except Exception as e:
        return False, f"video check parse error: {e}"

def _sportai_submit(video_url):
    r = requests.post(
        "https://api.sportai.com/api/statistics",
        json={"video_url": video_url, "only_in_rally_data": False, "version": "stable"},
        headers={"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"},
        timeout=60,
    )
    if r.status_code not in (200, 201, 202):
        return None, f"SportAI submit HTTP {r.status_code}: {r.text}"
    return r.json()["data"]["task_id"], None

def _sportai_status(task_id):
    r = requests.get(
        f"https://api.sportai.com/api/statistics/{task_id}/status",
        headers={"Authorization": f"Bearer {SPORTAI_TOKEN}"},
        timeout=30,
    )
    if r.status_code != 200:
        return None, None, f"status HTTP {r.status_code}: {r.text}"
    j = r.json().get("data", {})
    return j.get("status"), j.get("progress"), None

def _download_result_and_ingest(task_id, save_prefix):
    meta = requests.get(
        f"https://api.sportai.com/api/statistics/{task_id}",
        headers={"Authorization": f"Bearer {SPORTAI_TOKEN}"},
        timeout=60,
    )
    if meta.status_code != 200:
        return
    result_url = meta.json()["data"]["result_url"]
    res = requests.get(result_url, timeout=300)
    if res.status_code != 200:
        return

    os.makedirs("data", exist_ok=True)
    fn = os.path.join("data", f"{save_prefix}_{task_id}.json")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(res.text)

    # Auto-ingest into your DB via existing backend
    if OPS_KEY:
        try:
            with open(fn, "rb") as fh:
                files = {"file": (os.path.basename(fn), fh, "application/json")}
                requests.post(
                    f"{BASE_URL}/ops/ingest-file",
                    params={"key": OPS_KEY, "replace": "1"},
                    files=files,
                    timeout=300,
                )
        except Exception:
            pass

def _poll_and_download(task_id, save_prefix):
    for _ in range(120):  # ~10 minutes
        st, prog, err = _sportai_status(task_id)
        if st in ("done", "completed"):
            _download_result_and_ingest(task_id, save_prefix)
            break
        if st == "failed":
            break
        time.sleep(5)

# ---------- Routes ----------
@ui_bp.get("/")
def upload_page():
    return render_template(
        "upload.html",
        max_upload_mb=MAX_UPLOAD_MB,
        dropbox_ready=bool(DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH_TOKEN),
        sportai_ready=bool(SPORTAI_TOKEN),
        target_folder=DROPBOX_TARGET_FOLDER,
    )

@ui_bp.post("/")
def upload_and_analyze():
    """Accept ONLY a local file, upload it into YOUR Dropbox, then send to SportAI."""
    if not SPORTAI_TOKEN:
        return jsonify({"error": "SPORT_AI_TOKEN not configured on server"}), 500

    email = (request.form.get("email") or "").strip().replace("@", "_at_")
    file = request.files.get("video")

    if not file or not file.filename:
        return jsonify({"error": "Please choose a .mp4/.mov file to upload."}), 400

    # Client-side should limit size, but guard here too if Content-Length present
    clen = request.content_length
    if clen and clen > MAX_UPLOAD_MB * 1024 * 1024:
        return jsonify({"error": f"File exceeds server limit of {MAX_UPLOAD_MB} MB."}), 413

    # Upload into YOUR Dropbox, then generate direct URL
    link, err = _dbx_upload_and_link(file)
    if err:
        return jsonify({"error": err}), 502

    ok, err = _sportai_check(link)
    if not ok:
        return jsonify({"error": err or "Video not accepted by SportAI"}), 400

    task_id, err = _sportai_submit(link)
    if not task_id:
        return jsonify({"error": err or "Submit to SportAI failed"}), 502

    prefix = f"sportai_{email or 'anon'}_{int(time.time())}"
    Thread(target=_poll_and_download, args=(task_id, prefix), daemon=True).start()

    return jsonify({
        "message": "OK. Uploaded to our Dropbox and submitted to SportAI.",
        "dropbox_url": link,
        "task_id": task_id,
        "sportai_task_id": task_id,
    })

@ui_bp.get("/task_status/<task_id>")
def ui_task_status(task_id):
    st, prog, err = _sportai_status(task_id)
    if err:
        return jsonify({"error": err}), 502
    mapped = "completed" if st in ("done", "completed") else ("failed" if st == "failed" else st or "queued")
    progress = float(prog) if isinstance(prog, (int, float)) else (1.0 if mapped in ("completed", "failed") else 0.5)
    return jsonify({"data": {"task_status": mapped, "task_progress": progress}})
