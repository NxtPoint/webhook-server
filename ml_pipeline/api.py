"""
ml_pipeline/api.py — Flask blueprint for ML analysis endpoints.

Register in upload_app.py:
    from ml_pipeline.api import ml_analysis_bp
    app.register_blueprint(ml_analysis_bp)

Auth: OPS_KEY via X-Ops-Key header or Authorization: Bearer <key>.
"""

import os
import json
import logging

import boto3
from flask import Blueprint, request, jsonify
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

ml_analysis_bp = Blueprint("ml_analysis", __name__)

OPS_KEY = os.getenv("OPS_KEY", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


def _guard():
    """Header-only ops auth (same pattern as upload_app.py)."""
    hk = request.headers.get("X-OPS-Key") or request.headers.get("X-Ops-Key")
    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    supplied = (hk or "").strip()
    return bool(OPS_KEY) and supplied == OPS_KEY


def _forbid():
    return jsonify({"error": "unauthorized"}), 401


def _get_engine():
    """Lazy import to avoid circular imports at module load time."""
    from db_init import engine
    return engine


# ── GET /api/analysis/jobs/<job_id> ─────────────────────────────────────────

@ml_analysis_bp.route("/api/analysis/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    """Get full job status and metadata."""
    if not _guard():
        return _forbid()

    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql_text("""
            SELECT job_id, task_id, s3_key, status, current_stage, progress_pct,
                   error_message, video_duration_sec, video_fps, video_width, video_height,
                   total_frames, frame_errors, processing_time_sec, ms_per_frame,
                   court_detected, court_confidence, court_used_fallback,
                   ball_heatmap_s3_key, player_heatmap_s3_keys,
                   batch_job_id, batch_duration_sec, estimated_cost_usd,
                   created_at, updated_at
            FROM ml_analysis.video_analysis_jobs
            WHERE job_id = :jid
        """), {"jid": job_id}).fetchone()

    if not row:
        return jsonify({"error": "job not found"}), 404

    cols = row._mapping
    return jsonify({k: _serialize(v) for k, v in cols.items()})


# ── GET /api/analysis/results/<match_id> ─────────────────────────────────────

@ml_analysis_bp.route("/api/analysis/results/<match_id>", methods=["GET"])
def get_results(match_id):
    """
    Get analysis results for a match (by task_id).
    Returns the match_analytics row plus job metadata.
    """
    if not _guard():
        return _forbid()

    engine = _get_engine()
    with engine.connect() as conn:
        # Find job by task_id
        row = conn.execute(sql_text("""
            SELECT j.job_id, j.task_id, j.status, j.s3_key,
                   a.ball_detection_rate, a.bounce_count, a.bounces_in, a.bounces_out,
                   a.max_speed_kmh, a.avg_speed_kmh,
                   a.rally_count, a.avg_rally_length, a.serve_count, a.first_serve_pct,
                   a.player_count, a.total_frames, a.frame_errors, a.processing_time_sec,
                   j.ball_heatmap_s3_key, j.player_heatmap_s3_keys,
                   j.created_at, j.updated_at
            FROM ml_analysis.video_analysis_jobs j
            LEFT JOIN ml_analysis.match_analytics a ON a.job_id = j.job_id
            WHERE j.task_id = :tid
            ORDER BY j.created_at DESC
            LIMIT 1
        """), {"tid": match_id}).fetchone()

    if not row:
        return jsonify({"error": "no analysis found for this match"}), 404

    cols = row._mapping
    return jsonify({k: _serialize(v) for k, v in cols.items()})


# ── GET /api/analysis/heatmap/<job_id>/<type> ────────────────────────────────

@ml_analysis_bp.route("/api/analysis/heatmap/<job_id>/<heatmap_type>", methods=["GET"])
def get_heatmap(job_id, heatmap_type):
    """
    Return a presigned S3 URL for a heatmap image (1hr expiry).

    heatmap_type: 'ball', 'player_0', 'player_1'
    """
    if not _guard():
        return _forbid()

    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql_text("""
            SELECT ball_heatmap_s3_key, player_heatmap_s3_keys
            FROM ml_analysis.video_analysis_jobs
            WHERE job_id = :jid
        """), {"jid": job_id}).fetchone()

    if not row:
        return jsonify({"error": "job not found"}), 404

    ball_key = row[0]
    player_keys = row[1] or {}
    if isinstance(player_keys, str):
        player_keys = json.loads(player_keys)

    # Resolve the S3 key based on type
    if heatmap_type == "ball":
        s3_key = ball_key
    elif heatmap_type.startswith("player_"):
        filename = f"{heatmap_type}.png"
        s3_key = player_keys.get(filename)
    else:
        return jsonify({"error": f"unknown heatmap type: {heatmap_type}"}), 400

    if not s3_key:
        return jsonify({"error": f"heatmap not available: {heatmap_type}"}), 404

    s3 = boto3.client("s3", region_name=AWS_REGION)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=3600,
    )
    return jsonify({"url": url, "s3_key": s3_key, "expires_in": 3600})


# ── POST /api/analysis/retry/<job_id> ────────────────────────────────────────

@ml_analysis_bp.route("/api/analysis/retry/<job_id>", methods=["POST"])
def retry_job(job_id):
    """
    Retry a failed job by resetting status to 'queued' and resubmitting to Batch.
    """
    if not _guard():
        return _forbid()

    engine = _get_engine()
    with engine.begin() as conn:
        row = conn.execute(sql_text("""
            SELECT job_id, s3_key, status
            FROM ml_analysis.video_analysis_jobs
            WHERE job_id = :jid
        """), {"jid": job_id}).fetchone()

    if not row:
        return jsonify({"error": "job not found"}), 404

    if row[2] not in ("failed", "complete"):
        return jsonify({"error": f"cannot retry job with status '{row[2]}'"}), 400

    s3_key = row[1]

    # Reset job status
    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE ml_analysis.video_analysis_jobs
            SET status = 'queued', current_stage = 'queued', progress_pct = 0,
                error_message = NULL, updated_at = now()
            WHERE job_id = :jid
        """), {"jid": job_id})

        # Clear old detection data
        conn.execute(sql_text(
            "DELETE FROM ml_analysis.ball_detections WHERE job_id = :jid"
        ), {"jid": job_id})
        conn.execute(sql_text(
            "DELETE FROM ml_analysis.player_detections WHERE job_id = :jid"
        ), {"jid": job_id})

    # Submit new Batch job
    batch_job_queue = os.getenv("BATCH_JOB_QUEUE", "ten-fifty5-ml-queue")
    batch_job_def = os.getenv("BATCH_JOB_DEF", "ten-fifty5-ml-pipeline")

    try:
        batch = boto3.client("batch", region_name=AWS_REGION)
        resp = batch.submit_job(
            jobName=f"ml-pipeline-retry-{job_id[:8]}",
            jobQueue=batch_job_queue,
            jobDefinition=batch_job_def,
            containerOverrides={
                "command": [
                    "python", "-m", "ml_pipeline",
                    "--job-id", job_id,
                    "--s3-key", s3_key,
                ],
            },
            tags={
                "Project": "TEN-FIFTY5",
                "Environment": "production",
                "JobId": job_id,
                "Retry": "true",
            },
        )
        new_batch_id = resp["jobId"]

        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET batch_job_id = :bid, updated_at = now()
                WHERE job_id = :jid
            """), {"jid": job_id, "bid": new_batch_id})

        return jsonify({
            "job_id": job_id,
            "status": "queued",
            "batch_job_id": new_batch_id,
        })

    except Exception as e:
        logger.exception(f"Failed to resubmit Batch job for {job_id}")
        return jsonify({"error": f"batch submit failed: {e}"}), 500


def _serialize(val):
    """Make values JSON-safe."""
    import datetime
    if isinstance(val, datetime.datetime):
        return val.isoformat()
    if isinstance(val, datetime.date):
        return val.isoformat()
    return val
