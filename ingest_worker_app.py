import os
import threading
from flask import Flask, request, jsonify

from upload_app import _do_ingest, _resolve_result_url_for_task

app = Flask(__name__)

INGEST_WORKER_OPS_KEY = (os.getenv("INGEST_WORKER_OPS_KEY") or "").strip()

if not INGEST_WORKER_OPS_KEY:
    raise RuntimeError("INGEST_WORKER_OPS_KEY env var is required")


def _auth_ok(req) -> bool:
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {INGEST_WORKER_OPS_KEY}"


def _run_ingest_async(task_id: str, result_url: str) -> None:
    try:
        app.logger.info("INGEST WORKER BACKGROUND START task_id=%s", task_id)
        _do_ingest(task_id, result_url)
        app.logger.info("INGEST WORKER BACKGROUND DONE task_id=%s", task_id)
    except Exception as e:
        app.logger.exception("INGEST WORKER BACKGROUND FAILED task_id=%s: %s", task_id, e)


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

    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    result_url = supplied_result_url or _resolve_result_url_for_task(task_id)
    if not result_url:
        return jsonify({
            "ok": False,
            "error": "result_url_not_available",
            "task_id": task_id,
        }), 400

    app.logger.info(
        "INGEST WORKER ACCEPTED task_id=%s result_url_present=%s",
        task_id,
        True,
    )

    t = threading.Thread(
        target=_run_ingest_async,
        args=(task_id, result_url),
        daemon=True,
    )
    t.start()

    return jsonify({
        "ok": True,
        "accepted": True,
        "task_id": task_id,
    }), 202