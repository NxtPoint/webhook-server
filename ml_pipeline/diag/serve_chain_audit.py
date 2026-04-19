"""serve_chain_audit.py — pinpoint where the pose→serve chain is losing
data for a given task. Produces a funnel report:

  pose rows (pid=0)
    ↓ with keypoints that parse
      ↓ in near-baseline zone (court_y 18.5-28.0)
        ↓ "usable" pose (dom_wrist + shoulder confident)
          ↓ trophy pose (dom_wr above nose OR dom_shoulder)
            ↓ serves detected

At each step we also show a couple of sample rows so you can eyeball
whether the numbers look sane. Same funnel for pid=1 / far zone.

Runs read-only against ml_analysis.player_detections — needs only
DATABASE_URL. No Batch, no image rebuild.

Usage (Render shell):
    python -m ml_pipeline.diag.serve_chain_audit \\
        --task 9fe8c096-09b6-44f8-bceb-ab9185e24ca9
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.serve_detector.pose_signal import (
    MIN_KP_CONF,
    parse_keypoints,
    score_pose_frame,
)


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _funnel_for_player(conn, task_id: str, player_id: int, zone_name: str,
                        court_y_lo: float, court_y_hi: float,
                        is_left_handed: bool):
    total = conn.execute(sql_text("""
        SELECT COUNT(*) FROM ml_analysis.player_detections
        WHERE job_id = :tid AND player_id = :pid
    """), {"tid": task_id, "pid": player_id}).scalar() or 0

    with_kps = conn.execute(sql_text("""
        SELECT COUNT(*) FROM ml_analysis.player_detections
        WHERE job_id = :tid AND player_id = :pid AND keypoints IS NOT NULL
    """), {"tid": task_id, "pid": player_id}).scalar() or 0

    in_zone = conn.execute(sql_text("""
        SELECT COUNT(*) FROM ml_analysis.player_detections
        WHERE job_id = :tid AND player_id = :pid
          AND keypoints IS NOT NULL
          AND court_y BETWEEN :lo AND :hi
    """), {"tid": task_id, "pid": player_id, "lo": court_y_lo, "hi": court_y_hi}).scalar() or 0

    # Load zone rows with keypoints to do the pose scoring locally
    rows = conn.execute(sql_text("""
        SELECT frame_idx, keypoints, court_y
        FROM ml_analysis.player_detections
        WHERE job_id = :tid AND player_id = :pid
          AND keypoints IS NOT NULL
          AND court_y BETWEEN :lo AND :hi
        ORDER BY frame_idx
    """), {"tid": task_id, "pid": player_id, "lo": court_y_lo, "hi": court_y_hi}).mappings().all()

    n_parsed = 0
    n_usable = 0
    n_trophy = 0
    n_toss = 0
    n_bothup = 0
    n_score3 = 0
    sample_rows = []
    for r in rows:
        kp = parse_keypoints(r["keypoints"])
        if kp is None:
            continue
        n_parsed += 1
        ps = score_pose_frame(kp, is_left_handed)
        if ps.usable:
            n_usable += 1
        if ps.trophy:
            n_trophy += 1
        if ps.toss:
            n_toss += 1
        if ps.both_up:
            n_bothup += 1
        if ps.total == 3:
            n_score3 += 1
        if len(sample_rows) < 3 and ps.usable:
            sample_rows.append({
                "frame": int(r["frame_idx"]),
                "court_y": float(r["court_y"]),
                "trophy": ps.trophy,
                "toss": ps.toss,
                "both_up": ps.both_up,
                "total": ps.total,
                "dom_wr_y": ps.dom_wrist_y,
                "nose_y": ps.nose_y,
                "shoulder_y": ps.shoulder_y,
            })

    # Also pull the serve_events that the detector persisted
    events = conn.execute(sql_text("""
        SELECT frame_idx, ts, pose_score, has_ball_toss
        FROM ml_analysis.serve_events
        WHERE task_id = :tid AND player_id = :pid
        ORDER BY ts
    """), {"tid": task_id, "pid": player_id}).mappings().all()

    print(f"\n=== FUNNEL: player_id={player_id} ({zone_name} baseline) ===")
    print(f"  total rows for pid={player_id}:                  {total}")
    print(f"    with keypoints JSON:                         {with_kps}  "
          f"({100*with_kps/max(1,total):.1f}% of total)")
    print(f"      in baseline zone (court_y {court_y_lo}-{court_y_hi}):  {in_zone}")
    print(f"        keypoints parsed ok:                     {n_parsed}")
    print(f"          usable pose (dom_wr + shoulder valid): {n_usable}")
    print(f"            trophy==True (dom_wr above nose):   {n_trophy}")
    print(f"            toss==True   (pas_wr above pas_sh): {n_toss}")
    print(f"            both_up==True (both wr > shoulder): {n_bothup}")
    print(f"          full score==3 (trophy+toss+both_up):  {n_score3}")
    print(f"  serve_events persisted for this player:         {len(events)}")

    if sample_rows:
        print(f"  Sample usable rows:")
        for s in sample_rows:
            print(f"    frame={s['frame']:>5} court_y={s['court_y']:.1f}  "
                  f"trophy={int(s['trophy'])} toss={int(s['toss'])} "
                  f"both_up={int(s['both_up'])} total={s['total']}  "
                  f"(dom_wr_y={s['dom_wr_y']:.0f} nose_y={s['nose_y']:.0f} "
                  f"shoulder_y={s['shoulder_y']:.0f})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--left-handed", action="store_true",
                    help="Is the player left-handed? (affects dom_wrist choice)")
    args = ap.parse_args(argv)

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2
    engine = create_engine(_normalize_db_url(db_url))

    with engine.connect() as conn:
        print(f"=== serve_chain_audit task={args.task} "
              f"is_left_handed={args.left_handed} ===")
        print(f"  MIN_KP_CONF = {MIN_KP_CONF}")
        _funnel_for_player(conn, args.task, player_id=0, zone_name="near",
                           court_y_lo=18.5, court_y_hi=28.0,
                           is_left_handed=args.left_handed)
        _funnel_for_player(conn, args.task, player_id=1, zone_name="far",
                           court_y_lo=-3.5, court_y_hi=4.5,
                           is_left_handed=args.left_handed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
