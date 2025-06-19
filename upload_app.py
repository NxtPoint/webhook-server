from flask import Flask, request, jsonify, make_response, render_template
import requests
import os

print("✅ Flask app is launching...")
print("🔥 Hello from inside app.py")

app = Flask(__name__)
SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"
ALLOWED_ORIGIN = "https://api.nextpointtennis.com"


@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['OPTIONS', 'POST'])
def upload():
    if request.method == 'OPTIONS':
        response = make_response('', 204)
        response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return response

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

    final_response = None
    if response.status_code == 201:
        task_id = response.json()['data']['task_id']
        final_response = jsonify({"message": "Task created", "task_id": task_id}), 201
    else:
        final_response = jsonify({
            "error": "Upload failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

    if isinstance(final_response, tuple):
        res = make_response(final_response[0], final_response[1])
    else:
        res = make_response(final_response)

    res.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    return res

@app.route('/status', methods=['OPTIONS', 'POST'])
def status():
    if request.method == 'OPTIONS':
        response = make_response('', 204)
        response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return response

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
        res = jsonify(response.json())
    else:
        res = jsonify({
            "error": "Status check failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

    if isinstance(res, tuple):
        res_obj = make_response(res[0], res[1])
    else:
        res_obj = make_response(res)

    res_obj.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    return res_obj

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
