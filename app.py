from flask import Flask, request, jsonify, render_template
import requests
import os
import json

print("Flask app is launching...")

app = Flask(__name__)

SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")


# 🔄 Get fresh Dropbox token using refresh token
def get_fresh_dropbox_token():
    response = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
            "client_id": DROPBOX_APP_KEY,
            "client_secret": DROPBOX_APP_SECRET,
        },
    )
    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        print("❌ Dropbox token refresh failed:", response.text)
        return None


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

    access_token = get_fresh_dropbox_token()
    if not access_token:
        return jsonify({"error": "Dropbox token refresh failed"}), 401

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
        return jsonify({
            "error": "Dropbox upload failed",
            "status": upload_res.status_code,
            "details": upload_res.text
        }), 500

    # Step 2: Try to create a share link, fallback to get existing if it already exists
    link_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json={"path": dropbox_path, "settings": {"requested_visibility": "public"}}
    )

    if link_res.status_code != 200:
        link_json = link_res.json()
        if link_json.get('error', {}).get('.tag') == 'shared_link_already_exists':
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
                return jsonify({
                    "error": "Failed to retrieve existing shared link",
                    "details": existing_link_res.text
                }), 500
        else:
            return jsonify({
                "error": "Failed to create Dropbox link",
                "details": link_res.text
            }), 500
    else:
        link_data = link_res.json()
        raw_url = link_data['url'].replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    # Step 3: Send to Sport AI
    payload = {
        "video_url": raw_url,
        "version": "latest"
    }

    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}",
        "Content-Type": "application/json"
    }

    ai_response = requests.post("https://api.sportai.com/api/statistics", json=payload, headers=headers)

    if ai_response.status_code == 201:
        task_id = ai_response.json()['data']['task_id']
        return jsonify({
            "message": "Upload successful",
            "dropbox_path": dropbox_path,
            "sportai_task_id": task_id
        }), 201
    else:
        return jsonify({
            "error": "Failed to trigger Sport AI",
            "status": ai_response.status_code,
            "details": ai_response.text
        }), ai_response.status_code


@app.route("/check_status/<task_id>", methods=["GET"])
def check_status(task_id):
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
    url = f"https://api.sportai.com/api/statistics/{task_id}/status"
    response = requests.get(url, headers=headers)
    return jsonify(response.json()), response.status_code


@app.route("/get_result/<task_id>", methods=["GET"])
def get_result(task_id):
    headers = {"Authorization": f"Bearer {SPORT_AI_TOKEN}"}
    url = f"https://api.sportai.com/api/statistics/{task_id}"
    response = requests.get(url, headers=headers)
    return jsonify(response.json()), response.status_code


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
