from flask import Flask, request, jsonify, send_from_directory
import os
import json
from datetime import datetime

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("✅ Received JSON data:", data)
    os.makedirs('data', exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    filename = f"data/sportai-{timestamp}.json"
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)
    return jsonify({"message": "Webhook received and saved"}), 200

@app.route('/files', methods=['GET'])
def list_files():
    folder = 'data'
    if not os.path.exists(folder):
        return jsonify({"files": []})
    files = sorted(os.listdir(folder))
    return jsonify({"files": files})

@app.route('/data/<filename>', methods=['GET'])
def get_file(filename):
    return send_from_directory('data', filename)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
