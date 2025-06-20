from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
import os
import json
from datetime import datetime
import requests

app = Flask(__name__)
CORS(app, resources={r"/upload": {"origins": "https://www.nextpointtennis.com"}})

# Dropbox secure credentials (use Render's environment variables)
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
SPORT_AI_TOKEN = os.getenv("SPORT_AI_TOKEN") or "your-sportai-token"

# 🔁 Get new Dropbox access token using refresh token
def get_fresh_dropbox_token():
    response = requests.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": DROPBOX_REFRESH_TOKEN,
            "client_id": DROPBOX_APP_KEY,
            "client_secret": DROPBOX_APP_SECRET
        }
    )
    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        print("❌ Dropbox token refresh failed:", response.text)
        return None

# ---- PAGE: Upload Form ----
@app.route("/")
def index():
    return render_template("upload.html")

# ---- FILE UPLOAD LOGIC ----
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("video")

    if not file:
        return render_template("upload.html", message="❌ No file uploaded.")

    # 🔐 Always get fresh access token
    access_token = get_fresh_dropbox_token()
    if not access_token:
        return render_template("upload.html", message="❌ Dropbox authorization failed.")

    file_bytes = file.read()
    filename = file.filename
    dropbox_path = f"/wix-uploads/{filename}"

    # 📤 Upload to Dropbox
    upload_res = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Dropbox-API-Arg": json.dumps({
                "path": dropbox_path,
                "mode": "add",
                "autorename": True
            }),
            "Content-Type": "application/octet-stream"
        },
        data=file_bytes
    )

    if not upload_res.ok:
        return render_template("upload.html", message="❌ Dropbox upload failed.")

    # 🔗 Create share link
    share_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json={
            "path": dropbox_path,
            "settings": {
                "requested_visibility": "public"
            }
        }
    )

    if not share_res.ok:
        return render_template("upload.html", message="❌ Failed to create Dropbox link.")

    raw_url = share_res.json()["url"].replace("dl=0", "raw=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")

    # 📡 Optional: Send to Sport AI
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
        return render_template("upload.html", message="✅ Upload successful & sent to Sport AI!")
    else:
        return render_template("upload.html", message="⚠️ Uploaded to Dropbox, but Sport AI failed.")

# ---- Run locally ----
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
