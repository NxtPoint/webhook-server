# ui_app.py
import os, time, json, requests
from threading import Thread
from flask import Blueprint, request, render_template, jsonify

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

# Dropbox OAuth (MUST be YOUR Dropbox account)
DBX_APP_KEY       = os.getenv("DROPBOX_APP_KEY", "")
DBX_APP_SECRET    = os.getenv("DROPBOX_APP_SECRET", "")
DBX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN", "")

# Where file requests drop videos inside YOUR Dropbox (e.g. "/uploads/incoming")
DROPBOX_TARGET_FOLDER = os.getenv("DROPBOX_TARGET_FOLDER", "/uploads")

# A public File Request link that drops files into DROPBOX_TARGET_FOLDER
# (Create this in Dropbox → File requests, point to the same folder)
DROPBOX_FILE_REQUEST_URL = os.getenv("DROPBOX_FILE_REQUEST_URL", "")

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

def _dbx_list_latest_video(token: str, folder_path: str):
    """
    List files in folder_path and return the most-recent .mp4/.mov entry.
    Chooses by server_modified (fallback client_modified).
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url_list = "https://api.dropboxapi.com/2/files/list_folder"
    url_cont = "https://api.dropboxapi.com/2/files/list_folder/continue"

    body = {
        "path": folder_path,
        "recursive": False,
        "include_non_downloadable_files": False,
    }
    entries = []
    r = requests.post(url_list, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        return None, f"Dropbox list error: {r.text}"
    j = r.json()
    entries.extend(j.get("entries", []))
    while j.get("has_more"):
        r = requests.post(url_cont, headers=headers, json={"cursor": j["cursor"]}, timeout=30)
        if r.status_code != 200:
            return None, f"Dropbox list-continue error: {r.text}"
        j = r.json()
        entries.extend(j.get("entries", []))

    vids = []
    for e in entries:
        if e.get(".tag") != "file":
            continue
        nm = str(e.get("name", "")).lower()
        if not (nm.endswith(".mp4") or nm.endswith(".mov")):
            continue
        vids.append(e)

    if not vids:
        return None, "No .mp4 or .mov files found in the Dropbox target folder."

    def _ts(e):
        return e.get("server_modified") or e.get("client_modified") or ""
    vids.sort(key=_ts, reverse=True)
    latest = vids[0]
    return latest, None

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
    # Surface helpful info in the template
    return render_template(
        "upload.html",
        file_request_url=DROPBOX_FILE_REQUEST_URL,
        target_folder=DROPBOX_TARGET_FOLDER,
        sportai_ready=bool(SPORTAI_TOKEN),
        dbx_ready=bool(DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH_TOKEN),
    )

@ui_bp.post("/analyze-latest")
def analyze_latest():
    """Pick the most recent .mp4/.mov from YOUR Dropbox target folder and send to SportAI."""
    if not SPORTAI_TOKEN:
        return jsonify({"error": "SPORT_AI_TOKEN not configured on server"}), 500

    email = (request.form.get("email") or "").strip().replace("@", "_at_")

    token, err = _dbx_access_token()
    if err:
        return jsonify({"error": err}), 500

    latest, err = _dbx_list_latest_video(token, DROPBOX_TARGET_FOLDER)
    if err:
        return jsonify({"error": err}), 400

    path = latest.get("path_lower") or latest.get("path_display")
    name = latest.get("name")
    if not path:
        return jsonify({"error": "Could not resolve Dropbox path for the latest video."}), 400

    direct_url, err = _dbx_create_or_get_shared_link(token, path)
    if err:
        return jsonify({"error": err}), 400

    ok, err = _sportai_check(direct_url)
    if not ok:
        return jsonify({"error": err or "Video not accepted by SportAI"}), 400

    task_id, err = _sportai_submit(direct_url)
    if not task_id:
        return jsonify({"error": err or "Submit to SportAI failed"}), 502

    prefix = f"sportai_{email or 'anon'}_{int(time.time())}"
    Thread(target=_poll_and_download, args=(task_id, prefix), daemon=True).start()

    return jsonify({
        "message": "OK. Submitted latest Dropbox video to SportAI.",
        "dropbox_path": path,
        "dropbox_name": name,
        "dropbox_url": direct_url,
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
