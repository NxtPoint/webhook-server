from flask import Flask, request, jsonify, render_template
import requests
import os
import json
import time

app = Flask(__name__)

# Environment variables
SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")


def get_dropbox_access_token():
    """🔄 Refresh Dropbox access token"""
    res = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
            "client_id": DROPBOX_APP_KEY,
            "client_secret": DROPBOX_APP_SECRET
        }
    )
    if res.status_code == 200:
        return res.json()['access_token']
    print("❌ Dropbox token refresh failed:", res.text)
    return None


def check_video_accessibility(video_url):
    """🧠 Confirm Dropbox video is readable by Sport AI"""
    res = requests.post(
        "https://api.sportai.com/api/videos/check",
        json={"version": "stable", "video_urls": [video_url]},
        headers={"Authorization": f"Bearer {SPORT_AI_TOKEN}", "Content-Type": "application/json"}
    )
    return res.status_code == 200


@app.route('/')
def index():
    return render_template("upload.html")


@app.route('/upload', methods=['POST'])
def upload():
    if 'video' not in request.files:
        return jsonify({"error": "No video uploaded"}), 400

    video = request.files['video']
    file_name = video.filename
    file_bytes = video.read()
    dropbox_path = f"/wix-uploads/{file_name}"

    # 🔐 Refresh Dropbox access token
    token = get_dropbox_access_token()
    if not token:
        return jsonify({"error": "Dropbox token refresh failed"}), 500

    # 📤 Upload to Dropbox
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

    # 🔗 Create or fetch shared link
    link_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"path": dropbox_path, "settings": {"requested_visibility": "public"}}
    )

    if link_res.status_code != 200:
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

    # 🧼 Clean URL
    raw_url = raw_url.replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    # ✅ Check video accessibility
    if not check_video_accessibility(raw_url):
        return jsonify({"error": "Video is not accessible by Sport AI"}), 400

    # 🎯 Send to Sport AI
    payload = {"video_url": raw_url, "version": "latest"}
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}", "Content-Type": "application/json"}
    res = requests.post("https://api.sportai.com/api/statistics", json=payload, headers=headers)

    if res.status_code != 201:
        return jsonify({"error": "Sport AI failed to accept video", "details": res.text}), 500

    task_id = res.json()['data']['task_id']
    return jsonify({"message": "Video sent to Sport AI", "sportai_task_id": task_id}), 201


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


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
