"""Stage 1 local harness for ml_pipeline.roi_extractors.bounces.

Loads a bench fixture (ball_rows + sa_truth), calibrates the court
detector against the local test video, then calls extract_far_bounces
with engine=None so nothing touches the DB. Reports:
  - anchor / cluster / window counts
  - row counts (in service-box zone, bounces)
  - per-SA-serve nearest ROI bounce (sanity: do they line up?)

NOT for Render — this is pure local validation. Renders the same input
the production caller would supply (in-memory ball detections), but
runs on the bench fixture's pre-recorded ball_rows so no GPU is needed
beyond the TrackNet pass on the cropped windows.

Usage:

    .venv/Scripts/python -m ml_pipeline.diag.replay_roi_bounces \\
        --fixture ml_pipeline/fixtures/a798eff0.pkl.gz \\
        --video   ml_pipeline/test_videos/a798eff0_sa_video.mp4 \\
        --max-windows 3

Runtime note: each window is ±2.5 s = 125 frames at 25 fps. TrackNet on
CPU is ~5x slower than the cropped frame budget (~1 s/frame in pessimistic
runs). Default max-windows=3 keeps total runtime under ~5 min on a laptop.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import os
import pickle
import sys
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger("replay_roi_bounces")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


@dataclass
class _MockBallDetection:
    """Subset of BallDetection needed by extract_far_bounces — just enough
    for the anchor-selection path. Mirrors ml_pipeline.ball_tracker.BallDetection."""
    frame_idx: int
    x: float
    y: float
    court_x: Optional[float] = None
    court_y: Optional[float] = None
    is_bounce: bool = False


def _load_fixture(path: str) -> dict:
    with gzip.open(path, "rb") as f:
        return pickle.load(f)


def _build_mock_bounces(ball_rows) -> list:
    """Convert ball_rows dicts to _MockBallDetection objects.

    The production caller passes result.ball_detections (a list of
    BallDetection dataclasses) — duck-typed by extract_far_bounces via
    getattr(d, ...). The fixture stores dicts loaded from
    ml_analysis.ball_detections, with the same shape."""
    out = []
    for r in ball_rows:
        out.append(_MockBallDetection(
            frame_idx=int(r["frame_idx"]),
            x=float(r.get("x") or 0.0),
            y=float(r.get("y") or 0.0),
            court_x=r.get("court_x"),
            court_y=r.get("court_y"),
            is_bounce=bool(r.get("is_bounce")),
        ))
    return out


def _calibrate_court(video_path: str, n_frames: int = 300):
    """Run CourtDetector on the first n_frames; return the locked detector."""
    import cv2
    from ml_pipeline.court_detector import CourtDetector
    detector = CourtDetector()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        for idx in range(n_frames + 1):
            ok, frame = cap.read()
            if not ok:
                break
            detector.detect(frame, idx)
    finally:
        cap.release()
    if (detector._locked_detection is None
            and detector._best_detection is None):
        raise RuntimeError("court calibration failed — no detection produced")
    logger.info(
        "court_calibration: locked=%s best_validated_inliers=%d calibration=%s",
        detector._locked_detection is not None,
        detector._best_validated_inliers,
        detector._calibration is not None,
    )
    return detector


def _summarise(rows: list, sa_truth: list, fps: float) -> None:
    n_total = len(rows)
    n_bounces = sum(1 for r in rows if r["is_bounce"])
    n_far = sum(
        1 for r in rows
        if r["is_bounce"] and r["court_y"] is not None and r["court_y"] <= 11.885
    )
    n_near = sum(
        1 for r in rows
        if r["is_bounce"] and r["court_y"] is not None and r["court_y"] > 11.885
    )

    print()
    print("=== ROI rows summary ===")
    print(f"  total rows in service-box zone: {n_total}")
    print(f"  bounces:                        {n_bounces}")
    print(f"    in FAR service box (y <= 11.885):  {n_far}")
    print(f"    in NEAR service box (y > 11.885):  {n_near}")

    if not sa_truth:
        return

    # Per-SA-serve: closest ROI bounce within 3s. Sanity: do we land close?
    print()
    print("=== Per SA-truth proximity ===")
    print(f"{'SA ts':>7} {'role':>4} {'side':>5} | {'closest ROI ts':>15} "
          f"{'dt (s)':>8} {'court_x':>8} {'court_y':>8} {'bounce':>7}")
    print("-" * 80)
    bounces_only = [r for r in rows if r["is_bounce"]]
    for sa in sa_truth:
        sa_ts = float(sa["ts"]) if sa["ts"] is not None else None
        if sa_ts is None:
            continue
        best = None
        best_dt = None
        for r in bounces_only:
            r_ts = r["frame_idx"] / fps
            dt = abs(r_ts - sa_ts)
            if best_dt is None or dt < best_dt:
                best, best_dt = r, dt
        if best is None:
            print(f"{sa_ts:>7.2f} {sa['role']:>4} {(sa.get('side') or '-'):>5} "
                  f"| {'-':>15} {'-':>8} {'-':>8} {'-':>8} {'-':>7}")
        else:
            r_ts = best["frame_idx"] / fps
            cx_s = (f"{best['court_x']:.2f}"
                    if best["court_x"] is not None else "-")
            cy_s = (f"{best['court_y']:.2f}"
                    if best["court_y"] is not None else "-")
            print(f"{sa_ts:>7.2f} {sa['role']:>4} {(sa.get('side') or '-'):>5} "
                  f"| {r_ts:>15.2f} {best_dt:>8.2f} {cx_s:>8} {cy_s:>8} "
                  f"{'Y' if best['is_bounce'] else 'N':>7}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture",
                    default="ml_pipeline/fixtures/a798eff0.pkl.gz",
                    help="Bench fixture path (default a798eff0)")
    ap.add_argument("--video",
                    default="ml_pipeline/test_videos/a798eff0_sa_video.mp4",
                    help="Local video path matching the fixture")
    ap.add_argument("--max-windows", type=int, default=3,
                    help="Cap windows for runtime (default 3)")
    ap.add_argument("--window-s", type=float, default=2.5)
    ap.add_argument("--cluster-gap-s", type=float, default=0.5)
    args = ap.parse_args(argv)

    if not os.path.exists(args.fixture):
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 1
    if not os.path.exists(args.video):
        print(f"video not found: {args.video}", file=sys.stderr)
        return 1

    fixture = _load_fixture(args.fixture)
    fps = float(fixture.get("fps") or 25.0)
    ball_rows = fixture["ball_rows"]
    sa_truth = fixture.get("sa_truth", [])

    print(f"=== fixture: {fixture.get('task_id', '<unknown>')[:8]} ===")
    print(f"  fps:        {fps}")
    print(f"  ball_rows:  {len(ball_rows)}  "
          f"({sum(1 for r in ball_rows if r.get('is_bounce'))} bounces)")
    print(f"  sa_truth:   {len(sa_truth)} serves")
    print()

    print("Calibrating court...")
    detector = _calibrate_court(args.video)

    mock_bounces = _build_mock_bounces(ball_rows)
    print(f"  built {len(mock_bounces)} mock BallDetection objects")

    print()
    print(f"Running extract_far_bounces "
          f"(window_s={args.window_s}, cluster_gap_s={args.cluster_gap_s}, "
          f"max_windows={args.max_windows})...")
    from ml_pipeline.roi_extractors.bounces import extract_far_bounces
    count, rows = extract_far_bounces(
        video_path=args.video,
        job_id=fixture.get("task_id", "stage1-test"),
        engine=None,
        court_detector=detector,
        bounces=mock_bounces,
        fps=fps,
        window_s=args.window_s,
        cluster_gap_s=args.cluster_gap_s,
        max_windows=args.max_windows,
        return_rows=True,
    )
    print()
    print(f"extract_far_bounces returned {count} rows")

    _summarise(rows, sa_truth, fps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
