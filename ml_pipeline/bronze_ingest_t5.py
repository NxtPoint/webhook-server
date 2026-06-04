"""
ml_pipeline/bronze_ingest_t5.py — Ingest T5 ML pipeline bronze from S3 gzipped JSON.

Runs on Render (same region as Postgres) for fast bulk inserts.
Downloads the JSON exported by ml_pipeline/bronze_export.py on the Batch side.

Memory model (Lever — OOM fix 2026-05-27): the export for a long match is large
(e.g. a 44-min match = 71.7 MB decompressed JSON, 35k ball + 72k player
detections with keypoints). The old path did get_object().read() +
gzip.decompress() + json.loads() of the WHOLE payload, peaking ~400-540 MB and
OOM-killing the 512 MB main API on long matches (player_detections never landed
→ stuck ingest). This version streams instead: download the gz to a temp FILE
(disk, not RAM), grab the small top-level fields with targeted ijson reads, and
stream each big array straight into psycopg COPY one row at a time — peak memory
is bounded to ~a single row regardless of match length. Output is identical
(ijson use_float=True matches json.loads' float parsing).

Usage:
    from ml_pipeline.bronze_ingest_t5 import ingest_bronze_t5
    result = ingest_bronze_t5(job_id='...', engine=engine, replace=True)
"""

import gzip
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

import boto3
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

BRONZE_S3_KEY_TEMPLATE = "analysis/{job_id}/bronze.json.gz"


def _get_s3_client():
    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("s3", region_name=region)


def _download_bronze_to_tempfile(bucket: str, key: str, s3_client=None) -> str:
    """Stream the gzipped JSON from S3 to a temp file (NOT into memory).

    Returns the local path; caller is responsible for deleting it."""
    if s3_client is None:
        s3_client = _get_s3_client()
    fd, path = tempfile.mkstemp(suffix=".json.gz", prefix="t5bronze_")
    os.close(fd)
    t0 = time.time()
    s3_client.download_file(bucket, key, path)
    logger.info(
        "bronze_ingest_t5: downloaded s3://%s/%s in %.0fms (%.1fMB gzipped)",
        bucket, key, (time.time() - t0) * 1000, os.path.getsize(path) / 1024 / 1024,
    )
    return path


def _stream_top_field(gz_path: str, prefix: str, default=None):
    """Return the first ijson item at a top-level prefix (scalar or object),
    streaming so the big arrays are never materialised. use_float=True matches
    json.loads' number parsing (non-integers as float, not Decimal)."""
    import ijson
    with gzip.open(gz_path, "rb") as f:
        for v in ijson.items(f, prefix, use_float=True):
            return v
    return default


def _as_int(v):
    return int(v) if v is not None else None


def _copy_ball_detections_stream(conn_raw, job_id: str, gz_path: str) -> int:
    """Stream ball_detections from the gz file straight into COPY, one row at a
    time (no full-list materialisation)."""
    import ijson
    n = 0
    with conn_raw.cursor() as cur:
        with cur.copy(
            "COPY ml_analysis.ball_detections "
            "(job_id, frame_idx, x, y, court_x, court_y, speed_kmh, is_bounce, is_in) "
            "FROM STDIN"
        ) as copy:
            with gzip.open(gz_path, "rb") as f:
                for r in ijson.items(f, "ball_detections.item", use_float=True):
                    copy.write_row([
                        job_id,
                        _as_int(r.get("frame_idx")),
                        r.get("x"),
                        r.get("y"),
                        r.get("court_x"),
                        r.get("court_y"),
                        r.get("speed_kmh"),
                        r.get("is_bounce", False),
                        r.get("is_in"),
                    ])
                    n += 1
    return n


def _copy_player_detections_stream(conn_raw, job_id: str, gz_path: str) -> int:
    """Stream player_detections into COPY one row at a time. Reconstructs nested
    keypoints from flat [x,y,c,...] back to [[x,y,c],...] to match the JSONB
    schema used by stroke inference (identical to the prior implementation)."""
    import ijson
    n = 0
    with conn_raw.cursor() as cur:
        with cur.copy(
            "COPY ml_analysis.player_detections "
            "(job_id, frame_idx, player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2, "
            " center_x, center_y, court_x, court_y, keypoints, stroke_class) "
            "FROM STDIN"
        ) as copy:
            with gzip.open(gz_path, "rb") as f:
                for r in ijson.items(f, "player_detections.item", use_float=True):
                    kp_flat = r.get("keypoints")
                    kp_json = None
                    if kp_flat is not None and len(kp_flat) >= 51:
                        nested = [[kp_flat[i * 3], kp_flat[i * 3 + 1], kp_flat[i * 3 + 2]]
                                  for i in range(17)]
                        kp_json = json.dumps(nested)
                    copy.write_row([
                        job_id,
                        _as_int(r.get("frame_idx")),
                        _as_int(r.get("player_id")),
                        r.get("bbox_x1"),
                        r.get("bbox_y1"),
                        r.get("bbox_x2"),
                        r.get("bbox_y2"),
                        r.get("center_x"),
                        r.get("center_y"),
                        r.get("court_x"),
                        r.get("court_y"),
                        kp_json,
                        r.get("stroke_class"),  # swing-type fact from Batch (may be None)
                    ])
                    n += 1
    return n


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

    Streams the (potentially large) export so peak memory stays bounded — see
    the module docstring for the OOM history.

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

    gz_path = _download_bronze_to_tempfile(s3_bucket, s3_key)
    try:
        # Small top-level fields (tiny) — streamed so the big arrays aren't built.
        t0 = time.time()
        schema_version = _stream_top_field(gz_path, "schema_version")
        if schema_version != 1:
            logger.warning("bronze_ingest_t5: unexpected schema_version=%s", schema_version)
        task_id = _stream_top_field(gz_path, "task_id") or job_id
        pipeline_meta = _stream_top_field(gz_path, "pipeline_metadata", {}) or {}
        analytics = _stream_top_field(gz_path, "match_analytics", {}) or {}
        logger.info("bronze_ingest_t5: read metadata fields in %.0fms", (time.time() - t0) * 1000)

        t0 = time.time()
        counts = {"ball_rows": 0, "player_rows": 0, "analytics_row": 0}

        with engine.begin() as conn:
            if replace:
                # Preserve source='roi_prod' bounces: roi_bounces (Batch) writes
                # them DIRECTLY to ml_analysis and self-replaces within its own
                # run, but they are NOT in this JSON export — a blanket DELETE
                # here wiped ~16 min of ROI bounce work on every auto-ingest.
                # Delete only the main/null-source rows the COPY re-inserts.
                conn.execute(sql_text(
                    "DELETE FROM ml_analysis.ball_detections "
                    "WHERE job_id = :jid AND source IS DISTINCT FROM 'roi_prod'"
                ), {"jid": job_id})
                conn.execute(sql_text(
                    "DELETE FROM ml_analysis.player_detections WHERE job_id = :jid"
                ), {"jid": job_id})
                conn.execute(sql_text(
                    "DELETE FROM ml_analysis.match_analytics WHERE job_id = :jid"
                ), {"jid": job_id})

            # Singletons (small)
            _update_job_metadata(conn, job_id, pipeline_meta)
            counts["analytics_row"] = _upsert_match_analytics(conn, job_id, task_id, analytics)

            # Bulk ball + player via streamed COPY — need the underlying psycopg conn
            raw = conn.connection  # DBAPI connection (psycopg)
            dbapi_conn = getattr(raw, "driver_connection", None) or raw
            counts["ball_rows"] = _copy_ball_detections_stream(dbapi_conn, job_id, gz_path)
            counts["player_rows"] = _copy_player_detections_stream(dbapi_conn, job_id, gz_path)

        logger.info(
            "bronze_ingest_t5: inserted job_id=%s ball=%d player=%d in %.0fms",
            job_id, counts["ball_rows"], counts["player_rows"], (time.time() - t0) * 1000,
        )
        return counts
    finally:
        try:
            os.unlink(gz_path)
        except Exception:
            pass
