from flask import Flask, request, jsonify, render_template, send_file
import requests
import os
import json
from datetime import datetime
from werkzeug.utils import secure_filename

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
        return False, "Video is not accessible (status code != 200)"

    try:
        resp_json = res.json()
        inner = resp_json["data"][video_url]
        if not inner.get("video_ok", False):
            return False, "Video quality is too low for analysis"
        return True, None
    except Exception as e:
        return False, f"Video quality check failed to parse: {str(e)}"


@app.route('/')
def index():
    return render_template("upload.html")


@app.route('/upload', methods=['POST'])
def upload():
    if 'video' not in request.files or 'email' not in request.form:
        return jsonify({"error": "Video and email are required"}), 400

    email = request.form['email']
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

    # Submit to Sport AI
    payload = {"video_url": raw_url, "version": "latest"}
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}", "Content-Type": "application/json"}
    res = requests.post("https://api.sportai.com/api/activity_detection", json=payload, headers=headers)

    if res.status_code not in [200, 201, 202]:
        return jsonify({"error": "Activity detection failed", "details": res.text}), 500

    task_id = res.json().get("task_id")

    # Fetch result metadata after a delay (in real use, use polling or webhook)
    fetch_url = f"https://api.sportai.com/api/activity_detection/{task_id}"
    fetch_res = requests.get(fetch_url, headers=headers)
    if fetch_res.status_code == 200:
        result = fetch_res.json()
        os.makedirs("data", exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        filename = f"data/activity_result_{task_id}_{timestamp}.json"
        with open(filename, "w") as f:
            json.dump(result, f, indent=2)
    else:
        return jsonify({"error": "Fetch failed", "details": fetch_res.text}), 500

    return jsonify({
        "message": "Uploaded and analyzed successfully",
        "dropbox_url": raw_url,
        "task_id": task_id,
        "result_file": filename
    }), 200


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
