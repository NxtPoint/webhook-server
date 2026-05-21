"""Pre-flight diagnostic for extract_far_bounces anchor strategies.

Replicates the Step A SQL diagnostic against a local bench fixture, then
projects the anchor set through the production clustering + windowing logic
to predict how many SA serves each strategy would cover. Pure-Python, no
TrackNet, no DB — runs in seconds. Used 2026-05-21 to discover that the
kickoff doc's option-(c) zone-filtered-all-detections default covered only
1/24 SA serves on the 880dff02 fixture; led to the change in defaults
(`anchor_zone_filter=False, anchor_bounce_only=True`).

Reuse this script whenever:
  - The anchor strategy default is being tuned again
  - A new fixture lands and we want to predict how 5a will cover its serves
  - We're debating window_s / cluster_gap_s on a specific fixture

Usage:
    .venv/Scripts/python -m ml_pipeline.diag.probe_roi_anchor_strategy \\
        ml_pipeline/fixtures/880dff02.pkl.gz

Output format mirrors the 2026-05-21 finding in
.claude/session_2026-05-21_phase5a_pivot.md.
"""
from __future__ import annotations

import argparse
import gzip
import pickle
import sys


# Court geometry — kept in sync with ml_pipeline/roi_extractors/bounces.py
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = 23.77 / 2.0
FAR_SERVICE_LINE_M = HALF_Y - 6.40
NEAR_SERVICE_LINE_M = HALF_Y + 6.40
SB_MARGIN = 1.5


def _in_zone(cx, cy):
    if cx is None or cy is None:
        return False
    if not (-SB_MARGIN <= cx <= COURT_WIDTH_DOUBLES_M + SB_MARGIN):
        return False
    return FAR_SERVICE_LINE_M - SB_MARGIN <= cy <= NEAR_SERVICE_LINE_M + SB_MARGIN


def _cluster_and_window(anchors, fps, gap_s, window_s):
    if not anchors:
        return [], []
    anchors = sorted(anchors)
    gap_f = max(1, int(round(gap_s * fps)))
    clusters = [[anchors[0]]]
    for a in anchors[1:]:
        if a - clusters[-1][-1] <= gap_f:
            clusters[-1].append(a)
        else:
            clusters.append([a])
    centroids = [int(round(sum(c) / len(c))) for c in clusters]
    half = max(1, int(round(window_s * fps)))
    merged = []
    for c in sorted(centroids):
        s, e = max(0, c - half), c + half + 1
        if merged and s <= merged[-1][1]:
            ps, pe, pc = merged[-1]
            merged[-1] = (ps, max(pe, e), pc)
        else:
            merged.append((s, e, c))
    return centroids, merged


def _serve_coverage(windows, sa_truth, fps):
    if not sa_truth:
        return 0, 0
    total = sum(1 for s in sa_truth if s.get("ts") is not None)
    covered = 0
    for s in sa_truth:
        ts = s.get("ts")
        if ts is None:
            continue
        sa_f = float(ts) * fps
        if any(start <= sa_f < end for start, end, _ in windows):
            covered += 1
    return covered, total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", help="Bench fixture (.pkl.gz)")
    ap.add_argument("--window-s", type=float, default=2.5)
    ap.add_argument("--cluster-gap-s", type=float, default=0.5)
    args = ap.parse_args(argv)

    with gzip.open(args.fixture, "rb") as f:
        fx = pickle.load(f)
    rows = fx["ball_rows"]
    fps = float(fx.get("fps") or 25.0)
    sa = fx.get("sa_truth", [])
    task = fx.get("task_id", "?")

    print(f"=== {task[:8]} (fps={fps}, window_s={args.window_s}s, "
          f"cluster_gap_s={args.cluster_gap_s}s) ===")
    print(f"  total ball_rows: {len(rows)}")
    print(f"  bounces:         {sum(1 for r in rows if r.get('is_bounce'))}")
    if rows:
        print(f"  match length:    {max(r['frame_idx'] for r in rows) / fps:.1f}s "
              f"({max(r['frame_idx'] for r in rows)} frames)")
    print(f"  SA serves:       {len(sa)}")
    print()

    # Step A SQL diagnostic equivalent (zone-filtered counts)
    in_zone = [r for r in rows if _in_zone(r.get("court_x"), r.get("court_y"))]
    in_zone_b = [r for r in in_zone if r.get("is_bounce")]
    print("=== Step A SQL diagnostic equivalent ===")
    print(f"  total (in service-box zone): {len(in_zone)}")
    print(f"  bounces (in zone):           {len(in_zone_b)}")
    if in_zone:
        first_f = min(r["frame_idx"] for r in in_zone)
        last_f = max(r["frame_idx"] for r in in_zone)
        buckets = set((r["frame_idx"] // 250) for r in in_zone)
        print(f"  first_frame: {first_f}, last_frame: {last_f}")
        print(f"  distinct 10s buckets: {len(buckets)}")
    print()

    # Strategy comparison
    strategies = [
        ("zone=T, bounce=F",
         sorted(r["frame_idx"] for r in rows
                if _in_zone(r.get("court_x"), r.get("court_y")))),
        ("zone=T, bounce=T",
         sorted(r["frame_idx"] for r in rows
                if r.get("is_bounce")
                and _in_zone(r.get("court_x"), r.get("court_y")))),
        ("zone=F, bounce=F",
         sorted(r["frame_idx"] for r in rows)),
        ("zone=F, bounce=T (DEFAULT)",
         sorted(r["frame_idx"] for r in rows if r.get("is_bounce"))),
    ]
    print(f"{'strategy':<28} {'anchors':>8} {'clusters':>9} {'windows':>8} "
          f"{'cov_s':>7} {'serves_covered':>16}")
    print("-" * 84)
    for name, anchors in strategies:
        c, w = _cluster_and_window(anchors, fps, args.cluster_gap_s, args.window_s)
        cov_s = sum(e - s for s, e, _ in w) / fps
        cov_n, cov_total = _serve_coverage(w, sa, fps)
        pct = f"{cov_n}/{cov_total} ({100*cov_n/max(1,cov_total):.0f}%)"
        print(f"{name:<28} {len(anchors):>8} {len(c):>9} {len(w):>8} "
              f"{cov_s:>7.1f} {pct:>16}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
