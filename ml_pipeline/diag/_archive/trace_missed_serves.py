"""Trace WHY the pose-first serve detector missed specific SportAI serve
timestamps. Runs find_serve_candidates on pid=0 pose rows in a ±window
around each target timestamp and reports cluster structure + peak scores
+ ball/rally context, so we can see which gate (if any) is rejecting it.

Usage (Render shell):
    python -m ml_pipeline.diag.trace_missed_serves \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088 \\
        --targets 120.28,148.52,178.44
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.serve_detector.pose_signal import (
    score_pose_frame,
    find_serve_candidates,
)


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _fetch_pose_rows(conn, task_id, player_id, ts_lo, ts_hi, fps):
    rows = conn.execute(sql_text("""
        SELECT frame_idx, keypoints, court_x, court_y,
               bbox_x1, bbox_y1, bbox_x2, bbox_y2
        FROM ml_analysis.player_detections
        WHERE job_id = :tid
          AND player_id = :pid
          AND keypoints IS NOT NULL
          AND frame_idx BETWEEN :lo AND :hi
        ORDER BY frame_idx
    """), {
        "tid": task_id,
        "pid": player_id,
        "lo": int(ts_lo * fps),
        "hi": int(ts_hi * fps),
    }).mappings().all()
    return [
        {
            "frame_idx": r["frame_idx"],
            "keypoints": r["keypoints"],
            "court_x": r["court_x"],
            "court_y": r["court_y"],
            "bbox": (r["bbox_x1"], r["bbox_y1"], r["bbox_x2"], r["bbox_y2"]),
        }
        for r in rows
    ]


def _fetch_ball_bounces(conn, task_id, ts_lo, ts_hi, fps):
    rows = conn.execute(sql_text("""
        SELECT frame_idx, court_x, court_y
        FROM ml_analysis.ball_detections
        WHERE job_id = :tid
          AND is_bounce = TRUE
          AND frame_idx BETWEEN :lo AND :hi
        ORDER BY frame_idx
    """), {
        "tid": task_id,
        "lo": int(ts_lo * fps),
        "hi": int(ts_hi * fps),
    }).fetchall()
    return [(r.frame_idx / fps, r.court_x, r.court_y) for r in rows]


def _fetch_existing_serves(conn, task_id, ts_lo, ts_hi):
    rows = conn.execute(sql_text("""
        SELECT ts, player_id, source, confidence, pose_score
        FROM ml_analysis.serve_events
        WHERE task_id = CAST(:tid AS uuid)
          AND ts BETWEEN :lo AND :hi
        ORDER BY ts
    """), {"tid": task_id, "lo": ts_lo, "hi": ts_hi}).fetchall()
    return [
        (float(r.ts), int(r.player_id), r.source, float(r.confidence),
         float(r.pose_score) if r.pose_score is not None else None)
        for r in rows
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--targets", required=True,
                    help="Comma-separated SA serve timestamps to investigate")
    ap.add_argument("--window", type=float, default=3.0,
                    help="Seconds ± each target to fetch pose rows")
    ap.add_argument("--player-id", type=int, default=0,
                    help="player_id to trace (default 0 = near)")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--left-handed", action="store_true")
    args = ap.parse_args(argv)

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2

    engine = create_engine(_normalize_db_url(db_url))
    targets = [float(x.strip()) for x in args.targets.split(",")]

    with engine.connect() as conn:
        for tgt in targets:
            print(f"\n{'='*80}")
            print(f"TARGET SA serve ts = {tgt:.2f}s  (pid={args.player_id}, ±{args.window}s)")
            print(f"{'='*80}")

            ts_lo = tgt - args.window
            ts_hi = tgt + args.window
            pose_rows = _fetch_pose_rows(
                conn, args.task, args.player_id, ts_lo, ts_hi, args.fps,
            )
            bounces = _fetch_ball_bounces(conn, args.task, ts_lo, ts_hi, args.fps)
            existing = _fetch_existing_serves(conn, args.task, ts_lo, ts_hi)

            print(f"  pid={args.player_id} pose rows in window: {len(pose_rows)}")
            print(f"  ball bounces in window:         {len(bounces)}")
            for ts, bx, by in bounces:
                print(f"    bounce ts={ts:.2f}  court=({bx},{by})")
            print(f"  serve_events already in window: {len(existing)}")
            for ts, pid, src, conf, psc in existing:
                print(f"    serve_event ts={ts:.2f} pid={pid} source={src} "
                      f"conf={conf:.2f} pose_score={psc}")

            # Score every pose row, show usable counts
            n_usable = 0
            n_score_1 = 0
            n_score_2 = 0
            n_score_3 = 0
            n_trophy = 0
            for row in pose_rows:
                s = score_pose_frame(row["keypoints"], args.left_handed)
                if s.usable:
                    n_usable += 1
                    if s.total >= 1: n_score_1 += 1
                    if s.total >= 2: n_score_2 += 1
                    if s.total >= 3: n_score_3 += 1
                    if s.trophy: n_trophy += 1

            print(f"\n  Pose scoring breakdown of {len(pose_rows)} rows:")
            print(f"    usable:        {n_usable}")
            print(f"    score >= 1:    {n_score_1}")
            print(f"    score >= 2:    {n_score_2}")
            print(f"    score >= 3:    {n_score_3}")
            print(f"    trophy True:   {n_trophy}")

            # Run find_serve_candidates — shows which clusters survive
            candidates = find_serve_candidates(
                pose_rows=pose_rows,
                player_id=args.player_id,
                is_left_handed=args.left_handed,
                fps=args.fps,
            )
            print(f"\n  find_serve_candidates returned {len(candidates)} candidates:")
            for c in candidates:
                dt = abs(c.ts - tgt)
                dt_flag = "  ← ~MATCH" if dt < 1.0 else ""
                print(f"    ts={c.ts:.2f} peak_score={c.peak_score} "
                      f"cluster_size={c.cluster_size} conf={c.confidence:.2f} "
                      f"(dt={dt:.2f}s){dt_flag}")
                if c.court_y is not None:
                    print(f"      court=({c.court_x:.1f},{c.court_y:.1f})")

            # Interpret
            if not candidates:
                if n_score_3 == 0 and n_score_2 == 0:
                    print(f"\n  VERDICT: no trophy poses found in window — pose scoring")
                    print(f"  doesn't fire at all. Keypoint confidence or geometry")
                    print(f"  is insufficient. Check video frame for occlusion.")
                elif n_score_2 == 0:
                    print(f"\n  VERDICT: only score==1 frames. Cluster-peak requirement")
                    print(f"  (min_cluster_peak=1) should still pass but maybe cluster")
                    print(f"  size < 4 frames. Loosen min_cluster_size.")
                else:
                    print(f"\n  VERDICT: score>=2 frames exist but clusters didn't survive")
                    print(f"  find_serve_candidates. Likely cluster_size or min_serve_interval.")
            else:
                close = [c for c in candidates if abs(c.ts - tgt) < 1.0]
                if close:
                    print(f"\n  VERDICT: pose detected a candidate at dt<1s.")
                    print(f"  If no serve_event was stored, the downstream gate")
                    print(f"  (rally-state IN_RALLY + peak_score<3, or cooldown) rejected it.")
                else:
                    print(f"\n  VERDICT: candidates exist but none within 1s of the SA serve.")
                    print(f"  Possibly peak picking selected wrong frame, or ts alignment off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
