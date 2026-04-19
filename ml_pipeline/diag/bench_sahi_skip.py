"""B4+ benchmark — measure the new SAHI-skip predicate without rebuilding Docker.

Loads N frames from the MATCHI reference video, runs the real YOLOv8x-pose
model on each, then evaluates both the OLD and NEW `skip_sahi` predicates
that live in `player_tracker.py::detect_frame`. Reports skip rates + any
per-frame detection-count divergence.

Key properties:
- Uses the same weights that Docker uses (`ml_pipeline/models/yolov8x-pose.pt`)
- Does NOT require SAHI installed — we only measure WHICH frames would skip
  SAHI, not what SAHI would have produced. SAHI quality regression is a
  separate concern that requires running SAHI itself; here we're confirming
  (a) the new rule fires often enough, and (b) whenever the new rule fires
  but the old rule doesn't, full-frame YOLO already found a convincing
  far-half person (otherwise we're skipping in a situation where the old
  rule would have run SAHI — which might be valid if pose-confirmed, but is
  the risk surface).
- Runs on CPU at ~1-2 s per frame on this box. 200 frames ≈ 5-7 min.

Usage (repo root, venv active):
    python -m ml_pipeline.diag.bench_sahi_skip [--frames 200] [--start 0] [--stride 60]

Output: per-frame line + aggregate summary (old vs new skip rate, rule
breakdown, any frames where new-skips-but-old-doesn't that LOOK like
real far-player loss).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[2]
VIDEO = REPO_ROOT / "ml_pipeline" / "test_videos" / "match_90ad59a8.mp4.mp4"
WEIGHTS = REPO_ROOT / "ml_pipeline" / "models" / "yolov8x-pose.pt"


# Mirror the two rules from player_tracker.detect_frame. Keep these in
# lockstep with the real code. If these drift, update both places.

def old_rule(full_boxes, full_kps, to_court_coords, frame_h):
    """OLD rule: any full-frame YOLO candidate projects to metric far-
    baseline (-5 ≤ y ≤ 5) via to_court_coords with strict=True (default)."""
    if to_court_coords is None:
        return False, {}
    for box in full_boxes:
        cx = (box[0] + box[2]) / 2
        y2 = box[3]
        try:
            pt = to_court_coords(cx, y2)
        except Exception:
            pt = None
        if pt is not None and -5.0 <= pt[1] <= 5.0:
            return True, {"reason": "old_metric_far"}
    return False, {}


def new_rule(full_boxes, full_kps, to_court_coords, frame_h):
    """NEW rule: skip if A (spatial coverage via pose) OR B (relaxed metric far-baseline).

    Mirrors player_tracker.detect_frame post-B4+.
    """
    midline_y = frame_h / 2
    dead_zone = frame_h * 0.05

    # A: pose-carrying candidates span both halves with size gates
    has_near_pose = False
    has_far_pose = False
    for box, kp in zip(full_boxes, full_kps):
        if kp is None:
            continue
        cy = (box[1] + box[3]) / 2
        bbox_h = box[3] - box[1]
        if cy > midline_y + dead_zone and bbox_h >= 40:
            has_near_pose = True
        elif cy < midline_y - dead_zone and bbox_h >= 20:
            has_far_pose = True
        if has_near_pose and has_far_pose:
            break
    skip_A = has_near_pose and has_far_pose

    # B: relaxed metric far-baseline with strict=False
    skip_B = False
    if to_court_coords is not None:
        for box in full_boxes:
            cx = (box[0] + box[2]) / 2
            y2 = box[3]
            try:
                pt = to_court_coords(cx, y2, strict=False)
            except TypeError:
                try:
                    pt = to_court_coords(cx, y2)
                except Exception:
                    pt = None
            except Exception:
                pt = None
            if pt is not None and -10.0 <= pt[1] <= 5.0:
                skip_B = True
                break

    return (skip_A or skip_B), {
        "A": skip_A,
        "B": skip_B,
        "has_near_pose": has_near_pose,
        "has_far_pose": has_far_pose,
    }


def run_yolo_pose(model, frame):
    """Mirror PlayerTracker._run_yolo with confidence=0.25 (YOLO_CONFIDENCE)
    and imgsz=1280 (YOLO_IMGSZ). Returns (boxes, kps) in full-frame coords."""
    results = model.predict(frame, conf=0.25, imgsz=1280, verbose=False)
    if not results:
        return [], []
    r = results[0]
    boxes_out = []
    kps_out = []
    boxes = r.boxes if r.boxes is not None else []
    kps_data = r.keypoints if r.keypoints is not None else None
    for bi in range(len(boxes)):
        x1, y1, x2, y2 = boxes.xyxy[bi].cpu().numpy()
        boxes_out.append((float(x1), float(y1), float(x2), float(y2)))
        if kps_data is not None and bi < len(kps_data.data):
            kps_out.append(kps_data.data[bi].cpu().numpy())
        else:
            kps_out.append(None)
    return boxes_out, kps_out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=200,
                        help="How many frames to sample (default 200)")
    parser.add_argument("--start", type=int, default=500,
                        help="First frame index (default 500 — past calibration lock)")
    parser.add_argument("--stride", type=int, default=60,
                        help="Frame stride (default 60 — ~2.4 s at 25 fps)")
    parser.add_argument("--no-court", action="store_true",
                        help="Skip court calibration (rule B disabled, rule A only)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-frame decision")
    args = parser.parse_args(argv)

    if not VIDEO.exists():
        print(f"Missing video: {VIDEO}", file=sys.stderr)
        return 2
    if not WEIGHTS.exists():
        print(f"Missing weights: {WEIGHTS}", file=sys.stderr)
        return 2

    # Lazy ultralytics import so `--help` works without deps.
    from ultralytics import YOLO

    cap = cv2.VideoCapture(str(VIDEO))
    if not cap.isOpened():
        print(f"Could not open video: {VIDEO}", file=sys.stderr)
        return 2
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Video: {w}x{h} @ {fps:.1f} fps, {total_frames} total frames")

    print(f"Loading YOLOv8x-pose from {WEIGHTS.relative_to(REPO_ROOT)} ...")
    model = YOLO(str(WEIGHTS))
    print("Loaded.")

    # Optional court calibration — if present, Rule B can fire.
    to_court_coords = None
    if not args.no_court:
        try:
            # Run the real calibration procedure on the same stream.
            from ml_pipeline.court_detector import CourtDetector
            from ml_pipeline.config import COURT_CALIBRATION_FRAMES
            cd = CourtDetector()
            print(f"Calibrating court over first {COURT_CALIBRATION_FRAMES} frames ...")
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            for fi in range(COURT_CALIBRATION_FRAMES + 1):
                ok, frame = cap.read()
                if not ok:
                    break
                try:
                    cd.detect(frame, fi)
                except Exception as e:
                    if fi < 5:
                        print(f"  calib frame {fi}: {e}")
            print(f"Court calibrated (locked={getattr(cd, '_calibration', None) is not None})")
            to_court_coords = cd.to_court_coords
        except Exception as e:
            print(f"Court calibration failed, falling back to Rule-A-only: {e}")
            to_court_coords = None

    frame_indices = [args.start + i * args.stride for i in range(args.frames)]
    frame_indices = [fi for fi in frame_indices if fi < total_frames]

    old_skips = 0
    new_skips = 0
    # Per-frame divergence: new says skip, old doesn't (or vice versa)
    new_only = []           # new skip, old no-skip
    old_only = []           # old skip, new no-skip
    both_skip = 0
    neither_skip = 0

    # Per-rule counters for new
    newA_only = 0
    newB_only = 0
    newAB = 0

    # Detection stats
    full_box_counts = []
    far_half_box_counts = []
    far_half_pose_counts = []

    print()
    if args.verbose:
        print(f"{'frame':>6} {'old':>3} {'new':>3} {'A':>2} {'B':>2} {'full':>4} {'far':>3} {'farpose':>7}")
        print("-" * 50)

    for fi in frame_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok:
            continue
        frame_h = frame.shape[0]

        full_boxes, full_kps = run_yolo_pose(model, frame)

        old_skip, _ = old_rule(full_boxes, full_kps, to_court_coords, frame_h)
        new_skip, meta = new_rule(full_boxes, full_kps, to_court_coords, frame_h)

        if old_skip:
            old_skips += 1
        if new_skip:
            new_skips += 1
        if new_skip and old_skip:
            both_skip += 1
        elif not new_skip and not old_skip:
            neither_skip += 1
        elif new_skip and not old_skip:
            new_only.append(fi)
        elif old_skip and not new_skip:
            old_only.append(fi)

        if new_skip:
            if meta["A"] and meta["B"]:
                newAB += 1
            elif meta["A"]:
                newA_only += 1
            elif meta["B"]:
                newB_only += 1

        # Detection-level accounting
        midline = frame_h / 2
        far_count = sum(1 for b in full_boxes if (b[1] + b[3]) / 2 < midline)
        far_pose_count = sum(1 for b, k in zip(full_boxes, full_kps)
                             if k is not None and (b[1] + b[3]) / 2 < midline)
        full_box_counts.append(len(full_boxes))
        far_half_box_counts.append(far_count)
        far_half_pose_counts.append(far_pose_count)

        if args.verbose:
            print(f"{fi:>6} {'Y' if old_skip else '-':>3} {'Y' if new_skip else '-':>3} "
                  f"{'Y' if meta['A'] else '-':>2} {'Y' if meta['B'] else '-':>2} "
                  f"{len(full_boxes):>4} {far_count:>3} {far_pose_count:>7}")

    cap.release()

    n = len(frame_indices)
    print()
    print("=" * 60)
    print(f"Frames evaluated: {n}")
    print(f"Old skip rate: {old_skips:>4} / {n}  ({100.0*old_skips/n:.1f}%)")
    print(f"New skip rate: {new_skips:>4} / {n}  ({100.0*new_skips/n:.1f}%)")
    print()
    print(f"  both skip:    {both_skip:>4}")
    print(f"  neither skip: {neither_skip:>4}")
    print(f"  new-only skip (new says skip, old did not): {len(new_only)} frames")
    print(f"  old-only skip (old said skip, new does not): {len(old_only)} frames")
    print()
    print("New rule firing breakdown:")
    print(f"  A only (pose-spanning):        {newA_only}")
    print(f"  B only (metric far-baseline):  {newB_only}")
    print(f"  Both (A AND B):                {newAB}")
    print()

    # Quality cross-check: on frames where the new rule skips but old does
    # not, it's expected that full-frame YOLO found a pose-carrying far-half
    # person (Rule A). If not, Rule B must have fired — which is legit if
    # calibration now agrees a candidate is near the far baseline that the
    # old strict=True check rejected.
    if new_only:
        # How many of those new-only-skip frames actually have a far-half
        # pose-carrying candidate (i.e. full-frame YOLO found the far
        # player itself → SAHI legitimately redundant)?
        # Re-run to collect detail (we already computed meta per frame,
        # but didn't retain it; re-collect here by running the new rule again).
        suspicious = []
        for fi in new_only:
            cap2 = cv2.VideoCapture(str(VIDEO))
            cap2.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap2.read()
            cap2.release()
            if not ok:
                continue
            fh = frame.shape[0]
            fb, fk = run_yolo_pose(model, frame)
            _, meta = new_rule(fb, fk, to_court_coords, fh)
            # Suspicious = new-only-skip WITHOUT pose confirmation of far
            # player. If ALL new-only skips have far-half pose (meta['A']),
            # the rule is provably safe — SAHI would have found the same
            # far player. Frames where only B fires (meta['B'] True, ['A']
            # False) depend on calibration correctness — flag for inspection.
            if not meta["A"]:
                suspicious.append((fi, meta))

        print(f"  Risk audit: new-only-skip frames WITHOUT pose-confirmed far player: "
              f"{len(suspicious)}/{len(new_only)}")
        if suspicious and args.verbose:
            for fi, meta in suspicious[:10]:
                print(f"    frame {fi}: A={meta['A']} B={meta['B']}")

    print()
    print(f"Full-frame YOLO box count per frame — avg {np.mean(full_box_counts):.2f}, "
          f"median {int(np.median(full_box_counts))}, max {max(full_box_counts) if full_box_counts else 0}")
    print(f"Far-half boxes — avg {np.mean(far_half_box_counts):.2f}, "
          f"frames with ≥1 far-box: {sum(1 for c in far_half_box_counts if c>=1)}/{n}")
    print(f"Far-half pose boxes — avg {np.mean(far_half_pose_counts):.2f}, "
          f"frames with ≥1 far-pose: {sum(1 for c in far_half_pose_counts if c>=1)}/{n}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
