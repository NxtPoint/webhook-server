"""Query silver.point_detail around SportAI's near-player-serve timestamps
to see what stroke_d T5 assigned (if any) for the near player at those
moments.

If the answer is "Forehand" for every SA near-serve timestamp, it
confirms that silver's own serve gate is rejecting near-player serves
and stroke_d is falling through to groundstroke labels — separate
from whatever ml_analysis.serve_events contains.

Usage (Render shell):
    python -m ml_pipeline.diag.query_near_serve_shadow \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text


# SportAI near-player serve timestamps from the reconcile output for task
# 4a194ff3 (same video content across all submissions).
SA_NEAR_SERVES = [
    54.48, 73.12, 83.36, 104.56, 120.28, 142.40, 148.52,
    178.44, 195.04, 224.96, 272.76, 286.84, 323.04, 347.08,
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--window", type=float, default=3.0,
                    help="Seconds of ±window around each SA timestamp")
    args = ap.parse_args(argv)

    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("DB_URL"))
    if not url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    engine = create_engine(url)

    print(f"{'sa_ts':>8}  {'t5_ts':>8}  {'pid':>3}  {'stroke':<10} "
          f"{'swing':<12} {'serve_d':>7}  {'hy':>6}  {'plr_cy':>7}")
    print("-" * 78)

    with engine.connect() as conn:
        for sa_ts in SA_NEAR_SERVES:
            rows = conn.execute(sql_text("""
                SELECT ball_hit_s, player_id, stroke_d, swing_type,
                       serve_d,
                       ROUND(ball_hit_location_y::numeric, 2) AS hy,
                       ROUND(court_y::numeric, 2) AS plr_cy
                FROM silver.point_detail
                WHERE task_id = :tid
                  AND ball_hit_s BETWEEN :lo AND :hi
                ORDER BY ball_hit_s
            """), {"tid": args.task,
                   "lo": sa_ts - args.window,
                   "hi": sa_ts + args.window}).fetchall()

            if not rows:
                print(f"{sa_ts:>8.2f}  (no T5 rows in window)")
            for r in rows:
                ts = float(r.ball_hit_s)
                print(f"{sa_ts:>8.2f}  {ts:>8.2f}  {r.player_id:>3}  "
                      f"{str(r.stroke_d):<10} {str(r.swing_type)[:12]:<12} "
                      f"{str(r.serve_d):>7}  {str(r.hy):>6}  {str(r.plr_cy):>7}")
            if rows:
                print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
