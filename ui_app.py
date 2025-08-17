# ui_app.py
import os, io, time, json, requests
from threading import Thread
from flask import Flask, request, render_template, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-ui")

BASE_URL = os.getenv("BASE_URL", "https://api.nextpointtennis.com")
OPS_KEY  = os.getenv("OPS_KEY", "")

# Auth/config
SPORTAI_TOKEN       = os.getenv("SPORT_AI_TOKEN") or os.getenv("SPORTAI_TOKEN", "")
DBX_APP_KEY         = os.getenv("DROPBOX_APP_KEY", "")
DBX_APP_SECRET      = os.getenv("DROPBOX_APP_SECRET", "")
DBX_REFRESH_TOKEN   = os.getenv("DROPBOX_REFRESH_TOKEN", "")

# ---------- Dropbox ----------
def _dbx_access_token():
    if not (DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH_TOKEN):
        return None, "Dropbox credentials not configured"
    r = requests.post("https://api.dropbox.com/oauth2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": DBX_REFRESH_TOKEN,
        "client_id": DBX_APP_KEY,
        "client_secret": DBX_APP_SECRET,
    }, timeout=30)
    if r.status_code != 200:
        return None, r.text
    return r.json().get("access_token"), None

def _dbx_upload_and_link(file_storage):
    token, err = _dbx_access_token()
    if err: return None, err
    filename = secure_filename(file_storage.filename or f"upload_{int(time.time())}.mp4")
    path = f"/uploads/{filename}"
    data = file_storage.read()

    up = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite", "mute": True}),
            "Content-Type": "application/octet-stream",
        },
        data=data, timeout=600
    )
    if up.status_code != 200:
        return None, up.text

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sh = requests.post("https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
                       headers=headers, json={"path": path, "settings": {"requested_visibility": "public"}}, timeout=30)
    if sh.status_code == 409:
        ls = requests.post("https://api.dropboxapi.com/2/sharing/list_shared_links",
                           headers=headers, json={"path": path, "direct_only": True}, timeout=30)
        if ls.status_code != 200: return None, ls.text
        url = ls.json()["links"][0]["url"]
    elif sh.status_code != 200:
        return None, sh.text
    else:
        url = sh.json()["url"]

    # normalize → direct bytes
    url = url.replace("www.dropbox.com", "dl.dropboxusercontent.com").replace("?dl=0", "")
    if "?raw=1" not in url:
        url = url + ("&raw=1" if "?" in url else "?raw=1")
    return url, None

# ---------- SportAI ----------
def _sportai_check(video_url):
    r = requests.post("https://api.sportai.com/api/videos/check",
                      json={"version": "stable", "video_urls": [video_url]},
                      headers={"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"},
                      timeout=60)
    if r.status_code != 200:
        return False, r.text
    try:
        ok = r.json()["data"][video_url]["video_ok"]
        return bool(ok), None
    except Exception as e:
        return False, str(e)

def _sportai_submit(video_url):
    r = requests.post("https://api.sportai.com/api/statistics",
                      json={"video_url": video_url, "only_in_rally_data": False, "version": "stable"},
                      headers={"Authorization": f"Bearer {SPORTAI_TOKEN}", "Content-Type": "application/json"},
                      timeout=60)
    if r.status_code not in (200, 201, 202):
        return None, r.text
    return r.json()["data"]["task_id"], None

def _sportai_status(task_id):
    r = requests.get(f"https://api.sportai.com/api/statistics/{task_id}/status",
                     headers={"Authorization": f"Bearer {SPORTAI_TOKEN}"}, timeout=30)
    if r.status_code != 200:
        return None, None, r.text
    j = r.json().get("data", {})
    return j.get("status"), j.get("progress"), None

def _ingest_local_json(filepath):
    if not OPS_KEY:
        return
    with open(filepath, "rb") as f:
        files = {"file": (os.path.basename(filepath), f, "application/json")}
        requests.post(f"{BASE_URL}/ops/ingest-file",
                      params={"key": OPS_KEY, "replace": "1"},
                      files=files, timeout=300)

def _poll_and_download(task_id, save_prefix):
    for _ in range(90):
        st, prog, err = _sportai_status(task_id)
        if st == "done":
            meta = requests.get(f"https://api.sportai.com/api/statistics/{task_id}",
                                headers={"Authorization": f"Bearer {SPORTAI_TOKEN}"}, timeout=60)
            if meta.status_code == 200:
                result_url = meta.json()["data"]["result_url"]
                res = requests.get(result_url, timeout=300)
                if res.status_code == 200:
                    os.makedirs("data", exist_ok=True)
                    fn = os.path.join("data", f"{save_prefix}_{task_id}.json")
                    with open(fn, "w", encoding="utf-8") as f:
                        f.write(res.text)
                    _ingest_local_json(fn)
            break
        if st == "failed":
            break
        time.sleep(5)

# ---------- Routes (mounted at /upload via wsgi.py) ----------
@app.get("/")
def page():
    return render_template(
        "upload.html",
        have_ops=bool(OPS_KEY),
        have_dbx=bool(DBX_APP_KEY and DBX_APP_SECRET and DBX_REFRESH_TOKEN),
        have_sportai=bool(SPORTAI_TOKEN),
        base_url=BASE_URL
    )

@app.post("/ui/upload-json")
def ui_upload_json():
    f = request.files.get("json_file")
    if not f:
        return render_template("result.html", title="Error", pre="Please choose a .json file.")
    if not OPS_KEY:
        return render_template("result.html", title="Server Error", pre="OPS_KEY not configured on server.")
    files = {"file": (f.filename, f.stream, f.mimetype or "application/json")}
    r = requests.post(f"{BASE_URL}/ops/ingest-file",
                      params={"key": OPS_KEY, "replace": "1"},
                      files=files, timeout=300)
    pre = r.text
    try:
        pre = json.dumps(r.json(), indent=2)
    except Exception:
        pass
    return render_template("result.html", title="JSON Ingest Result", pre=pre)

@app.post("/ui/upload-video")
def ui_upload_video():
    if not SPORTAI_TOKEN:
        return render_template("result.html", title="Error", pre="SPORT_AI_TOKEN not configured.")
    file = request.files.get("video_file")
    email = (request.form.get("email") or "").strip().replace("@", "_at_")
    if not file:
        return render_template("result.html", title="Error", pre="Please choose a video file.")

    link, err = _dbx_upload_and_link(file)
    if err:
        return render_template("result.html", title="Dropbox Error", pre=str(err))

    ok, err = _sportai_check(link)
    if not ok:
        return render_template("result.html", title="SportAI Check Failed", pre=str(err or "Video not acceptable"))

    task_id, err = _sportai_submit(link)
    if not task_id:
        return render_template("result.html", title="SportAI Submit Failed", pre=str(err))

    prefix = f"sportai_{email or 'anon'}_{int(time.time())}"
    Thread(target=_poll_and_download, args=(task_id, prefix), daemon=True).start()

    msg = f"Video uploaded.\nDropbox: {link}\nTask: {task_id}\n\nWe'll auto-download the JSON when ready and ingest it into the DB."
    return render_template("result.html", title="Submitted to SportAI", pre=msg)

@app.get("/ui/task-status/<task_id>")
def ui_task_status(task_id):
    st, prog, err = _sportai_status(task_id)
    if err:
        return jsonify({"error": err}), 502
    prog = prog if isinstance(prog, (int, float)) else (1.0 if st in ("done", "failed") else 0.5)
    return jsonify({"status": st, "progress": prog})
