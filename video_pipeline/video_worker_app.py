# ============================================================
# video_worker_app.py
# ============================================================
# Flask service for video trimming. Runs in Docker (see Dockerfile.worker).
# Entry point: video_worker_wsgi.py (Gunicorn).
#
# Endpoint: POST /trim
#   - Validates OPS key against VIDEO_WORKER_OPS_KEY env var.
#   - Accepts JSON body: { task_id, s3_key, edl, callback_url }.
#   - Launches a detached subprocess via ffmpeg_trim_worker.run_ffmpeg_trim().
#   - Returns HTTP 202 immediately (fire-and-forget).
#   - On completion the subprocess POSTs a callback to VIDEO_TRIM_CALLBACK_URL
#     with { task_id, status, output_s3_key } so the main API can update
#     trim_status on bronze.submission_context.
#
# Auth: VIDEO_WORKER_OPS_KEY header (X-Ops-Key).
# ============================================================

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import traceback
from typing import Any, Dict

import requests
from flask import Flask, jsonify, request

from video_pipeline.ffmpeg_trim_worker import run_ffmpeg_trim

APP = Flask(__name__)

VIDEO_WORKER_OPS_KEY = (os.getenv("VIDEO_WORKER_OPS_KEY") or "").strip()
CALLBACK_TIMEOUT_S = int(os.getenv("VIDEO_TRIM_CALLBACK_TIMEOUT_S", "20"))
CALLBACK_MAX_RETRIES = int(os.getenv("VIDEO_TRIM_CALLBACK_MAX_RETRIES", "3"))
CALLBACK_RETRY_BASE_S = float(os.getenv("VIDEO_TRIM_CALLBACK_RETRY_BASE_S", "2.0"))

# Subprocess log directory — logs are preserved for debugging failed trims
TRIM_LOG_DIR = os.getenv("TRIM_LOG_DIR", "/tmp/trim_logs")

if not VIDEO_WORKER_OPS_KEY:
    raise RuntimeError("VIDEO_WORKER_OPS_KEY env var is required")


def _auth_ok(req) -> bool:
    import hmac
    auth = (req.headers.get("Authorization") or "").strip()
    expected = f"Bearer {VIDEO_WORKER_OPS_KEY}"
    return hmac.compare_digest(auth, expected)


def _require_non_empty_str(v: Any, field_name: str) -> str:
    out = str(v or "").strip()
    if not out:
        raise ValueError(f"{field_name} is required")
    return out


def _validate_trim_request(body: Dict[str, Any]) -> Dict[str, Any]:
    task_id = _require_non_empty_str(body.get("task_id"), "task_id")
    s3_bucket = _require_non_empty_str(body.get("s3_bucket"), "s3_bucket")
    s3_key = _require_non_empty_str(body.get("s3_key"), "s3_key")

    edl = body.get("edl")
    if not isinstance(edl, dict):
        raise ValueError("edl must be a dict")

    segments = edl.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("edl.segments is required and must be a non-empty list")

    callback_url = _require_non_empty_str(body.get("callback_url"), "callback_url")

    callback_headers = body.get("callback_headers") or {}
    if not isinstance(callback_headers, dict):
        raise ValueError("callback_headers must be a dict when provided")

    return {
        "task_id": task_id,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "edl": edl,
        "callback_url": callback_url,
        "callback_headers": callback_headers,
    }


def _callback(callback_url: str, callback_headers: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """POST callback with retry + exponential backoff."""
    headers = {"Content-Type": "application/json"}
    for k, v in (callback_headers or {}).items():
        if v is not None:
            headers[str(k)] = str(v)

    last_err: Exception | None = None
    for attempt in range(1, CALLBACK_MAX_RETRIES + 1):
        try:
            r = requests.post(
                callback_url,
                json=payload,
                headers=headers,
                timeout=CALLBACK_TIMEOUT_S,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"callback_failed_http_{r.status_code}: {r.text}")
            return  # success
        except Exception as e:
            last_err = e
            if attempt < CALLBACK_MAX_RETRIES:
                wait = CALLBACK_RETRY_BASE_S * (2 ** (attempt - 1))
                APP.logger.warning(
                    "VIDEO TRIM CALLBACK attempt %d/%d failed task_id=%s error=%s — retrying in %.1fs",
                    attempt, CALLBACK_MAX_RETRIES, payload.get("task_id"), e, wait,
                )
                time.sleep(wait)

    raise RuntimeError(
        f"callback_failed_after_{CALLBACK_MAX_RETRIES}_attempts: {last_err}"
    )


def _run_trim_job(
    *,
    task_id: str,
    s3_bucket: str,
    s3_key: str,
    edl: Dict[str, Any],
    callback_url: str,
    callback_headers: Dict[str, Any],
) -> None:
    try:
        APP.logger.info("VIDEO TRIM START task_id=%s s3_bucket=%s s3_key=%s", task_id, s3_bucket, s3_key)

        result = run_ffmpeg_trim(
            task_id=task_id,
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            edl=edl,
        )

        _callback(
            callback_url,
            callback_headers,
            {
                "task_id": task_id,
                "status": "completed",
                "output_s3_key": result["output_s3_key"],
                "source_duration_s": result["source_duration_s"],
                "trimmed_duration_s": result["trimmed_duration_s"],
                "segment_count": result["segment_count"],
                "seconds_removed": result["seconds_removed"],
            },
        )

        APP.logger.info(
            "VIDEO TRIM COMPLETE task_id=%s output_s3_key=%s trimmed_duration_s=%s",
            task_id,
            result["output_s3_key"],
            result["trimmed_duration_s"],
        )

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        tb = traceback.format_exc()

        APP.logger.exception("VIDEO TRIM FAILED task_id=%s error=%s", task_id, err)

        try:
            _callback(
                callback_url,
                callback_headers,
                {
                    "task_id": task_id,
                    "status": "failed",
                    "error": err,
                },
            )
        except Exception as cb_e:
            APP.logger.exception(
                "VIDEO TRIM CALLBACK FAILED task_id=%s callback_error=%s traceback=%s",
                task_id,
                cb_e,
                tb,
            )


def _launch_trim_subprocess(
    *,
    task_id: str,
    s3_bucket: str,
    s3_key: str,
    edl: Dict[str, Any],
    callback_url: str,
    callback_headers: Dict[str, Any],
) -> None:
    payload = {
        "task_id": task_id,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "edl": edl,
        "callback_url": callback_url,
        "callback_headers": callback_headers,
    }

    py_code = (
        "import json, sys; "
        "from video_pipeline.video_worker_app import _run_trim_job; "
        "payload = json.loads(sys.argv[1]); "
        "_run_trim_job(**payload)"
    )

    env = os.environ.copy()

    # Route subprocess output to log files for post-mortem debugging
    os.makedirs(TRIM_LOG_DIR, exist_ok=True)
    log_path = os.path.join(TRIM_LOG_DIR, f"trim_{task_id[:8]}.log")

    log_fh = open(log_path, "a", encoding="utf-8")
    APP.logger.info(
        "VIDEO TRIM SUBPROCESS launching task_id=%s log=%s",
        task_id, log_path,
    )

    subprocess.Popen(
        [sys.executable, "-c", py_code, json.dumps(payload)],
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        close_fds=False,
        start_new_session=True,
        cwd=os.getcwd(),
        env=env,
    )


@APP.post("/trim")
def trim():
    if not _auth_ok(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        body = request.get_json(force=True) or {}
        payload = _validate_trim_request(body)
    except Exception as e:
        return jsonify({
            "ok": False,
            "accepted": False,
            "error": str(e),
        }), 400

    try:
        _launch_trim_subprocess(
            task_id=payload["task_id"],
            s3_bucket=payload["s3_bucket"],
            s3_key=payload["s3_key"],
            edl=payload["edl"],
            callback_url=payload["callback_url"],
            callback_headers=payload["callback_headers"],
        )
    except Exception as e:
        APP.logger.exception("VIDEO TRIM LAUNCH FAILED task_id=%s error=%s", payload["task_id"], e)
        return jsonify({
            "ok": False,
            "accepted": False,
            "task_id": payload["task_id"],
            "error": f"job_launch_failed: {e}",
        }), 500

    return jsonify({
        "ok": True,
        "accepted": True,
        "task_id": payload["task_id"],
        "status": "accepted",
    }), 202


@APP.get("/healthz")
def healthz():
    return "OK", 200


@APP.get("/")
def root():
    return jsonify({"ok": True, "service": "nextpoint-video-worker"}), 200