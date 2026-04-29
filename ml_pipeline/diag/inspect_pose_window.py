"""Per-frame pose-keypoint profiler around a target timestamp.

Complements `inspect_cluster_topology` (which only shows score>=1 frames) by
dumping EVERY frame in the window with the raw keypoint geometry that drives
the `pose_signal.score_pose_frame` decision tree:

  - dom_wrist_y, dom_wrist_conf
  - pas_wrist_y, pas_wrist_conf
  - shoulder_y, shoulder_conf (max of L/R)
  - arm_ext_dom = shoulder_y - dom_wrist_y       (positive = wrist above shoulder)
  - arm_ext_pas = pas_shoulder_y - pas_wrist_y   (toss reference)
  - nose_y, nose_conf
  - score (trophy/toss/both_up/total)
  - court_x, court_y, bbox area
  - origin (which side of the bronze/ROI merge produced the row)

Use cases:
  1. Diagnose "FAIL_other_gate" — was the cluster killed by the
     min_arm_extension_px gate or somewhere else?
  2. Confirm or refute "no scorable trophy frames" claims (if every frame
     has trophy=0 AND arm_ext_dom <= 0, it's truly upstream).
  3. See whether nose detection is the issue (when nose is invalid, the
     trophy-test falls back to "30 px above shoulder" which is harder).
  4. Quantify the arm_ext distribution at the peak — small consistent gap
     means a threshold change might fix it; one-off zero frame surrounded
     by below-shoulder frames means it's noise, not signal.

Output:
  - Per-frame table (column-aligned, score-flagged)
  - Summary stats: arm_ext_dom percentiles, nose validity rate,
    dom_wrist_conf percentiles, score distribution
  - "Verdict" — one of:
      TRUE_DATA_GAP        no usable frame, wrist always below shoulder
      THRESHOLD_BORDERLINE peak arm_ext in 5-29 px range; gate change might recover
      RECOVERABLE_TROPHY   peak arm_ext >= 30 px somewhere — should already pass
      LOW_CONF_KEYPOINTS   wrist or shoulder conf consistently below MIN_KP_CONF

Usage:
    python -m ml_pipeline.diag.inspect_pose_window \\
        ml_pipeline/fixtures/a798eff0.pkl.gz \\
        --ts 148.52 --player 0 --win 5
"""
from __future__ import annotations

import argparse
import gzip
import pickle
import statistics
import sys
from typing import Optional

from ml_pipeline.serve_detector.pose_signal import (
    parse_keypoints,
    score_pose_frame,
    MIN_KP_CONF,
)
from ml_pipeline.serve_detector.detector import _baseline_zone


def _is_valid(p) -> bool:
    return p is not None and p[2] >= MIN_KP_CONF and not (p[0] == 0.0 and p[1] == 0.0)


def _profile_frame(row: dict, is_left_handed: bool) -> dict:
    """Extract every measurement that score_pose_frame uses, plus diagnostics."""
    kp = parse_keypoints(row.get("keypoints"))
    score = score_pose_frame(row.get("keypoints"), is_left_handed)

    out = {
        "frame_idx": row["frame_idx"],
        "court_y": row.get("court_y"),
        "court_x": row.get("court_x"),
        "score_total": score.total,
        "trophy": int(score.trophy),
        "toss": int(score.toss),
        "both_up": int(score.both_up),
        "usable": score.usable,
    }
    if kp is None:
        out.update({"kp_parsed": False})
        return out

    nose = kp[0]
    l_sh = kp[5]
    r_sh = kp[6]
    l_wr = kp[9]
    r_wr = kp[10]
    dom_wr = l_wr if is_left_handed else r_wr
    pas_wr = r_wr if is_left_handed else l_wr
    dom_sh = l_sh if is_left_handed else r_sh
    pas_sh = r_sh if is_left_handed else l_sh

    nose_valid = _is_valid(nose)
    l_sh_ok = _is_valid(l_sh)
    r_sh_ok = _is_valid(r_sh)
    dom_wr_ok = _is_valid(dom_wr)
    pas_wr_ok = _is_valid(pas_wr)

    if l_sh_ok and r_sh_ok:
        shoulder_y = (l_sh[1] + r_sh[1]) / 2
        shoulder_conf = max(l_sh[2], r_sh[2])
    elif l_sh_ok:
        shoulder_y = l_sh[1]
        shoulder_conf = l_sh[2]
    elif r_sh_ok:
        shoulder_y = r_sh[1]
        shoulder_conf = r_sh[2]
    else:
        shoulder_y = None
        shoulder_conf = 0.0

    arm_ext_dom = (shoulder_y - dom_wr[1]) if (shoulder_y is not None and dom_wr_ok) else None
    pas_sh_y = pas_sh[1] if _is_valid(pas_sh) else None
    arm_ext_pas = (pas_sh_y - pas_wr[1]) if (pas_sh_y is not None and pas_wr_ok) else None

    bbox = row.get("bbox")
    bbox_area = None
    if bbox is not None and len(bbox) == 4 and all(v is not None for v in bbox):
        bbox_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])

    out.update({
        "kp_parsed": True,
        "nose_y": nose[1] if nose_valid else None,
        "nose_conf": nose[2],
        "nose_valid": nose_valid,
        "shoulder_y": shoulder_y,
        "shoulder_conf": shoulder_conf,
        "dom_wrist_y": dom_wr[1] if dom_wr_ok else None,
        "dom_wrist_conf": dom_wr[2],
        "dom_wrist_valid": dom_wr_ok,
        "pas_wrist_y": pas_wr[1] if pas_wr_ok else None,
        "pas_wrist_conf": pas_wr[2],
        "pas_wrist_valid": pas_wr_ok,
        "arm_ext_dom": arm_ext_dom,
        "arm_ext_pas": arm_ext_pas,
        "bbox_area": bbox_area,
        "in_baseline_zone": _baseline_zone(row.get("court_y")) is not None,
    })
    return out


def _percentiles(values: list, ps=(5, 25, 50, 75, 95)) -> dict:
    if not values:
        return {f"p{p}": None for p in ps}
    s = sorted(values)
    n = len(s)
    return {f"p{p}": s[min(n - 1, int(round(p / 100 * (n - 1))))] for p in ps}


def _verdict(profiles: list, target_pid: int, target_frame: float, fps: float) -> str:
    """Classify the window into one of 5 buckets.

    Restricts the analysis to frames within ±2s of the target (the pair window
    used by reconcile / audit). A high arm_ext frame 3+s away from target is a
    DIFFERENT serve, not evidence that this one is recoverable.
    """
    near_target = [p for p in profiles
                   if abs(p["frame_idx"] - target_frame) <= 2.0 * fps]
    if not near_target:
        return "TRUE_DATA_GAP — zero pose rows within ±2s of target"

    bz_near = [p for p in near_target if p.get("in_baseline_zone")]
    if not bz_near:
        return ("TRUE_DATA_GAP — pose rows exist within ±2s of target but NONE "
                "in baseline zone (court_y not populated or out of range)")

    cluster = [p for p in bz_near
               if p.get("kp_parsed") and p.get("usable")
               and p.get("score_total", 0) >= 1]
    if not cluster:
        return ("TRUE_DATA_GAP — baseline-zone rows exist within ±2s of target "
                "but ZERO score>=1 frames (no serve-pose signal at all)")

    arm_exts = [p["arm_ext_dom"] for p in cluster
                if p.get("arm_ext_dom") is not None]
    if not arm_exts:
        return "LOW_CONF_KEYPOINTS — score>=1 frames exist but arm_ext_dom uncomputable (wrist/shoulder conf below threshold)"

    peak = max(arm_exts)
    threshold = 30.0 if target_pid == 0 else 2.5

    if peak >= threshold:
        return (f"RECOVERABLE_TROPHY — peak arm_ext={peak:.1f}px >= "
                f"threshold {threshold} within ±2s of target; should already pass — "
                f"investigate why find_serve_candidates dropped it")
    if peak >= 5.0:
        return (f"THRESHOLD_BORDERLINE — peak arm_ext={peak:.1f}px in [5, {threshold}) within ±2s of target; "
                f"a gate-change MIGHT recover it but needs FP-risk assessment")
    return (f"TRUE_DATA_GAP — peak arm_ext={peak:.1f}px below threshold {threshold} "
            f"and below recoverable floor 5px within ±2s of target; upstream pose layer needed")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--ts", type=float, required=True, help="Target SA serve ts")
    ap.add_argument("--player", type=int, default=0, help="0=near, 1=far")
    ap.add_argument("--win", type=float, default=5.0,
                    help="Half-width seconds (default ±5)")
    ap.add_argument("--all-frames", action="store_true",
                    help="Print every frame, not just kp_parsed=True")
    args = ap.parse_args(argv)

    with gzip.open(args.fixture, "rb") as f:
        fixture = pickle.load(f)
    fps = fixture["fps"]
    is_lh = fixture["is_left_handed"]
    pose_rows = (fixture["pose_near"] if args.player == 0
                 else fixture["pose_far"])

    target_frame = args.ts * fps
    lo_f = (args.ts - args.win) * fps
    hi_f = (args.ts + args.win) * fps
    in_window = sorted(
        (r for r in pose_rows if lo_f <= r["frame_idx"] <= hi_f),
        key=lambda r: r["frame_idx"],
    )

    print(f"=== inspect_pose_window  pid={args.player}  target_ts={args.ts}  "
          f"win=±{args.win}s  fps={fps:.2f}  left_handed={is_lh} ===")
    print(f"  fixture pose_rows total: {len(pose_rows)}")
    print(f"  rows in window: {len(in_window)}")
    print()

    profiles = [_profile_frame(r, is_lh) for r in in_window]

    # Per-frame table
    print("  frame   ts     bz  score t/o/b  dom_wY domC  pas_wY pasC  shY shC  noseY noseC arm_dom arm_pas bbox    cy")
    print("  " + "-" * 117)
    for p in profiles:
        if not args.all_frames and not p.get("kp_parsed"):
            continue
        f = p["frame_idx"]
        ts_s = f / fps
        bz = "Y" if p.get("in_baseline_zone") else "."
        score = p.get("score_total", 0)
        t = p.get("trophy", 0)
        o = p.get("toss", 0)
        b = p.get("both_up", 0)
        marker = ""
        if abs(f - target_frame) <= 0.2 * fps:
            marker = "  <-- TARGET"
        elif abs(f - target_frame) <= 2.0 * fps:
            marker = "  *"

        def fmt(v, fmt_s=":>6.1f"):
            if v is None:
                return f"{'-':>6}"
            return f"{v:{fmt_s[1:]}}"

        def fmtc(v, fmt_s=":>4.2f"):
            if v is None:
                return f"{'-':>4}"
            return f"{v:{fmt_s[1:]}}"

        def fmtarm(v):
            if v is None:
                return f"{'-':>7}"
            return f"{v:>7.1f}"

        if p.get("kp_parsed"):
            print(f"  {f:>6}  {ts_s:>5.2f}  {bz:>2}    {score}  {t}/{o}/{b}  "
                  f"{fmt(p.get('dom_wrist_y'))} {fmtc(p.get('dom_wrist_conf'))}  "
                  f"{fmt(p.get('pas_wrist_y'))} {fmtc(p.get('pas_wrist_conf'))}  "
                  f"{fmt(p.get('shoulder_y'))} {fmtc(p.get('shoulder_conf'))}  "
                  f"{fmt(p.get('nose_y'))} {fmtc(p.get('nose_conf'))} "
                  f"{fmtarm(p.get('arm_ext_dom'))} {fmtarm(p.get('arm_ext_pas'))} "
                  f"{fmt(p.get('bbox_area'), ':>6.0f')}  {fmt(p.get('court_y'))}{marker}")
        else:
            print(f"  {f:>6}  {ts_s:>5.2f}   .  (no keypoints / unparseable){marker}")

    # Summary stats
    print()
    print("=== SUMMARY ===")
    parsed = [p for p in profiles if p.get("kp_parsed")]
    bz = [p for p in parsed if p.get("in_baseline_zone")]
    usable = [p for p in bz if p.get("usable")]
    score_ge1 = [p for p in usable if p.get("score_total", 0) >= 1]

    print(f"  parsed:               {len(parsed):>3} / {len(profiles)}")
    print(f"  baseline-zone (bz):   {len(bz):>3} / {len(parsed)}")
    print(f"  usable:               {len(usable):>3} / {len(bz)}")
    print(f"  score >= 1:           {len(score_ge1):>3} / {len(usable)}")
    if score_ge1:
        scores = [p["score_total"] for p in score_ge1]
        print(f"  score distribution:   "
              f"max={max(scores)}  mean={statistics.mean(scores):.2f}  "
              f"trophy_frames={sum(p['trophy'] for p in score_ge1)}  "
              f"toss_frames={sum(p['toss'] for p in score_ge1)}  "
              f"both_up_frames={sum(p['both_up'] for p in score_ge1)}")

    arm_exts = [p["arm_ext_dom"] for p in score_ge1
                if p.get("arm_ext_dom") is not None]
    if arm_exts:
        pcts = _percentiles(arm_exts)
        print(f"  arm_ext_dom (score>=1, n={len(arm_exts)}):  "
              f"min={min(arm_exts):.1f}  p25={pcts['p25']:.1f}  "
              f"p50={pcts['p50']:.1f}  p75={pcts['p75']:.1f}  "
              f"p95={pcts['p95']:.1f}  max={max(arm_exts):.1f}")
    arm_exts_all = [p["arm_ext_dom"] for p in usable
                    if p.get("arm_ext_dom") is not None]
    if arm_exts_all:
        pcts = _percentiles(arm_exts_all)
        n_above_0 = sum(1 for v in arm_exts_all if v > 0)
        n_above_5 = sum(1 for v in arm_exts_all if v >= 5)
        n_above_30 = sum(1 for v in arm_exts_all if v >= 30)
        print(f"  arm_ext_dom (all usable bz, n={len(arm_exts_all)}):  "
              f"min={min(arm_exts_all):.1f}  p50={pcts['p50']:.1f}  "
              f"p95={pcts['p95']:.1f}  max={max(arm_exts_all):.1f}  "
              f">0: {n_above_0}  >=5: {n_above_5}  >=30: {n_above_30}")

    nose_valid_n = sum(1 for p in usable if p.get("nose_valid"))
    if usable:
        print(f"  nose validity rate:   {nose_valid_n}/{len(usable)} "
              f"({100 * nose_valid_n / len(usable):.0f}%)")
    dom_wr_confs = [p["dom_wrist_conf"] for p in parsed
                    if p.get("dom_wrist_conf") is not None]
    if dom_wr_confs:
        pcts = _percentiles(dom_wr_confs)
        print(f"  dom_wrist_conf (parsed, n={len(dom_wr_confs)}):  "
              f"p25={pcts['p25']:.2f}  p50={pcts['p50']:.2f}  p95={pcts['p95']:.2f}")

    # Top arm_ext frames — surfaces frames where the wrist genuinely was
    # above the shoulder (in pixel terms). If these frames are NOT in the
    # score>=1 cluster, score_pose_frame's other gates are masking real
    # signal — worth examining the wrist/shoulder geometry directly.
    top = sorted(
        (p for p in usable if p.get("arm_ext_dom") is not None),
        key=lambda p: p["arm_ext_dom"], reverse=True,
    )[:8]
    if top:
        print()
        print("  === TOP arm_ext_dom frames (any score) ===")
        print("  frame   ts     score  arm_ext  dom_wY  shY  noseY  noseV  bbox     cy")
        for p in top:
            f = p["frame_idx"]
            print(f"  {f:>6}  {f/fps:>5.2f}    {p.get('score_total', 0)}  "
                  f"{p['arm_ext_dom']:>7.1f}  {p.get('dom_wrist_y') or 0:>6.1f}  "
                  f"{p.get('shoulder_y') or 0:>5.1f}  "
                  f"{p.get('nose_y') or 0:>5.1f}  "
                  f"{'Y' if p.get('nose_valid') else '.'}      "
                  f"{(p.get('bbox_area') or 0):>6.0f}  "
                  f"{(p.get('court_y') or 0):>5.2f}")

    print()
    print(f"  VERDICT: {_verdict(profiles, args.player, target_frame, fps)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
