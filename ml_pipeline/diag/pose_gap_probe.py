"""Phase 1.1 diagnostic — does YOLOv8x-pose find Player 0 during the
pose-gap minutes (1-5) when run directly on raw frames?

If yes → pipeline bug. The model works; something in pipeline.py /
player_tracker.py is suppressing pose-carrying detections.

If no → model limitation. YOLOv8x-pose genuinely can't handle Player 0
on these frames (atypical pose, lighting, etc.) and we need a different
detection strategy.

Samples 20 frames spanning the zero-pose minutes on task 081e089c, runs
the same weights shipped in ml_pipeline/models/yolov8x-pose.pt, and
reports per-frame pose counts + near-baseline pose counts + raw
keypoint confidence for the dominant wrist.

Usage (repo root, .venv active):
    python -m ml_pipeline.diag.pose_gap_probe
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np
from ultralytics import YOLO


TASK = "081e089c-f7b1-49ce-b51c-d623bcc60953"
VIDEO = Path("ml_pipeline/test_videos/match_90ad59a8.mp4.mp4")
WEIGHTS = Path("ml_pipeline/models/yolov8x-pose.pt")

# Frames to probe — spanning the whole match, weighted to the zero-pose block
# (minutes 1-5 = frames 1500-9000). Include a couple of "known-good" frames
# from minute 0 as positive controls.
PROBE_FRAMES = [
    # Minute 0 (known-good pose coverage)
    375, 750,
    # Minute 1 (0% pose) — around SA ts=73.12 / 83.36
    1830, 2085, 2300, 2600,
    # Minute 2 (0% pose) — around SA ts=120.28 / 142.4 / 148.52
    3007, 3250, 3559, 3713, 3900,
    # Minute 3 (0% pose) — around SA ts=178.44 / 195.04 (far side start ~195)
    4461, 4700, 4875,
    # Minute 4 (22% pose partial)
    5624, 6000, 6500,
    # Minute 5 (7% pose partial) — around SA ts=272.76
    6819, 7170,
    # Minute 7 (known-good)
    11451,
]


def run_pose_on_frame(model: YOLO, frame: np.ndarray) -> List[dict]:
    """Run YOLOv8x-pose on a raw frame. Return all person detections with
    bbox + 17 COCO keypoints + confidences."""
    results = model(frame, verbose=False, classes=[0])  # class 0 = person
    out = []
    for r in results:
        if r.boxes is None or len(r.boxes) == 0:
            continue
        boxes = r.boxes.xyxy.cpu().numpy()
        confs = r.boxes.conf.cpu().numpy()
        kps_xy = r.keypoints.xy.cpu().numpy() if r.keypoints is not None else None
        kps_conf = r.keypoints.conf.cpu().numpy() if r.keypoints is not None else None
        for i, box in enumerate(boxes):
            x1, y1, x2, y2 = box
            w = float(x2 - x1)
            h = float(y2 - y1)
            cx = float((x1 + x2) / 2)
            cy = float((y1 + y2) / 2)
            det = {
                "bbox": (float(x1), float(y1), float(x2), float(y2)),
                "w": w,
                "h": h,
                "center": (cx, cy),
                "conf": float(confs[i]),
            }
            if kps_xy is not None:
                kp = []
                for k in range(17):
                    kp.append((
                        float(kps_xy[i][k][0]),
                        float(kps_xy[i][k][1]),
                        float(kps_conf[i][k]) if kps_conf is not None else 0.0,
                    ))
                det["kps"] = kp
            out.append(det)
    return out


def classify_position(bbox_cy: float, frame_h: int) -> str:
    """Near baseline = pixel y > 700 (on 1080p), far baseline = y < 300."""
    if bbox_cy > frame_h * 0.65:
        return "NEAR"
    if bbox_cy < frame_h * 0.30:
        return "FAR"
    return "MID"


def main(argv=None) -> int:
    if not VIDEO.exists():
        print(f"Missing video: {VIDEO}", file=sys.stderr)
        return 2
    if not WEIGHTS.exists():
        print(f"Missing weights: {WEIGHTS}", file=sys.stderr)
        return 2

    cap = cv2.VideoCapture(str(VIDEO))
    if not cap.isOpened():
        print(f"Could not open video: {VIDEO}", file=sys.stderr)
        return 2

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Video: {w}x{h} @ {fps:.1f} fps")

    print(f"Loading {WEIGHTS} ...")
    model = YOLO(str(WEIGHTS))
    print(f"Loaded. Probing {len(PROBE_FRAMES)} frames.\n")

    # Dump every person found with its pixel-y, bbox, and wrist+shoulder confidences.
    print(f"{'frame':>6} {'ts':>6}  n  {'pos':>4} {'cy':>4}  {'det':>4} {'bbox':>10}  {'Ns':>4} {'Ls':>4} {'Rs':>4} {'Lw':>4} {'Rw':>4}")
    print("-" * 85)

    for fi in PROBE_FRAMES:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok:
            print(f"{fi:>6} (couldn't read)")
            continue

        dets = run_pose_on_frame(model, frame)
        if not dets:
            print(f"{fi:>6} {fi/fps:>6.1f}  0  (no persons found)")
            continue
        # Sort by bbox area descending
        dets.sort(key=lambda d: d["w"] * d["h"], reverse=True)
        for j, d in enumerate(dets):
            cy_px = int(d["center"][1])
            pos = classify_position(cy_px, h)
            kp = d["kps"]
            ns, ls, rs, lw, rw = kp[0][2], kp[5][2], kp[6][2], kp[9][2], kp[10][2]
            prefix = f"{fi:>6} {fi/fps:>6.1f}  {len(dets)}" if j == 0 else f"{'':>6} {'':>6}  {'':>1}"
            print(f"{prefix}  {pos:>4} {cy_px:>4}  {d['conf']:>4.2f} {d['w']:>3.0f}x{d['h']:>4.0f}  "
                  f"{ns:>4.2f} {ls:>4.2f} {rs:>4.2f} {lw:>4.2f} {rw:>4.2f}")

    cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
