"""Probe a single serve-time window — why doesn't find_serve_candidates fire?

Replays the detector's pose-first pipeline on pose rows around one SA
ground-truth serve timestamp, with instrumentation at every gate:

  1. baseline-zone filter  (_baseline_zone on court_y)
  2. score_pose_frame      (per-frame trophy/toss/both_up)
  3. min_peak_score filter (score >= 1)
  4. clustering by frame gap (cluster_max_gap_s)
  5. min_cluster_size gate
  6. min_cluster_peak gate
  7. arm-extension gate
  8. min_serve_interval temporal dedup

Prints per-frame rows plus cluster-level verdict. Use this when the
serve-count diag shows strong signals for a ts but 0 candidates emerge.

Usage (Render shell):
    python -m ml_pipeline.diag.probe_serve_window \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088 \\
        --ts 463.52 --win 3.0 --player 1
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.serve_detector.pose_signal import (
    score_pose_frame,
    find_serve_candidates,
    parse_keypoints,
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--ts", type=float, required=True,
                    help="Target SA ground-truth ts (seconds)")
    ap.add_argument("--win", type=float, default=3.0,
                    help="± window around --ts (seconds, default 3.0)")
    ap.add_argument("--player", type=int, default=1,
                    help="player_id (0=near, 1=far; default 1)")
    args = ap.parse_args(argv)

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
        pose_rows = _load_pose_rows(conn, args.task, args.player)

    print(f"=== probe_serve_window ===")
    print(f"  task:          {args.task}")
    print(f"  target ts:     {args.ts}s (±{args.win}s)")
    print(f"  player_id:     {args.player} ({'far' if args.player == 1 else 'near'})")
    print(f"  left-handed:   {is_left_handed}")
    print(f"  fps:           {fps:.3f}")
    print(f"  total pose rows (player): {len(pose_rows)}")
    print()

    lo_f = (args.ts - args.win) * fps
    hi_f = (args.ts + args.win) * fps
    window_rows = [r for r in pose_rows
                   if lo_f <= r["frame_idx"] <= hi_f]
    window_rows.sort(key=lambda r: r["frame_idx"])
    print(f"  rows in [{args.ts - args.win:.2f}s, {args.ts + args.win:.2f}s]: "
          f"{len(window_rows)}")
    print()

    # Gate 1: baseline zone
    baseline_kept = []
    baseline_dropped = []
    for r in window_rows:
        z = _baseline_zone(r.get("court_y"))
        if z is None:
            baseline_dropped.append(r)
        else:
            baseline_kept.append(r)
    print(f"  baseline-zone filter:  kept {len(baseline_kept)}, "
          f"dropped {len(baseline_dropped)}")
    if baseline_dropped[:3]:
        print(f"    sample dropped court_y: "
              f"{[round(float(r.get('court_y') or 0), 2) for r in baseline_dropped[:5]]}")
    print()

    # Gate 2-3: per-frame scoring + min_peak_score=1 filter
    print(f"  {'frame':>6} {'ts':>7} {'cy':>6} | "
          f"{'usable':>6} {'trop':>4} {'toss':>4} {'bup':>4} {'tot':>3} | "
          f"{'dom_y':>6} {'pas_y':>6} {'sh_y':>5} {'arm':>5}")
    print("  " + "-" * 95)
    scored_keep = []  # passes min_peak_score
    n_usable = 0
    n_score_ge1 = 0
    n_score_eq3 = 0
    for r in baseline_kept:
        s = score_pose_frame(r["keypoints"], is_left_handed)
        ts = r["frame_idx"] / fps
        cy = r.get("court_y")
        cy_s = f"{cy:.2f}" if cy is not None else "None"
        if s.usable:
            n_usable += 1
            arm = s.shoulder_y - s.dom_wrist_y
            print(f"  {r['frame_idx']:>6} {ts:>7.2f} {cy_s:>6} | "
                  f"{'Y':>6} {int(s.trophy):>4} {int(s.toss):>4} "
                  f"{int(s.both_up):>4} {s.total:>3} | "
                  f"{s.dom_wrist_y:>6.1f} {s.pas_wrist_y:>6.1f} "
                  f"{s.shoulder_y:>5.1f} {arm:>5.1f}")
            if s.total >= 1:
                n_score_ge1 += 1
                scored_keep.append((r, s))
            if s.total == 3:
                n_score_eq3 += 1
        else:
            print(f"  {r['frame_idx']:>6} {ts:>7.2f} {cy_s:>6} | "
                  f"{'N':>6} {'-':>4} {'-':>4} {'-':>4} {'-':>3} | "
                  f"{'-':>6} {'-':>6} {'-':>5} {'-':>5}")

    print()
    print(f"  scored usable: {n_usable}")
    print(f"  score >= 1:    {n_score_ge1}")
    print(f"  score == 3:    {n_score_eq3}")
    print()

    if not scored_keep:
        print("  VERDICT: 0 frames passed score>=1 filter -> no cluster possible")
        return 0

    # Gate 4: clustering
    cluster_max_gap_s = 1.2
    gap_frames = max(1, int(round(fps * cluster_max_gap_s)))
    clusters = [[scored_keep[0]]]
    for row, score in scored_keep[1:]:
        prev_row = clusters[-1][-1][0]
        if row["frame_idx"] - prev_row["frame_idx"] <= gap_frames:
            clusters[-1].append((row, score))
        else:
            clusters.append([(row, score)])

    print(f"  clustering (gap_frames={gap_frames}, i.e. {cluster_max_gap_s}s @ {fps:.1f}fps):")
    print(f"  {len(clusters)} cluster(s) formed")
    for i, c in enumerate(clusters):
        first = c[0][0]["frame_idx"]
        last = c[-1][0]["frame_idx"]
        peaks = [s.total for _, s in c]
        print(f"    cluster {i}: frames {first}..{last} "
              f"(span {last - first} frames, {(last-first)/fps:.2f}s), "
              f"size={len(c)}, peak_scores={peaks}")
    print()

    # Gates 5-7: size, peak, arm extension
    min_cluster_size = 4 if args.player == 0 else 3
    min_arm_extension_px = 30.0 if args.player == 0 else 5.0
    min_cluster_peak = 1
    print(f"  cluster-level gates (player={args.player}): "
          f"min_size={min_cluster_size}, min_peak={min_cluster_peak}, "
          f"min_arm_ext_px={min_arm_extension_px}")

    for i, c in enumerate(clusters):
        verdict = "PASS"
        reasons = []
        if len(c) < min_cluster_size:
            reasons.append(f"size {len(c)} < {min_cluster_size}")
            verdict = "DROP"
        max_score = max(s.total for _, s in c)
        if max_score < min_cluster_peak:
            reasons.append(f"peak {max_score} < {min_cluster_peak}")
            verdict = "DROP"
        peak_row, peak_score = min(c, key=lambda x: x[1].dom_wrist_y)
        arm_ext = peak_score.shoulder_y - peak_score.dom_wrist_y
        if arm_ext < min_arm_extension_px:
            reasons.append(f"arm_ext {arm_ext:.1f} < {min_arm_extension_px}")
            verdict = "DROP"
        peak_ts = peak_row["frame_idx"] / fps
        print(f"    cluster {i}: {verdict}  "
              f"(size={len(c)}, peak_score={max_score}, arm_ext={arm_ext:.1f}, "
              f"peak frame={peak_row['frame_idx']} @ ts={peak_ts:.2f}) "
              f"{'-> ' + '; '.join(reasons) if reasons else ''}")

    print()
    # Gate 8: call the real function to cross-check
    cands = find_serve_candidates(
        pose_rows=baseline_kept,
        player_id=args.player,
        is_left_handed=is_left_handed,
        fps=fps,
    )
    print(f"  find_serve_candidates returned: {len(cands)}")
    for c in cands:
        print(f"    ts={c.ts:.2f} score={c.peak_score} size={c.cluster_size} "
              f"arm_y={c.dom_wrist_y_peak:.1f} conf={c.confidence:.2f}")

    print()
    print("=== SUMMARY ===")
    if not cands:
        print(f"  NO candidate for target ts {args.ts}. See cluster table above.")
    else:
        closest = min(cands, key=lambda c: abs(c.ts - args.ts))
        dt = abs(closest.ts - args.ts)
        tag = "MATCH" if dt <= 0.5 else ("WEAK" if dt <= 1.0 else "FAR")
        print(f"  closest candidate to target {args.ts}: "
              f"ts={closest.ts:.2f} (dt={dt:.2f}s, {tag})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
