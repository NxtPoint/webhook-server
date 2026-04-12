# ============================================================
# ingest_worker_app.py — Dedicated ingest worker service (Render, 3600s timeout).
#
# Receives POST /ingest from upload_app.py, returns 202 immediately, and runs the
# full ingest pipeline in a background thread. Self-contained — does NOT import upload_app.
#
# Pipeline steps (sequential, all idempotent):
#   1. Download SportAI result JSON (gzip-aware, up to 900s timeout)
#   2. Bronze ingest — parse JSON into typed bronze tables via ingest_bronze_strict()
#   3. Silver build — run build_silver_v2 to compute point_detail analytics
#   4. Video trim trigger — fire-and-forget POST to video worker service
#   5. Billing sync — sync completed task into billing consumption records
#   6. Mark complete — set ingest_finished_at on submission_context
#
# Business rules:
#   - Duplicate prevention: in-memory thread lock prevents concurrent ingests for same task_id
#   - Each step is wrapped in try/except so failures in trim/billing don't block completion
#   - Ingest errors are persisted to submission_context.ingest_error for ops visibility
#   - Auth: requires Authorization: Bearer <INGEST_WORKER_OPS_KEY> header
#
# Endpoints:
#   POST /ingest         — accepts {task_id, result_url}, returns 202
#   GET  /ingest/status  — lightweight status check from submission_context
#   GET  /               — service identity
#   GET  /healthz        — liveness probe
# ============================================================

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests
from flask import Flask, request, jsonify
from sqlalchemy import text as sql_text

app = Flask(__name__)
log = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================

INGEST_WORKER_OPS_KEY = (os.getenv("INGEST_WORKER_OPS_KEY") or "").strip()
OPS_KEY = (os.getenv("OPS_KEY") or "").strip()

if not INGEST_WORKER_OPS_KEY:
    raise RuntimeError("INGEST_WORKER_OPS_KEY env var is required")

DEFAULT_REPLACE_ON_INGEST = (
    os.getenv("INGEST_REPLACE_EXISTING")
    or os.getenv("DEFAULT_REPLACE_ON_INGEST")
    or "1"
).strip().lower() in ("1", "true", "yes", "y")

# Video worker config
VIDEO_WORKER_BASE_URL = (os.getenv("VIDEO_WORKER_BASE_URL") or "").strip().rstrip("/")
VIDEO_WORKER_OPS_KEY = (os.getenv("VIDEO_WORKER_OPS_KEY") or "").strip()

# ============================================================
# IMPORTS — heavy modules loaded here (worker has 3600s timeout)
# ============================================================

from db_init import engine  # noqa: E402
from ingest_bronze import ingest_bronze_strict, _run_bronze_init  # noqa: E402
from build_silver_v2 import build_silver_v2 as build_silver_point_detail  # noqa: E402
from billing_import_from_bronze import sync_usage_for_task_id  # noqa: E402


# ============================================================
# AUTH
# ============================================================

def _auth_ok(req) -> bool:
    import hmac
    auth = (req.headers.get("Authorization") or "").strip()
    expected = f"Bearer {INGEST_WORKER_OPS_KEY}"
    return hmac.compare_digest(auth, expected)


# ============================================================
# DB HELPERS (self-contained, no upload_app dependency)
# ============================================================

def _ensure_schema(conn):
    """Idempotent schema bootstrap for submission_context columns we touch."""
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze"))
    for ddl in (
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_started_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_error TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS session_id TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_result_url TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notified_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_error TEXT",
    ):
        conn.execute(sql_text(ddl))


# ============================================================
# VIDEO TRIM TRIGGER
# ============================================================

def _trigger_video_trim(task_id: str) -> None:
    """Trigger video trim via the dedicated video_trim_api module."""
    try:
        from video_pipeline.video_trim_api import trigger_video_trim
        out = trigger_video_trim(task_id)
        app.logger.info("INGEST WORKER video trim triggered task_id=%s out=%s", task_id, out)
    except Exception as e:
        app.logger.exception("INGEST WORKER video trim failed task_id=%s: %s", task_id, e)


# ============================================================
# CORE INGEST PIPELINE
# ============================================================

def _do_ingest(task_id: str, result_url: str) -> bool:
    """
    Run the full ingest pipeline.

    Steps:
      1. Download SportAI result JSON
      2. Bronze ingest
      3. Silver build
      4. Video trim trigger        (fire-and-forget)
      5. Billing sync              (fire-and-forget)
      6. Wix notify                (data is ready after silver)
      7. Mark complete
    """
    sid = None

    try:
        app.logger.info("INGEST START task_id=%s result_url=%s", task_id, result_url)

        # Mark started
        with engine.begin() as conn:
            _ensure_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_started_at = COALESCE(ingest_started_at, now()),
                       ingest_finished_at = NULL,
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})

        # -------------------------
        # STEP 1: DOWNLOAD RESULT JSON
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=download_result_start", task_id)

        r = requests.get(result_url, timeout=900, stream=True)
        r.raise_for_status()

        content_encoding = (r.headers.get("Content-Encoding") or "").lower().strip()
        app.logger.info(
            "INGEST STEP task_id=%s step=download_result_headers status=%s content_length=%s encoding=%s",
            task_id, r.status_code, r.headers.get("Content-Length"), content_encoding,
        )

        if "gzip" in content_encoding:
            payload = json.load(gzip.GzipFile(fileobj=r.raw))
        else:
            payload = json.load(r.raw)

        app.logger.info("INGEST STEP task_id=%s step=download_result_done", task_id)

        # -------------------------
        # STEP 2: BRONZE INGEST
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=bronze_ingest_start", task_id)

        with engine.begin() as conn:
            _run_bronze_init(conn)
            res = ingest_bronze_strict(
                conn,
                payload,
                replace=DEFAULT_REPLACE_ON_INGEST,
                src_hint=result_url,
                task_id=task_id,
            )
            sid = res.get("session_id")

            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET session_id      = :sid,
                       ingest_error    = NULL,
                       last_result_url = :url,
                       last_status     = 'completed',
                       last_status_at  = now()
                 WHERE task_id = :t
            """), {"sid": sid, "t": task_id, "url": result_url})

        app.logger.info("INGEST STEP task_id=%s step=bronze_ingest_done session_id=%s", task_id, sid)

        try:
            del payload
        except Exception:
            pass

        # -------------------------
        # STEP 3: SILVER BUILD
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=silver_build_start", task_id)
        build_silver_point_detail(task_id=task_id, replace=True)
        app.logger.info("INGEST STEP task_id=%s step=silver_build_done", task_id)

        # -------------------------
        # STEP 4: VIDEO TRIM TRIGGER (fire-and-forget)
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=video_trim_trigger_start", task_id)
        _trigger_video_trim(task_id)

        # -------------------------
        # STEP 5: BILLING SYNC (fire-and-forget)
        # -------------------------
        app.logger.info("INGEST STEP task_id=%s step=billing_sync_start", task_id)
        try:
            out = sync_usage_for_task_id(task_id, dry_run=False)
            app.logger.info(
                "INGEST STEP task_id=%s step=billing_sync_done inserted=%s",
                task_id, out.get("inserted"),
            )
        except Exception as e:
            app.logger.exception("INGEST STEP task_id=%s billing_sync_failed: %s", task_id, e)

        # -------------------------
        # STEP 6: FINAL SUCCESS
        # -------------------------
        with engine.begin() as conn:
            _ensure_schema(conn)
            conn.execute(sql_text("""
                UPDATE bronze.submission_context
                   SET ingest_finished_at = now(),
                       ingest_error = NULL
                 WHERE task_id = :t
            """), {"t": task_id})

        app.logger.info("INGEST COMPLETE task_id=%s", task_id)
        return True

    except Exception as e:
        app.logger.exception("INGEST FAILED task_id=%s result_url=%s", task_id, result_url)

        err_txt = f"{e.__class__.__name__}: {e}"
        try:
            with engine.begin() as conn:
                _ensure_schema(conn)
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                       SET ingest_error = :err,
                           ingest_finished_at = now()
                     WHERE task_id = :t
                """), {"t": task_id, "err": err_txt})
        except Exception:
            app.logger.exception("INGEST FAILED — could not persist error for task_id=%s", task_id)

        return False


# ============================================================
# BACKGROUND RUNNER
# ============================================================

# Track in-flight ingests to prevent duplicate launches
_active_ingests: Dict[str, threading.Thread] = {}
_active_lock = threading.Lock()


def _run_ingest_background(task_id: str, result_url: str) -> bool:
    """
    Launch ingest in a background thread within this process.
    Returns False if already running for this task_id.
    """
    with _active_lock:
        existing = _active_ingests.get(task_id)
        if existing and existing.is_alive():
            return False  # already running

        def _worker():
            try:
                _do_ingest(task_id, result_url)
            finally:
                with _active_lock:
                    _active_ingests.pop(task_id, None)

        t = threading.Thread(target=_worker, name=f"ingest-{task_id[:8]}", daemon=True)
        _active_ingests[task_id] = t
        t.start()
        return True


# ============================================================
# ENDPOINTS
# ============================================================

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
        return jsonify({"ok": False, "error": "result_url required", "task_id": task_id}), 400

    launched = _run_ingest_background(task_id, result_url)

    if not launched:
        app.logger.info("INGEST WORKER already running task_id=%s", task_id)
        return jsonify({
            "ok": True,
            "accepted": False,
            "task_id": task_id,
            "status": "already_running",
        }), 200

    app.logger.info("INGEST WORKER ACCEPTED task_id=%s", task_id)
    return jsonify({
        "ok": True,
        "accepted": True,
        "task_id": task_id,
        "status": "accepted",
    }), 202


@app.get("/ingest/status")
def ingest_status():
    """Lightweight status check — reads from submission_context."""
    if not _auth_ok(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    task_id = (request.args.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"ok": False, "error": "task_id required"}), 400

    with engine.begin() as conn:
        _ensure_schema(conn)
        row = conn.execute(sql_text("""
            SELECT
              session_id,
              ingest_started_at,
              ingest_finished_at,
              ingest_error,
              wix_notify_status,
              trim_status,
              trim_error,
              trim_output_s3_key
            FROM bronze.submission_context
            WHERE task_id = :t
            LIMIT 1
        """), {"t": task_id}).mappings().first()

    if not row:
        return jsonify({"ok": False, "error": "task_not_found", "task_id": task_id}), 404

    row = dict(row)
    ingest_started = row.get("ingest_started_at") is not None
    ingest_finished = row.get("ingest_finished_at") is not None
    ingest_error = row.get("ingest_error")

    if ingest_error:
        status = "failed"
    elif ingest_finished:
        status = "completed"
    elif ingest_started:
        status = "running"
    else:
        status = "pending"

    # Check if in-flight in this worker
    with _active_lock:
        active_here = task_id in _active_ingests

    return jsonify({
        "ok": True,
        "task_id": task_id,
        "ingest_status": status,
        "active_in_worker": active_here,
        "session_id": row.get("session_id"),
        "ingest_error": ingest_error,
        "wix_notify_status": row.get("wix_notify_status"),
        "trim_status": row.get("trim_status"),
    })
