from flask import Flask, render_template, request, jsonify, make_response
import requests
import os

print("✅ Flask app is launching...")
print("🔥 Hello from inside app.py")

app = Flask(__name__)

SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"

# ✅ Add CORS headers to all responses (POST)
@app.after_request
def apply_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "https://www.nextpointtennis.com"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response

@app.route('/')
def index():
    return render_template('upload.html')

# ✅ Manually handle OPTIONS preflight with headers
@app.route('/upload', methods=['OPTIONS'])
def upload_options():
    response = make_response('', 204)
    response.headers["Access-Control-Allow-Origin"] = "https://www.nextpointtennis.com"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response

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
            "error": "Upload failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

# ✅ Manually handle preflight for /status
@app.route('/status', methods=['OPTIONS'])
def status_options():
    response = make_response('', 204)
    response.headers["Access-Control-Allow-Origin"] = "https://www.nextpointtennis.com"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response

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
            "error": "Status check failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
