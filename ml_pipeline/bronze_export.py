"""
ml_pipeline/bronze_export.py — Export ML pipeline results to gzipped JSON on S3.

Replaces direct cross-region DB writes (which were painfully slow: 22min for 13K rows).
Mirrors SportAI's delivery pattern: one compressed JSON file on S3, ingested by the
Render service in the same region as the database.

Usage (from ml_pipeline/__main__.py, Batch side):

    from ml_pipeline.bronze_export import export_bronze_to_s3
    s3_key = export_bronze_to_s3(
        job_id=job_id, task_id=task_id, result=result,
        s3_client=s3, s3_bucket=s3_bucket,
        practice=practice,
    )

The file lands at: s3://{bucket}/analysis/{job_id}/bronze.json.gz
"""

import gzip
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BRONZE_S3_KEY_TEMPLATE = "analysis/{job_id}/bronze.json.gz"


def _ball_detection_to_dict(d) -> Dict[str, Any]:
    """Convert BallDetection dataclass → plain dict for JSON serialization."""
    return {
        "frame_idx": int(d.frame_idx),
        "x": float(d.x) if d.x is not None else None,
        "y": float(d.y) if d.y is not None else None,
        "court_x": float(d.court_x) if d.court_x is not None else None,
        "court_y": float(d.court_y) if d.court_y is not None else None,
        "speed_kmh": float(d.speed_kmh) if d.speed_kmh is not None else None,
        "is_bounce": bool(d.is_bounce),
        "is_in": bool(d.is_in) if d.is_in is not None else None,
    }


def _player_detection_to_dict(d) -> Dict[str, Any]:
    """Convert PlayerDetection dataclass → plain dict for JSON serialization."""
    bbox = d.bbox if hasattr(d, "bbox") else (None, None, None, None)
    center = d.center if hasattr(d, "center") else (None, None)

    # Keypoints: numpy array (17, 3) → nested list
    keypoints = None
    if getattr(d, "keypoints", None) is not None:
        try:
            kp = d.keypoints
            if hasattr(kp, "tolist"):
                keypoints = kp.tolist()
            else:
                keypoints = list(kp)
        except Exception:
            keypoints = None

    return {
        "frame_idx": int(d.frame_idx),
        "player_id": int(d.player_id),
        "bbox_x1": float(bbox[0]) if bbox[0] is not None else None,
        "bbox_y1": float(bbox[1]) if bbox[1] is not None else None,
        "bbox_x2": float(bbox[2]) if bbox[2] is not None else None,
        "bbox_y2": float(bbox[3]) if bbox[3] is not None else None,
        "center_x": float(center[0]) if center[0] is not None else None,
        "center_y": float(center[1]) if center[1] is not None else None,
        "court_x": float(d.court_x) if d.court_x is not None else None,
        "court_y": float(d.court_y) if d.court_y is not None else None,
        "keypoints": keypoints,
    }


def build_bronze_payload(
    job_id: str,
    task_id: Optional[str],
    result,
    practice: bool = False,
    filter_players_to_ball_frames: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Build the complete bronze JSON payload from ML pipeline result.

    Args:
        job_id: ML job identifier
        task_id: Associated task_id (usually same as job_id for T5)
        result: TennisAnalysisPipeline result object with ball_detections,
                player_detections, match_analytics, and metadata attributes
        practice: practice mode flag (filters player detections if True by default)
        filter_players_to_ball_frames: if True, only keep player detections at frames
            where a ball was detected. Defaults to the value of `practice`.

    Returns:
        Dict ready for JSON serialization.
    """
    if filter_players_to_ball_frames is None:
        filter_players_to_ball_frames = practice

    ball_dets = list(result.ball_detections or [])
    player_dets = list(result.player_detections or [])

    if filter_players_to_ball_frames:
        ball_frames = {d.frame_idx for d in ball_dets}
        before = len(player_dets)
        player_dets = [d for d in player_dets if d.frame_idx in ball_frames]
        logger.info(
            "bronze_export: filtered player detections %d -> %d (practice/ball-frame mode)",
            before, len(player_dets),
        )

    # Pipeline video metadata (from VideoMetadata dataclass)
    vm = getattr(result, "video_metadata", None)
    video_fps = float(vm.fps) if vm and getattr(vm, "fps", None) is not None else None
    video_duration_sec = float(vm.duration_sec) if vm and getattr(vm, "duration_sec", None) is not None else None
    video_width = int(vm.width) if vm and getattr(vm, "width", None) is not None else None
    video_height = int(vm.height) if vm and getattr(vm, "height", None) is not None else None

    payload = {
        "schema_version": SCHEMA_VERSION,
        "job_id": str(job_id),
        "task_id": str(task_id) if task_id else None,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "practice_mode": bool(practice),
        "pipeline_metadata": {
            "total_frames": int(getattr(result, "total_frames_processed", 0) or 0),
            "video_fps": video_fps,
            "video_duration_sec": video_duration_sec,
            "video_width": video_width,
            "video_height": video_height,
            "court_detected": bool(getattr(result, "court_detected", False)),
            "court_confidence": float(getattr(result, "court_confidence", 0.0) or 0.0),
            "court_used_fallback": bool(getattr(result, "court_used_fallback", False)),
            "processing_time_sec": float(getattr(result, "processing_time_sec", 0.0) or 0.0),
            "ms_per_frame": float(getattr(result, "ms_per_frame", 0.0) or 0.0),
            "frame_errors": int(getattr(result, "frame_errors", 0) or 0),
        },
        "match_analytics": {
            "ball_detection_rate": float(getattr(result, "ball_detection_rate", 0.0) or 0.0),
            "bounce_count": int(getattr(result, "bounce_count", 0) or 0),
            "bounces_in": int(getattr(result, "bounces_in", 0) or 0),
            "bounces_out": int(getattr(result, "bounces_out", 0) or 0),
            "max_speed_kmh": float(getattr(result, "max_speed_kmh", 0.0) or 0.0),
            "avg_speed_kmh": float(getattr(result, "avg_speed_kmh", 0.0) or 0.0),
            "rally_count": int(getattr(result, "rally_count", 0) or 0),
            "avg_rally_length": float(getattr(result, "avg_rally_length", 0.0) or 0.0),
            "serve_count": int(getattr(result, "serve_count", 0) or 0),
            "first_serve_pct": float(getattr(result, "first_serve_pct", 0.0) or 0.0),
            "player_count": int(getattr(result, "player_count", 0) or 0),
        },
        "ball_detections": [_ball_detection_to_dict(d) for d in ball_dets],
        "player_detections": [_player_detection_to_dict(d) for d in player_dets],
    }

    logger.info(
        "bronze_export: built payload ball=%d player=%d",
        len(payload["ball_detections"]), len(payload["player_detections"]),
    )
    return payload


def export_bronze_to_s3(
    job_id: str,
    task_id: Optional[str],
    result,
    s3_client,
    s3_bucket: str,
    practice: bool = False,
) -> str:
    """
    Build the bronze payload, gzip it, and upload to S3.

    Returns the S3 key of the uploaded file.
    """
    payload = build_bronze_payload(
        job_id=job_id, task_id=task_id, result=result, practice=practice,
    )

    # Serialize with compact separators (smaller file)
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    uncompressed_size = len(payload_json.encode("utf-8"))

    # Gzip compress
    payload_gz = gzip.compress(payload_json.encode("utf-8"), compresslevel=6)
    compressed_size = len(payload_gz)

    logger.info(
        "bronze_export: job_id=%s sizes uncompressed=%.1fMB compressed=%.1fMB ratio=%.1fx",
        job_id,
        uncompressed_size / 1024 / 1024,
        compressed_size / 1024 / 1024,
        uncompressed_size / max(compressed_size, 1),
    )

    # Upload to S3
    s3_key = BRONZE_S3_KEY_TEMPLATE.format(job_id=job_id)
    s3_client.put_object(
        Bucket=s3_bucket,
        Key=s3_key,
        Body=payload_gz,
        ContentType="application/json",
        ContentEncoding="gzip",
    )
    logger.info("bronze_export: uploaded s3://%s/%s", s3_bucket, s3_key)
    return s3_key
