"""
ml_pipeline/bronze_ingest_t5.py — Ingest T5 ML pipeline bronze from S3 gzipped JSON.

Runs on Render (same region as Postgres) for fast bulk inserts.
Downloads the JSON exported by ml_pipeline/bronze_export.py on the Batch side.

Usage:
    from ml_pipeline.bronze_ingest_t5 import ingest_bronze_t5
    result = ingest_bronze_t5(job_id='...', engine=engine, replace=True)
"""

import gzip
import io
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import boto3
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

BRONZE_S3_KEY_TEMPLATE = "analysis/{job_id}/bronze.json.gz"


def _get_s3_client():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("s3", region_name=region)


def _download_bronze_from_s3(bucket: str, key: str, s3_client=None) -> Dict[str, Any]:
    """Download gzipped JSON from S3 and return the parsed dict."""
    if s3_client is None:
        s3_client = _get_s3_client()

    t0 = time.time()
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    download_ms = (time.time() - t0) * 1000
    logger.info(
        "bronze_ingest_t5: downloaded s3://%s/%s in %.0fms (%.1fMB gzipped)",
        bucket, key, download_ms, len(body) / 1024 / 1024,
    )

    t0 = time.time()
    decompressed = gzip.decompress(body)
    payload = json.loads(decompressed.decode("utf-8"))
    parse_ms = (time.time() - t0) * 1000
    logger.info(
        "bronze_ingest_t5: decompressed + parsed in %.0fms (%.1fMB uncompressed)",
        parse_ms, len(decompressed) / 1024 / 1024,
    )
    return payload


def _bulk_insert_ball_detections(conn_raw, job_id: str, rows: list) -> int:
    """Bulk insert ball detections using psycopg COPY (fast)."""
    if not rows:
        return 0

    # Use psycopg's COPY for maximum speed
    with conn_raw.cursor() as cur:
        with cur.copy(
            "COPY ml_analysis.ball_detections "
            "(job_id, frame_idx, x, y, court_x, court_y, speed_kmh, is_bounce, is_in) "
            "FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row([
                    job_id,
                    r.get("frame_idx"),
                    r.get("x"),
                    r.get("y"),
                    r.get("court_x"),
                    r.get("court_y"),
                    r.get("speed_kmh"),
                    r.get("is_bounce", False),
                    r.get("is_in"),
                ])
    return len(rows)


def _bulk_insert_player_detections(conn_raw, job_id: str, rows: list) -> int:
    """Bulk insert player detections using psycopg COPY (fast)."""
    if not rows:
        return 0

    with conn_raw.cursor() as cur:
        with cur.copy(
            "COPY ml_analysis.player_detections "
            "(job_id, frame_idx, player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2, "
            " center_x, center_y, court_x, court_y, keypoints) "
            "FROM STDIN"
        ) as copy:
            for r in rows:
                kp = r.get("keypoints")
                kp_json = json.dumps(kp) if kp is not None else None
                copy.write_row([
                    job_id,
                    r.get("frame_idx"),
                    r.get("player_id"),
                    r.get("bbox_x1"),
                    r.get("bbox_y1"),
                    r.get("bbox_x2"),
                    r.get("bbox_y2"),
                    r.get("center_x"),
                    r.get("center_y"),
                    r.get("court_x"),
                    r.get("court_y"),
                    kp_json,
                ])
    return len(rows)


def _upsert_match_analytics(conn, job_id: str, task_id: Optional[str], analytics: Dict[str, Any]) -> int:
    """Upsert match analytics row."""
    if not analytics:
        return 0

    conn.execute(sql_text("""
        INSERT INTO ml_analysis.match_analytics (
            job_id, task_id, ball_detection_rate, bounce_count, bounces_in, bounces_out,
            max_speed_kmh, avg_speed_kmh, rally_count, avg_rally_length,
            serve_count, first_serve_pct, player_count
        ) VALUES (
            :job_id, :task_id, :ball_detection_rate, :bounce_count, :bounces_in, :bounces_out,
            :max_speed_kmh, :avg_speed_kmh, :rally_count, :avg_rally_length,
            :serve_count, :first_serve_pct, :player_count
        )
        ON CONFLICT (job_id) DO UPDATE SET
            task_id = EXCLUDED.task_id,
            ball_detection_rate = EXCLUDED.ball_detection_rate,
            bounce_count = EXCLUDED.bounce_count,
            bounces_in = EXCLUDED.bounces_in,
            bounces_out = EXCLUDED.bounces_out,
            max_speed_kmh = EXCLUDED.max_speed_kmh,
            avg_speed_kmh = EXCLUDED.avg_speed_kmh,
            rally_count = EXCLUDED.rally_count,
            avg_rally_length = EXCLUDED.avg_rally_length,
            serve_count = EXCLUDED.serve_count,
            first_serve_pct = EXCLUDED.first_serve_pct,
            player_count = EXCLUDED.player_count
    """), {
        "job_id": job_id,
        "task_id": task_id,
        "ball_detection_rate": analytics.get("ball_detection_rate"),
        "bounce_count": analytics.get("bounce_count"),
        "bounces_in": analytics.get("bounces_in"),
        "bounces_out": analytics.get("bounces_out"),
        "max_speed_kmh": analytics.get("max_speed_kmh"),
        "avg_speed_kmh": analytics.get("avg_speed_kmh"),
        "rally_count": analytics.get("rally_count"),
        "avg_rally_length": analytics.get("avg_rally_length"),
        "serve_count": analytics.get("serve_count"),
        "first_serve_pct": analytics.get("first_serve_pct"),
        "player_count": analytics.get("player_count"),
    })
    return 1


def _update_job_metadata(conn, job_id: str, pipeline_meta: Dict[str, Any]) -> None:
    """Update video_analysis_jobs with pipeline metadata."""
    conn.execute(sql_text("""
        UPDATE ml_analysis.video_analysis_jobs
        SET video_fps = COALESCE(:video_fps, video_fps),
            video_duration_sec = COALESCE(:video_duration_sec, video_duration_sec),
            video_width = COALESCE(:video_width, video_width),
            video_height = COALESCE(:video_height, video_height),
            total_frames = COALESCE(:total_frames, total_frames),
            court_detected = COALESCE(:court_detected, court_detected),
            court_confidence = COALESCE(:court_confidence, court_confidence),
            processing_time_sec = COALESCE(:processing_time_sec, processing_time_sec),
            updated_at = now()
        WHERE job_id = :job_id
    """), {
        "job_id": job_id,
        "video_fps": pipeline_meta.get("video_fps"),
        "video_duration_sec": pipeline_meta.get("video_duration_sec"),
        "video_width": pipeline_meta.get("video_width"),
        "video_height": pipeline_meta.get("video_height"),
        "total_frames": pipeline_meta.get("total_frames"),
        "court_detected": pipeline_meta.get("court_detected"),
        "court_confidence": pipeline_meta.get("court_confidence"),
        "processing_time_sec": pipeline_meta.get("processing_time_sec"),
    })


def ingest_bronze_t5(
    job_id: str,
    engine=None,
    replace: bool = True,
    s3_bucket: Optional[str] = None,
    s3_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Download T5 bronze JSON from S3 and bulk-insert into ml_analysis.* tables.

    Args:
        job_id: ML analysis job_id (also used as task_id for T5)
        engine: SQLAlchemy engine (auto-resolved if None)
        replace: if True, delete existing rows for this job_id before inserting
        s3_bucket: S3 bucket (defaults to env S3_BUCKET)
        s3_key: explicit S3 key (defaults to analysis/{job_id}/bronze.json.gz)

    Returns:
        dict with counts: ball_rows, player_rows, analytics_row
    """
    if engine is None:
        from db_init import engine as db_engine
        engine = db_engine

    if s3_bucket is None:
        s3_bucket = os.environ.get("S3_BUCKET")
        if not s3_bucket:
            raise RuntimeError("S3_BUCKET env var not set and no s3_bucket arg provided")

    if s3_key is None:
        s3_key = BRONZE_S3_KEY_TEMPLATE.format(job_id=job_id)

    logger.info("bronze_ingest_t5: job_id=%s s3=%s/%s replace=%s",
                 job_id, s3_bucket, s3_key, replace)

    # Download + parse
    payload = _download_bronze_from_s3(s3_bucket, s3_key)

    if payload.get("schema_version") != 1:
        logger.warning("bronze_ingest_t5: unexpected schema_version=%s", payload.get("schema_version"))

    task_id = payload.get("task_id") or job_id
    pipeline_meta = payload.get("pipeline_metadata") or {}
    analytics = payload.get("match_analytics") or {}
    ball_rows = payload.get("ball_detections") or []
    player_rows = payload.get("player_detections") or []

    logger.info(
        "bronze_ingest_t5: parsed job_id=%s ball=%d player=%d",
        job_id, len(ball_rows), len(player_rows),
    )

    t0 = time.time()
    counts = {"ball_rows": 0, "player_rows": 0, "analytics_row": 0}

    with engine.begin() as conn:
        if replace:
            conn.execute(sql_text(
                "DELETE FROM ml_analysis.ball_detections WHERE job_id = :jid"
            ), {"jid": job_id})
            conn.execute(sql_text(
                "DELETE FROM ml_analysis.player_detections WHERE job_id = :jid"
            ), {"jid": job_id})
            conn.execute(sql_text(
                "DELETE FROM ml_analysis.match_analytics WHERE job_id = :jid"
            ), {"jid": job_id})

        # Update job metadata (singleton)
        _update_job_metadata(conn, job_id, pipeline_meta)

        # Match analytics (singleton upsert)
        counts["analytics_row"] = _upsert_match_analytics(conn, job_id, task_id, analytics)

        # Bulk ball + player via COPY — need the underlying psycopg connection
        raw = conn.connection  # DBAPI connection (psycopg)
        dbapi_conn = getattr(raw, "driver_connection", None) or raw
        counts["ball_rows"] = _bulk_insert_ball_detections(dbapi_conn, job_id, ball_rows)
        counts["player_rows"] = _bulk_insert_player_detections(dbapi_conn, job_id, player_rows)

    elapsed_ms = (time.time() - t0) * 1000
    logger.info(
        "bronze_ingest_t5: inserted job_id=%s ball=%d player=%d in %.0fms",
        job_id, counts["ball_rows"], counts["player_rows"], elapsed_ms,
    )
    return counts
