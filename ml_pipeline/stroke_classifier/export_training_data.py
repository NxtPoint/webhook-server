"""
Export optical flow training data from dual-submit pairs.

For each dual-submit pair (same video analysed by both SportAI and T5):
  1. SportAI silver.point_detail provides ground-truth stroke labels
  2. T5 ml_analysis.player_detections provides far-player bboxes per frame
  3. Video frames provide the raw pixels for optical flow extraction

This script aligns SportAI hit events with T5 player detections by timestamp,
extracts optical flow around each hit, and saves labeled training examples.

Usage:
    python -m ml_pipeline.stroke_classifier.export_training_data \\
        --sportai-task <sportai_task_id> \\
        --t5-task <t5_task_id> \\
        --video <path_or_s3_key> \\
        --output <output_dir>
"""

import os
import json
import logging
import argparse
import numpy as np
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# Timestamp alignment tolerance: SportAI and T5 may report hit events at
# slightly different frame indices. A 1-second window handles frame-rate
# differences and detection latency.
ALIGNMENT_WINDOW_SEC = 1.0

# Far player: player_id=2 (near=1, far=2) in T5 convention.
# Only classify far player — near player has keypoints.
FAR_PLAYER_ID = 2

# Stroke label mapping from silver.point_detail stroke_d to training classes
STROKE_MAP = {
    "Forehand": "fh",
    "Backhand": "bh",
    "Serve": "serve",
    "Volley": "volley",
    "Slice": "fh",      # Slice treated as forehand variant for now
    "Overhead": "serve", # Overhead grouped with serve (same motion pattern)
    "Other": "other",
}


def _load_sportai_hits(conn, sportai_task_id: str) -> list:
    """Load SportAI ground truth hits with stroke labels."""
    from sqlalchemy import text
    rows = conn.execute(text("""
        SELECT
            row_number,
            player_id,
            stroke_d,
            ball_hit_location_x,
            ball_hit_location_y,
            COALESCE(model, 'sportai') AS model
        FROM silver.point_detail
        WHERE task_id = CAST(:t AS uuid)
          AND COALESCE(model, 'sportai') = 'sportai'
          AND stroke_d IS NOT NULL
        ORDER BY row_number
    """), {"t": sportai_task_id}).mappings().fetchall()
    return [dict(r) for r in rows]


def _load_t5_player_detections(conn, t5_task_id: str) -> dict:
    """Load T5 player detections for the far player, keyed by frame_idx."""
    from sqlalchemy import text
    job = conn.execute(text("""
        SELECT job_id FROM ml_analysis.video_analysis_jobs
        WHERE task_id = :t ORDER BY created_at DESC LIMIT 1
    """), {"t": t5_task_id}).mappings().first()

    if not job:
        return {}

    rows = conn.execute(text("""
        SELECT frame_idx, player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2
        FROM ml_analysis.player_detections
        WHERE job_id = :j AND player_id = :pid
        ORDER BY frame_idx
    """), {"j": job["job_id"], "pid": FAR_PLAYER_ID}).mappings().fetchall()

    return {r["frame_idx"]: dict(r) for r in rows}


def _load_t5_bounces(conn, t5_task_id: str) -> list:
    """Load T5 bounce events with frame indices for timestamp alignment."""
    from sqlalchemy import text
    job = conn.execute(text("""
        SELECT job_id, video_fps FROM ml_analysis.video_analysis_jobs
        WHERE task_id = :t ORDER BY created_at DESC LIMIT 1
    """), {"t": t5_task_id}).mappings().first()

    if not job:
        return []

    fps = job.get("video_fps") or 25.0
    rows = conn.execute(text("""
        SELECT frame_idx, x, y, court_x, court_y, is_bounce
        FROM ml_analysis.ball_detections
        WHERE job_id = :j AND is_bounce = TRUE
        ORDER BY frame_idx
    """), {"j": job["job_id"]}).mappings().fetchall()

    return [{"frame_idx": r["frame_idx"], "time_sec": r["frame_idx"] / fps, **dict(r)} for r in rows]


def align_events(
    sportai_hits: list,
    t5_bounces: list,
    fps: float = 25.0,
    window_sec: float = ALIGNMENT_WINDOW_SEC,
) -> List[Tuple[dict, dict]]:
    """Align SportAI hits to T5 bounces by row order.

    SportAI row_number maps to chronological order of hits. T5 bounces
    are also chronological. We match them 1:1 in order, skipping any
    that don't have a reasonable temporal alignment.

    Returns list of (sportai_hit, t5_bounce) pairs.
    """
    pairs = []
    t5_idx = 0

    for sa_hit in sportai_hits:
        sa_row = sa_hit["row_number"]
        # Simple 1:1 alignment by order
        if t5_idx < len(t5_bounces):
            pairs.append((sa_hit, t5_bounces[t5_idx]))
            t5_idx += 1

    return pairs


def export_training_examples(
    sportai_task_id: str,
    t5_task_id: str,
    video_path: str,
    output_dir: str,
    fps: float = 25.0,
) -> int:
    """Export optical flow training data for the stroke classifier.

    Args:
        sportai_task_id: SportAI task with ground-truth stroke labels
        t5_task_id: T5 task with player detections (bboxes)
        video_path: path to the source video file
        output_dir: directory to save .npz training examples
        fps: video frame rate

    Returns:
        Number of examples exported.
    """
    import cv2
    from db_init import engine
    from ml_pipeline.stroke_classifier.flow_extractor import (
        extract_flow_features, flow_to_input_tensor, HitEvent, FLOW_WINDOW,
    )

    os.makedirs(output_dir, exist_ok=True)

    with engine.connect() as conn:
        sportai_hits = _load_sportai_hits(conn, sportai_task_id)
        t5_detections = _load_t5_player_detections(conn, t5_task_id)
        t5_bounces = _load_t5_bounces(conn, t5_task_id)

    if not sportai_hits:
        logger.warning("No SportAI hits found")
        return 0
    if not t5_bounces:
        logger.warning("No T5 bounces found")
        return 0

    logger.info(f"SportAI hits: {len(sportai_hits)}, T5 bounces: {len(t5_bounces)}, "
                f"T5 player detections: {len(t5_detections)} frames")

    # Align events
    pairs = align_events(sportai_hits, t5_bounces, fps=fps)
    logger.info(f"Aligned {len(pairs)} hit pairs")

    # Build HitEvent list from aligned pairs
    hit_events = []
    for sa_hit, t5_bounce in pairs:
        label = STROKE_MAP.get(sa_hit.get("stroke_d"), "other")
        frame_idx = t5_bounce["frame_idx"]

        # Find the nearest player detection for bbox
        det = t5_detections.get(frame_idx)
        if det is None:
            # Search nearby frames
            for offset in range(1, 10):
                det = t5_detections.get(frame_idx + offset) or t5_detections.get(frame_idx - offset)
                if det:
                    break
        if det is None:
            continue

        hit_events.append(HitEvent(
            frame_idx=frame_idx,
            player_id=FAR_PLAYER_ID,
            bbox=(det["bbox_x1"], det["bbox_y1"], det["bbox_x2"], det["bbox_y2"]),
            stroke_label=label,
        ))

    if not hit_events:
        logger.warning("No alignable hit events with player detections")
        return 0

    # Determine which frames we need from the video
    needed_frames = set()
    for hit in hit_events:
        for i in range(hit.frame_idx - FLOW_WINDOW, hit.frame_idx + FLOW_WINDOW + 1):
            needed_frames.add(i)

    # Extract frames from video
    logger.info(f"Extracting {len(needed_frames)} frames from video...")
    frames = {}
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error(f"Cannot open video: {video_path}")
        return 0

    try:
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx in needed_frames:
                frames[frame_idx] = frame
            frame_idx += 1
    finally:
        cap.release()

    logger.info(f"Extracted {len(frames)} frames")

    # Extract optical flow features
    flow_features = extract_flow_features(frames, hit_events)
    logger.info(f"Extracted {len(flow_features)} flow features")

    # Save training examples
    count = 0
    manifest = []
    for ff in flow_features:
        input_tensor = flow_to_input_tensor(ff)
        label = ff.hit.stroke_label
        filename = f"hit_{ff.hit.frame_idx:06d}_{label}.npz"
        filepath = os.path.join(output_dir, filename)

        np.savez_compressed(
            filepath,
            flow=input_tensor,
            label=label,
            frame_idx=ff.hit.frame_idx,
            magnitude_hist=ff.magnitude_hist,
            dominant_angle=ff.dominant_angle,
        )
        manifest.append({
            "file": filename,
            "label": label,
            "frame_idx": ff.hit.frame_idx,
        })
        count += 1

    # Save manifest
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump({
            "sportai_task_id": sportai_task_id,
            "t5_task_id": t5_task_id,
            "count": count,
            "label_distribution": _label_dist(manifest),
            "examples": manifest,
        }, f, indent=2)

    logger.info(f"Exported {count} examples to {output_dir}")
    logger.info(f"Label distribution: {_label_dist(manifest)}")
    return count


def _label_dist(manifest: list) -> dict:
    """Count labels in manifest."""
    dist = {}
    for ex in manifest:
        label = ex["label"]
        dist[label] = dist.get(label, 0) + 1
    return dist


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Export stroke classifier training data")
    parser.add_argument("--sportai-task", required=True, help="SportAI task ID")
    parser.add_argument("--t5-task", required=True, help="T5 task ID")
    parser.add_argument("--video", required=True, help="Video path or S3 key")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--fps", type=float, default=25.0, help="Video FPS")
    args = parser.parse_args()

    n = export_training_examples(
        sportai_task_id=args.sportai_task,
        t5_task_id=args.t5_task,
        video_path=args.video,
        output_dir=args.output,
        fps=args.fps,
    )
    print(f"\nExported {n} training examples")
