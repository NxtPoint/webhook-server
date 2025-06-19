from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
import os
import json
from datetime import datetime
import requests

app = Flask(__name__)
CORS(app, resources={r"/upload": {"origins": "https://www.nextpointtennis.com"}})

# Set tokens (use real tokens or pull from environment)
DROPBOX_TOKEN = os.getenv("DROPBOX_TOKEN") or "your-dropbox-token"
SPORT_AI_TOKEN = os.getenv("SPORT_AI_TOKEN") or "your-sportai-token"

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

    file_bytes = file.read()
    filename = file.filename
    dropbox_path = f"/wix-uploads/{filename}"

    # Upload to Dropbox
    upload_res = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {DROPBOX_TOKEN}",
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

    # Create share link
    share_res = requests.post(
        "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
        headers={
            "Authorization": f"Bearer {DROPBOX_TOKEN}",
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
        return render_template("upload.html"_
