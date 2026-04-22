"""Build a TrackNet V2 training dataset from SA-GT serve-bounce labels.

Wires together:
  1. label_serve_bounces.py — produces JSON with {hit_frame, bounce_frame_est,
     pixel_x, pixel_y} in SOURCE video space (1920x1080).
  2. extract_frames.py — pulls JPEGs from the video for the labeled frames
     (plus N frames before/after for TrackNet V2's 3-frame sliding window).
  3. tracknet_dataset.py — expects {frame_idx, x, y} in MODEL space (640x360).

This script:
  - Reads ONE OR MORE label JSONs (multi-match concatenation)
  - Extracts the needed JPEGs from each source video
  - Rescales pixel coords to 640x360
  - Writes a combined labels.json in tracknet_dataset format
  - Prints a readiness summary (label count, frame count, per-role breakdown)

Usage:
    python -m ml_pipeline.training.build_serve_bounce_dataset \\
        --label-json ml_pipeline/training/labels/8a5e0b5e_serve_bounces_v2.json \\
        --video      ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --output-dir ml_pipeline/training/datasets/match_90ad59a8

After this runs, training:
    python -m ml_pipeline.training.train_tracknet \\
        --frames-dir ml_pipeline/training/datasets/match_90ad59a8/frames \\
        --labels     ml_pipeline/training/datasets/match_90ad59a8/labels.json

Pairs (label-json, video) for multi-match datasets — pass the flag
multiple times in matching order.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List

import cv2

logger = logging.getLogger("build_serve_bounce_dataset")

# TrackNet V2 input size — must match tracknet_dataset.py defaults
MODEL_W = 640
MODEL_H = 360


def _extract_needed_frames(video_path: str, frame_indices: set, output_dir: str) -> int:
    """Extract ONLY the frames we need (label frame +/- 1 for 3-frame window).

    Avoids the cost of extracting all 15k frames from a 10-min match
    when we only need ~75 of them (25 serves * 3 frames).
    """
    needed = sorted(frame_indices)
    logger.info("extracting %d frames from %s", len(needed), video_path)
    os.makedirs(output_dir, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")

    n_written = 0
    last = -1
    try:
        for target in needed:
            # Sequential read from last position is cheaper than random seek
            # for a few-frame delta; use seek for big deltas.
            if target - last > 60:
                cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                last = target - 1
            while last < target:
                ok, frame = cap.read()
                if not ok:
                    logger.warning("ran out of frames at %d (wanted %d)", last + 1, target)
                    break
                last += 1
            if last != target:
                continue
            path = os.path.join(output_dir, f"frame_{target:06d}.jpg")
            cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            n_written += 1
    finally:
        cap.release()
    return n_written


def _rescale(pixel_xy: tuple, src_wh: tuple) -> tuple:
    src_w, src_h = src_wh
    x_model = pixel_xy[0] * MODEL_W / src_w
    y_model = pixel_xy[1] * MODEL_H / src_h
    return float(round(x_model, 3)), float(round(y_model, 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label-json", action="append", required=True,
                    help="Path to a label JSON (from label_serve_bounces.py). "
                         "Pass multiple times to combine matches.")
    ap.add_argument("--video", action="append", required=True,
                    help="Local video path, same order as --label-json")
    ap.add_argument("--output-dir", required=True,
                    help="Directory to write frames/ + labels.json into")
    ap.add_argument("--sequence-length", type=int, default=3,
                    help="TrackNet V2 sliding-window length (default 3). We "
                         "extract frames [bounce - (N-1), ..., bounce] so the "
                         "label applies to the LAST frame of the window — "
                         "matching the TrackNet V2 convention (predict ball "
                         "in frame t given frames [t-2, t-1, t]).")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if len(args.label_json) != len(args.video):
        logger.error("--label-json and --video must be passed in matching pairs "
                     "(got %d labels vs %d videos)",
                     len(args.label_json), len(args.video))
        return 2

    out_dir = Path(args.output_dir)
    frames_dir = out_dir / "frames"
    labels_path = out_dir / "labels.json"
    out_dir.mkdir(parents=True, exist_ok=True)

    merged_labels = []
    per_source_counts = []

    for lbl_path, vid_path in zip(args.label_json, args.video):
        if not os.path.exists(lbl_path):
            raise RuntimeError(f"label json not found: {lbl_path}")
        if not os.path.exists(vid_path):
            raise RuntimeError(f"video not found: {vid_path}")

        lj = json.load(open(lbl_path))
        src_w = int(lj.get("frame_width", 1920))
        src_h = int(lj.get("frame_height", 1080))
        task_short = lj.get("task_id", "unknown")[:8]
        logger.info(
            "loaded %s: %d labels, source=%dx%d",
            lbl_path, lj["label_count"], src_w, src_h,
        )

        # Decide what frames to extract. TrackNet V2 predicts frame t given
        # [t-(N-1), ..., t] so labels apply to window end. Extract frames
        # [bounce - (N-1), ..., bounce] — label stays on the bounce frame.
        needed_frames = set()
        per_label_rows = []
        N = args.sequence_length
        for l in lj["labels"]:
            cf = int(l["bounce_frame_est"])
            for delta in range(-(N - 1), 1):
                needed_frames.add(cf + delta)
            xm, ym = _rescale((l["pixel_x"], l["pixel_y"]), (src_w, src_h))
            per_label_rows.append({
                "frame_idx": cf,
                "x": xm,
                "y": ym,
                "_role": l.get("role"),
                "_source_match": task_short,
            })

        n = _extract_needed_frames(vid_path, needed_frames, frames_dir)
        logger.info("  wrote %d frames from %s", n, vid_path)

        merged_labels.extend(per_label_rows)
        per_source_counts.append((task_short, len(per_label_rows), n))

    # Write combined labels in tracknet_dataset.py expected format
    out = {
        "label_count": len(merged_labels),
        "labels": merged_labels,
    }
    labels_path.write_text(json.dumps(out, indent=2))

    # --- Summary ---
    logger.info("")
    logger.info("=== DATASET SUMMARY ===")
    logger.info("  frames:  %s", frames_dir)
    logger.info("  labels:  %s", labels_path)
    total_frames = sum(1 for _ in frames_dir.iterdir()) if frames_dir.exists() else 0
    logger.info("  total frames on disk: %d", total_frames)
    logger.info("  total labels: %d", len(merged_labels))
    per_role = {}
    for r in merged_labels:
        per_role[r.get("_role", "?")] = per_role.get(r.get("_role", "?"), 0) + 1
    logger.info("  per role: %s", per_role)
    logger.info("  per source match: %s", per_source_counts)

    if len(merged_labels) < 50:
        logger.warning(
            "LOW DATA WARNING: %d labels will overfit on fine-tune. "
            "Recommend dual-submitting at least 5+ matches to reach 100-200 labels.",
            len(merged_labels),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
