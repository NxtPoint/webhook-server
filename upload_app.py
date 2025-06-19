from flask import Flask, request, jsonify, make_response, render_template
import requests
import os
import json

print("Flask app is launching...")
print("Hello from inside app.py")

app = Flask(__name__)
SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"
ALLOWED_ORIGINS = [
    "https://api.nextpointtennis.com",
    "https://www.nextpointtennis.com"
]

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['OPTIONS', 'POST'])
def upload():
    origin = request.headers.get("Origin", "")
    if request.method == 'OPTIONS':
        response = make_response('', 204)
        if origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
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

    if response.status_code == 201:
        task_id = response.json()['data']['task_id']
        final_response = jsonify({"message": "Task created", "task_id": task_id}), 201
    else:
        final_response = jsonify({
            "error": "Upload failed",
            "status": response.status_code,
            "details": response.text
        }), response.status_code

    res = make_response(*final_response) if isinstance(final_response, tuple) else make_response(final_response)
    if origin in ALLOWED_ORIGINS:
        res.headers["Access-Control-Allow-Origin"] = origin
    return res

@app.route('/status', methods=['OPTIONS', 'POST'])
def status():
    origin = request.headers.get("Origin", "")
    if request.method == 'OPTIONS':
        response = make_response('', 204)
        if origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
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

    res_obj = make_response(*res) if isinstance(res, tuple) else make_response(res)
    if origin in ALLOWED_ORIGINS:
        res_obj.headers["Access-Control-Allow-Origin"] = origin
    return res_obj

@app.route('/upload_from_wix', methods=['POST'])
def upload_from_wix():
    try:
        data = request.get_json()
        file_url = data.get("file_url")
        file_name = data.get("file_name", "upload.mp4")

        if not file_url:
            return jsonify({"error": "No file URL provided"}), 400

        print(f"Downloading file from Wix: {file_url}")

        # Download the file from Wix
        wix_file = requests.get(file_url)
        if wix_file.status_code != 200:
            return jsonify({"error": "Failed to download file from Wix"}), 500

        file_bytes = wix_file.content

        # Upload to Dropbox
        dropbox_path = f"/wix-uploads/{file_name}"
        DROPBOX_TOKEN = os.environ.get("DROPBOX_TOKEN")

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
            print(f"Dropbox upload failed: {upload_res.text}")
            return jsonify({"error": "Dropbox upload failed"}), 500

        print(f"Uploaded to Dropbox: {dropbox_path}")
        return jsonify({"message": "Upload successful", "path": dropbox_path})

    except Exception as e:
        print(f"Upload error: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
