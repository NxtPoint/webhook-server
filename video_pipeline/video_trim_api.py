# ============================================================
# video_trim_api.py
# ============================================================
# Triggers async video trimming for a completed match ingest.
#
# Entry point: trigger_video_trim(task_id) — called from ingest_worker_app.py
# at step 4 of the ingest pipeline.
#
# Flow:
#   1. Check trim_status on bronze.submission_context — skip if already
#      'completed', 'accepted', or 'queued' (idempotent).
#   2. Build an EDL (Edit Decision List) by calling
#      build_video_timeline_from_silver(task_id), which reads silver.point_detail.
#   3. POST the EDL + source S3 key to the video worker service at
#      VIDEO_WORKER_BASE_URL/trim (auth: VIDEO_WORKER_OPS_KEY).
#   4. Update submission_context.trim_status to 'queued' on success.
#
# Status lifecycle: queued → accepted (worker ack) → completed / failed.
# State is stored in bronze.submission_context.trim_status.
# ============================================================

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import pandas as pd
import requests
from sqlalchemy import text

from db_init import engine
from video_pipeline.build_video_timeline import (
    build_video_timeline_from_silver,
    timeline_to_edl,
)


VIDEO_WORKER_BASE_URL = (os.getenv("VIDEO_WORKER_BASE_URL") or "").strip().rstrip("/")
VIDEO_WORKER_OPS_KEY = (os.getenv("VIDEO_WORKER_OPS_KEY") or "").strip()

# Main API callback endpoint that the worker will call when finished.
# Example:
#   https://your-upload-service.onrender.com/internal/video_trim_complete
VIDEO_TRIM_CALLBACK_URL = (os.getenv("VIDEO_TRIM_CALLBACK_URL") or "").strip()

# Optional auth key for worker -> main API callback
VIDEO_TRIM_CALLBACK_OPS_KEY = (os.getenv("VIDEO_TRIM_CALLBACK_OPS_KEY") or "").strip()

# Fallback source bucket if bronze.submission_context.s3_bucket is null
S3_BUCKET = (os.getenv("S3_BUCKET") or "").strip()

# Conservative outbound timeout: must never hang ingest flow
REQUEST_TIMEOUT_S = int(os.getenv("VIDEO_WORKER_REQUEST_TIMEOUT_S", "10"))

if not VIDEO_WORKER_BASE_URL:
    raise RuntimeError("VIDEO_WORKER_BASE_URL env var is required")
if not VIDEO_WORKER_OPS_KEY:
    raise RuntimeError("VIDEO_WORKER_OPS_KEY env var is required")
if not VIDEO_TRIM_CALLBACK_URL:
    raise RuntimeError("VIDEO_TRIM_CALLBACK_URL env var is required")


# ============================================================
# DB helpers
# ============================================================

def _ensure_trim_columns(conn) -> None:
    """
    Transitional only.
    Leave in place for safety until migration is fully deployed everywhere.
    Long-term this should be removed after schema is locked in migrations.
    """
    for ddl in (
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS s3_bucket TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS s3_key TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_requested_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_finished_at TIMESTAMPTZ",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_status TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_error TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_output_s3_key TEXT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_source_duration_s DOUBLE PRECISION",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_duration_s DOUBLE PRECISION",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_segment_count INT",
        "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_seconds_removed DOUBLE PRECISION",
    ):
        conn.execute(text(ddl))


def _get_submission_context_row(conn, task_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        text("""
            SELECT
                task_id,
                s3_bucket,
                s3_key,
                trim_status,
                trim_output_s3_key
            FROM bronze.submission_context
            WHERE task_id = :task_id
            LIMIT 1
        """),
        {"task_id": task_id},
    ).mappings().first()

    return dict(row) if row else None


def _load_silver_for_timeline(conn, task_id: str) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            SELECT
                task_id,
                point_number,
                ball_hit_s,
                exclude_d
            FROM silver.point_detail
            WHERE task_id = :task_id
              AND ball_hit_s IS NOT NULL
              AND point_number IS NOT NULL
        """),
        conn,
        params={"task_id": task_id},
    )


def _mark_trim_queued(conn, task_id: str) -> None:
    conn.execute(
        text("""
            UPDATE bronze.submission_context
               SET trim_requested_at = NOW(),
                   trim_finished_at = NULL,
                   trim_status = 'queued',
                   trim_error = NULL,
                   trim_output_s3_key = NULL,
                   trim_source_duration_s = NULL,
                   trim_duration_s = NULL,
                   trim_segment_count = NULL,
                   trim_seconds_removed = NULL
             WHERE task_id = :task_id
        """),
        {"task_id": task_id},
    )


def _mark_trim_trigger_failed(conn, task_id: str, err: str) -> None:
    conn.execute(
        text("""
            UPDATE bronze.submission_context
               SET trim_finished_at = NOW(),
                   trim_status = 'failed',
                   trim_error = LEFT(:err, 4000)
             WHERE task_id = :task_id
        """),
        {"task_id": task_id, "err": err},
    )


def _mark_trim_accepted(conn, task_id: str) -> None:
    conn.execute(
        text("""
            UPDATE bronze.submission_context
               SET trim_requested_at = COALESCE(trim_requested_at, NOW()),
                   trim_finished_at = NULL,
                   trim_status = 'accepted',
                   trim_error = NULL,
                   trim_output_s3_key = NULL,
                   trim_source_duration_s = NULL,
                   trim_duration_s = NULL,
                   trim_segment_count = NULL,
                   trim_seconds_removed = NULL
             WHERE task_id = :task_id
        """),
        {"task_id": task_id},
    )


# ============================================================
# Public API
# ============================================================

def trigger_video_trim(task_id: str) -> dict:
    """
    Fire-and-forget trigger for the external video worker service.

    Non-negotiable behavior:
      - Must not block ingest beyond a short outbound HTTP trigger
      - Must be idempotent
      - Must not raise fatal exceptions into the main ingest pipeline unless
        caller explicitly wants that behavior
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")

    # --------------------------
    # Gather data + prepare payload (read-only — no state change yet)
    # --------------------------
    with engine.begin() as conn:
        _ensure_trim_columns(conn)

        row = _get_submission_context_row(conn, task_id)
        if not row:
            raise ValueError(f"submission_context not found for task_id={task_id}")

        trim_status = str(row.get("trim_status") or "").strip().lower()
        trim_output_s3_key = str(row.get("trim_output_s3_key") or "").strip()

        # Idempotent skip: already completed
        if trim_status == "completed" and trim_output_s3_key:
            return {
                "ok": True,
                "accepted": False,
                "task_id": task_id,
                "status": "already_completed",
                "output_s3_key": trim_output_s3_key,
            }

        # Idempotent skip: already in flight
        if trim_status in {"queued", "accepted", "processing"}:
            return {
                "ok": True,
                "accepted": False,
                "task_id": task_id,
                "status": f"already_{trim_status}",
            }

        s3_bucket = str(row.get("s3_bucket") or "").strip() or S3_BUCKET
        s3_key = str(row.get("s3_key") or "").strip()

        if not s3_bucket:
            raise ValueError("submission_context missing s3_bucket and S3_BUCKET env var not set")
        if not s3_key:
            raise ValueError("submission_context missing s3_key")

        df_silver = _load_silver_for_timeline(conn, task_id)
        if df_silver.empty:
            raise ValueError(f"No silver.point_detail rows found for task_id={task_id}")

        df_timeline = build_video_timeline_from_silver(df_silver, task_id=task_id)
        if df_timeline.empty:
            raise ValueError(f"Timeline build returned no segments for task_id={task_id}")

        edl = timeline_to_edl(df_timeline)
        if not edl.get("segments"):
            raise ValueError(f"EDL contains no segments for task_id={task_id}")

    # NOTE: DB is NOT marked queued yet — we only mark after the worker accepts.
    # This prevents orphaned "queued" rows if the process dies before the POST.

    # --------------------------
    # Trigger worker
    # --------------------------
    url = f"{VIDEO_WORKER_BASE_URL}/trim"
    headers = {
        "Authorization": f"Bearer {VIDEO_WORKER_OPS_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "task_id": task_id,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "edl": edl,
        "callback_url": VIDEO_TRIM_CALLBACK_URL,
        "callback_headers": (
            {"Authorization": f"Bearer {VIDEO_TRIM_CALLBACK_OPS_KEY}"}
            if VIDEO_TRIM_CALLBACK_OPS_KEY
            else {}
        ),
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        out = resp.json() if resp.content else {}
    except Exception as e:
        # Worker trigger failed — mark as failed so it can be retried later
        with engine.begin() as conn:
            _ensure_trim_columns(conn)
            _mark_trim_trigger_failed(conn, task_id, f"worker_trigger_failed: {type(e).__name__}: {e}")
        raise

    # --------------------------
    # Mark accepted only after worker accepted (single atomic write)
    # --------------------------
    with engine.begin() as conn:
        _ensure_trim_columns(conn)
        _mark_trim_accepted(conn, task_id)

    return {
        "ok": True,
        "accepted": True,
        "task_id": task_id,
        "status": str(out.get("status") or "accepted"),
        "worker_response": out,
    }