"""Reproduce the pose-coverage gap by running PlayerTracker locally on
frames from minutes 1-4 of the baseline video, with heavy logging.

Answers: does YOLOv8x-pose output pose-carrying detections for Player 0
on these frames? If yes → something later in the tracker drops them.
If no → upstream model behaviour differs from isolated YOLO calls.

Usage (repo root, .venv active):
    python -m ml_pipeline.diag.repro_pose_gap
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import cv2
import numpy as np

# Force verbose logging for the tracker module before import
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(name)s: %(message)s')

from ml_pipeline.player_tracker import PlayerTracker
from ml_pipeline.config import YOLO_IMGSZ, YOLO_CONFIDENCE, PLAYER_DETECTION_INTERVAL

VIDEO = Path("ml_pipeline/test_videos/match_90ad59a8.mp4.mp4")

# Probe frames: 1 per "minute" of interest, scattered across the gap.
# These correspond to SA near-player serve moments so we know the real
# player SHOULD be detectable.
PROBE_FRAMES = [
    375,    # minute 0 — known good (92% pose)
    1830,   # minute 1 ~SA ts=73
    3007,   # minute 2 ~SA ts=120
    4461,   # minute 3 ~SA ts=178
    6000,   # minute 4
    11451,  # minute 7 — known good
]


def main():
    if not VIDEO.exists():
        print(f"Missing video: {VIDEO}", file=sys.stderr)
        return 2

    print(f"config: YOLO_IMGSZ={YOLO_IMGSZ}  YOLO_CONFIDENCE={YOLO_CONFIDENCE}  "
          f"PLAYER_DETECTION_INTERVAL={PLAYER_DETECTION_INTERVAL}")
    print()

    tracker = PlayerTracker()
    # Force the detect-every-frame path — we want YOLO run for each probe frame
    tracker._detect_interval = 1

    cap = cv2.VideoCapture(str(VIDEO))
    if not cap.isOpened():
        print(f"Could not open video: {VIDEO}", file=sys.stderr)
        return 2

    for fi in PROBE_FRAMES:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok:
            print(f"frame {fi}: couldn't read")
            continue

        print("=" * 78)
        print(f"FRAME {fi}  (ts {fi/25:.1f}s)")
        print("=" * 78)

        # Step 1 — raw YOLO output (mirror of tracker._run_yolo)
        boxes, kps_list = tracker._run_yolo(frame)
        with_pose = sum(1 for kp in kps_list if kp is not None)
        print(f"  _run_yolo: {len(boxes)} boxes, {with_pose} with pose")
        for i, (box, kp) in enumerate(zip(boxes, kps_list)):
            x1, y1, x2, y2 = box
            w, h = x2 - x1, y2 - y1
            cy = (y1 + y2) / 2
            pose_flag = "POSE" if kp is not None else "NO-POSE"
            print(f"    [{i}] bbox=({x1:.0f},{y1:.0f})-({x2:.0f},{y2:.0f}) "
                  f"{w:.0f}x{h:.0f} cy={cy:.0f}  {pose_flag}")

        # Step 2 — full detect_frame pipeline (same as Batch)
        detections = tracker.detect_frame(frame, fi)
        print(f"  detect_frame: {len(detections)} final detections")
        for d in detections:
            pose_flag = "POSE" if d.keypoints is not None else "NO-POSE"
            print(f"    pid={d.player_id} bbox={d.bbox} center={d.center} {pose_flag}")
        print()

    cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
