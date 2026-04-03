import os
from flask import Flask, request, jsonify

from upload_app import _do_ingest, _resolve_result_url_for_task

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
        app.logger.warning("INGEST WORKER unauthorized request")
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    task_id = str(body.get("task_id") or "").strip()
    supplied_result_url = str(body.get("result_url") or "").strip()

    app.logger.info(
        "INGEST WORKER REQUEST task_id=%s supplied_result_url_present=%s",
        task_id,
        bool(supplied_result_url),
    )

    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    result_url = supplied_result_url or _resolve_result_url_for_task(task_id)
    if not result_url:
        app.logger.warning("INGEST WORKER result_url unavailable task_id=%s", task_id)
        return jsonify({
            "ok": False,
            "error": "result_url_not_available",
            "task_id": task_id,
        }), 400

    app.logger.info(
        "INGEST WORKER START task_id=%s resolved_result_url_present=%s",
        task_id,
        True,
    )

    ok = _do_ingest(task_id, result_url)

    return jsonify({
        "ok": bool(ok),
        "task_id": task_id,
    }), (200 if ok else 500)