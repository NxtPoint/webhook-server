from flask import Flask, request, jsonify, render_template
import requests
import os
import json

app = Flask(__name__)

SPORT_AI_TOKEN = os.environ.get("SPORT_AI_TOKEN")

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

    DROPBOX_TOKEN = os.environ.get("DROPBOX_TOKEN")
    dropbox_path = f"/wix-uploads/{file_name}"

    # ✅ Upload to Dropbox
    upload_res = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {DROPBOX_TOKEN}",
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

    print("✅ Dropbox upload successful")

    # ✅ Try to create a shared link (or get existing one)
    shared_link = None
    link_url = "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings"
    headers = {
        "Authorization": f"Bearer {DROPBOX_TOKEN}",
        "Content-Type": "application/json"
    }
    link_payload = {
        "path": dropbox_path,
        "settings": {"requested_visibility": "public"}
    }

    link_res = requests.post(link_url, headers=headers, json=link_payload)
    if link_res.status_code == 200:
        shared_link = link_res.json().get("url")
    elif "shared_link_already_exists" in link_res.text:
        # ✅ Fallback: Get the existing shared link
        existing_link_res = requests.post(
            "https://api.dropboxapi.com/2/sharing/list_shared_links",
            headers=headers,
            json={"path": dropbox_path, "direct_only": True}
        )
        if existing_link_res.status_code == 200:
            shared_link = existing_link_res.json()["links"][0]["url"]
        else:
            return jsonify({"error": "Failed to retrieve existing Dropbox link"}), 500
    else:
        return jsonify({
            "error": "Failed to create Dropbox link",
            "details": link_res.text
        }), 500

    # ✅ Clean the URL
    raw_url = shared_link.replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    # ✅ Register task with SportAI
    sportai_res = requests.post(
        "https://api.sportai.com/api/activity_detection",
        headers={
            "Authorization": f"Bearer {SPORT_AI_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "video_url": raw_url,
            "version": "latest"
        }
    )

    if sportai_res.status_code == 201:
        task_id = sportai_res.json()['data']['task_id']
        return jsonify({
            "message": "Upload successful",
            "dropbox_path": dropbox_path,
            "sportai_task_id": task_id
        }), 201
    else:
        return jsonify({
            "error": "Failed to trigger Sport AI",
            "status": sportai_res.status_code,
            "details": sportai_res.text
        }), sportai_res.status_code

# ✅ For Render deployment
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
