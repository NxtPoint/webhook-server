from flask import Flask, request, jsonify, render_template, send_file
import requests
import os
import json
from datetime import datetime
from werkzeug.utils import secure_filename
from threading import Thread
import time

app = Flask(__name__)

# Environment variables
SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")

def get_dropbox_access_token():
    res = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
            "client_id": DROPBOX_APP_KEY,
            "client_secret": DROPBOX_APP_SECRET
        }
    )
    if res.status_code in [200, 201, 202]:
        return res.json()['access_token']
    print("❌ Dropbox token refresh failed:", res.text)
    return None

def check_video_accessibility(video_url):
    res = requests.post(
        "https://api.sportai.com/api/videos/check",
        json={"version": "stable", "video_urls": [video_url]},
        headers={"Authorization": f"Bearer {SPORT_AI_TOKEN}", "Content-Type": "application/json"}
    )
    if res.status_code not in [200, 201, 202]:
        return False, "Video is not accessible"
    try:
        resp_json = res.json()
        inner = resp_json["data"][video_url]
        if not inner.get("video_ok", False):
            return False, "Video quality too low"
        return True, None
    except Exception as e:
        return False, f"Video check failed: {str(e)}"

@app.route('/')
def index():
    return render_template("upload.html")

@app.route('/upload', methods=['POST'])
def upload():
    if 'video' not in request.files or 'email' not in request.form:
        return jsonify({"error": "Video and email are required"}), 400

    email = request.form['email'].strip().replace("@", "_at_").replace(".", "_")
    video = request.files['video']
    file_name = video.filename
    file_bytes = video.read()
    dropbox_path = f"/wix-uploads/{file_name}"

    token = get_dropbox_access_token()
    if not token:
        return jsonify({"error": "Dropbox token refresh failed"}), 500

    upload_res = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({
                "path": dropbox_path,
                "mode": "add",
                "autorename": True,
                "mute": False
            }),
            "Content-Type": "application/octet-stream"
        },
        data=file_bytes
    )

    if not upload_res.ok:
        return jsonify({"error": "Dropbox upload failed", "details": upload_res.text}), 500

    link_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"path": dropbox_path, "settings": {"requested_visibility": "public"}}
    )

    if link_res.status_code not in [200, 201, 202]:
        err = link_res.json()
        if err.get('error', {}).get('.tag') == 'shared_link_already_exists':
            link_data = requests.post(
                "https://api.dropboxapi.com/2/sharing/list_shared_links",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"path": dropbox_path, "direct_only": True}
            ).json()
            raw_url = link_data['links'][0]['url']
        else:
            return jsonify({"error": "Failed to generate Dropbox link"}), 500
    else:
        raw_url = link_res.json()['url']

    raw_url = raw_url.replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    return jsonify({
        "message": "Uploaded successfully",
        "dropbox_url": raw_url
    }), 200

@app.route('/check_video', methods=['POST'])
def check_video():
    data = request.get_json()
    video_url = data.get("video_url")
    if not video_url:
        return jsonify({"error": "Missing video URL"}), 400

    is_ok, error = check_video_accessibility(video_url)
    if not is_ok:
        return jsonify({"error": error}), 400
    return jsonify({"message": "Video quality is OK"}), 200

@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    video_url = data.get("video_url")
    if not video_url:
        return jsonify({"error": "Missing video URL"}), 400

    payload = {
        "video_url": video_url,
        "only_in_rally_data": False,
        "version": "stable"
    }

    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}",
        "Content-Type": "application/json"
    }

    res = requests.post("https://api.sportai.com/api/statistics", json=payload, headers=headers)

    if res.status_code not in [200, 201, 202]:
        return jsonify({"error": "Statistics task registration failed", "details": res.text}), 500

    try:
        task_id = res.json()["data"]["task_id"]
        return jsonify({"sportai_task_id": task_id}), 200
    except Exception as e:
        return jsonify({"error": "Task ID parsing error", "details": str(e)}), 500

@app.route('/task_status/<task_id>')
def task_status(task_id):
    url = f"https://api.sportai.com/api/statistics/{task_id}/status"
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
    response = requests.get(url, headers=headers)
    return jsonify(response.json()), response.status_code

@app.route('/get_result/<task_id>')
def get_result(task_id):
    url = f"https://api.sportai.com/api/statistics/{task_id}"
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
    response = requests.get(url, headers=headers)
    return jsonify(response.json()), response.status_code

# ✅ FINALIZE: Triggers polling in the background
@app.route('/finalize/<task_id>/<email>', methods=['GET'])
def finalize(task_id, email):
    email_sanitized = email.strip().replace("@", "_at_").replace(".", "_")
    thread = Thread(target=poll_and_save_result, args=(task_id, email_sanitized))
    thread.start()
    return jsonify({"message": "Polling started in background"}), 200

def poll_and_save_result(task_id, email):
    print(f"⏳ Polling started for task {task_id}...")
    max_attempts = 60
    delay = 10
    url = f"https://api.sportai.com/api/statistics/{task_id}/status"
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}

    for attempt in range(max_attempts):
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            status = res.json()["data"].get("status")
            print(f"🔄 Attempt {attempt+1}: Status = {status}")
            if status == "done":
                filename = fetch_and_save_result(task_id, email)
                if filename:
                    print(f"✅ Result saved to {filename}")
                return
        else:
            print("⚠️ Failed to check status:", res.text)
        time.sleep(delay)
    print("❌ Polling timed out after waiting.")

def fetch_and_save_result(task_id, email):
    try:
        url = f"https://api.sportai.com/api/statistics/{task_id}"
        headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
        meta_res = requests.get(url, headers=headers)

        if meta_res.status_code in [200, 201]:
            result_url = meta_res.json()["data"]["result_url"]
            result_res = requests.get(result_url)

            if result_res.status_code == 200:
                os.makedirs("data", exist_ok=True)
                timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
                filename = f"data/sportai-{task_id}-{email}-{timestamp}.json"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(result_res.text)
                return filename
            else:
                print(f"❌ Failed to download result JSON. Status: {result_res.status_code}")
        else:
            print(f"❌ Failed to get metadata. Status: {meta_res.status_code}")
    except Exception as e:
        print("❌ Exception in fetch_and_save_result:", str(e))
    return None

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        os.makedirs("data", exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        filename = f"data/sportai-webhook-{timestamp}.json"
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
        return jsonify({"message": "Webhook data saved", "filename": filename}), 200
    except Exception as e:
        print("❌ Webhook error:", str(e))
        return jsonify({"error": "Failed to save webhook data"}), 500

@app.route('/results', methods=['GET'])
def list_results():
    try:
        files = sorted(os.listdir('data'), reverse=True)
        json_files = [f for f in files if f.endswith('.json')]
        return jsonify({"results": json_files})
    except Exception as e:
        return jsonify({"error": "Could not list result files", "details": str(e)}), 500

@app.route('/download/<filename>', methods=['GET'])
def download_result(filename):
    try:
        safe_name = secure_filename(filename)
        filepath = os.path.join('data', safe_name)
        if not os.path.exists(filepath):
            return jsonify({"error": "File not found"}), 404
        return send_file(filepath, as_attachment=True)
    except Exception as e:
        return jsonify({"error": "Download failed", "details": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
