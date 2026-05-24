"""Pose + ball fusion ball-hit detector -- non-trained, no labels needed.

Strategic probe for Phase 6 (ball-hit / stroke detection). Follow-up to
`ball_hit_baseline.py` after that probe scored 0/12 -- the published
y-reversal heuristic doesn't apply to amateur side-cam footage because
it assumes broadcast/top-down camera geometry.

## Why this should work where the baseline didn't

On side-cam footage, the ball moves mostly horizontally; y-reversals are
tiny and don't correspond to hits. But we have a signal the published
repo doesn't: **YOLOv8x-pose** keypoints stored in
`ml_analysis.player_detections.keypoints` (JSONB, 17 COCO keypoints per
player per frame).

A tennis hit happens at the moment the ball touches the racquet, which
is held in the player's hand. The wrist keypoint (COCO 9=left, 10=right)
is the closest stable proxy for the racquet head. So:

  **hit_frame ~ argmin_t distance(ball(t), nearest_wrist(t))**

filtered to local minima where the distance is plausibly racquet-length
(a tennis racquet extends ~60-70cm from the wrist; at typical side-cam
distance that's ~40-120 pixels).

This is essentially the Silent Impact 2025 / TAL4Tennis pose-first
approach we already use for serve detection -- generalized to all strokes.

## Algorithm

1. Load all ball detections (frame_idx, x, y) for the T5 task.
2. Load all player_detections (frame_idx, player_id, keypoints) for the
   same task. Index by (frame_idx, player_id).
3. For each ball detection at frame F:
   - Within +/-POSE_LOOKBACK frames, find the NEAREST player_detection row
     for each of player_id in {0 (NEAR), 1 (FAR)}.
   - Extract left_wrist and right_wrist (keypoints[9] and [10]) where
     confidence > MIN_KP_CONF.
   - Compute Euclidean distance from ball to each available wrist
     (NEAR-L, NEAR-R, FAR-L, FAR-R). Take the minimum across all four.
4. The result is a time series: frame_idx -> min_wrist_distance.
5. Find local minima below MAX_HIT_DISTANCE_PX, enforcing
   MIN_GAP_FRAMES between accepted hits. Each local minimum is a
   candidate ball-hit event.
6. Bench against SA's bronze.player_swing.ball_hit_s ground truth.

## Decision rule (same as ball_hit_baseline.py)

  recall >= 80%: pose+ball fusion alone is enough; training optional
  40-80%:        useful candidate generator; downstream refiner may help
  < 40%:         training is justified

## Usage

    .venv/bin/python -m ml_pipeline.diag.ball_hit_fusion \\
        --sa-task 0d0514df-68aa-4346-9e2d-64413429e47f \\
        --t5-task 78c32f53-5580-4a88-a4e7-7506e59b2b52

The CLI exposes the tuning knobs (--max-hit-distance, --min-gap-frames,
--pose-lookback, --min-kp-conf) so we can sweep parameters without
re-shipping the file. Default tuning is informed by side-cam geometry
but will probably need calibration on the first match.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Tuple


# --- Tuning knobs (defaults; CLI can override) ----------------------------
MAX_HIT_DISTANCE_PX = 120     # ball-to-wrist max for hit candidate (racquet length proxy)
MIN_GAP_FRAMES = 15           # enforce >= 0.6s between consecutive hits at 25fps
POSE_LOOKBACK_FRAMES = 2      # search +/-this many frames if exact-frame pose is missing
MIN_KP_CONF = 0.3             # YOLO confidence threshold for a wrist to be used
TOLERANCE_FRAMES = 3          # +/- this when matching predicted to SA truth
DEFAULT_FPS = 25.0

# COCO keypoint indices (matches ml_pipeline/player_tracker.py constants)
KP_LEFT_WRIST = 9
KP_RIGHT_WRIST = 10


def _connect_db():
    """SQLAlchemy engine on DATABASE_URL, psycopg-compatible. Mirrors
    ball_hit_baseline.py so the diag tools behave identically.
    """
    from sqlalchemy import create_engine
    url = os.getenv("DATABASE_URL") or os.getenv("EXTERNAL_DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set in env")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


def fetch_t5_ball_trajectory(engine, t5_task_id: str) -> List[Tuple[int, float, float]]:
    """Same query as ball_hit_baseline.py -- UUID-first with int-FK fallback."""
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, x, y
            FROM ml_analysis.ball_detections
            WHERE job_id::text = :tid
            ORDER BY frame_idx
        """), {"tid": t5_task_id}).fetchall()
    if rows:
        return [(int(r[0]), float(r[1]), float(r[2])) for r in rows]
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT bd.frame_idx, bd.x, bd.y
            FROM ml_analysis.ball_detections bd
            JOIN ml_analysis.video_analysis_jobs vaj ON bd.job_id = vaj.id::text
            WHERE vaj.task_id::text = :tid
            ORDER BY bd.frame_idx
        """), {"tid": t5_task_id}).fetchall()
    return [(int(r[0]), float(r[1]), float(r[2])) for r in rows]


def fetch_t5_player_poses(engine, t5_task_id: str) -> dict:
    """Return {(frame_idx, player_id): keypoints} where keypoints is the
    raw 17x3 list-of-lists from JSONB.

    Rows where keypoints IS NULL are skipped (SAHI-only detections don't
    carry pose). The dict is indexed for O(1) lookup by (frame, player_id).
    """
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, player_id, keypoints
            FROM ml_analysis.player_detections
            WHERE job_id::text = :tid
              AND keypoints IS NOT NULL
            ORDER BY frame_idx, player_id
        """), {"tid": t5_task_id}).fetchall()
    if not rows:
        # int-FK fallback (same shape as ball_detections schema variance)
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT pd.frame_idx, pd.player_id, pd.keypoints
                FROM ml_analysis.player_detections pd
                JOIN ml_analysis.video_analysis_jobs vaj ON pd.job_id = vaj.id::text
                WHERE vaj.task_id::text = :tid
                  AND pd.keypoints IS NOT NULL
                ORDER BY pd.frame_idx, pd.player_id
            """), {"tid": t5_task_id}).fetchall()
    out = {}
    for r in rows:
        frame_idx, player_id, kps = int(r[0]), int(r[1]), r[2]
        # kps may come back as a JSON string or a parsed list depending on driver.
        # SQLAlchemy + psycopg returns JSONB as a Python list/dict already.
        if isinstance(kps, str):
            try:
                kps = json.loads(kps)
            except Exception:
                continue
        out[(frame_idx, player_id)] = kps
    return out


def fetch_sa_hit_frames(engine, sa_task_id: str, fps: float) -> List[int]:
    """Same as ball_hit_baseline.py -- converts SA's seconds to frame indices."""
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT ball_hit_s
            FROM bronze.player_swing
            WHERE task_id::text = :tid AND ball_hit_s IS NOT NULL
            ORDER BY ball_hit_s
        """), {"tid": sa_task_id}).fetchall()
    return [int(round(float(r[0]) * fps)) for r in rows]


def _wrist_positions(keypoints, min_conf: float) -> List[Tuple[float, float]]:
    """Extract (x, y) for left_wrist and right_wrist if confidence > min_conf.

    keypoints is a 17-element list of [x, y, conf] triples.
    Returns 0-2 positions depending on how many wrists pass the confidence gate.
    """
    out = []
    for idx in (KP_LEFT_WRIST, KP_RIGHT_WRIST):
        try:
            x, y, c = keypoints[idx]
        except (IndexError, TypeError, ValueError):
            continue
        if c is not None and float(c) >= min_conf:
            out.append((float(x), float(y)))
    return out


def _nearest_pose_at_frame(
    poses: dict, frame: int, player_id: int, lookback: int,
) -> Optional[list]:
    """Find the closest-frame pose for (player_id) within [frame-lookback, frame+lookback]."""
    for delta in range(lookback + 1):
        for f in (frame - delta, frame + delta) if delta > 0 else (frame,):
            kps = poses.get((f, player_id))
            if kps is not None:
                return kps
    return None


def compute_wrist_distances(
    trajectory: List[Tuple[int, float, float]],
    poses: dict,
    pose_lookback: int,
    min_kp_conf: float,
) -> List[Tuple[int, float]]:
    """For each ball detection, return (frame_idx, min_wrist_distance).

    Searches both NEAR (player_id=0) and FAR (player_id=1) for the
    nearest wrist (left or right). Returns infinity for frames where no
    valid wrist is found -- caller filters before detecting hits.
    """
    out = []
    for frame, bx, by in trajectory:
        min_dist = float("inf")
        for player_id in (0, 1):
            kps = _nearest_pose_at_frame(poses, frame, player_id, pose_lookback)
            if kps is None:
                continue
            for wx, wy in _wrist_positions(kps, min_kp_conf):
                d = ((wx - bx) ** 2 + (wy - by) ** 2) ** 0.5
                if d < min_dist:
                    min_dist = d
        out.append((frame, min_dist))
    return out


def detect_hit_local_minima(
    distances: List[Tuple[int, float]],
    max_hit_distance: float,
    min_gap_frames: int,
) -> List[int]:
    """Return frame indices of local minima of wrist distance below threshold.

    A local minimum is a point where the distance is lower than its
    immediate neighbours AND below max_hit_distance. The min_gap_frames
    constraint suppresses re-firing on long contiguous low-distance runs
    (e.g., player holding the ball before serving).
    """
    if len(distances) < 3:
        return []

    hits = []
    last_accepted_frame = -10**9

    for i in range(1, len(distances) - 1):
        frame, d = distances[i]
        if d > max_hit_distance:
            continue
        _, d_prev = distances[i - 1]
        _, d_next = distances[i + 1]
        # Strict local minimum (must be strictly less than at least one
        # neighbour and not strictly greater than either -- handles flat
        # bottoms by taking the first frame of the plateau)
        if d <= d_prev and d <= d_next and (d < d_prev or d < d_next):
            if frame - last_accepted_frame >= min_gap_frames:
                hits.append(frame)
                last_accepted_frame = frame
    return hits


def evaluate_against_truth(
    predicted: List[int], truth: List[int], tolerance: int,
) -> dict:
    """Greedy nearest-first matching -- same logic as ball_hit_baseline.py.
    Kept duplicated so the two diag tools stay self-contained.
    """
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
        description="Pose + ball fusion ball-hit detector vs SportAI truth.",
    )
    ap.add_argument("--sa-task", required=True, help="SportAI task_id (ground truth)")
    ap.add_argument("--t5-task", required=True, help="T5 task_id (ball + pose data)")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS,
                    help=f"Frame sampling rate (default {DEFAULT_FPS})")
    ap.add_argument("--max-hit-distance", type=float, default=MAX_HIT_DISTANCE_PX,
                    help=f"Max ball-to-wrist distance for a hit candidate, in pixels "
                         f"(default {MAX_HIT_DISTANCE_PX})")
    ap.add_argument("--min-gap-frames", type=int, default=MIN_GAP_FRAMES,
                    help=f"Minimum frame gap between consecutive hits "
                         f"(default {MIN_GAP_FRAMES} ~ 0.6s at 25fps)")
    ap.add_argument("--pose-lookback", type=int, default=POSE_LOOKBACK_FRAMES,
                    help=f"Frame window to search for pose if exact frame is missing "
                         f"(default +/-{POSE_LOOKBACK_FRAMES})")
    ap.add_argument("--min-kp-conf", type=float, default=MIN_KP_CONF,
                    help=f"Min YOLO keypoint confidence to use a wrist "
                         f"(default {MIN_KP_CONF})")
    ap.add_argument("--tolerance-frames", type=int, default=TOLERANCE_FRAMES,
                    help=f"Tolerance for matching predicted to truth (default {TOLERANCE_FRAMES})")
    ap.add_argument("--verbose", action="store_true",
                    help="Print distance distribution + sample matches")
    args = ap.parse_args(argv)

    engine = _connect_db()

    print(f"=== ball-hit fusion (pose + ball) ===")
    print(f"  SA task: {args.sa_task}")
    print(f"  T5 task: {args.t5_task}")
    print(f"  fps={args.fps}  max_dist={args.max_hit_distance}px  "
          f"min_gap={args.min_gap_frames}f  pose_lookback=+/-{args.pose_lookback}f  "
          f"min_kp_conf={args.min_kp_conf}  tolerance=+/-{args.tolerance_frames}f")
    print()

    print("Loading T5 ball trajectory...")
    trajectory = fetch_t5_ball_trajectory(engine, args.t5_task)
    if not trajectory:
        print(f"  ERROR: no ball detections for T5 task {args.t5_task}", file=sys.stderr)
        return 1
    print(f"  loaded {len(trajectory)} ball detections "
          f"(frames {trajectory[0][0]} - {trajectory[-1][0]})")

    print("Loading T5 player poses...")
    poses = fetch_t5_player_poses(engine, args.t5_task)
    if not poses:
        print(f"  ERROR: no player poses for T5 task {args.t5_task} "
              f"(player_detections.keypoints all NULL -- was this run with SAHI-only?)",
              file=sys.stderr)
        return 1
    pose_frames = sorted(set(f for f, _ in poses.keys()))
    print(f"  loaded {len(poses)} player-frame pose entries "
          f"across {len(pose_frames)} unique frames "
          f"({pose_frames[0]} - {pose_frames[-1]})")

    print("Loading SA ground-truth hits...")
    truth = fetch_sa_hit_frames(engine, args.sa_task, args.fps)
    if not truth:
        print(f"  ERROR: no SA hits for task {args.sa_task}", file=sys.stderr)
        return 1
    print(f"  loaded {len(truth)} SA hit frames "
          f"(frame range {min(truth)} - {max(truth)})")

    print("Computing per-frame min wrist distance...")
    distances = compute_wrist_distances(
        trajectory, poses,
        pose_lookback=args.pose_lookback, min_kp_conf=args.min_kp_conf,
    )
    finite = [d for _, d in distances if d != float("inf")]
    if not finite:
        print("  ERROR: no frame had both ball + valid wrist -- pose lookback "
              "too tight, or no overlap between ball and pose frames",
              file=sys.stderr)
        return 1
    print(f"  {len(finite)}/{len(distances)} frames have both ball + valid wrist; "
          f"min={min(finite):.1f}px  median={sorted(finite)[len(finite)//2]:.1f}px  "
          f"p10={sorted(finite)[max(0, len(finite)//10)]:.1f}px")

    print("Detecting hit local minima...")
    predicted = detect_hit_local_minima(
        distances,
        max_hit_distance=args.max_hit_distance,
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
        verdict = "HIGH RECALL -- fusion is enough; Phase 6 may be doable without training"
    elif recall >= 0.40:
        verdict = "MEDIUM RECALL -- fusion is a good candidate generator; refine knobs or add downstream classifier"
    else:
        verdict = "LOW RECALL -- try tuning (--max-hit-distance, --min-kp-conf) before declaring; training may be needed"
    print(f"  verdict: {verdict}")

    if args.verbose:
        if result.get("unmatched_truth_sample"):
            print()
            print(f"  Sample missed truth (first 10): "
                  f"{result['unmatched_truth_sample']}")
        if result.get("false_positive_sample"):
            print(f"  Sample false positives (first 10): "
                  f"{result['false_positive_sample']}")
        # Histogram of wrist distances at SA truth frames vs everywhere
        truth_set = set(truth)
        dists_at_truth = [d for f, d in distances
                          if f in truth_set and d != float("inf")]
        if dists_at_truth:
            dists_at_truth.sort()
            n = len(dists_at_truth)
            print()
            print(f"  Wrist distance AT SA truth frames (n={n}):")
            print(f"    min={dists_at_truth[0]:.1f}  p10={dists_at_truth[n//10]:.1f}  "
                  f"median={dists_at_truth[n//2]:.1f}  "
                  f"p90={dists_at_truth[min(n-1, n*9//10)]:.1f}  "
                  f"max={dists_at_truth[-1]:.1f}")
            print(f"    This tells you the right --max-hit-distance to set: "
                  f"~p90 captures 90% of true hits.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
