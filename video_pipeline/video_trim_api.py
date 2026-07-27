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

from db_init import engine, log_task_event
from video_pipeline.build_video_timeline import (
    MIN_POINT_DURATION_S,
    build_video_timeline_from_silver,
    timeline_to_edl,
)

# Synthetic span given to a single-shot point (an ace) so it survives the
# builder's MIN_POINT_DURATION_S filter. 1s matches what the practice loader
# has always used.
SINGLE_SHOT_PAD_S = float(os.getenv("TRIM_SINGLE_SHOT_PAD_S", "1.0"))


# Master kill switch. VIDEO_TRIM_ENABLED=0 makes trigger_video_trim a no-op that
# leaves trim_status NULL — deliberately NOT 'failed', because the SPAs treat NULL
# as "no reel, show the original video" and degrade cleanly, while 'failed' would
# generate ops-alert noise on every ingest.
#
# Why this exists (2026-07-27): the video-worker is a separate always-on Render
# service. Long-match trims need far more CPU than its plan provides, so while
# that is unresolved the service can be suspended to stop the spend — and this
# flag stops the pipeline from POSTing at a service that isn't there. Ingest is
# unaffected either way: all three callers wrap the trigger in try/except
# precisely so a trim problem can never fail an ingest.
VIDEO_TRIM_ENABLED = (os.getenv("VIDEO_TRIM_ENABLED", "1").strip().lower()
                      not in ("0", "false", "no", "n", "off"))

# Which backend runs the encode:
#   'batch' — AWS Batch on Fargate, one per-use job (DEFAULT since 2026-07-27).
#   'http'  — the legacy always-on Render video-worker service (rollback).
# Fargate bills per second, so a 16-vCPU job that finishes in minutes costs
# roughly the same as a 0.5-vCPU one that runs for hours — and nothing at all
# between trims. The Render worker cost $25/month whether or not it trimmed
# anything, and its 0.5 CPU could not finish a long match at all.
TRIM_BACKEND = (os.getenv("TRIM_BACKEND") or "batch").strip().lower()

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

# Only demand the worker wiring when trims are enabled AND the legacy HTTP
# backend is in use — otherwise a suspended/removed worker would break this
# module at IMPORT time, taking the ingest modules down with it. The Batch
# backend needs no worker URL or worker key at all.
if VIDEO_TRIM_ENABLED:
    if not VIDEO_TRIM_CALLBACK_URL:
        raise RuntimeError("VIDEO_TRIM_CALLBACK_URL env var is required")
    if TRIM_BACKEND == "http":
        if not VIDEO_WORKER_BASE_URL:
            raise RuntimeError("VIDEO_WORKER_BASE_URL env var is required (TRIM_BACKEND=http)")
        if not VIDEO_WORKER_OPS_KEY:
            raise RuntimeError("VIDEO_WORKER_OPS_KEY env var is required (TRIM_BACKEND=http)")


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
                trim_output_s3_key,
                sport_type
            FROM bronze.submission_context
            WHERE task_id = :task_id
            LIMIT 1
        """),
        {"task_id": task_id},
    ).mappings().first()

    return dict(row) if row else None


def _load_silver_for_timeline(conn, task_id: str) -> pd.DataFrame:
    """Match points for the timeline builder.

    Single-shot points get a synthetic second row 1s later, mirroring what
    _load_practice_for_timeline already does for single-shot rallies.

    WHY (2026-07-27): a point's span is max(ball_hit_s) - min(ball_hit_s), and
    the builder drops anything under MIN_POINT_DURATION_S (0.5s). An ACE has
    exactly one ball-hit event — the serve — so its span is 0.00s and it was
    silently cut from the highlight reel. Measured on df594aea: 9 of 100 points
    dropped, and all 9 were serve-only points — 7 aces plus 2 serve errors. The
    reel was losing precisely the most watchable points in the match.

    A one-shot point is a real point, not a degenerate segment: with the
    builder's +/-2s padding it yields a perfectly watchable ~5s clip.
    """
    df = pd.read_sql(
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
    return pad_single_shot_points(df)


def pad_single_shot_points(df: pd.DataFrame) -> pd.DataFrame:
    """Give every single-shot point a synthetic second row so it survives the
    builder's MIN_POINT_DURATION_S filter. Pure DataFrame in / out so it can be
    tested without a database — see video_pipeline/tests/test_timeline_aces.py.
    """
    if df is None or df.empty:
        return df

    # Span is measured over the SPINE (exclude_d IS NOT TRUE) because that is
    # what the builder filters on — measuring over all rows would mask a
    # one-shot point that has excluded rows around it.
    spine = df.loc[~df["exclude_d"].fillna(False).astype(bool)]
    if spine.empty:
        return df

    spans = spine.groupby("point_number")["ball_hit_s"].transform(lambda g: g.max() - g.min())
    singles = spine.loc[spans < MIN_POINT_DURATION_S].copy()
    if singles.empty:
        return df

    # One synthetic row per point (a point may legitimately have >1 row here).
    singles = singles.drop_duplicates(subset=["point_number"])
    singles["ball_hit_s"] = singles["ball_hit_s"] + SINGLE_SHOT_PAD_S
    return pd.concat([df, singles], ignore_index=True)


def _load_practice_for_timeline(conn, task_id: str) -> pd.DataFrame:
    """Load practice data mapped to the same shape as match silver.

    For single-shot rallies (duration=0), adds a synthetic second row 1s later
    so the timeline builder treats them as a real segment (MIN_POINT_DURATION_S=0.5).
    """
    df = pd.read_sql(
        text("""
            SELECT
                task_id,
                sequence_num  AS point_number,
                timestamp_s   AS ball_hit_s,
                FALSE         AS exclude_d
            FROM silver.practice_detail
            WHERE task_id = :task_id
              AND timestamp_s IS NOT NULL
              AND sequence_num IS NOT NULL
        """),
        conn,
        params={"task_id": task_id},
    )
    if df.empty:
        return df

    # Pad single-shot rallies with a synthetic end row 1s later
    durations = df.groupby("point_number")["ball_hit_s"].transform(
        lambda g: g.max() - g.min()
    )
    singles = df.loc[durations < 0.5].copy()
    if not singles.empty:
        singles["ball_hit_s"] = singles["ball_hit_s"] + 1.0
        df = pd.concat([df, singles], ignore_index=True)

    return df


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
    # Append-only pipeline log (never raises). This is the one trim-failure case the
    # main-API /internal/video_trim_complete callback never sees (the worker was never
    # reached), so log it here to keep the per-step log truthful.
    log_task_event(task_id, "trim", "failed", error=str(err)[:1000])


def _mark_trim_accepted(conn, task_id: str) -> None:
    conn.execute(
        text("""
            UPDATE bronze.submission_context
               SET trim_requested_at = NOW(),
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

_PRACTICE_SPORT_TYPES = {"serve_practice", "rally_practice"}

# All T5 sport types — ML pipeline pre-compresses video + deletes raw source,
# so the trim step must re-trim the compressed video (not skip as "already done").
_T5_SPORT_TYPES = {"serve_practice", "rally_practice", "tennis_singles_t5"}


def trigger_video_trim(task_id: str) -> dict:
    """
    Fire-and-forget trigger for the external video worker service.

    Works for both match (silver.point_detail) and practice (silver.practice_detail)
    jobs — sport_type on submission_context determines the silver source.

    For practice: the ML pipeline already produces a compressed practice.mp4,
    so we re-trim that (cutting dead time between rallies) to produce review.mp4.

    Non-negotiable behavior:
      - Must not block ingest beyond a short outbound HTTP trigger
      - Must be idempotent
      - Must not raise fatal exceptions into the main ingest pipeline unless
        caller explicitly wants that behavior
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")

    if not VIDEO_TRIM_ENABLED:
        # No-op, and deliberately no DB write: trim_status stays NULL so the SPAs
        # fall back to the original video and nothing raises an ops alert. This
        # also lets the stale-trim sweep quietly retire any pre-existing orphan
        # (it resets the row to NULL, calls this, and the row simply stays NULL).
        return {
            "ok": True,
            "accepted": False,
            "task_id": task_id,
            "status": "disabled",
            "reason": "VIDEO_TRIM_ENABLED=0",
        }

    # --------------------------
    # Gather data + prepare payload (read-only — no state change yet)
    # --------------------------
    with engine.begin() as conn:
        _ensure_trim_columns(conn)

        row = _get_submission_context_row(conn, task_id)
        if not row:
            raise ValueError(f"submission_context not found for task_id={task_id}")

        sport_type = str(row.get("sport_type") or "").strip()
        is_practice = sport_type in _PRACTICE_SPORT_TYPES
        is_t5 = sport_type in _T5_SPORT_TYPES

        trim_status = str(row.get("trim_status") or "").strip().lower()
        trim_output_s3_key = str(row.get("trim_output_s3_key") or "").strip()

        if is_t5 and trim_status == "completed" and trim_output_s3_key:
            # T5 (practice + match): ML pipeline compressed the full video and
            # deleted the raw source. Re-trim the compressed video to cut dead time.
            s3_bucket = str(row.get("s3_bucket") or "").strip() or S3_BUCKET
            s3_key = trim_output_s3_key
        elif trim_status == "completed" and trim_output_s3_key:
            # Match: already trimmed — skip
            return {
                "ok": True,
                "accepted": False,
                "task_id": task_id,
                "status": "already_completed",
                "output_s3_key": trim_output_s3_key,
            }
        else:
            s3_bucket = str(row.get("s3_bucket") or "").strip() or S3_BUCKET
            s3_key = str(row.get("s3_key") or "").strip()

        # Idempotent skip: already in flight
        if trim_status in {"queued", "accepted", "processing"}:
            return {
                "ok": True,
                "accepted": False,
                "task_id": task_id,
                "status": f"already_{trim_status}",
            }

        if not s3_bucket:
            raise ValueError("submission_context missing s3_bucket and S3_BUCKET env var not set")
        if not s3_key:
            raise ValueError("submission_context missing s3_key")

        # Load silver data — practice or match
        if is_practice:
            df_silver = _load_practice_for_timeline(conn, task_id)
            if df_silver.empty:
                raise ValueError(f"No silver.practice_detail rows for task_id={task_id}")
        else:
            df_silver = _load_silver_for_timeline(conn, task_id)
            if df_silver.empty:
                raise ValueError(f"No silver.point_detail rows for task_id={task_id}")

        df_timeline = build_video_timeline_from_silver(df_silver, task_id=task_id)
        if df_timeline.empty:
            raise ValueError(f"Timeline build returned no segments for task_id={task_id}")

        edl = timeline_to_edl(df_timeline)
        if not edl.get("segments"):
            raise ValueError(f"EDL contains no segments for task_id={task_id}")

    # NOTE: DB is NOT marked queued yet — we only mark after the worker accepts.
    # This prevents orphaned "queued" rows if the process dies before the POST.

    # --------------------------
    # Trigger the encode — Batch job (default) or the legacy HTTP worker
    # --------------------------
    if TRIM_BACKEND == "batch":
        from video_pipeline.fargate_trim.submit import submit_trim_job
        try:
            out = submit_trim_job(
                task_id=task_id,
                s3_bucket=s3_bucket,
                s3_key=s3_key,
                edl=edl,
                callback_url=VIDEO_TRIM_CALLBACK_URL,
                callback_ops_key=VIDEO_TRIM_CALLBACK_OPS_KEY or None,
            )
        except Exception as e:
            with engine.begin() as conn:
                _ensure_trim_columns(conn)
                _mark_trim_trigger_failed(
                    conn, task_id, f"batch_submit_failed: {type(e).__name__}: {e}")
            raise

        # Same contract as the HTTP path: only mark accepted once the work is
        # genuinely handed off, so a crash before submit leaves no phantom row.
        with engine.begin() as conn:
            _ensure_trim_columns(conn)
            _mark_trim_accepted(conn, task_id)
        return {
            "ok": True,
            "accepted": True,
            "task_id": task_id,
            "status": "accepted",
            "backend": "batch",
            "job_id": out.get("job_id"),
        }

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