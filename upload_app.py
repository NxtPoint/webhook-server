from flask import Flask, request, jsonify, make_response, render_template
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

    # Upload to Dropbox
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
    return jsonify({"message": "Upload successful", "path": dropbox_path})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
