"""Quick standalone query: dump ml_analysis.player_detections rows for a
task + frame window. Exists because multi-line heredocs in Render shell
are flaky — a single-module invocation is more reliable.

Usage (Render shell):
    python -m ml_pipeline.diag.query_detections \\
        --task f181aaf7-6862-4364-bd03-7e92ff5346e9 \\
        --from 1990 --to 2010

Prints one line per row with bbox dimensions + pose flag, plus summary.
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--from", dest="frame_from", type=int, required=True)
    ap.add_argument("--to", dest="frame_to", type=int, required=True)
    args = ap.parse_args(argv)

    url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("DB_URL")
    )
    if not url:
        print("DATABASE_URL env var required", file=sys.stderr)
        return 2
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, player_id,
                   ROUND(court_x::numeric, 1) AS cx,
                   ROUND(court_y::numeric, 1) AS cy,
                   ROUND((bbox_x2 - bbox_x1)::numeric, 0) AS w_px,
                   ROUND((bbox_y2 - bbox_y1)::numeric, 0) AS h_px,
                   (keypoints IS NOT NULL) AS pose
            FROM ml_analysis.player_detections
            WHERE job_id = :tid
              AND frame_idx BETWEEN :lo AND :hi
            ORDER BY frame_idx, player_id
        """), {"tid": args.task, "lo": args.frame_from, "hi": args.frame_to}).fetchall()

    print(f"{'frame':>6} {'pid':>3} {'cx':>6} {'cy':>6} {'w_px':>5} {'h_px':>5} {'pose':>5}")
    print("-" * 45)
    for r in rows:
        print(f"{r.frame_idx:>6} {r.player_id:>3} {str(r.cx):>6} {str(r.cy):>6} "
              f"{str(r.w_px):>5} {str(r.h_px):>5} {str(r.pose):>5}")
    print(f"\ntotal rows: {len(rows)}")
    n_pid0 = sum(1 for r in rows if r.player_id == 0)
    n_pid1 = sum(1 for r in rows if r.player_id == 1)
    n_pid0_pose = sum(1 for r in rows if r.player_id == 0 and r.pose)
    n_pid0_nopose = sum(1 for r in rows if r.player_id == 0 and not r.pose)
    print(f"pid=0: {n_pid0} rows ({n_pid0_pose} with pose, {n_pid0_nopose} without)")
    print(f"pid=1: {n_pid1} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
