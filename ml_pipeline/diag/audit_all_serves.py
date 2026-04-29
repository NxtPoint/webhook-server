"""Per-serve audit: which gate kills which SA serve, on every fixture.

For every SA truth row in the fixture:
  1. Run the prod detector pipeline (offline replay).
  2. Pair the SA serve with the closest T5 event within ±2s on EITHER pid.
  3. Classify the outcome:
     - PASS         T5 fired on expected side, |dt|<=0.5s
     - WEAK_TIME    T5 fired on expected side but |dt| in 0.5-1.0s
     - WRONG_SIDE   T5 fired but on the OTHER pid
     - NO_MATCH     no T5 event within ±2s on any side
  4. For NO_MATCH and WRONG_SIDE, run the probe gates on the EXPECTED
     side: count window pose rows, baseline-zone survival, score>=1
     frames, max score, cluster size, and what `find_serve_candidates`
     emitted in isolation. This tells us EXACTLY which gate killed it
     OR whether no scorable trophy exists at all (the trainable wall).

Output is a per-serve matrix you can paste back. Once every NO_MATCH
shows "no scorable pose frames in window", we've hit the floor of what
gate tweaks can fix and the next move is more training data /
detection-time signal extraction.

Usage:
    python -m ml_pipeline.diag.audit_all_serves \\
        ml_pipeline/fixtures/a798eff0.pkl.gz
"""
from __future__ import annotations

import argparse
import gzip
import math
import pickle
import sys
from pathlib import Path

from ml_pipeline.serve_detector.pose_signal import (
    score_pose_frame,
    find_serve_candidates,
)
from ml_pipeline.serve_detector.detector import _baseline_zone

from ml_pipeline.diag.replay_serves import replay, _load_fixture


def _gate_probe(pose_rows: list, sa_ts: float, win: float, fps: float,
                is_left_handed: bool, player_id: int) -> dict:
    """Replay the gates on a windowed slice of pose rows for one player."""
    lo_f = (sa_ts - win) * fps
    hi_f = (sa_ts + win) * fps
    window = [r for r in pose_rows if lo_f <= r["frame_idx"] <= hi_f]
    if not window:
        return {"window_rows": 0, "bz_kept": 0, "scored>=1": 0, "max_score": 0,
                "largest_cluster": 0, "candidates": 0, "gate": "FAIL_no_window_rows"}

    bz_kept = [r for r in window if _baseline_zone(r.get("court_y")) is not None]
    if not bz_kept:
        return {"window_rows": len(window), "bz_kept": 0, "scored>=1": 0,
                "max_score": 0, "largest_cluster": 0, "candidates": 0,
                "gate": "FAIL_no_baseline_rows"}

    scored = []
    max_score = 0
    for r in bz_kept:
        s = score_pose_frame(r["keypoints"], is_left_handed)
        if s.usable:
            max_score = max(max_score, s.total)
            if s.total >= 1:
                scored.append((r["frame_idx"], s.total))

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

    cands = find_serve_candidates(
        bz_kept, fps=fps, player_id=player_id,
        is_left_handed=is_left_handed,
    )

    if cands:
        gate = "PASS_isolated"
    elif not scored:
        gate = "FAIL_no_score>=1"
    elif largest < (3 if player_id == 1 else 4):
        gate = "FAIL_cluster_size"
    elif max_score < 1:
        gate = "FAIL_peak_score"
    else:
        gate = "FAIL_other_gate"

    return {
        "window_rows": len(window),
        "bz_kept": len(bz_kept),
        "scored>=1": len(scored),
        "max_score": max_score,
        "largest_cluster": largest,
        "candidates": len(cands),
        "gate": gate,
    }


def audit(fixture: dict, *, window: float = 2.0) -> list:
    """Run the prod pipeline + per-serve gate probe. Returns one row per SA serve."""
    result = replay(fixture, window=window)
    fps = fixture["fps"]
    is_lh = fixture["is_left_handed"]

    rows = []
    for sa, evt in result["pairs"]:
        sa_ts = float(sa["ts"]) if sa["ts"] is not None else None
        role = sa["role"]
        expected_pid = 0 if role == "NEAR" else (1 if role == "FAR" else None)

        # Look on the OTHER pid too — for the wrong-side case
        other_evt = None
        if sa_ts is not None and expected_pid is not None:
            other_pid = 1 - expected_pid
            best_gap = window + 1
            for e in result["all_evts"]:
                if e.player_id != other_pid:
                    continue
                gap = abs(e.ts - sa_ts)
                if gap <= window and gap < best_gap:
                    other_evt = e
                    best_gap = gap

        # Determine the chosen "matching" event:
        #   - If detector fired on expected side within window → that wins.
        #   - Else if detector fired on OTHER side → that wins (WRONG_SIDE).
        #   - Else NO_MATCH.
        same_side_evt = None
        if evt is not None and evt.player_id == expected_pid:
            same_side_evt = evt
        elif evt is not None:
            # the closest overall is on wrong side; check if there's also
            # one on the expected side
            best_gap = window + 1
            for e in result["all_evts"]:
                if e.player_id != expected_pid:
                    continue
                gap = abs(e.ts - sa_ts)
                if gap <= window and gap < best_gap:
                    same_side_evt = e
                    best_gap = gap

        # Verdict
        if same_side_evt is not None:
            dt_raw = abs(sa_ts - same_side_evt.ts)
            if same_side_evt.source.startswith("pose"):
                dt = min(dt_raw, abs(sa_ts - (same_side_evt.ts + 0.5)))
            elif same_side_evt.source == "bounce_only":
                dt = min(dt_raw, abs(sa_ts - (same_side_evt.ts - 0.5)))
            else:
                dt = dt_raw
            verdict = "PASS" if dt <= 0.5 else "WEAK_TIME"
        elif other_evt is not None:
            verdict = "WRONG_SIDE"
            dt = abs(sa_ts - other_evt.ts) if sa_ts else None
        else:
            verdict = "NO_MATCH"
            dt = None

        # If not PASS, run gate probe on expected side to find the blocker
        gate = None
        if verdict != "PASS" and sa_ts is not None and expected_pid is not None:
            pose_rows = (fixture["pose_near"] if expected_pid == 0
                         else fixture["pose_far"])
            gate = _gate_probe(pose_rows, sa_ts, window, fps, is_lh, expected_pid)

        rows.append({
            "ts": sa_ts,
            "role": role,
            "side": sa.get("side"),
            "verdict": verdict,
            "dt": dt,
            "t5_event": same_side_evt or other_evt,
            "gate": gate,
        })
    return rows


def _print(rows: list, *, fixture_name: str = "") -> None:
    print(f"=== audit_all_serves  {fixture_name} ===")
    print()
    print(f"{'ts':>7} {'role':>4} {'side':>5} {'verdict':<11} "
          f"{'dt':>5} {'t5_pid':>6} {'src':<14} | "
          f"{'win':>4} {'bz':>4} {'scr':>4} {'mx':>3} {'lrg':>3} {'cnd':>3}  blocker")
    print("-" * 130)
    counts = {"PASS": 0, "WEAK_TIME": 0, "WRONG_SIDE": 0, "NO_MATCH": 0}
    fail_buckets: dict[str, int] = {}
    for r in rows:
        v = r["verdict"]
        counts[v] = counts.get(v, 0) + 1
        evt = r["t5_event"]
        gate = r["gate"] or {}
        ts = r["ts"]
        ts_s = f"{ts:.2f}" if ts is not None else "-"
        dt_s = f"{r['dt']:.2f}" if r['dt'] is not None else "-"
        t5_pid = str(evt.player_id) if evt else "-"
        t5_src = (evt.source[:14] if evt else "-")
        gate_str = gate.get("gate", "-") if gate else "-"
        if gate_str.startswith("FAIL"):
            fail_buckets[gate_str] = fail_buckets.get(gate_str, 0) + 1
        win_n = gate.get("window_rows", "-") if gate else "-"
        bz = gate.get("bz_kept", "-") if gate else "-"
        scr = gate.get("scored>=1", "-") if gate else "-"
        mx = gate.get("max_score", "-") if gate else "-"
        lrg = gate.get("largest_cluster", "-") if gate else "-"
        cnd = gate.get("candidates", "-") if gate else "-"
        print(f"{ts_s:>7} {r['role']:>4} "
              f"{(r.get('side') or '-'):>5} {v:<11} "
              f"{dt_s:>5} {t5_pid:>6} {t5_src:<14} | "
              f"{str(win_n):>4} {str(bz):>4} {str(scr):>4} {str(mx):>3} "
              f"{str(lrg):>3} {str(cnd):>3}  {gate_str}")
    print()
    print("=== SUMMARY ===")
    total = len(rows)
    for k in ("PASS", "WEAK_TIME", "WRONG_SIDE", "NO_MATCH"):
        n = counts.get(k, 0)
        print(f"  {k:<11} {n:>3} / {total}  ({100*n/max(1,total):.0f}%)")
    if fail_buckets:
        print()
        print("=== BLOCKER BUCKETS (NO_MATCH + WRONG_SIDE + WEAK_TIME) ===")
        for k, n in sorted(fail_buckets.items(), key=lambda x: -x[1]):
            print(f"  {k:<28} {n}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", help="Path to <task>.pkl.gz fixture")
    ap.add_argument("--window", type=float, default=2.0,
                    help="Pair window in seconds (default 2.0)")
    args = ap.parse_args(argv)

    fixture = _load_fixture(args.fixture)
    rows = audit(fixture, window=args.window)
    _print(rows, fixture_name=Path(args.fixture).stem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
