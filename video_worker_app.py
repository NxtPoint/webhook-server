# ============================================================
# video_worker_app.py
#
# PURPOSE
#   Standalone Docker service that performs FFmpeg trimming.
#   Triggered by your existing API via HTTP.
#
# SECURITY
#   Requires header: Authorization: Bearer <VIDEO_WORKER_OPS_KEY>
#
# ENDPOINTS
#   POST /trim  (starts + runs trim synchronously in this service)
#
# CALLBACK
#   POST {CALLBACK_BASE_URL}/internal/video_trim_complete
# ============================================================

import os
import json
import traceback
import requests
from flask import Flask, request, jsonify

from ffmpeg_trim_worker import run_ffmpeg_trim

APP = Flask(__name__)

VIDEO_WORKER_OPS_KEY = os.getenv("VIDEO_WORKER_OPS_KEY")
CALLBACK_BASE_URL = os.getenv("CALLBACK_BASE_URL")
CALLBACK_OPS_KEY = os.getenv("CALLBACK_OPS_KEY")  # what your API expects

if not VIDEO_WORKER_OPS_KEY:
    raise RuntimeError("VIDEO_WORKER_OPS_KEY env var is required")
if not CALLBACK_BASE_URL:
    raise RuntimeError("CALLBACK_BASE_URL env var is required")
if not CALLBACK_OPS_KEY:
    raise RuntimeError("CALLBACK_OPS_KEY env var is required")

def _auth_ok(req) -> bool:
    h = req.headers.get("Authorization", "")
    return h == f"Bearer {VIDEO_WORKER_OPS_KEY}"

def _callback(payload: dict) -> None:
    url = f"{CALLBACK_BASE_URL.rstrip('/')}/internal/video_trim_complete"
    headers = {"Authorization": f"Bearer {CALLBACK_OPS_KEY}"}
    # short + safe; callback is not critical path for trimming
    requests.post(url, json=payload, headers=headers, timeout=15)

@APP.post("/trim")
def trim():
    if not _auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True) or {}

    task_id = str(body.get("task_id") or "").strip()
    s3_bucket = str(body.get("s3_bucket") or "").strip()
    s3_key = str(body.get("s3_key") or "").strip()
    edl = body.get("edl")

    if not task_id or not s3_bucket or not s3_key or not isinstance(edl, dict):
        return jsonify({"error": "task_id, s3_bucket, s3_key, edl are required"}), 400

    try:
        result = run_ffmpeg_trim(
            task_id=task_id,
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            edl=edl,
        )

        # callback to API (mark complete + store output key)
        _callback({
            "task_id": task_id,
            "status": "completed",
            "output_s3_key": result["output_s3_key"],
            "duration_s": result["duration_s"],
        })

        return jsonify(result), 200

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()

        _callback({
            "task_id": task_id,
            "status": "failed",
            "error": err,
            "traceback": tb,
        })

        return jsonify({"task_id": task_id, "status": "failed", "error": err}), 500


if __name__ == "__main__":
    APP.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
