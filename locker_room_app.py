# locker_room_app.py — Minimal Flask server for Locker Room dashboard
# Serves locker_room.html as a static single-page app.
# No DB connection — all data comes from the webhook-server API.

import os
from flask import Flask, send_file, jsonify

app = Flask(__name__)


@app.get("/")
def index():
    return send_file("locker_room.html")


@app.get("/__alive")
def alive():
    return jsonify({"ok": True, "service": "locker-room"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True)
