from flask import Flask, render_template, request
import requests
import os

print("✅ Flask app is launching...")
print("🔥 Hello from inside app.py")

app = Flask(__name__, template_folder='templates')

SPORT_AI_TOKEN = "qA3X6Tg6Ac8Gixyqv7eQTz999zoXvgRDlFTryanrST"

@app.route('/')
def index():
    return render_template('upload.html')


@app.route('/upload', methods=['POST'])
def upload():
    dropbox_link = request.form['dropbox_link']

    # Normalize Dropbox link to raw=1 for clean streaming
    if "dl=0" in dropbox_link:
        dropbox_link = dropbox_link.replace("dl=0", "raw=1")
    elif "dl=1" in dropbox_link:
        dropbox_link = dropbox_link.replace("dl=1", "raw=1")
    elif "raw=1" not in dropbox_link:
        dropbox_link += "?raw=1"

    # Send to Sport AI
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
        return render_template('upload.html', message=f"✅ Task created. Task ID: {task_id}")
    else:
        return render_template('upload.html', message=f"❌ Upload failed: {response.status_code} - {response.text}")


@app.route('/status', methods=['POST'])
def check_status():
    task_id = request.form['task_id']

    headers = {
        "Authorization": f"Bearer {SPORT_AI_TOKEN}"
    }

    url = f"https://api.sportai.com/api/activity_detection/tasks/{task_id}"

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        status_info = response.json()
        return render_template('upload.html', status=status_info)
    else:
        return render_template('upload.html', message=f"❌ Error checking status: {response.status_code} - {response.text}")


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
