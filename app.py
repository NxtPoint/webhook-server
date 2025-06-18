
from flask_cors import CORS
from flask import Flask, request, jsonify, send_from_directory
import os
import json
from datetime import datetime

app = Flask(__name__)
CORS(app, origins=["https://www.nextpointtennis.com"])  # ✅ Allow Wix site)


# Route to handle incoming webhook from Sport AI
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("✅ Received JSON data")

    os.makedirs('data', exist_ok=True)

    # Save with timestamp for archiving
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    archive_filename = f"data/sportai-{timestamp}.json"
    with open(archive_filename, 'w') as f:
        json.dump(data, f, indent=2)

    # Save latest version to a fixed file
    latest_file = "data/latest.json"
    with open(latest_file, 'w') as f:
        json.dump(data, f, indent=2)

    return jsonify({"message": "✅ Webhook received and files saved"}), 200

# Route to list all saved files
@app.route('/files', methods=['GET'])
def list_files():
    folder = 'data'
    if not os.path.exists(folder):
        return jsonify({"files": []})
    files = sorted(os.listdir(folder))
    return jsonify({"files": files})

# Route to get a specific file (e.g. for Power BI)
@app.route('/data/<filename>', methods=['GET'])
def get_file(filename):
    return send_from_directory('data', filename)

# Shortcut route to get the latest JSON file
@app.route('/data/latest.json', methods=['GET'])
def get_latest():
    return send_from_directory('data', 'latest.json')

# Run the server (Render uses PORT environment variable)
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
