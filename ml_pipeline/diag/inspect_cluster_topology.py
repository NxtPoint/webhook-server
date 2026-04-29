"""Local-only probe: inspect cluster topology around a target timestamp.

For a fixture and ts, dump:
  1. Every score>=1 frame in pose_far within ±10s of ts (frame_idx, ts, score, dom_wrist_y)
  2. The clusters formed at gap_s=1.2 (current)
  3. The clusters formed at gap_s=0.6 (proposed tighter)
  4. For each cluster: its peak-pick result + size + is-it-near-target?

Tells us in one shot whether the wandering is a "cluster too big" problem
(merging adjacent score-1 noise) or a "peak-pick rank" problem (rank
function picks the wrong frame inside an OK-sized cluster).
"""
from __future__ import annotations

import argparse
import gzip
import pickle
import sys

from ml_pipeline.serve_detector.pose_signal import score_pose_frame, find_serve_candidates
from ml_pipeline.serve_detector.detector import _baseline_zone


def _form_clusters(scored, gap_frames):
    if not scored:
        return []
    clusters = [[scored[0]]]
    for row, score in scored[1:]:
        prev_row = clusters[-1][-1][0]
        if row["frame_idx"] - prev_row["frame_idx"] <= gap_frames:
            clusters[-1].append((row, score))
        else:
            clusters.append([(row, score)])
    return clusters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--ts", type=float, required=True, help="Target SA serve ts")
    ap.add_argument("--player", type=int, default=1)
    ap.add_argument("--win", type=float, default=10.0,
                    help="Inspection window (default 10s)")
    args = ap.parse_args()

    with gzip.open(args.fixture, "rb") as f:
        fixture = pickle.load(f)
    fps = fixture["fps"]
    is_lh = fixture["is_left_handed"]
    pose_rows = (fixture["pose_near"] if args.player == 0
                 else fixture["pose_far"])

    target_ts = args.ts
    target_frame = int(target_ts * fps)
    lo = (target_ts - args.win) * fps
    hi = (target_ts + args.win) * fps

    # Mirror prod: baseline-zone filter applied first
    baseline_rows = [r for r in pose_rows
                     if _baseline_zone(r.get("court_y")) is not None]
    in_window = [r for r in baseline_rows
                 if lo <= r["frame_idx"] <= hi]

    print(f"=== cluster topology  pid={args.player}  target_ts={target_ts}  "
          f"win=±{args.win}s  fps={fps:.2f} ===")
    print(f"  baseline_rows total: {len(baseline_rows)}")
    print(f"  in window:          {len(in_window)}")
    print()

    # Score every row; print score>=1 frames
    scored_rows = []
    for r in sorted(in_window, key=lambda x: x["frame_idx"]):
        s = score_pose_frame(r["keypoints"], is_lh)
        if not s.usable:
            continue
        if s.total >= 1:
            scored_rows.append((r, s))

    print(f"  score>=1 frames in window: {len(scored_rows)}")
    print()
    print("  frame_idx     ts    score   trophy  toss  both_up  dom_wrist_y  marker")
    print("  " + "-" * 80)
    for r, s in scored_rows:
        marker = ""
        f = r["frame_idx"]
        if abs(f - target_frame) <= int(0.2 * fps):
            marker = "  <-- TARGET"
        elif abs(f - target_frame) <= int(2.0 * fps):
            marker = "  (within ±2s)"
        print(f"  {f:>9}  {f/fps:>6.2f}  {s.total:>5}   "
              f"{int(s.trophy):>6}  {int(s.toss):>4}  {int(s.both_up):>7}  "
              f"{s.dom_wrist_y:>11.1f}  {marker}")

    # Form clusters at current gap (1.2s)
    print()
    print(f"  === clusters at gap=1.2s (current) ===")
    clusters_12 = _form_clusters([(r, s) for r, s in scored_rows],
                                 gap_frames=int(round(fps * 1.2)))
    for i, c in enumerate(clusters_12):
        first_f = c[0][0]["frame_idx"]
        last_f = c[-1][0]["frame_idx"]
        max_score = max(s.total for _, s in c)
        # Pick peak by score, tie-break on min dom_wrist_y
        def _rank(x):
            r, s = x
            return (-s.total, s.dom_wrist_y or 1e9)
        peak_row, peak_score = min(c, key=_rank)
        peak_f = peak_row["frame_idx"]
        peak_ts = peak_f / fps
        marker = "  <-- target inside" if first_f <= target_frame <= last_f else ""
        print(f"    cluster {i}: frames [{first_f}-{last_f}]  "
              f"({first_f/fps:.2f}-{last_f/fps:.2f}s)  "
              f"size={len(c)}  max_score={max_score}  "
              f"peak_f={peak_f}  peak_ts={peak_ts:.2f}{marker}")

    # Form clusters at gap=0.6s
    print()
    print(f"  === clusters at gap=0.6s (tighter proposal) ===")
    clusters_06 = _form_clusters([(r, s) for r, s in scored_rows],
                                 gap_frames=int(round(fps * 0.6)))
    for i, c in enumerate(clusters_06):
        first_f = c[0][0]["frame_idx"]
        last_f = c[-1][0]["frame_idx"]
        max_score = max(s.total for _, s in c)
        def _rank(x):
            r, s = x
            return (-s.total, s.dom_wrist_y or 1e9)
        peak_row, peak_score = min(c, key=_rank)
        peak_f = peak_row["frame_idx"]
        peak_ts = peak_f / fps
        marker = "  <-- target inside" if first_f <= target_frame <= last_f else ""
        print(f"    cluster {i}: frames [{first_f}-{last_f}]  "
              f"({first_f/fps:.2f}-{last_f/fps:.2f}s)  "
              f"size={len(c)}  max_score={max_score}  "
              f"peak_f={peak_f}  peak_ts={peak_ts:.2f}{marker}")

    # Run find_serve_candidates with both gaps and check what survives near target
    print()
    print("  === find_serve_candidates output ===")
    for gap in [1.2, 0.6, 0.4]:
        cands = find_serve_candidates(
            pose_rows=baseline_rows, player_id=args.player,
            is_left_handed=is_lh, fps=fps, cluster_max_gap_s=gap,
        )
        near_target = [c for c in cands if abs(c.ts - target_ts) <= 2.0]
        all_total = len(cands)
        print(f"    gap={gap}s  total_cands={all_total}  "
              f"within_±2s_of_target={len(near_target)}")
        for c in near_target:
            print(f"      ts={c.ts:.2f}  frame={c.frame_idx}  score={c.peak_score}  "
                  f"cluster_size={c.cluster_size}  conf={c.confidence:.2f}  "
                  f"dom_wrist_y={c.dom_wrist_y_peak:.1f}")


if __name__ == "__main__":
    main()
