"""Strict serve reconciliation: SportAI ground truth vs T5 detected events.

Tighter than `harness eval-serve` (which uses 3s greedy matching). For each
SportAI serve, finds the closest T5 serve_event within ±2s and prints a
side-by-side row with time delta, bounce distance, and a verdict:

    MATCH           — dt <= 0.5s AND bounce_dist <= 4m (confident same serve)
    WEAK_TIME       — dt 0.5-1.0s
    SUSPECT_BOUNCE  — dt small but bounce position > 4m apart
    FAR_IN_TIME     — dt > 1.0s (probably coincidental within the 2s window)
    NO_MATCH        — no T5 event within 2s

This tells us whether the 14 matched TP in eval-serve are really the same
physical serves, or whether loose matching is inflating the number.

Usage (Render shell):
    python -m ml_pipeline.diag.reconcile_serves_strict \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088

Defaults to SportAI reference 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb; override
with --sportai <uuid> if needed.
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text


DEFAULT_SA = "4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb"


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True,
                    help="T5 task_id to reconcile")
    ap.add_argument("--sportai", default=DEFAULT_SA,
                    help=f"SportAI reference task_id (default {DEFAULT_SA})")
    ap.add_argument("--window", type=float, default=2.0,
                    help="Max ts gap seconds to consider a candidate match")
    args = ap.parse_args(argv)

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2

    engine = create_engine(_normalize_db_url(db_url))

    sql = """
    WITH sa AS (
      SELECT
        ball_hit_s AS ts,
        serve_side_d AS side,
        CASE
          WHEN ball_hit_location_y > 22 THEN 'NEAR'
          WHEN ball_hit_location_y < 2 THEN 'FAR'
          ELSE '?'
        END AS role,
        ROUND(ball_hit_location_y::numeric, 1) AS hy,
        ROUND(court_x::numeric, 1) AS bx,
        ROUND(court_y::numeric, 1) AS by
      FROM silver.point_detail
      WHERE task_id = CAST(:sa_tid AS uuid)
        AND model = 'sportai'
        AND serve_d = TRUE
    ),
    paired AS (
      SELECT
        sa.*,
        t5.ts AS t5_ts,
        t5.player_id AS t5_pid,
        t5.source AS t5_source,
        ROUND(t5.confidence::numeric, 2) AS t5_conf,
        ROUND(t5.hitter_court_y::numeric, 1) AS t5_hy,
        ROUND(t5.bounce_court_x::numeric, 1) AS t5_bx,
        ROUND(t5.bounce_court_y::numeric, 1) AS t5_by,
        -- Timing semantics: SA ball_hit_s is the HIT (racket contact)
        -- time. T5 events are stamped at different physical moments:
        --   pose_only / pose_and_* : TROPHY PEAK (frame with highest
        --     dom_wrist_y). Trophy peak happens 0.3-0.6 s BEFORE hit
        --     in a normal serve motion. Most matches cluster at +0.5 s.
        --   bounce_only : BOUNCE time, ~0.5 s AFTER hit (ball flight).
        -- Correct dt by shifting T5 ts by the appropriate flight / toss
        -- offset so dt = 0 means "correctly detected the same physical
        -- serve". Without these shifts, a correctly-firing pose event
        -- at trophy peak has dt ≈ 0.5 s by construction and sits right
        -- on the MATCH / WEAK_TIME boundary — inflating WEAK verdicts
        -- for physically-correct detections. Take the MIN of raw and
        -- shifted dt so a single offset value handles both "pose at
        -- trophy" (normal) and "pose at follow-through" (rare; peak
        -- picking caught post-hit frame). Same for bounce: raw or -0.5.
        ROUND(LEAST(
          ABS(sa.ts - t5.ts),
          CASE
            WHEN t5.source LIKE 'pose%' THEN ABS(sa.ts - (t5.ts + 0.5))
            WHEN t5.source = 'bounce_only' THEN ABS(sa.ts - (t5.ts - 0.5))
            ELSE ABS(sa.ts - t5.ts)
          END
        )::numeric, 2) AS dt,
        CASE
          WHEN t5.bounce_court_x IS NOT NULL AND sa.bx IS NOT NULL
          THEN ROUND(SQRT(POWER(sa.bx - t5.bounce_court_x, 2)
                        + POWER(sa.by - t5.bounce_court_y, 2))::numeric, 1)
          ELSE NULL
        END AS bounce_dist_m
      FROM sa
      LEFT JOIN LATERAL (
        SELECT ts, player_id, source, confidence,
               hitter_court_x, hitter_court_y,
               bounce_court_x, bounce_court_y
        FROM ml_analysis.serve_events
        WHERE task_id = CAST(:t5_tid AS uuid)
          AND ABS(ts - sa.ts) <= :win
        ORDER BY ABS(ts - sa.ts)
        LIMIT 1
      ) t5 ON TRUE
    )
    SELECT
      ts, role, side, hy, bx, by,
      t5_ts, t5_pid, t5_source, t5_conf,
      t5_hy, t5_bx, t5_by,
      dt, bounce_dist_m,
      CASE
        WHEN t5_ts IS NULL THEN 'NO_MATCH'
        WHEN dt > 1.0 THEN 'FAR_IN_TIME'
        WHEN dt > 0.5 THEN 'WEAK_TIME'
        WHEN bounce_dist_m IS NOT NULL AND bounce_dist_m > 4.0 THEN 'SUSPECT_BOUNCE'
        ELSE 'MATCH'
      END AS verdict
    FROM paired
    ORDER BY ts
    """

    with engine.connect() as conn:
        rows = conn.execute(sql_text(sql), {
            "sa_tid": args.sportai,
            "t5_tid": args.task,
            "win": args.window,
        }).mappings().all()

    print(f"=== reconcile_serves_strict ===")
    print(f"  SA (ground truth):  {args.sportai}")
    print(f"  T5 (verify):        {args.task}")
    print(f"  match window:       ±{args.window}s")
    print()

    # Header
    print(f"{'SA ts':>7} {'role':>4} {'side':>6} {'SA hy':>6} "
          f"{'SA bx':>6} {'SA by':>6} | "
          f"{'T5 ts':>7} {'pid':>3} {'src':<15} {'cnf':>4} "
          f"{'T5 hy':>6} {'T5 bx':>6} {'T5 by':>6} | "
          f"{'dt':>5} {'d_b':>5} | verdict")
    print("-" * 135)

    verdict_counts = {}
    for r in rows:
        v = r["verdict"]
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        print(f"{str(r['ts']):>7} {r['role']:>4} "
              f"{str(r['side'])[:6]:>6} {str(r['hy']):>6} "
              f"{str(r['bx']):>6} {str(r['by']):>6} | "
              f"{str(r['t5_ts']):>7} {str(r['t5_pid']):>3} "
              f"{str(r['t5_source'])[:15]:<15} {str(r['t5_conf']):>4} "
              f"{str(r['t5_hy']):>6} {str(r['t5_bx']):>6} {str(r['t5_by']):>6} | "
              f"{str(r['dt']):>5} {str(r['bounce_dist_m']):>5} | {v}")

    print()
    print("=== VERDICT BREAKDOWN ===")
    total = len(rows)
    for v, n in sorted(verdict_counts.items(), key=lambda x: -x[1]):
        print(f"  {v:<16} {n:>3} / {total}  ({100*n/max(1,total):.0f}%)")

    near_match = sum(1 for r in rows
                     if r["role"] == "NEAR" and r["verdict"] == "MATCH")
    far_match = sum(1 for r in rows
                    if r["role"] == "FAR" and r["verdict"] == "MATCH")
    near_total = sum(1 for r in rows if r["role"] == "NEAR")
    far_total = sum(1 for r in rows if r["role"] == "FAR")
    print()
    print(f"  near MATCH:  {near_match}/{near_total}")
    print(f"  far MATCH:   {far_match}/{far_total}")
    print()
    if verdict_counts.get("MATCH", 0) >= total * 0.8:
        print("  Reconciliation is CLEAN — most matched pairs are the same physical serve.")
    elif verdict_counts.get("MATCH", 0) >= total * 0.5:
        print("  Reconciliation is PARTIAL — a substantial share of pairs are weak/suspect.")
    else:
        print("  Reconciliation is WEAK — few confident matches. Investigate the gaps.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
