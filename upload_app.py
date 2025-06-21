from flask import Flask, request, jsonify, render_template
import requests
import os
import json
from datetime import datetime

app = Flask(__name__)

SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")

def get_dropbox_access_token():
    print("🔄 Attempting to refresh Dropbox access token...")
    response = requests.post(
        "https://api.dropbox.com/oauth2/token",
        auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET),
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN
        }
    )
    if response.status_code == 200:
        token = response.json().get("access_token")
        print("✅ Dropbox token refreshed successfully.")
        return token
    else:
        print("❌ Dropbox token refresh failed.")
        print(response.text)
        return None

def check_video_accessibility(video_url):
    print("🔍 Checking video accessibility with Sport AI...")
    url = "https://api.sportai.com/api/videos/check"
    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "version": "stable",
        "video_urls": [video_url]
    }
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        print("✅ Video passed accessibility check.")
        return True
    else:
        print("❌ Video failed accessibility check.")
        print(response.text)
        return False

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'video' not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    file = request.files['video']
    file_name = file.filename
    file_bytes = file.read()
    dropbox_path = f"/wix-uploads/{file_name}"

    access_token = get_dropbox_access_token()
    if not access_token:
        return jsonify({"error": "Unable to refresh Dropbox token"}), 500

    upload_res = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {access_token}",
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
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json={"path": dropbox_path, "settings": {"requested_visibility": "public"}}
    )

    if link_res.status_code != 200:
        error_data = link_res.json()
        if error_data.get('error', {}).get('.tag') == 'shared_link_already_exists':
            existing_link_res = requests.post(
                "https://api.dropboxapi.com/2/sharing/list_shared_links",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                json={"path": dropbox_path, "direct_only": True}
            )
            if existing_link_res.status_code == 200:
                link_data = existing_link_res.json()
                raw_url = link_data['links'][0]['url'].replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")
            else:
                return jsonify({"error": "Failed to retrieve existing shared link", "details": existing_link_res.text}), 500
        else:
            return jsonify({"error": "Failed to create Dropbox link", "details": link_res.text}), 500
    else:
        link_data = link_res.json()
        raw_url = link_data['url'].replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    if not check_video_accessibility(raw_url):
        return jsonify({"error": "Video failed validation with Sport AI"}), 400

    payload = {
        "video_url": raw_url,
        "version": "latest"
    }
    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}",
        "Content-Type": "application/json"
    }

    query_params = {
        "min_activity_window": request.args.get("min_activity_window", "30"),
        "min_no_activity_window": request.args.get("min_no_activity_window", "10"),
        "n_players_threshold": request.args.get("n_players_threshold", "1")
    }

    ai_response = requests.post(
        "https://api.sportai.com/api/activity_detection",
        json=payload,
        headers=headers,
        params=query_params
    )

    if ai_response.status_code == 201:
        task_id = ai_response.json()['data']['task_id']

        status_url = f"https://api.sportai.com/api/activity_detection/{task_id}/status"
        result_url = f"https://api.sportai.com/api/activity_detection/{task_id}"

        status_headers = {
            "Authorization": f"Bearer {SPORT_AI_TOKEN}"
        }

        status_response = requests.get(status_url, headers=status_headers)
        result_response = requests.get(result_url, headers=status_headers)

        return jsonify({
            "message": "Upload successful",
            "dropbox_path": dropbox_path,
            "sportai_task_id": task_id,
            "initial_status": status_response.json(),
            "initial_result": result_response.json()
        }), 201

    else:
        return jsonify({
            "error": "Failed to trigger Sport AI",
            "status": ai_response.status_code,
            "details": ai_response.text
        }), ai_response.status_code

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
