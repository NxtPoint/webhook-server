from flask import Flask, request, jsonify, render_template
import requests
import os
import json

print("Flask app is launching...")

app = Flask(__name__)

SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"
ALLOWED_ORIGINS = [
    "https://api.nextpointtennis.com",
    "https://www.nextpointtennis.com"
]

# ✅ Show the form at the root URL
@app.route('/')
def index():
    return render_template('upload.html')


# ✅ Handle the form submission
@app.route('/upload', methods=['POST'])
def upload():
    if 'video' not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    file = request.files['video']
    file_name = file.filename
    file_bytes = file.read()

    # ✅ Upload to Dropbox
    DROPBOX_TOKEN = os.environ.get("DROPBOX_TOKEN")
    dropbox_path = f"/wix-uploads/{file_name}"

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
        print("❌ Dropbox upload failed!")
        print("📄 Status Code:", upload_res.status_code)
        print("📄 Response:", upload_res.text)
        print("📄 Headers:", upload_res.headers)
        return jsonify({
            "error": "Dropbox upload failed",
            "status": upload_res.status_code,
            "details": upload_res.text
        }), 500

    print("✅ Uploaded to Dropbox:", dropbox_path)

    # ✅ Create share link
    link_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={
            "Authorization": f"Bearer {DROPBOX_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"path": dropbox_path, "settings": {"requested_visibility": "public"}}
    )

    if link_res.status_code != 200:
        return jsonify({
            "error": "Failed to create Dropbox link",
            "details": link_res.text
        }), 500

    link_data = link_res.json()
    raw_url = link_data.get("url", "").replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    # ✅ Send to Sport AI
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


# ✅ Port binding for Render
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
