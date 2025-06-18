from flask import Flask, render_template, request, jsonify
import requests
import os
from flask_cors import CORS

print("✅ Flask app is launching...")
print("🔥 Hello from inside app.py")

app = Flask(__name__)
from flask_cors import CORS

CORS(app, resources={
    r"/upload": {
        "origins": "https://www.nextpointtennis.com",
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    },
    r"/status": {
        "origins": "https://www.nextpointtennis.com",
        "methods": ["POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload():
    data = request.get_json()
    dropbox_link = data.get('dropbox_link')

    if not dropbox_link:
        return jsonify({"error": "Missing dropbox_link"}), 400

    if "dl=0" in dropbox_link:
        dropbox_link = dropbox_link.replace("dl=0", "raw=1")
    elif "dl=1" in dropbox_link:
        dropbox_link = dropbox_link.replace("dl=1", "raw=1")
    elif "raw=1" not in dropbox_link:
        dropbox_link += "?raw=1"

    payload = {
        "video_url": dropbox_link,
        "version": "latest"
    }
    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post("https://api.sportai.com/api/activity_detection", json=payload, headers=headers)

    if response.status_code == 201:
        task_id = response.json()['data']['task_id']
        return jsonify({"message": "Task created", "task_id": task_id}), 201
    else:
        return jsonify({
            "error": f"Upload failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

@app.route('/status', methods=['POST'])
def check_status():
    data = request.get_json()
    task_id = data.get('task_id')

    if not task_id:
        return jsonify({"error": "Missing task_id"}), 400

    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}"
    }

    url = f"https://api.sportai.com/api/activity_detection/task/{task_id}"
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        return jsonify(response.json())
    else:
        return jsonify({
            "error": f"Status check failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
