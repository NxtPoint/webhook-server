from flask import Flask, request, jsonify, make_response, render_template
import requests
import os

print("✅ Flask app is launching...")
print("🔥 Hello from inside app.py")

app = Flask(__name__)
SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"
ALLOWED_ORIGINS = [
    "https://www.nextpointtennis.com",
    "https://nextpointtennis.com"
]

def set_cors_headers(response, origin):
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['OPTIONS', 'POST'])
def upload():
    origin = request.headers.get('Origin', '')
    if request.method == 'OPTIONS':
        return set_cors_headers(make_response('', 204), origin)

    if origin not in ALLOWED_ORIGINS:
        return jsonify({"error": "Origin not allowed"}), 403

    data = request.get_json()
    dropbox_link = data.get('dropbox_link')

    if not dropbox_link:
        return set_cors_headers(jsonify({"error": "Missing dropbox_link"}), origin), 400

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
        res = jsonify({"message": "Task created", "task_id": task_id}), 201
    else:
        res = jsonify({
            "error": "Upload failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

    if isinstance(res, tuple):
        return set_cors_headers(make_response(res[0], res[1]), origin)
    else:
        return set_cors_headers(make_response(res), origin)

@app.route('/status', methods=['OPTIONS', 'POST'])
def status():
    origin = request.headers.get('Origin', '')
    if request.method == 'OPTIONS':
        return set_cors_headers(make_response('', 204), origin)

    if origin not in ALLOWED_ORIGINS:
        return jsonify({"error": "Origin not allowed"}), 403

    data = request.get_json()
    task_id = data.get('task_id')

    if not task_id:
        return set_cors_headers(jsonify({"error": "Missing task_id"}), origin), 400

    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}"
    }

    url = f"https://api.sportai.com/api/activity_detection/task/{task_id}"
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        res = jsonify(response.json())
    else:
        res = jsonify({
            "error": "Status check failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

    if isinstance(res, tuple):
        return set_cors_headers(make_response(res[0], res[1]), origin)
    else:
        return set_cors_headers(make_response(res), origin)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
