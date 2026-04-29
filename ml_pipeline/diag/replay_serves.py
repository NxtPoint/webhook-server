"""Replay the serve_detector against a snapshot fixture — no DB, no Render.

Loads `<fixture>.pkl.gz` (produced by snapshot_task) and runs the EXACT
prod pipeline (`detector._run_pipeline`) against it. Prints the equivalent
of `reconcile_serves_strict`: every SA truth serve paired with the closest
T5 detection, plus a verdict (MATCH / WEAK_TIME / SUSPECT_BOUNCE /
FAR_IN_TIME / NO_MATCH) and the near/far totals.

Use this for fast iteration: edit the detector → `python -m
ml_pipeline.diag.replay_serves ml_pipeline/fixtures/<task>.pkl.gz` →
sub-second turnaround.

Usage:
    python -m ml_pipeline.diag.replay_serves ml_pipeline/fixtures/a798eff0.pkl.gz
"""
from __future__ import annotations

import argparse
import gzip
import math
import pickle
import sys
from pathlib import Path

from ml_pipeline.serve_detector.detector import detect_serves_offline


def _load_fixture(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"fixture not found: {path}")
    with gzip.open(p, "rb") as f:
        return pickle.load(f)


def _classify(sa_ts: float, sa_role: str, sa_bx, sa_by,
              event) -> tuple[str, float, float | None]:
    """Match one SA truth against one T5 event. Mirrors reconcile_serves_strict.

    Returns (verdict, dt, bounce_dist_m).
    """
    if event is None:
        return ("NO_MATCH", float("inf"), None)

    raw = abs(sa_ts - event.ts)
    if event.source.startswith("pose"):
        shifted = abs(sa_ts - (event.ts + 0.5))
    elif event.source == "bounce_only":
        shifted = abs(sa_ts - (event.ts - 0.5))
    else:
        shifted = raw
    dt = min(raw, shifted)

    bounce_dist = None
    if (event.bounce_court_x is not None and sa_bx is not None
            and event.bounce_court_y is not None and sa_by is not None):
        bounce_dist = math.sqrt(
            (sa_bx - event.bounce_court_x) ** 2
            + (sa_by - event.bounce_court_y) ** 2
        )

    if dt > 1.0:
        verdict = "FAR_IN_TIME"
    elif dt > 0.5:
        verdict = "WEAK_TIME"
    elif bounce_dist is not None and bounce_dist > 4.0:
        verdict = "SUSPECT_BOUNCE"
    else:
        verdict = "MATCH"
    return (verdict, dt, bounce_dist)


def _pair(sa_truth: list, events: list, window: float = 2.0):
    """For each SA truth row, find the closest T5 event within ±window seconds."""
    pairs = []
    for sa in sa_truth:
        sa_ts = float(sa["ts"]) if sa["ts"] is not None else None
        if sa_ts is None:
            pairs.append((sa, None))
            continue
        # closest event within window
        best = None
        best_gap = window + 1
        for e in events:
            gap = abs(e.ts - sa_ts)
            if gap <= window and gap < best_gap:
                best = e
                best_gap = gap
        pairs.append((sa, best))
    return pairs


def replay(fixture: dict, *, window: float = 2.0) -> dict:
    """Run prod detector against the fixture and pair to SA truth.

    Returns a dict with: events_split, pairs, verdict_counts,
    near_match/total, far_match/total — same shape as reconcile_serves_strict.
    """
    near_evts, far_pose_evts, far_bounce_evts = detect_serves_offline(
        task_id=fixture["task_id"],
        pose_rows_near=fixture["pose_near"],
        pose_rows_far=fixture["pose_far"],
        ball_rows=fixture["ball_rows"],
        is_left_handed=fixture["is_left_handed"],
        fps=fixture["fps"],
        return_split=True,
    )
    all_evts = sorted(near_evts + far_pose_evts + far_bounce_evts,
                      key=lambda e: e.ts)
    pairs = _pair(fixture["sa_truth"], all_evts, window=window)

    verdict_counts: dict[str, int] = {}
    near_match = far_match = near_total = far_total = 0
    for sa, evt in pairs:
        v, dt, dist = _classify(
            float(sa["ts"]) if sa["ts"] is not None else 0.0,
            sa["role"], sa.get("bx"), sa.get("by"), evt,
        )
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
        if sa["role"] == "NEAR":
            near_total += 1
            if v == "MATCH":
                near_match += 1
        elif sa["role"] == "FAR":
            far_total += 1
            if v == "MATCH":
                far_match += 1

    return {
        "task_id": fixture["task_id"],
        "near_evts": near_evts,
        "far_pose_evts": far_pose_evts,
        "far_bounce_evts": far_bounce_evts,
        "all_evts": all_evts,
        "pairs": pairs,
        "verdict_counts": verdict_counts,
        "near_match": near_match,
        "near_total": near_total,
        "far_match": far_match,
        "far_total": far_total,
        "total": len(pairs),
    }


def _print_report(result: dict, *, show_pairs: bool = True) -> None:
    print(f"=== replay_serves task={result['task_id'][:8]} ===")
    print(f"  emitted: near_pose={len(result['near_evts'])}  "
          f"far_pose={len(result['far_pose_evts'])}  "
          f"far_bounce={len(result['far_bounce_evts'])}  "
          f"total={len(result['all_evts'])}")
    print()

    if show_pairs:
        print(f"{'SA ts':>7} {'role':>4} {'side':>5} {'SA hy':>6} | "
              f"{'T5 ts':>7} {'pid':>3} {'src':<14} "
              f"{'dt':>5} {'d_b':>5} | verdict")
        print("-" * 95)
        for sa, evt in result["pairs"]:
            sa_ts = sa["ts"]
            v, dt, dist = _classify(
                float(sa_ts) if sa_ts is not None else 0.0,
                sa["role"], sa.get("bx"), sa.get("by"), evt,
            )
            t5_ts = f"{evt.ts:.2f}" if evt else "-"
            t5_pid = str(evt.player_id) if evt else "-"
            t5_src = (evt.source[:14] if evt else "-")
            dt_s = f"{dt:.2f}" if dt != float("inf") else "-"
            dist_s = f"{dist:.1f}" if dist is not None else "-"
            print(f"{float(sa_ts):>7.2f} {sa['role']:>4} "
                  f"{(sa.get('side') or '-'):>5} {float(sa['hy']):>6.1f} | "
                  f"{t5_ts:>7} {t5_pid:>3} {t5_src:<14} "
                  f"{dt_s:>5} {dist_s:>5} | {v}")
        print()

    print("=== VERDICT BREAKDOWN ===")
    total = result["total"]
    for v, n in sorted(result["verdict_counts"].items(), key=lambda x: -x[1]):
        pct = 100 * n / max(1, total)
        print(f"  {v:<16} {n:>3} / {total}  ({pct:.0f}%)")
    print()
    print(f"  near MATCH: {result['near_match']}/{result['near_total']}")
    print(f"  far  MATCH: {result['far_match']}/{result['far_total']}")
    print(f"  total MATCH: {result['near_match'] + result['far_match']}/{total}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", help="Path to <task>.pkl.gz fixture")
    ap.add_argument("--window", type=float, default=2.0,
                    help="Pair window in seconds (default 2.0)")
    ap.add_argument("--no-pairs", action="store_true",
                    help="Skip per-serve pairing table; only show verdict summary")
    args = ap.parse_args(argv)

    fixture = _load_fixture(args.fixture)
    result = replay(fixture, window=args.window)
    _print_report(result, show_pairs=not args.no_pairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
