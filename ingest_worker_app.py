import os
from flask import Flask, request, jsonify

# Reuse the proven ingest logic from upload_app
from upload_app import _do_ingest

app = Flask(__name__)

INGEST_WORKER_OPS_KEY = (os.getenv("INGEST_WORKER_OPS_KEY") or "").strip()

if not INGEST_WORKER_OPS_KEY:
    raise RuntimeError("INGEST_WORKER_OPS_KEY env var is required")

def _auth_ok(req) -> bool:
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {INGEST_WORKER_OPS_KEY}"

@app.get("/")
def root_ok():
    return jsonify({"ok": True, "service": "nextpoint-ingest-worker"})

@app.get("/healthz")
def healthz_ok():
    return "OK", 200

@app.post("/ingest")
def ingest():
    if not _auth_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}

    task_id = str(body.get("task_id") or "").strip()
    result_url = str(body.get("result_url") or "").strip()

    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400
    if not result_url:
        return jsonify({"ok": False, "error": "result_url required"}), 400

    ok = _do_ingest(task_id, result_url)

    return jsonify({
        "ok": bool(ok),
        "task_id": task_id,
        "result_url": result_url,
    }), (200 if ok else 500)