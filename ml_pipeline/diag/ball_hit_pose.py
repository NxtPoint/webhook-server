"""Pose-only stroke event detector -- no ball signal needed.

Strategic probe for Phase 6 (stroke detection). Third in a series:

  1. ball_hit_baseline.py    - y-reversal heuristic on ball trajectory.
                               Got 0% recall: assumes broadcast camera; our
                               side-cam ball movement is mostly horizontal.

  2. ball_hit_fusion.py      - ball position vs wrist keypoint distance.
                               Got 15% recall. Test 2 showed why: the ball is
                               OCCLUDED by the racquet/player at the millisecond
                               of contact -- the WASB coverage gap aligns
                               exactly with SA's truth hit frames. No
                               ball-based heuristic can recover this signal.

  3. THIS FILE - pose alone. Wrist velocity peaks indicate swings even when
                 the ball is invisible. This is the signal that's actually
                 present at the moments we care about (87% pose coverage on
                 the 78c32f53 fixture, including frames 1360-1369 around the
                 truth hit at 1362 where the ball detection has a 3-frame gap).

Same architectural pattern our serve detector already uses (Silent Impact 2025
/ TAL4Tennis). Our existing pose-first serve detector hits 20/24 + 23/24 on
bench; this generalizes that pattern to all strokes.

## Algorithm

1. Load all player_detections (frame_idx, player_id, keypoints) for the T5 task.
2. For each player (0=NEAR, 1=FAR), build a time-ordered list of left+right
   wrist positions (where keypoint confidence > MIN_KP_CONF).
3. Compute per-frame wrist velocity (Euclidean delta of position between
   consecutive frames where pose exists; skip gaps).
4. Take the MAX velocity across all 4 wrists (2 players x left/right) at each
   frame. This is robust to player_id swap glitches and "which hand is on
   the racquet" ambiguity (covers both 1H and 2H strokes).
5. Smooth with rolling mean over SMOOTH_WINDOW frames.
6. Find local maxima above MIN_VELOCITY_PX_PER_FRAME with MIN_GAP_FRAMES
   between accepted peaks.
7. Bench against SA's bronze.player_swing.ball_hit_s ground truth.

## Decision rule (same as the prior two probes)

  recall >= 80%: pose-only is enough; Phase 6 buildable without training
  40-80%:        useful candidate generator; small refiner may close the gap
  < 40%:         training is justified

## Usage

    .venv/bin/python -m ml_pipeline.diag.ball_hit_pose \\
        --sa-task 0d0514df-68aa-4346-9e2d-64413429e47f \\
        --t5-task 78c32f53-5580-4a88-a4e7-7506e59b2b52 \\
        --verbose

Verbose mode dumps the velocity distribution AT SA truth frames so the right
--min-velocity can be picked empirically. Default tuning is a guess; expect
to need calibration on the first run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Tuple


# --- Tuning knobs (defaults; CLI can override) ----------------------------
MIN_VELOCITY_PX_PER_FRAME = 30   # peak threshold for wrist velocity (px / frame)
SMOOTH_WINDOW = 3                # rolling mean window for velocity smoothing
MIN_GAP_FRAMES = 15              # enforce >= 0.6s between strokes at 25fps
MIN_KP_CONF = 0.3                # YOLO keypoint confidence threshold
MAX_GAP_FRAMES_FOR_VELOCITY = 3  # don't compute velocity across gaps > this
TOLERANCE_FRAMES = 3             # +/- when matching predicted to SA truth
DEFAULT_FPS = 25.0

# COCO keypoint indices (matches ml_pipeline/player_tracker.py)
KP_LEFT_WRIST = 9
KP_RIGHT_WRIST = 10


def _connect_db():
    """SQLAlchemy engine on DATABASE_URL. Mirrors the other two probes."""
    from sqlalchemy import create_engine
    url = os.getenv("DATABASE_URL") or os.getenv("EXTERNAL_DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set in env")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


def fetch_t5_player_poses(engine, t5_task_id: str) -> List[Tuple[int, int, list]]:
    """Return [(frame_idx, player_id, keypoints), ...] sorted by frame, player.

    Same query shape as ball_hit_fusion.py with UUID-first / int-FK fallback.
    """
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, player_id, keypoints
            FROM ml_analysis.player_detections
            WHERE job_id::text = :tid AND keypoints IS NOT NULL
            ORDER BY player_id, frame_idx
        """), {"tid": t5_task_id}).fetchall()
    if not rows:
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT pd.frame_idx, pd.player_id, pd.keypoints
                FROM ml_analysis.player_detections pd
                JOIN ml_analysis.video_analysis_jobs vaj ON pd.job_id = vaj.id::text
                WHERE vaj.task_id::text = :tid AND pd.keypoints IS NOT NULL
                ORDER BY pd.player_id, pd.frame_idx
            """), {"tid": t5_task_id}).fetchall()

    out = []
    for r in rows:
        kps = r[2]
        if isinstance(kps, str):
            try:
                kps = json.loads(kps)
            except Exception:
                continue
        out.append((int(r[0]), int(r[1]), kps))
    return out


def fetch_sa_hit_frames(engine, sa_task_id: str, fps: float) -> List[int]:
    """Same as the other probes."""
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT ball_hit_s
            FROM bronze.player_swing
            WHERE task_id::text = :tid AND ball_hit_s IS NOT NULL
            ORDER BY ball_hit_s
        """), {"tid": sa_task_id}).fetchall()
    return [int(round(float(r[0]) * fps)) for r in rows]


def _wrist_positions(keypoints, min_conf: float) -> Tuple[Optional[Tuple[float, float]],
                                                          Optional[Tuple[float, float]]]:
    """Return (left_wrist_pos, right_wrist_pos) where each is (x, y) or None
    if confidence is below threshold or the keypoint is missing.
    """
    out = [None, None]
    for slot, idx in [(0, KP_LEFT_WRIST), (1, KP_RIGHT_WRIST)]:
        try:
            x, y, c = keypoints[idx]
        except (IndexError, TypeError, ValueError):
            continue
        if c is None or float(c) < min_conf:
            continue
        out[slot] = (float(x), float(y))
    return out[0], out[1]


def compute_per_player_velocity(
    poses: List[Tuple[int, int, list]],
    min_kp_conf: float,
    max_gap_frames: int,
) -> dict:
    """Return {player_id: {frame: max(left_vel, right_vel)}}.

    Velocity is Euclidean delta of wrist position between the current frame
    and the NEAREST PRIOR FRAME within max_gap_frames that had a valid
    detection of the same wrist. Velocity across larger gaps is dropped
    (we can't tell what happened in between).

    Returns max of left+right wrist velocities at each frame -- handles both
    1H and 2H strokes (whichever hand moved fastest is the "swing" hand).
    """
    # Group by player
    per_player: dict = {}
    for frame, pid, kps in poses:
        per_player.setdefault(pid, []).append((frame, kps))

    out: dict = {}
    for pid, rows in per_player.items():
        # Sort by frame (input is already sorted, but ensure)
        rows = sorted(rows, key=lambda r: r[0])
        # Last seen wrist position per side
        last_left: Optional[Tuple[int, float, float]] = None   # (frame, x, y)
        last_right: Optional[Tuple[int, float, float]] = None
        out[pid] = {}
        for frame, kps in rows:
            left, right = _wrist_positions(kps, min_kp_conf)
            v_left = v_right = None
            if left is not None:
                if last_left is not None and frame - last_left[0] <= max_gap_frames:
                    dx = left[0] - last_left[1]
                    dy = left[1] - last_left[2]
                    df = frame - last_left[0]
                    v_left = ((dx * dx + dy * dy) ** 0.5) / max(df, 1)
                last_left = (frame, left[0], left[1])
            if right is not None:
                if last_right is not None and frame - last_right[0] <= max_gap_frames:
                    dx = right[0] - last_right[1]
                    dy = right[1] - last_right[2]
                    df = frame - last_right[0]
                    v_right = ((dx * dx + dy * dy) ** 0.5) / max(df, 1)
                last_right = (frame, right[0], right[1])
            # Take the max across left/right for this player at this frame
            cands = [v for v in (v_left, v_right) if v is not None]
            if cands:
                out[pid][frame] = max(cands)
    return out


def compute_global_max_velocity(per_player_vel: dict) -> dict:
    """Merge across players: {frame: max(velocity across all wrists)}.

    Robust to player_id swap glitches: if tracking briefly labels NEAR as
    player 1 instead of player 0, the actual swing wrist's velocity still
    shows up in the merged signal.
    """
    out: dict = {}
    for pid, fv in per_player_vel.items():
        for frame, v in fv.items():
            if frame not in out or v > out[frame]:
                out[frame] = v
    return out


def smooth_velocity(velocity_by_frame: dict, window: int) -> List[Tuple[int, float]]:
    """Apply rolling-mean smoothing across consecutive frames.

    Returns a list [(frame, smoothed_velocity), ...] sorted by frame.
    Frames without any velocity entry are skipped.
    """
    if not velocity_by_frame:
        return []
    frames = sorted(velocity_by_frame.keys())
    smoothed = []
    for i, f in enumerate(frames):
        lo = max(0, i - window + 1)
        window_vals = [velocity_by_frame[frames[j]] for j in range(lo, i + 1)]
        smoothed.append((f, sum(window_vals) / len(window_vals)))
    return smoothed


def detect_velocity_peaks(
    smoothed: List[Tuple[int, float]],
    min_velocity: float,
    min_gap_frames: int,
) -> List[int]:
    """Find local maxima of smoothed velocity above min_velocity, with
    min_gap_frames between accepted peaks.

    A frame F is a peak if velocity(F) > velocity(F-1) AND
    velocity(F) >= velocity(F+1) (handles flat tops by taking the
    earliest frame of a plateau).
    """
    if len(smoothed) < 3:
        return []
    peaks: List[int] = []
    last_accepted = -10**9
    for i in range(1, len(smoothed) - 1):
        f, v = smoothed[i]
        if v < min_velocity:
            continue
        _, v_prev = smoothed[i - 1]
        _, v_next = smoothed[i + 1]
        if v > v_prev and v >= v_next:
            if f - last_accepted >= min_gap_frames:
                peaks.append(f)
                last_accepted = f
    return peaks


def evaluate_against_truth(
    predicted: List[int], truth: List[int], tolerance: int,
) -> dict:
    """Greedy nearest-first matching. Same logic as the other probes."""
    truth_used = [False] * len(truth)
    truth_sorted = sorted(truth)
    matched_truth, unmatched_pred = [], []
    for p in sorted(predicted):
        best_j, best_d = -1, tolerance + 1
        for j, t in enumerate(truth_sorted):
            if truth_used[j]:
                continue
            d = abs(p - t)
            if d <= tolerance and d < best_d:
                best_j, best_d = j, d
        if best_j >= 0:
            truth_used[best_j] = True
            matched_truth.append(truth_sorted[best_j])
        else:
            unmatched_pred.append(p)
    unmatched_truth = [t for j, t in enumerate(truth_sorted) if not truth_used[j]]
    recall = len(matched_truth) / len(truth) if truth else None
    precision = (len(predicted) - len(unmatched_pred)) / len(predicted) if predicted else None
    return {
        "tolerance_frames": tolerance,
        "predicted_total": len(predicted),
        "truth_total": len(truth),
        "matched": len(matched_truth),
        "missed_truth": len(unmatched_truth),
        "false_positives": len(unmatched_pred),
        "recall": recall,
        "precision": precision,
        "unmatched_truth_sample": unmatched_truth[:10],
        "false_positive_sample": unmatched_pred[:10],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pose-only ball-hit detector via wrist-velocity peaks.",
    )
    ap.add_argument("--sa-task", required=True, help="SportAI task_id (ground truth)")
    ap.add_argument("--t5-task", required=True, help="T5 task_id (pose data)")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS,
                    help=f"Frame sampling rate (default {DEFAULT_FPS})")
    ap.add_argument("--min-velocity", type=float, default=MIN_VELOCITY_PX_PER_FRAME,
                    help=f"Minimum wrist velocity (px/frame) for a peak to be a hit "
                         f"(default {MIN_VELOCITY_PX_PER_FRAME})")
    ap.add_argument("--smooth-window", type=int, default=SMOOTH_WINDOW,
                    help=f"Rolling-mean smoothing window in frames (default {SMOOTH_WINDOW})")
    ap.add_argument("--min-gap-frames", type=int, default=MIN_GAP_FRAMES,
                    help=f"Min frame gap between accepted peaks (default {MIN_GAP_FRAMES})")
    ap.add_argument("--min-kp-conf", type=float, default=MIN_KP_CONF,
                    help=f"Min YOLO keypoint confidence to use a wrist (default {MIN_KP_CONF})")
    ap.add_argument("--max-gap-for-velocity", type=int, default=MAX_GAP_FRAMES_FOR_VELOCITY,
                    help=f"Don't compute velocity across pose gaps larger than this "
                         f"(default {MAX_GAP_FRAMES_FOR_VELOCITY})")
    ap.add_argument("--tolerance-frames", type=int, default=TOLERANCE_FRAMES,
                    help=f"Tolerance for matching predicted to truth (default {TOLERANCE_FRAMES})")
    ap.add_argument("--verbose", action="store_true",
                    help="Print velocity distribution at SA truth frames -- "
                         "lets you pick the right --min-velocity empirically")
    args = ap.parse_args(argv)

    engine = _connect_db()

    print("=== ball-hit pose-only ===")
    print(f"  SA task: {args.sa_task}")
    print(f"  T5 task: {args.t5_task}")
    print(f"  fps={args.fps}  min_velocity={args.min_velocity}px/f  "
          f"smooth={args.smooth_window}f  min_gap={args.min_gap_frames}f  "
          f"min_kp_conf={args.min_kp_conf}  tolerance=+/-{args.tolerance_frames}f")
    print()

    print("Loading T5 player poses...")
    poses = fetch_t5_player_poses(engine, args.t5_task)
    if not poses:
        print(f"  ERROR: no poses for T5 task {args.t5_task}", file=sys.stderr)
        return 1
    n_per_player: dict = {}
    for f, pid, _ in poses:
        n_per_player[pid] = n_per_player.get(pid, 0) + 1
    print(f"  loaded {len(poses)} pose entries  "
          f"(per-player counts: {dict(sorted(n_per_player.items()))})")

    print("Loading SA ground-truth hits...")
    truth = fetch_sa_hit_frames(engine, args.sa_task, args.fps)
    if not truth:
        print(f"  ERROR: no SA hits for task {args.sa_task}", file=sys.stderr)
        return 1
    print(f"  loaded {len(truth)} SA hit frames "
          f"(range {min(truth)} - {max(truth)})")

    print("Computing per-player wrist velocity...")
    per_player_vel = compute_per_player_velocity(
        poses, min_kp_conf=args.min_kp_conf,
        max_gap_frames=args.max_gap_for_velocity,
    )
    n_vel = sum(len(v) for v in per_player_vel.values())
    print(f"  computed {n_vel} per-player-frame velocity samples")

    global_vel = compute_global_max_velocity(per_player_vel)
    print(f"  global max-velocity has {len(global_vel)} frames covered")

    smoothed = smooth_velocity(global_vel, window=args.smooth_window)

    if smoothed:
        vels = sorted(v for _, v in smoothed)
        n = len(vels)
        print(f"  smoothed velocity stats: "
              f"min={vels[0]:.1f}  p50={vels[n//2]:.1f}  "
              f"p90={vels[min(n-1, n*9//10)]:.1f}  "
              f"p99={vels[min(n-1, n*99//100)]:.1f}  max={vels[-1]:.1f}")

    print("Detecting velocity peaks...")
    predicted = detect_velocity_peaks(
        smoothed,
        min_velocity=args.min_velocity,
        min_gap_frames=args.min_gap_frames,
    )
    print(f"  predicted {len(predicted)} hits")
    print()

    result = evaluate_against_truth(predicted, truth, args.tolerance_frames)
    print("=== RESULT ===")
    print(f"  Predicted hits:    {result['predicted_total']}")
    print(f"  SA truth hits:     {result['truth_total']}")
    print(f"  Matched (+/-{result['tolerance_frames']} frames): {result['matched']}")
    print(f"  Missed truth:      {result['missed_truth']}")
    print(f"  False positives:   {result['false_positives']}")
    print()
    if result["recall"] is not None:
        print(f"  RECALL    : {result['recall']:.1%}  (matched / SA truth)")
    if result["precision"] is not None:
        print(f"  PRECISION : {result['precision']:.1%}  (matched / predicted)")
    print()

    recall = result["recall"] or 0.0
    if recall >= 0.80:
        verdict = "HIGH RECALL -- pose-only is enough; Phase 6 buildable without training"
    elif recall >= 0.40:
        verdict = "MEDIUM RECALL -- useful candidate generator; refine knobs or add downstream filter"
    else:
        verdict = ("LOW RECALL -- try tuning (--min-velocity, --smooth-window) before declaring; "
                   "if all tuning fails, training may be the answer")
    print(f"  verdict: {verdict}")

    if args.verbose:
        # Velocity AT SA truth frames vs everywhere
        truth_set = set(truth)
        vel_dict = {f: v for f, v in smoothed}
        vels_at_truth = []
        for t in truth:
            # Find nearest frame within tolerance
            for delta in range(args.tolerance_frames + 1):
                for f in (t - delta, t + delta) if delta > 0 else (t,):
                    if f in vel_dict:
                        vels_at_truth.append(vel_dict[f])
                        break
                else:
                    continue
                break

        if vels_at_truth:
            vels_at_truth.sort()
            n = len(vels_at_truth)
            print()
            print(f"  Wrist velocity AT (or near) SA truth frames (n={n}):")
            print(f"    min={vels_at_truth[0]:.1f}  p10={vels_at_truth[n//10]:.1f}  "
                  f"p50={vels_at_truth[n//2]:.1f}  "
                  f"p90={vels_at_truth[min(n-1, n*9//10)]:.1f}  "
                  f"max={vels_at_truth[-1]:.1f}")
            print(f"    Pick --min-velocity at ~p10 to capture 90% of true hits.")

        if result.get("unmatched_truth_sample"):
            print()
            print(f"  Sample missed truth (first 10): "
                  f"{result['unmatched_truth_sample']}")
        if result.get("false_positive_sample"):
            print(f"  Sample false positives (first 10): "
                  f"{result['false_positive_sample']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
