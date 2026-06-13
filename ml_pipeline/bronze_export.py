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
    """Convert BallDetection dataclass → plain dict for JSON serialization.

    source='main' tags the global WASB ball. roi_far_ball rows are carried
    separately via build_bronze_payload(extra_ball_rows=...) so they survive
    the Render re-ingest's blanket DELETE+COPY (the export+reingest-carry rule).
    """
    return {
        "frame_idx": int(d.frame_idx),
        "x": float(d.x) if d.x is not None else None,
        "y": float(d.y) if d.y is not None else None,
        "court_x": float(d.court_x) if d.court_x is not None else None,
        "court_y": float(d.court_y) if d.court_y is not None else None,
        "speed_kmh": float(d.speed_kmh) if d.speed_kmh is not None else None,
        "is_bounce": bool(d.is_bounce),
        "is_in": bool(d.is_in) if d.is_in is not None else None,
        "source": getattr(d, "source", None) or "main",
    }


def _player_detection_to_dict(d) -> Dict[str, Any]:
    """Convert PlayerDetection dataclass → plain dict for JSON serialization.

    Keypoints stored as flat array [x1,y1,c1,x2,y2,c2,...] (17 keypoints × 3 = 51 floats).
    This is ~30% smaller than nested arrays in JSON.
    """
    bbox = d.bbox if hasattr(d, "bbox") else (None, None, None, None)
    center = d.center if hasattr(d, "center") else (None, None)

    # Keypoints: numpy array (17, 3) → flat list of 51 floats
    keypoints_flat = None
    if getattr(d, "keypoints", None) is not None:
        try:
            kp = d.keypoints
            if hasattr(kp, "flatten"):
                keypoints_flat = [float(v) for v in kp.flatten()]
            else:
                # Already a list — flatten manually
                keypoints_flat = [float(v) for row in kp for v in row]
        except Exception:
            keypoints_flat = None

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
        "keypoints": keypoints_flat,
        # Swing-type classifier output (bronze fact). Without this, the Batch-side
        # stroke_class is lost in the export→Render-reingest round-trip (the
        # reingest DELETEs + re-COPYs from this JSON), so silver never sees the
        # model and falls back to the pose heuristic. 2026-06-04.
        "stroke_class": getattr(d, "stroke_class", None),
    }


def build_bronze_payload(
    job_id: str,
    task_id: Optional[str],
    result,
    practice: bool = False,
    player_window_frames: int = 5,
    extra_ball_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Build the complete bronze JSON payload from ML pipeline result.

    Optimization: SportAI sends ~5K rows total in 14MB. We're aggressively
    filtered to match — most non-bounce frames are useless to the silver builder.
    Strategy:
      - ball_detections: keep ALL (small ~70 bytes/row, useful for trajectory)
      - player_detections: keep only those within ±N frames of any bounce
        (the silver builder only needs nearest-player to each bounce)

    Args:
        job_id: ML job identifier
        task_id: Associated task_id (usually same as job_id for T5)
        result: TennisAnalysisPipeline result object
        practice: practice mode flag
        player_window_frames: keep player detections within ±N frames of bounces (default 5)

    Returns:
        Dict ready for JSON serialization.
    """
    ball_dets = list(result.ball_detections or [])
    player_dets_full = list(result.player_detections or [])

    # D1 v3 spectator drop — SAME predicate as db_writer.save_player_
    # detections, applied here TOO because this payload is built from the
    # IN-MEMORY result and the Render re-ingest (DELETE+COPY from this
    # JSON) wipes the direct DB write — p13 proved the db_writer-only drop
    # gets undone by the export->ingest path (the export+reingest-carry
    # rule, feedback_batch_enrichments_need_export_reingest_carry). Rows
    # whose STORED (strict-bounded, trustworthy) court_x sits clearly off
    # the playing surface are spectators; NULL-coord rows always pass
    # (the far player's unprojectable majority).
    _n_pre_drop = len(player_dets_full)
    player_dets_full = [
        d for d in player_dets_full
        if d.court_x is None or -2.0 <= d.court_x <= 12.97  # doubles W + 2m
    ]
    if len(player_dets_full) != _n_pre_drop:
        logger.info(
            "bronze_export: dropped %d off-court-x player rows "
            "(spectator band) of %d", _n_pre_drop - len(player_dets_full),
            _n_pre_drop)

    # Filter NON-POSE player detections to a window around each bounce —
    # silver only needs nearest-player per bounce. But keep ALL pose-
    # carrying rows (keypoints is not None). The serve_detector needs
    # continuous pose data across rally-entry sequences (trophy pose
    # happens ~0.5-1s BEFORE the ball hit), so a ±N-frames-from-bounce
    # filter starves it of exactly the frames that matter. Prod eval on
    # task 9fe8c096 returned 0/24 serves while offline validation from
    # locally-extracted pose gave 12/14 — the gap was this filter.
    # Pose rows are ~500 bytes each; keeping them all adds ~2-3 MB to a
    # typical match's bronze JSON. Still well within SportAI-comparable
    # scale (~14 MB / 5K rows).
    bounce_frames = sorted({d.frame_idx for d in ball_dets if d.is_bounce})
    keep_frames = set()
    for bf in bounce_frames:
        for offset in range(-player_window_frames, player_window_frames + 1):
            keep_frames.add(bf + offset)

    player_dets = [
        d for d in player_dets_full
        if (d.frame_idx in keep_frames) or (d.keypoints is not None)
    ]
    n_pose = sum(1 for d in player_dets if d.keypoints is not None)
    logger.info(
        "bronze_export: player_detections %d -> %d kept (%d pose-carrying, "
        "remaining filtered to ±%d frames around %d bounces)",
        len(player_dets_full), len(player_dets), n_pose,
        player_window_frames, len(bounce_frames),
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

    # Carry roi_far_ball rows (DB-direct writes from the far-ball ROI sweep)
    # into the export so they survive the Render re-ingest's blanket DELETE+COPY
    # (export+reingest-carry rule). Appended AFTER the bounce/keep computation so
    # they never shift the player keep-window; readers dedup via ball_merge, and
    # silver stays main-only — so these extra rows feed only the hit/bounce
    # trajectory readers, never silver's bounce-driven row count.
    if extra_ball_rows:
        for r in extra_ball_rows:
            payload["ball_detections"].append({
                "frame_idx": int(r["frame_idx"]),
                "x": float(r["x"]) if r.get("x") is not None else None,
                "y": float(r["y"]) if r.get("y") is not None else None,
                "court_x": float(r["court_x"]) if r.get("court_x") is not None else None,
                "court_y": float(r["court_y"]) if r.get("court_y") is not None else None,
                "speed_kmh": float(r["speed_kmh"]) if r.get("speed_kmh") is not None else None,
                "is_bounce": bool(r.get("is_bounce")),
                "is_in": bool(r["is_in"]) if r.get("is_in") is not None else None,
                "source": r.get("source") or "roi_far_ball",
            })
        logger.info("bronze_export: carried %d roi_far_ball rows into payload",
                    len(extra_ball_rows))

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
    extra_ball_rows: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Build the bronze payload, gzip it, and upload to S3.

    Returns the S3 key of the uploaded file.
    """
    payload = build_bronze_payload(
        job_id=job_id, task_id=task_id, result=result, practice=practice,
        extra_ball_rows=extra_ball_rows,
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
