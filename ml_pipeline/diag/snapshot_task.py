"""Snapshot a task's serve-detector inputs + SA ground truth to a local pickle.

Captures EVERYTHING the production serve_detector consumes for one task:

  - merged pose_near (bronze + ROI for player_id=0)
  - merged pose_far (bronze + ROI for player_id=1)
  - ball_rows (bronze + ROI bounces, full ball detections list)
  - fps, is_left_handed
  - SA ground truth from the paired SportAI task (ts, side, role, court_x/y,
    ball_hit_location_y) so reconcile can run without a DB connection

Usage (Render shell):
    python -m ml_pipeline.diag.snapshot_task \\
        --task a798eff0-551f-4b5a-838f-7933866a727c \\
        --sportai 2c1ad953-b65b-41b4-9999-975964ff92e1 \\
        --out ml_pipeline/fixtures/a798eff0.pkl.gz

After dumping, copy the file to your local checkout (Render shell → SCP / S3
sync / etc). Once local, replay_serves / bench / audit_all_serves operate
entirely offline — no DB, no Render round-trip, sub-second iteration.

The fixture is the new source of truth for offline detector validation.
Each detector tweak: edit code → run bench → see deltas. Cloudnet-vs-prod
drift is impossible because the harness uses the SAME _run_pipeline() the
production entry point uses.
"""
from __future__ import annotations

import argparse
import gzip
import os
import pickle
import sys
from pathlib import Path

from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.serve_detector.detector import (
    _load_pose_rows,
    _load_ball_rows,
    _get_dominant_hand,
)


SCHEMA_VERSION = 1
DEFAULT_SA = "2c1ad953-b65b-41b4-9999-975964ff92e1"


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _load_sa_truth(conn, sa_task_id: str) -> list:
    """Pull the SportAI serve ground truth from silver.point_detail."""
    rows = conn.execute(sql_text("""
        SELECT
            ball_hit_s AS ts,
            serve_side_d AS side,
            CASE
                WHEN ball_hit_location_y > 22 THEN 'NEAR'
                WHEN ball_hit_location_y < 2  THEN 'FAR'
                ELSE '?'
            END AS role,
            ball_hit_location_y AS hy,
            court_x AS bx,
            court_y AS by
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND serve_d = TRUE
        ORDER BY ball_hit_s
    """), {"tid": sa_task_id}).mappings().all()
    return [dict(r) for r in rows]


def _take_snapshot(conn, task_id: str, sa_task_id: str) -> dict:
    fps = conn.execute(sql_text(
        "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
        "WHERE job_id = :tid"
    ), {"tid": task_id}).scalar() or 25.0
    is_left_handed = _get_dominant_hand(conn, task_id)

    pose_near = _load_pose_rows(conn, task_id, 0, is_left_handed=is_left_handed)
    pose_far = _load_pose_rows(conn, task_id, 1, is_left_handed=is_left_handed)
    ball_rows = _load_ball_rows(conn, task_id)
    sa_truth = _load_sa_truth(conn, sa_task_id)

    # Convert any non-pickleable mapping types (RowMapping etc.) to plain
    # dicts. _load_pose_rows / _load_ball_rows already do this but be
    # defensive — pickle errors here would be silent on Render and
    # painful to debug locally.
    pose_near = [dict(r) for r in pose_near]
    pose_far = [dict(r) for r in pose_far]
    ball_rows = [dict(r) for r in ball_rows]

    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "sa_task_id": sa_task_id,
        "fps": float(fps),
        "is_left_handed": bool(is_left_handed),
        "pose_near": pose_near,
        "pose_far": pose_far,
        "ball_rows": ball_rows,
        "sa_truth": sa_truth,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="T5 task_id to snapshot")
    ap.add_argument("--sportai", default=DEFAULT_SA,
                    help=f"SportAI reference for ground truth (default {DEFAULT_SA})")
    ap.add_argument("--out", default=None,
                    help="Output path (default ml_pipeline/fixtures/<task[:8]>.pkl.gz)")
    args = ap.parse_args(argv)

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2

    out_path = args.out
    if out_path is None:
        out_dir = Path("ml_pipeline/fixtures")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / f"{args.task[:8]}.pkl.gz")

    engine = create_engine(_normalize_db_url(db_url))
    print(f"=== snapshot_task task={args.task[:8]} sa={args.sportai[:8]} ===")
    with engine.connect() as conn:
        snap = _take_snapshot(conn, args.task, args.sportai)

    print(f"  fps={snap['fps']:.2f} left_handed={snap['is_left_handed']}")
    print(f"  pose_near={len(snap['pose_near'])}  "
          f"pose_far={len(snap['pose_far'])}  "
          f"ball_rows={len(snap['ball_rows'])}  "
          f"sa_truth={len(snap['sa_truth'])}")

    with gzip.open(out_path, "wb") as f:
        pickle.dump(snap, f, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(out_path) / 1e6
    print(f"  -> wrote {out_path}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
