import os
import sys
import subprocess
from flask import Flask, request, jsonify

app = Flask(__name__)

INGEST_WORKER_OPS_KEY = (os.getenv("INGEST_WORKER_OPS_KEY") or "").strip()

if not INGEST_WORKER_OPS_KEY:
    raise RuntimeError("INGEST_WORKER_OPS_KEY env var is required")


def _auth_ok(req) -> bool:
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {INGEST_WORKER_OPS_KEY}"


def _launch_ingest_subprocess(task_id: str, result_url: str) -> None:
    """
    Launch ingest in a detached child Python process.
    Safer than daemon threads inside Flask/Gunicorn workers.
    """
    py_code = (
        "from upload_app import _do_ingest; "
        "import sys; "
        "_do_ingest(sys.argv[1], sys.argv[2])"
    )

    env = os.environ.copy()

    subprocess.Popen(
        [sys.executable, "-c", py_code, task_id, result_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=env,
    )


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
    result_url = str(body.get("result_url") or "").strip()

    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    if not result_url:
        return jsonify({
            "ok": False,
            "error": "result_url required",
            "task_id": task_id,
        }), 400

    app.logger.info(
        "INGEST WORKER ACCEPTED task_id=%s result_url_present=%s",
        task_id,
        True,
    )

    try:
        _launch_ingest_subprocess(task_id, result_url)
    except Exception as e:
        app.logger.exception("INGEST WORKER SUBPROCESS FAILED task_id=%s: %s", task_id, e)
        return jsonify({
            "ok": False,
            "error": f"subprocess_launch_failed: {e}",
            "task_id": task_id,
        }), 500

    return jsonify({
        "ok": True,
        "accepted": True,
        "task_id": task_id,
    }), 202