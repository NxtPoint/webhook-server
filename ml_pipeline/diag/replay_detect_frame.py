"""replay_detect_frame.py — reproduce detect_frame() locally on specific
gap frames with scoring instrumentation. Pinpoints which gate in
_choose_two_players drops the pose-carrying near-half bbox.

Runs the full court+player pipeline locally against the same video
Batch saw. To keep wall-clock reasonable on CPU, calibration phase
processes only the first ~600 frames (enough for CourtDetector's
lock-and-cache strategy to freeze), then skips to the target frames
via cap.read() without processing. Player detect_frame is called on
each target with _choose_two_players monkey-patched to log every
candidate's tier/motion/baseline/bbox/pose/total score.

Motion_mask is passed as None (MOG2 state can't be reconstructed from
a jump), so the +500 motion_bonus won't fire here. If local replay
shows the pose bbox winning WITHOUT motion_bonus, the scoring isn't
the bug; it's somewhere container-specific. If local replay ALSO
drops the pose bbox, the logged score breakdown shows which gate.

Usage (Windows, from repo root with .venv active):
    .venv\\Scripts\\python.exe -m ml_pipeline.diag.replay_detect_frame \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --frames 4750,4760,4770,4780

~2 min on CPU.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("replay")

from ml_pipeline.court_detector import CourtDetector
from ml_pipeline.player_tracker import PlayerTracker
from ml_pipeline.config import (
    COURT_LENGTH_M,
    COURT_WIDTH_DOUBLES_M,
    MOG2_MIN_MOTION_RATIO,
)


def _install_scoring_logger(pt: PlayerTracker):
    """Monkey-patch _choose_two_players to log every candidate's scoring.

    Reproduces the prod scoring logic verbatim so we see the exact tiers
    and bonuses the real pipeline would compute, then logs per-candidate.
    Returns the ORIGINAL method so we can call it after logging for the
    actual detection result.
    """
    original = pt._choose_two_players

    def instrumented(candidates, candidate_kps, court_bbox, frame_shape,
                     motion_mask=None, court_corners=None,
                     to_court_coords=None):
        frame_h = frame_shape[0]
        midline_y = frame_h / 2.0
        court_poly = None
        if court_corners is not None and len(court_corners) == 4:
            fl, fr, nl, nr = court_corners
            court_poly = np.array([fl, fr, nr, nl], dtype=np.float32)

        logger.info("  _choose_two_players: %d candidates (midline_y=%.0f)",
                    len(candidates), midline_y)

        # Re-score each candidate with full breakdown logging
        for idx, (box, kps) in enumerate(zip(candidates, candidate_kps)):
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            y2 = box[3]
            half = "far" if cy < midline_y else "near"
            w = box[2] - box[0]
            h = box[3] - box[1]
            bbox_area = w * h

            court_xy = None
            if to_court_coords is not None:
                try:
                    court_xy = to_court_coords(cx, y2, strict=False)
                except Exception:
                    court_xy = None

            # Reproduce tier logic (player_tracker.py:974-1028)
            if court_xy is not None:
                court_x_m, court_y_m = court_xy
                in_court = (0.0 <= court_x_m <= COURT_WIDTH_DOUBLES_M
                            and 0.0 <= court_y_m <= COURT_LENGTH_M)
                behind_baseline = (
                    -3.0 <= court_x_m <= COURT_WIDTH_DOUBLES_M + 3.0
                    and (-10.0 <= court_y_m < 0.0
                         or COURT_LENGTH_M < court_y_m <= COURT_LENGTH_M + 10.0)
                )
                wide_alley = (
                    (-1.0 <= court_x_m < 0.0
                     or COURT_WIDTH_DOUBLES_M < court_x_m <= COURT_WIDTH_DOUBLES_M + 1.0)
                    and 0.0 <= court_y_m <= COURT_LENGTH_M
                )
                if in_court:
                    tier = 3000
                elif behind_baseline:
                    tier = 2000
                elif wide_alley:
                    tier = 1000
                elif kps is not None:
                    tier = 500
                else:
                    tier = 0

                # Pixel-polygon sanity gate
                pixel_dist = None
                if court_poly is not None:
                    pixel_dist = cv2.pointPolygonTest(
                        court_poly, (float(cx), float(y2)), True
                    )
                    if pixel_dist < -300.0:
                        tier = 0

                # Bonuses
                NET_Y = COURT_LENGTH_M / 2
                dist_to_baseline = min(abs(court_y_m - 0.0),
                                       abs(court_y_m - COURT_LENGTH_M))
                baseline_closeness = max(0.0, 1.0 - dist_to_baseline / NET_Y) * 500
                bbox_score = min(200, bbox_area / 25.0)
                pose_bonus = 300 if kps is not None else 0

                # Motion
                motion_ratio = 0.0
                if motion_mask is not None:
                    # Same _compute_motion_ratio logic — inline mini-version
                    x1i, y1i = int(box[0]), int(box[1])
                    x2i, y2i = int(box[2]), int(box[3])
                    roi = motion_mask[max(0, y1i):y2i, max(0, x1i):x2i]
                    if roi.size > 0:
                        motion_ratio = float((roi > 0).sum()) / roi.size
                motion_bonus = 500 if motion_ratio >= MOG2_MIN_MOTION_RATIO else 0

                if tier == 0:
                    score = 0.0
                else:
                    score = (tier + motion_bonus + baseline_closeness
                             + bbox_score + pose_bonus)

                logger.info(
                    "    [%d] %s bbox=(%.0f,%.0f)-(%.0f,%.0f) %.0fx%.0f cy=%.0f y2=%.0f "
                    "court=(%.2f,%.2f) pose=%s tier=%d pixel_dist=%s "
                    "motion=%d baseline_cls=%.0f bbox=%.0f pose_b=%d → SCORE=%.0f",
                    idx, half, box[0], box[1], box[2], box[3], w, h, cy, y2,
                    court_x_m, court_y_m, kps is not None, tier,
                    f"{pixel_dist:.0f}" if pixel_dist is not None else "n/a",
                    motion_bonus, baseline_closeness, bbox_score, pose_bonus, score,
                )
            else:
                # Projection failed (court_xy is None)
                logger.info(
                    "    [%d] %s bbox=(%.0f,%.0f)-(%.0f,%.0f) %.0fx%.0f cy=%.0f pose=%s "
                    "court=None → tier=0 score=0",
                    idx, half, box[0], box[1], box[2], box[3], w, h, cy,
                    kps is not None,
                )

        # Now call the real scorer to get the actual kept candidates
        kept_boxes, kept_kps = original(
            candidates, candidate_kps, court_bbox, frame_shape,
            motion_mask=motion_mask, court_corners=court_corners,
            to_court_coords=to_court_coords,
        )
        logger.info("  → kept %d candidates after scoring:", len(kept_boxes))
        for kb in kept_boxes:
            cx = (kb[0] + kb[2]) / 2
            cy = (kb[1] + kb[3]) / 2
            half = "far" if cy < frame_shape[0] / 2 else "near"
            logger.info("    KEPT %s bbox=(%.0f,%.0f)-(%.0f,%.0f)", half,
                        kb[0], kb[1], kb[2], kb[3])
        return kept_boxes, kept_kps

    pt._choose_two_players = instrumented


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="ml_pipeline/test_videos/match_90ad59a8.mp4.mp4")
    ap.add_argument("--frames", default="4750",
                    help="Comma-separated yielded frame indices to replay")
    ap.add_argument("--calib-frames", type=int, default=600,
                    help="Process this many frames before jumping to targets")
    args = ap.parse_args(argv)

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"Missing video: {video_path}", file=sys.stderr)
        return 2

    target_frames = sorted(int(x.strip()) for x in args.frames.split(","))
    print(f"=== replay_detect_frame ===")
    print(f"  video         {video_path}")
    print(f"  target frames {target_frames}")
    print(f"  calib frames  0..{args.calib_frames}")
    print()

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"Could not open video", file=sys.stderr)
        return 2
    source_fps = cap.get(cv2.CAP_PROP_FPS)
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    print(f"  video meta    {frame_w}x{frame_h} @ {source_fps:.2f} fps")
    print()

    # Phase 1: calibrate court_detector on first N frames
    print(f"Phase 1 — court calibration")
    t0 = time.time()
    cd = CourtDetector()
    frame_idx = 0
    while frame_idx < args.calib_frames:
        ok, frame = cap.read()
        if not ok:
            break
        cd.detect(frame, frame_idx)
        frame_idx += 1
        if cd._locked_detection is not None and frame_idx >= 300:
            print(f"  calibration locked at frame {frame_idx}")
            break
    locked = cd._locked_detection is not None
    print(f"  processed {frame_idx} frames in {time.time()-t0:.1f}s, "
          f"locked={locked}")
    if not locked:
        print(f"  WARNING: calibration did not lock — using last_good_detection")
    corners = cd.get_court_corners_pixels()
    court_bbox = cd.get_court_bbox_pixels()
    print(f"  court_corners = {corners}")
    print(f"  court_bbox    = {court_bbox}")
    print()

    # Phase 2: load PlayerTracker with instrumentation
    print(f"Phase 2 — player tracker setup")
    pt = PlayerTracker()
    pt._detect_interval = 1  # force YOLO on every call (no reuse)
    _install_scoring_logger(pt)
    print()

    # Phase 3: replay each target frame
    print(f"Phase 3 — replay target frames")
    results = []
    for target in target_frames:
        # Skip forward via cap.read() without processing
        while frame_idx < target:
            ok, _ = cap.read()
            if not ok:
                break
            frame_idx += 1
        ok, frame = cap.read()
        if not ok:
            print(f"  Could not read frame {target}")
            continue
        frame_idx += 1

        print(f"\n--- frame {target} ---")
        t = time.time()
        detections = pt.detect_frame(
            frame, target,
            court_bbox=court_bbox,
            motion_mask=None,   # skipped — MOG2 state not tracked across skip
            court_corners=corners,
            to_court_coords=cd.to_court_coords,
            to_pixel_coords=cd.to_pixel_coords,
        )
        elapsed = time.time() - t
        print(f"  detect_frame returned {len(detections)} players "
              f"(elapsed {elapsed:.1f}s)")
        for d in detections:
            has_pose = d.keypoints is not None
            print(f"    pid={d.player_id} bbox={[f'{x:.0f}' for x in d.bbox]} "
                  f"center={[f'{x:.0f}' for x in d.center]} pose={has_pose}")
        results.append({
            "frame": target,
            "n_detections": len(detections),
            "pid0_present": any(d.player_id == 0 for d in detections),
            "pid0_has_pose": any(d.player_id == 0 and d.keypoints is not None
                                 for d in detections),
        })

    cap.release()

    print()
    print("=" * 80)
    print("REPLAY SUMMARY")
    print("=" * 80)
    print(f"{'frame':>6}  {'detections':>10}  {'pid=0':>7}  {'pid=0 pose':>10}")
    for r in results:
        print(f"{r['frame']:>6}  {r['n_detections']:>10}  "
              f"{'YES' if r['pid0_present'] else 'NO':>7}  "
              f"{'YES' if r['pid0_has_pose'] else 'NO':>10}")
    print()
    any_pid0 = any(r["pid0_present"] for r in results)
    if any_pid0:
        print("VERDICT: H1 (Batch-container-specific)")
        print("  Local replay FINDS the near player on at least one gap frame even")
        print("  without motion_bonus. Prod pipeline's drop is likely due to")
        print("  something only present in the Batch environment — GPU YOLO output")
        print("  differences, different ultralytics/torch versions, or an")
        print("  environment-specific race. Dig into the container.")
    else:
        print("VERDICT: H2 (scoring bug — check per-candidate log above)")
        print("  Local replay ALSO drops the near player, same as Batch. The")
        print("  per-candidate score log above shows which gate killed the")
        print("  pose bbox. Prime suspects: pixel-polygon sanity gate zeroing")
        print("  the tier (look for pixel_dist < -300), or the pose bbox falling")
        print("  outside all tier zones and scoring tier=0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
