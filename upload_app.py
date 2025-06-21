from flask import Flask, request, jsonify, render_template
import requests
import os
import json

app = Flask(__name__)
print("🚀 Flask app is launching...")

# ✅ Load env vars
DROPBOX_REFRESH_TOKEN = os.environ.get("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.environ.get("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.environ.get("DROPBOX_APP_SECRET")
SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")


# ✅ Utility to refresh Dropbox access token
def get_new_dropbox_token():
    token_res = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
            "client_id": DROPBOX_APP_KEY,
            "client_secret": DROPBOX_APP_SECRET,
        }
    )
    if token_res.status_code == 200:
        return token_res.json()['access_token']
    else:
        print("❌ Failed to refresh Dropbox token:", token_res.text)
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

    access_token = get_new_dropbox_token()
    if not access_token:
        return jsonify({"error": "Unable to refresh Dropbox token"}), 500

    dropbox_path = f"/wix-uploads/{file_name}"

    # ✅ Upload to Dropbox
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

    print("✅ Uploaded to Dropbox:", dropbox_path)

    # ✅ Try to create shared link
    link_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json={"path": dropbox_path, "settings": {"requested_visibility": "public"}}
    )

    # ✅ If shared link already exists, fetch it
    if link_res.status_code != 200:
        error_tag = link_res.json().get('error', {}).get('.tag')
        if error_tag == 'shared_link_already_exists':
            print("🔁 Shared link already exists, retrieving it...")
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

    # ✅ Submit to SportAI
    payload = {
        "video_url": raw_url,
        "version": "latest"
    }
    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}",
        "Content-Type": "application/json"
    }
    ai_response = requests.post("https://api.sportai.com/api/activity_detection", json=payload, headers=headers)

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


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
