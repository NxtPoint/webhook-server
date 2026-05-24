"""Dumb-baseline ball-hit detector — y-reversal heuristic, no training, no labels.

Ported from `ameynarwadkar/Tennis-Analysis-System` `BallTracker.get_ball_shot_frames`
as a strategic probe for Phase 6.

## What it tests

Before we commit to training a stroke classifier (Phase 6's planned next step),
we want to know: how much of the ball-hit detection problem can a 20-line
pandas heuristic already solve on our footage?

The heuristic:
  1. Take ball trajectory (frame_idx, x, y) sorted by frame.
  2. Compute rolling mean of y over a small window.
  3. Find sign-flips of d/dt(y_rolling).
  4. Accept a flip as a "hit" if the preceding run of constant sign was
     sustained for >= MIN_RUN_FRAMES.

That's it. No CNN. No optical flow. No pose.

## Ground truth

SportAI's `bronze.player_swing.ball_hit_s` is the per-swing timestamp (seconds
from video start). We convert to frame index via `frame * fps`, then check
recall + precision of the heuristic's predicted hits against the ground-truth
hit frames, within ±TOLERANCE_FRAMES.

## Decision rule

If the heuristic's recall against SportAI ground truth is high (say, >= 80%
within ±3 frames), then training a stroke classifier may be unnecessary for
the ball-hit-detection task — we can build hit detection on this heuristic
and move on. If recall is mediocre (40-80%), the heuristic is useful as a
candidate generator that a downstream classifier refines. If recall is low
(< 40%), training is required.

This is a probe, not production code — keep scope tight.

## Usage

    .venv/Scripts/python -m ml_pipeline.diag.ball_hit_baseline \\
        --sa-task 0d0514df-68aa-4346-9e2d-64413429e47f \\
        --t5-task 78c32f53-5580-4a88-a4e7-7506e59b2b52

Requires DATABASE_URL in env (set on Render shell automatically).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional


# --- Heuristic knobs (matches ameynarwadkar's defaults) -------------------
ROLLING_WINDOW = 5           # frames to smooth y over before differencing
MIN_RUN_FRAMES = 25          # require sustained sign before accepting a flip
TOLERANCE_FRAMES = 3         # ±this many frames when matching predicted to truth
DEFAULT_FPS = 25.0           # production sampling fps (FRAME_SAMPLE_FPS in config.py)


def detect_hits_y_reversal(
    frames: List[int],
    ys: List[float],
    rolling_window: int = ROLLING_WINDOW,
    min_run_frames: int = MIN_RUN_FRAMES,
) -> List[int]:
    """The 20-line heuristic. Returns frame indices where hits are predicted.

    Algorithm:
      - Sort by frame_idx (caller should already have done this).
      - Apply a centred rolling-mean smoothing to y over ``rolling_window``.
      - Walk forward; track the current sign of d(y_smooth)/d(frame).
      - When the sign flips AND the previous run of constant sign was at
        least ``min_run_frames`` long, the flip frame is a candidate hit.

    No numpy/pandas dependency — keeps the diag tool importable without
    pulling the heavy ML stack. The inner loop is O(n).
    """
    if len(frames) != len(ys):
        raise ValueError("frames + ys must be the same length")
    if len(frames) < rolling_window + min_run_frames:
        return []

    # Rolling mean (right-aligned, like pandas rolling().mean() with min_periods=1)
    smoothed: List[float] = []
    for i in range(len(ys)):
        lo = max(0, i - rolling_window + 1)
        window = ys[lo:i + 1]
        smoothed.append(sum(window) / len(window))

    hits: List[int] = []
    last_sign = 0
    run_len = 0
    run_start_idx = 0

    for i in range(1, len(smoothed)):
        dy = smoothed[i] - smoothed[i - 1]
        if dy == 0:
            run_len += 1
            continue
        sign = 1 if dy > 0 else -1

        if sign == last_sign or last_sign == 0:
            run_len += 1
            if last_sign == 0:
                last_sign = sign
                run_start_idx = i
        else:
            # Sign flip. The previous run was constant-sign.
            if run_len >= min_run_frames:
                hits.append(frames[run_start_idx + run_len // 2])
            last_sign = sign
            run_len = 1
            run_start_idx = i

    return hits


def evaluate_against_truth(
    predicted: List[int],
    truth: List[int],
    tolerance: int = TOLERANCE_FRAMES,
) -> dict:
    """Compute recall + precision with a frame-window tolerance.

    A predicted hit matches a truth hit if their frame indices are within
    ``tolerance`` frames. Each truth and predicted hit can match at most
    once (greedy nearest-first).
    """
    truth_remaining = sorted(truth)
    predicted_sorted = sorted(predicted)

    matched_truth: List[int] = []
    matched_pred: List[int] = []
    unmatched_pred: List[int] = []

    # Greedy: for each predicted, find nearest unused truth within tolerance.
    truth_used = [False] * len(truth_remaining)
    for p in predicted_sorted:
        best_j = -1
        best_d = tolerance + 1
        for j, t in enumerate(truth_remaining):
            if truth_used[j]:
                continue
            d = abs(p - t)
            if d <= tolerance and d < best_d:
                best_j = j
                best_d = d
        if best_j >= 0:
            truth_used[best_j] = True
            matched_truth.append(truth_remaining[best_j])
            matched_pred.append(p)
        else:
            unmatched_pred.append(p)

    unmatched_truth = [t for j, t in enumerate(truth_remaining) if not truth_used[j]]

    recall = len(matched_truth) / len(truth) if truth else None
    precision = len(matched_pred) / len(predicted) if predicted else None

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


def _connect_db():
    """Return a SQLAlchemy engine on DATABASE_URL, psycopg-compatible."""
    from sqlalchemy import create_engine
    url = os.getenv("DATABASE_URL") or os.getenv("EXTERNAL_DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set in env")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


def fetch_t5_ball_trajectory(engine, t5_task_id: str) -> List[tuple]:
    """Return [(frame_idx, x, y), ...] sorted by frame_idx for the T5 task.

    Tries UUID match first (ball_detections.job_id::text = task_id); falls
    back to int-FK match (ball_detections.job_id = video_analysis_jobs.id)
    if the UUID match returns 0 rows. Either schema variant works.
    """
    from sqlalchemy import text as sql_text

    # Try UUID match
    sql_uuid = sql_text("""
        SELECT frame_idx, x, y
        FROM ml_analysis.ball_detections
        WHERE job_id::text = :tid
        ORDER BY frame_idx
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql_uuid, {"tid": t5_task_id}).fetchall()
    if rows:
        return [(int(r[0]), float(r[1]), float(r[2])) for r in rows]

    # Fall back to int-FK match
    sql_int = sql_text("""
        SELECT bd.frame_idx, bd.x, bd.y
        FROM ml_analysis.ball_detections bd
        JOIN ml_analysis.video_analysis_jobs vaj
          ON bd.job_id = vaj.id
        WHERE vaj.task_id::text = :tid
        ORDER BY bd.frame_idx
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql_int, {"tid": t5_task_id}).fetchall()
    return [(int(r[0]), float(r[1]), float(r[2])) for r in rows]


def fetch_sa_hit_frames(engine, sa_task_id: str, fps: float) -> List[int]:
    """Return SA's ground-truth hit frames for the SportAI task.

    ``bronze.player_swing.ball_hit_s`` is seconds-from-video-start. We
    multiply by fps to get frame indices in the same space as
    ``ml_analysis.ball_detections.frame_idx``.

    Filters to swings that look like real ball contacts (ball_hit_s IS NOT NULL).
    """
    from sqlalchemy import text as sql_text
    sql = sql_text("""
        SELECT ball_hit_s
        FROM bronze.player_swing
        WHERE task_id::text = :tid
          AND ball_hit_s IS NOT NULL
        ORDER BY ball_hit_s
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"tid": sa_task_id}).fetchall()
    return [int(round(float(r[0]) * fps)) for r in rows]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Dumb-baseline ball-hit detector vs SportAI ground truth.",
    )
    ap.add_argument("--sa-task", required=True,
                    help="SportAI task_id (provides ground-truth hit frames via "
                         "bronze.player_swing.ball_hit_s)")
    ap.add_argument("--t5-task", required=True,
                    help="T5 task_id (provides ball trajectory via "
                         "ml_analysis.ball_detections)")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS,
                    help=f"Frame sampling rate (default {DEFAULT_FPS}, matches "
                         "FRAME_SAMPLE_FPS in ml_pipeline.config)")
    ap.add_argument("--rolling-window", type=int, default=ROLLING_WINDOW,
                    help=f"Rolling-mean smoothing window in frames (default "
                         f"{ROLLING_WINDOW})")
    ap.add_argument("--min-run-frames", type=int, default=MIN_RUN_FRAMES,
                    help=f"Minimum sustained-sign run before accepting a flip "
                         f"(default {MIN_RUN_FRAMES})")
    ap.add_argument("--tolerance-frames", type=int, default=TOLERANCE_FRAMES,
                    help=f"Frame tolerance for matching predicted to truth "
                         f"(default {TOLERANCE_FRAMES})")
    ap.add_argument("--verbose", action="store_true",
                    help="Print per-truth-hit match status (slow on long matches)")
    args = ap.parse_args(argv)

    engine = _connect_db()

    print(f"=== ball-hit baseline ===")
    print(f"  SA task: {args.sa_task}")
    print(f"  T5 task: {args.t5_task}")
    print(f"  fps={args.fps} rolling={args.rolling_window} "
          f"min_run={args.min_run_frames} tolerance=±{args.tolerance_frames}")
    print()

    print("Loading T5 ball trajectory...")
    trajectory = fetch_t5_ball_trajectory(engine, args.t5_task)
    if not trajectory:
        print(f"  ERROR: no ball detections found for T5 task {args.t5_task}",
              file=sys.stderr)
        return 1
    frames = [r[0] for r in trajectory]
    ys = [r[2] for r in trajectory]
    print(f"  loaded {len(trajectory)} ball detections "
          f"(frames {min(frames)} - {max(frames)})")

    print("Loading SA ground-truth hits...")
    truth = fetch_sa_hit_frames(engine, args.sa_task, args.fps)
    if not truth:
        print(f"  ERROR: no SA hits found for task {args.sa_task} "
              f"(check bronze.player_swing.ball_hit_s)", file=sys.stderr)
        return 1
    print(f"  loaded {len(truth)} SA hit frames "
          f"(frame range {min(truth)} - {max(truth)})")

    print("Running y-reversal heuristic...")
    predicted = detect_hits_y_reversal(
        frames, ys,
        rolling_window=args.rolling_window,
        min_run_frames=args.min_run_frames,
    )
    print(f"  heuristic predicted {len(predicted)} hits")
    print()

    result = evaluate_against_truth(predicted, truth, args.tolerance_frames)
    print("=== RESULT ===")
    print(f"  Predicted hits:    {result['predicted_total']}")
    print(f"  SA truth hits:     {result['truth_total']}")
    print(f"  Matched (±{result['tolerance_frames']} frames): {result['matched']}")
    print(f"  Missed truth:      {result['missed_truth']}")
    print(f"  False positives:   {result['false_positives']}")
    print()
    if result["recall"] is not None:
        print(f"  RECALL    : {result['recall']:.1%}  "
              f"(matched / SA truth)")
    if result["precision"] is not None:
        print(f"  PRECISION : {result['precision']:.1%}  "
              f"(matched / predicted)")
    print()

    # Decision hint — calibrated to the doc string's thresholds.
    recall = result["recall"] or 0.0
    if recall >= 0.80:
        verdict = "HIGH RECALL — heuristic may be enough; training likely unnecessary for ball-hit"
    elif recall >= 0.40:
        verdict = "MEDIUM RECALL — heuristic useful as candidate generator; downstream classifier may still help"
    else:
        verdict = "LOW RECALL — training is justified for ball-hit detection"
    print(f"  verdict: {verdict}")

    if args.verbose and result.get("unmatched_truth_sample"):
        print()
        print(f"  Sample of missed truth hits (first 10): "
              f"{result['unmatched_truth_sample']}")
    if args.verbose and result.get("false_positive_sample"):
        print(f"  Sample of false positives (first 10):   "
              f"{result['false_positive_sample']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
