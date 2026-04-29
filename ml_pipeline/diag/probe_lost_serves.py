"""Multi-timestamp diagnostic: why did serve_detector miss these SA serves?

Replays the detector's pose-first gates on the merged pose rows for each
given (task, ts) pair and emits a single-line summary per ts. Use this
when reconcile_serves_strict shows a cluster of NO_MATCH / FAR_IN_TIME
verdicts and you need to know WHICH gate killed each one before deciding
on a fix.

Output columns:
  ts          target SA-GT timestamp
  pid         player_id probed (0=near, 1=far)
  bronze      pose-carrying bronze rows in window
  roi         pose-carrying ROI rows in window
  bz_zone     bronze rows passing _baseline_zone
  roi_zone    ROI rows passing _baseline_zone
  scored>=1   merged rows scoring trophy/toss/both_up>=1
  max_score   highest pose-signal total in the window
  n_clusters  cluster count (consecutive frames within cluster_max_gap_s)
  largest     size of the largest cluster
  cand        candidates emitted by find_serve_candidates (the actual gate)
  verdict     PASS / FAIL_<gate>

Usage (Render shell):
    python -m ml_pipeline.diag.probe_lost_serves \\
        --task a798eff0-551f-4b5a-838f-7933866a727c \\
        --ts 434.20,458.08,463.52,555.68,502.72,584.92

By default probes pid=1 (far). Pass --player 0 to probe near. For
cross-player misclassifications (FAR_IN_TIME / WEAK_TIME hitting pid=0
when SA says FAR), run BOTH 0 and 1 to see what each side saw.
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
from ml_pipeline.serve_detector.detector import (
    _baseline_zone,
    _load_pose_rows,
    _get_dominant_hand,
)


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _summary_for_ts(pose_rows, ts, win, fps, is_left_handed, player_id):
    lo_f = (ts - win) * fps
    hi_f = (ts + win) * fps
    window = [r for r in pose_rows if lo_f <= r["frame_idx"] <= hi_f]
    n_bronze = sum(1 for r in window
                   if r.get("_origin", "").startswith("bronze")
                   or "_origin" not in r)
    n_roi = sum(1 for r in window if r.get("_origin", "") == "roi")

    # Baseline-zone filter
    bz_kept = [r for r in window if _baseline_zone(r.get("court_y")) is not None]
    bz_zone_bronze = sum(1 for r in bz_kept
                         if r.get("_origin", "").startswith("bronze"))
    bz_zone_roi = sum(1 for r in bz_kept if r.get("_origin", "") == "roi")

    # Score each
    scored = []
    max_score = 0
    for r in bz_kept:
        s = score_pose_frame(r["keypoints"], is_left_handed)
        if s.usable and s.total >= 1:
            scored.append((r["frame_idx"], s.total))
        if s.usable:
            max_score = max(max_score, s.total)

    # Cluster on consecutive frames (gap ≤ 1.2s = 30 frames @ 25fps)
    gap_frames = max(1, int(round(fps * 1.2)))
    scored.sort(key=lambda x: x[0])
    clusters = []
    if scored:
        clusters = [[scored[0]]]
        for f, sc in scored[1:]:
            if f - clusters[-1][-1][0] <= gap_frames:
                clusters[-1].append((f, sc))
            else:
                clusters.append([(f, sc)])
    largest = max((len(c) for c in clusters), default=0)

    # Run actual cluster gate via find_serve_candidates on the windowed rows
    # (bz_kept is already windowed + baseline-zone-filtered above)
    candidates = find_serve_candidates(
        bz_kept, fps=fps, player_id=player_id,
        is_left_handed=is_left_handed,
    )

    if candidates:
        verdict = "PASS"
    elif not bz_kept:
        verdict = "FAIL_no_baseline_rows"
    elif not scored:
        verdict = "FAIL_no_score>=1"
    elif largest < (3 if player_id == 1 else 4):
        verdict = "FAIL_cluster_size"
    elif max_score < 1:
        verdict = "FAIL_peak_score"
    else:
        verdict = "FAIL_other_gate"

    return {
        "ts": ts,
        "pid": player_id,
        "bronze": n_bronze,
        "roi": n_roi,
        "bz_zone": bz_zone_bronze,
        "roi_zone": bz_zone_roi,
        "scored>=1": len(scored),
        "max_score": max_score,
        "n_clusters": len(clusters),
        "largest": largest,
        "cand": len(candidates),
        "verdict": verdict,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--ts", required=True,
                    help="Comma-separated list of target ts (seconds)")
    ap.add_argument("--win", type=float, default=2.0,
                    help="± window around each ts (default 2.0s)")
    ap.add_argument("--player", type=int, default=1,
                    help="player_id (0=near, 1=far; default 1)")
    args = ap.parse_args(argv)

    ts_list = [float(t.strip()) for t in args.ts.split(",") if t.strip()]

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2
    engine = create_engine(_normalize_db_url(db_url))

    with engine.connect() as conn:
        fps = conn.execute(sql_text(
            "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
            "WHERE job_id = :t"
        ), {"t": args.task}).scalar() or 25.0
        is_left_handed = _get_dominant_hand(conn, args.task)
        pose_rows = _load_pose_rows(conn, args.task, args.player,
                                    is_left_handed=is_left_handed)

    print(f"=== probe_lost_serves task={args.task[:8]} pid={args.player} "
          f"win=±{args.win}s ts={ts_list} ===")
    print(f"  fps={fps:.2f} left_handed={is_left_handed} "
          f"merged_pose_rows={len(pose_rows)}")
    print()

    hdr = (f"  {'ts':>7} {'bronze':>6} {'roi':>4} "
           f"{'bz_zn':>5} {'roi_zn':>6} "
           f"{'scr>=1':>6} {'max':>3} {'#cl':>3} {'lrg':>3} {'cand':>4} "
           f"verdict")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for ts in ts_list:
        row = _summary_for_ts(
            pose_rows, ts, args.win, fps, is_left_handed, args.player,
        )
        print(f"  {row['ts']:>7.2f} {row['bronze']:>6} {row['roi']:>4} "
              f"{row['bz_zone']:>5} {row['roi_zone']:>6} "
              f"{row['scored>=1']:>6} {row['max_score']:>3} "
              f"{row['n_clusters']:>3} {row['largest']:>3} {row['cand']:>4} "
              f"{row['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
