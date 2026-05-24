"""Phase 7 ball-bounce geometric accuracy probe -- meters of error vs SA truth.

THE measurement Tomo flagged on 2026-05-24 as the most important product
metric. Bounce x,y accuracy drives every heatmap, every "where did the ball
land" coaching insight. Coverage is now ~50%+ post-WASB; this probe answers
the question coverage alone can't: when T5 reports a bounce at (x, y), how
far is it from SA's truth at the same time?

## Data sources

  SA bounces : bronze.ball_bounce          (timestamp_s, court_x, court_y)
  T5 bounces : ml_analysis.ball_detections (frame_idx, court_x, court_y)
              WHERE is_bounce = TRUE

T5 frame_idx -> seconds via FRAME_SAMPLE_FPS (25 default). Match by time
within +/- MATCH_TOLERANCE_S (0.5s default).

## Algorithm

  1. Load SA bounces for `--sa-task` from bronze.ball_bounce. Filter to rows
     with court_x AND court_y populated (i.e. SA reported a valid x,y).
  2. Load T5 bounces for `--t5-task` from ml_analysis.ball_detections where
     is_bounce = TRUE. Convert frame_idx to seconds via fps.
  3. Greedy nearest-time matching:
       - Sort both sets by timestamp.
       - For each SA bounce, find the unused T5 bounce within tolerance with
         the smallest time delta. Mark as matched.
  4. For each matched pair, compute Euclidean court-coord error in meters:
       error_m = sqrt((sa_x - t5_x)^2 + (sa_y - t5_y)^2)
  5. Aggregate: count, recall (matched / sa_total), and the error
     distribution (min, p25, median, p75, p90, p95, max, mean).

## Verdict thresholds (Phase 7 done-when in north_star.md = <2m median)

  median <= 1.0m  : EXCELLENT - production-grade for heatmaps
  median <= 2.0m  : ACCEPTABLE - meets Phase 7 done-when target
  median <= 3.0m  : MARGINAL - usable but heatmaps will look noisy
  median > 3.0m   : INSUFFICIENT - need calibration work before shipping

## Usage

    .venv/bin/python -m ml_pipeline.diag.bounce_xy_accuracy \\
        --sa-task 0d0514df-68aa-4346-9e2d-64413429e47f \\
        --t5-task 78c32f53-5580-4a88-a4e7-7506e59b2b52 \\
        --verbose

Verbose mode prints per-pair details so you can eyeball whether the
extreme outliers are real misses or just noise. Robust to UUID-vs-int-FK
ambiguity on ball_detections.job_id (same fallback pattern as the other
probes).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Tuple


# --- Defaults ---
DEFAULT_FPS = 25.0
MATCH_TOLERANCE_S = 0.5
# Phase 7 done-when target from docs/north_star.md
TARGET_MEDIAN_ERROR_M = 2.0


def _connect_db():
    """SQLAlchemy engine on DATABASE_URL. Same shape as the other probes."""
    from sqlalchemy import create_engine
    url = os.getenv("DATABASE_URL") or os.getenv("EXTERNAL_DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL not set in env")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


def fetch_sa_bounces(engine, sa_task_id: str) -> List[Tuple[float, float, float]]:
    """Return [(timestamp_s, court_x, court_y), ...] for the SA task.

    Filters to rows where both court_x and court_y are populated AND
    timestamp is set. The "timestamp" column is double-quoted in SQL
    because it's a reserved word.
    """
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        rows = conn.execute(sql_text('''
            SELECT "timestamp", court_x, court_y
            FROM bronze.ball_bounce
            WHERE task_id::text = :tid
              AND "timestamp" IS NOT NULL
              AND court_x IS NOT NULL
              AND court_y IS NOT NULL
            ORDER BY "timestamp"
        '''), {"tid": sa_task_id}).fetchall()
    return [(float(r[0]), float(r[1]), float(r[2])) for r in rows]


def fetch_t5_bounces(
    engine, t5_task_id: str, fps: float,
) -> List[Tuple[float, float, float]]:
    """Return [(seconds_from_video_start, court_x, court_y), ...] for the
    T5 task. Sourced from ml_analysis.ball_detections WHERE is_bounce=TRUE.

    Uses the same UUID-first / int-FK fallback as ball_hit_pose.py.
    """
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, court_x, court_y
            FROM ml_analysis.ball_detections
            WHERE job_id::text = :tid
              AND is_bounce = TRUE
              AND court_x IS NOT NULL
              AND court_y IS NOT NULL
            ORDER BY frame_idx
        """), {"tid": t5_task_id}).fetchall()
    if not rows:
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT bd.frame_idx, bd.court_x, bd.court_y
                FROM ml_analysis.ball_detections bd
                JOIN ml_analysis.video_analysis_jobs vaj
                  ON bd.job_id = vaj.id::text
                WHERE vaj.task_id::text = :tid
                  AND bd.is_bounce = TRUE
                  AND bd.court_x IS NOT NULL
                  AND bd.court_y IS NOT NULL
                ORDER BY bd.frame_idx
            """), {"tid": t5_task_id}).fetchall()
    return [(float(r[0]) / fps, float(r[1]), float(r[2])) for r in rows]


def match_bounces(
    sa: List[Tuple[float, float, float]],
    t5: List[Tuple[float, float, float]],
    tolerance_s: float,
) -> List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
    """Greedy nearest-time match. Each SA bounce gets at most one T5 bounce.

    Returns list of (sa_tuple, t5_tuple) pairs. Sorted by SA timestamp.
    """
    t5_used = [False] * len(t5)
    matched = []
    for sa_ts, sa_x, sa_y in sa:
        best_j, best_dt = -1, tolerance_s + 1e-9
        for j, (t5_ts, _, _) in enumerate(t5):
            if t5_used[j]:
                continue
            dt = abs(t5_ts - sa_ts)
            if dt < best_dt:
                best_j, best_dt = j, dt
        if best_j >= 0:
            t5_used[best_j] = True
            matched.append(((sa_ts, sa_x, sa_y), t5[best_j]))
    return matched


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return float("nan")
    idx = min(len(values) - 1, max(0, int(round(pct / 100.0 * (len(values) - 1)))))
    return sorted(values)[idx]


def report_errors(matched, sa_total: int, t5_total: int, tolerance_s: float) -> dict:
    """Compute aggregate error stats. Returns the result dict for verbose printing."""
    if not matched:
        return {
            "sa_total": sa_total, "t5_total": t5_total,
            "matched": 0, "recall": 0.0,
            "errors": [], "tolerance_s": tolerance_s,
        }
    errors = []
    for (sa_ts, sa_x, sa_y), (t5_ts, t5_x, t5_y) in matched:
        e = ((sa_x - t5_x) ** 2 + (sa_y - t5_y) ** 2) ** 0.5
        errors.append({
            "sa_ts": sa_ts, "t5_ts": t5_ts, "dt_s": t5_ts - sa_ts,
            "sa_x": sa_x, "sa_y": sa_y, "t5_x": t5_x, "t5_y": t5_y,
            "error_m": e,
        })
    errs = [e["error_m"] for e in errors]
    errs_sorted = sorted(errs)
    n = len(errs_sorted)
    return {
        "sa_total": sa_total, "t5_total": t5_total,
        "matched": len(matched),
        "recall": len(matched) / sa_total if sa_total else 0.0,
        "tolerance_s": tolerance_s,
        "errors": errors,
        "min_m": errs_sorted[0],
        "p25_m": _percentile(errs_sorted, 25),
        "median_m": _percentile(errs_sorted, 50),
        "p75_m": _percentile(errs_sorted, 75),
        "p90_m": _percentile(errs_sorted, 90),
        "p95_m": _percentile(errs_sorted, 95),
        "max_m": errs_sorted[-1],
        "mean_m": sum(errs) / n,
        "n_within_1m": sum(1 for e in errs if e <= 1.0),
        "n_within_2m": sum(1 for e in errs if e <= 2.0),
        "n_within_3m": sum(1 for e in errs if e <= 3.0),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 7 ball-bounce geometric accuracy probe (meters of error vs SA truth).",
    )
    ap.add_argument("--sa-task", required=True, help="SportAI task_id (ground truth)")
    ap.add_argument("--t5-task", required=True, help="T5 task_id")
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS,
                    help=f"FRAME_SAMPLE_FPS used by T5 pipeline (default {DEFAULT_FPS})")
    ap.add_argument("--tolerance-s", type=float, default=MATCH_TOLERANCE_S,
                    help=f"Max time delta to match a T5 bounce to an SA bounce, in seconds "
                         f"(default {MATCH_TOLERANCE_S})")
    ap.add_argument("--target-median-m", type=float, default=TARGET_MEDIAN_ERROR_M,
                    help=f"Phase 7 done-when target for median error in meters "
                         f"(default {TARGET_MEDIAN_ERROR_M})")
    ap.add_argument("--verbose", action="store_true",
                    help="Print per-pair (sa_x, sa_y, t5_x, t5_y, error_m, dt_s)")
    ap.add_argument("--n-worst", type=int, default=10,
                    help=f"Print this many worst-error pairs (default 10)")
    args = ap.parse_args(argv)

    engine = _connect_db()

    print("=== bounce x,y geometric accuracy (Phase 7 measurement) ===")
    print(f"  SA task: {args.sa_task}")
    print(f"  T5 task: {args.t5_task}")
    print(f"  fps={args.fps}  tolerance=+/-{args.tolerance_s}s  "
          f"target_median={args.target_median_m}m")
    print()

    print("Loading SA bounces...")
    sa = fetch_sa_bounces(engine, args.sa_task)
    if not sa:
        print(f"  ERROR: no SA bounces with court_x/court_y for task {args.sa_task}",
              file=sys.stderr)
        return 1
    print(f"  loaded {len(sa)} SA bounces "
          f"(t={sa[0][0]:.1f}s - {sa[-1][0]:.1f}s)")

    print("Loading T5 bounces...")
    t5 = fetch_t5_bounces(engine, args.t5_task, args.fps)
    if not t5:
        print(f"  ERROR: no T5 bounces with court_x/court_y for task {args.t5_task} "
              f"(check is_bounce flag on ml_analysis.ball_detections)",
              file=sys.stderr)
        return 1
    print(f"  loaded {len(t5)} T5 bounces "
          f"(t={t5[0][0]:.1f}s - {t5[-1][0]:.1f}s)")

    print("Matching by time...")
    matched = match_bounces(sa, t5, args.tolerance_s)
    print(f"  matched {len(matched)} pairs within +/-{args.tolerance_s}s")
    print()

    result = report_errors(matched, len(sa), len(t5), args.tolerance_s)

    print("=== RESULT ===")
    print(f"  SA bounces:        {result['sa_total']}")
    print(f"  T5 bounces:        {result['t5_total']}  (incl warmup/noise)")
    print(f"  Matched pairs:     {result['matched']}")
    print(f"  Time-match recall: {result['recall']:.1%}  (matched / SA total)")
    print()

    if result["matched"] == 0:
        print("  NO MATCHED PAIRS - cannot compute geometric error.")
        print("  Likely causes: clocks misaligned, fps wrong, or T5 didn't write court_x/y on bounces.")
        return 1

    print("  --- Euclidean error in court meters ---")
    print(f"  min       : {result['min_m']:.2f} m")
    print(f"  p25       : {result['p25_m']:.2f} m")
    print(f"  MEDIAN    : {result['median_m']:.2f} m   <-- the headline number")
    print(f"  p75       : {result['p75_m']:.2f} m")
    print(f"  p90       : {result['p90_m']:.2f} m")
    print(f"  p95       : {result['p95_m']:.2f} m")
    print(f"  max       : {result['max_m']:.2f} m")
    print(f"  mean      : {result['mean_m']:.2f} m")
    print()
    print(f"  Within  1m: {result['n_within_1m']}/{result['matched']}  "
          f"({100*result['n_within_1m']/result['matched']:.0f}%)")
    print(f"  Within  2m: {result['n_within_2m']}/{result['matched']}  "
          f"({100*result['n_within_2m']/result['matched']:.0f}%)")
    print(f"  Within  3m: {result['n_within_3m']}/{result['matched']}  "
          f"({100*result['n_within_3m']/result['matched']:.0f}%)")
    print()

    m = result["median_m"]
    if m <= 1.0:
        verdict = "EXCELLENT - production-grade for heatmaps. Phase 7 cleared."
    elif m <= args.target_median_m:
        verdict = f"ACCEPTABLE - meets Phase 7 done-when (<{args.target_median_m}m median). Production-ready."
    elif m <= 3.0:
        verdict = "MARGINAL - heatmaps will look noisy. Calibration work needed."
    else:
        verdict = "INSUFFICIENT - calibration is broken. Investigate before shipping."
    print(f"  verdict: {verdict}")
    print()

    if args.verbose and result["errors"]:
        worst = sorted(result["errors"], key=lambda e: -e["error_m"])[:args.n_worst]
        print(f"  --- {len(worst)} WORST pairs ---")
        print(f"  {'sa_ts':>7s} {'dt_s':>7s} {'sa_x':>7s} {'sa_y':>7s} "
              f"{'t5_x':>7s} {'t5_y':>7s} {'err_m':>7s}")
        for e in worst:
            print(f"  {e['sa_ts']:>7.1f} {e['dt_s']:>+7.2f} "
                  f"{e['sa_x']:>7.2f} {e['sa_y']:>7.2f} "
                  f"{e['t5_x']:>7.2f} {e['t5_y']:>7.2f} "
                  f"{e['error_m']:>7.2f}")
        print()
        best = sorted(result["errors"], key=lambda e: e["error_m"])[:5]
        print(f"  --- {len(best)} BEST pairs (for sanity) ---")
        print(f"  {'sa_ts':>7s} {'dt_s':>7s} {'sa_x':>7s} {'sa_y':>7s} "
              f"{'t5_x':>7s} {'t5_y':>7s} {'err_m':>7s}")
        for e in best:
            print(f"  {e['sa_ts']:>7.1f} {e['dt_s']:>+7.2f} "
                  f"{e['sa_x']:>7.2f} {e['sa_y']:>7.2f} "
                  f"{e['t5_x']:>7.2f} {e['t5_y']:>7.2f} "
                  f"{e['error_m']:>7.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
