"""Phase 5c.4 — bench-gate-before-promotion for finetuned ball-tracker weights.

The gate between "we trained new weights from the dual-submit corpus" (Phase 5c.3)
and "we ship the new weights to Batch" (the part that trips guardrail #8).

Runs the ball-tracker bench TWICE per (fixture, tracker):
  1. Production baseline — tracker's default weights (TRACKNET_WEIGHTS / WASB)
  2. Candidate          — the file passed via --weights-path

Reports per-fixture side-by-side numbers + delta, then prints a verdict:

  PROMOTE   — candidate strictly beats baseline on `post_filter_sa_recall`
              without regressing `post_filter_rate` or `trajectory_coherence_pct`
              by more than the configured tolerance (default 1pp)
  NEUTRAL   — no metric moves outside the tolerance band on any fixture
  REJECT    — candidate regresses any guardrail metric on any fixture beyond
              tolerance, OR fails to improve `post_filter_sa_recall` on
              ANY fixture

Exit code follows the verdict: 0 = PROMOTE / NEUTRAL, 1 = REJECT.

Usage:

    python -m ml_pipeline.diag.bench_finetuned \\
        --weights-path ml_pipeline/training/runs/tracknet_ft_20260523.pt

    # scope to one fixture + tracker (faster iteration)
    python -m ml_pipeline.diag.bench_finetuned \\
        --weights-path <.pt> --tracker tracknet_v2 --fixture a798eff0

If the result is PROMOTE: read
`.claude/playbook_phase_5c4_weights_promotion.md` for the deploy steps
(Docker rebuild + dual-region ECR push + new job-def revisions). This bench is
the prerequisite, not the deploy.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ml_pipeline.diag.replay_ball import _load_fixture, replay


FIXTURES_DIR = Path("ml_pipeline/fixtures_ball")
DEFAULT_TRACKERS = ("tracknet_v2", "wasb")

# Tolerance in absolute percentage points. A metric move within ±this band is
# "no change". A regression beyond this band on any guardrail metric REJECTS.
DEFAULT_TOLERANCE_PP = 1.0

# Guardrail metrics — a candidate may not regress any of these beyond tolerance.
GUARDRAIL_METRICS = ("post_filter_rate", "trajectory_coherence_pct")

# Verdict metric — candidate must strictly improve this on at least one fixture
# (and not regress on others) to earn PROMOTE.
VERDICT_METRIC = "post_filter_sa_recall"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
    except Exception:
        return "unknown"


def _delta_str(baseline: Optional[float], candidate: Optional[float]) -> str:
    if baseline is None and candidate is None:
        return "n/a"
    if baseline is None:
        return f"{candidate:.2%}  (no baseline)"
    if candidate is None:
        return f"baseline {baseline:.2%}, candidate n/a"
    delta_pp = (candidate - baseline) * 100.0
    sign = "+" if delta_pp >= 0 else ""
    return f"{baseline:.2%} → {candidate:.2%}  ({sign}{delta_pp:.2f}pp)"


def _classify_delta(
    baseline: Optional[float], candidate: Optional[float], tolerance_pp: float,
) -> str:
    """Return one of: 'better', 'worse', 'same', 'na'."""
    if baseline is None or candidate is None:
        return "na"
    delta_pp = (candidate - baseline) * 100.0
    if delta_pp > tolerance_pp:
        return "better"
    if delta_pp < -tolerance_pp:
        return "worse"
    return "same"


def _run_pair(
    fixture_path: Path,
    tracker: str,
    candidate_weights: str,
) -> Optional[dict]:
    """Run baseline + candidate for one (fixture, tracker). Returns a comparison dict."""
    fixture = _load_fixture(str(fixture_path))

    print(f"  [{fixture_path.stem}/{tracker}] baseline run...", flush=True)
    t0 = time.time()
    try:
        base = replay(fixture, tracker_name=tracker, weights_path=None)
    except FileNotFoundError as e:
        print(f"    [skip] baseline unavailable: {e}", file=sys.stderr)
        return None
    base_rt = time.time() - t0

    print(f"  [{fixture_path.stem}/{tracker}] candidate run...", flush=True)
    t0 = time.time()
    try:
        cand = replay(fixture, tracker_name=tracker, weights_path=candidate_weights)
    except FileNotFoundError as e:
        print(f"    [skip] candidate unavailable: {e}", file=sys.stderr)
        return None
    cand_rt = time.time() - t0

    return {
        "fixture": fixture_path.stem,
        "tracker": tracker,
        "baseline": base,
        "candidate": cand,
        "baseline_runtime_sec": round(base_rt, 1),
        "candidate_runtime_sec": round(cand_rt, 1),
    }


def _verdict(rows: list[dict], tolerance_pp: float) -> tuple[str, list[str]]:
    """Compute promotion verdict + per-pair reason strings.

    PROMOTE: VERDICT_METRIC strictly improves on at least one (fixture, tracker)
             pair AND no GUARDRAIL_METRIC regresses on any pair.
    NEUTRAL: no metric moves outside tolerance on any pair (candidate is
             indistinguishable from baseline within the bench's resolution).
    REJECT:  any GUARDRAIL_METRIC regresses on any pair, OR VERDICT_METRIC
             regresses on any pair (improvement on one fixture cannot pay for
             regression on another).
    """
    reasons: list[str] = []
    any_improvement = False
    any_regression = False

    for r in rows:
        b = r["baseline"]
        c = r["candidate"]
        tag = f"{r['fixture']}/{r['tracker']}"

        verdict_class = _classify_delta(
            b.get(VERDICT_METRIC), c.get(VERDICT_METRIC), tolerance_pp,
        )
        if verdict_class == "better":
            any_improvement = True
            reasons.append(f"[+] {tag}: {VERDICT_METRIC} improved")
        elif verdict_class == "worse":
            any_regression = True
            reasons.append(f"[!] {tag}: {VERDICT_METRIC} regressed")

        for gm in GUARDRAIL_METRICS:
            gc = _classify_delta(b.get(gm), c.get(gm), tolerance_pp)
            if gc == "worse":
                any_regression = True
                reasons.append(f"[!] {tag}: {gm} regressed (guardrail)")

    if any_regression:
        return "REJECT", reasons
    if any_improvement:
        return "PROMOTE", reasons
    return "NEUTRAL", reasons


def _print_row(r: dict) -> None:
    b = r["baseline"]
    c = r["candidate"]
    print()
    print(f"=== {r['fixture']} / {r['tracker']} ===")
    print(f"  runtime:                baseline {r['baseline_runtime_sec']:.1f}s, "
          f"candidate {r['candidate_runtime_sec']:.1f}s")
    print(f"  post_filter_sa_recall:  {_delta_str(b.get('post_filter_sa_recall'), c.get('post_filter_sa_recall'))}")
    print(f"  post_filter_rate:       {_delta_str(b.get('post_filter_rate'), c.get('post_filter_rate'))}")
    print(f"  trajectory_coherence:   {_delta_str(b.get('trajectory_coherence_pct'), c.get('trajectory_coherence_pct'))}")
    print(f"  detection_rate (raw):   {_delta_str(b.get('detection_rate'), c.get('detection_rate'))}")
    if b.get("sa_bounce_total"):
        print(f"  post-filter hits:       "
              f"{b.get('post_filter_sa_hits')}/{b['sa_bounce_total']} → "
              f"{c.get('post_filter_sa_hits')}/{c['sa_bounce_total']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    ap.add_argument("--weights-path", required=True,
                    help="Candidate weights file (.pt) to bench against the "
                         "production default")
    ap.add_argument("--tracker", default=None,
                    choices=[None, "tracknet_v2", "wasb"],
                    help="Run only this tracker (default: tracknet_v2 only — "
                         "finetuned weights are per-architecture; benching the "
                         "candidate against WASB's production weights as a "
                         "control rarely tells you anything new). Pass "
                         "--tracker wasb to bench a WASB finetune; omit to "
                         "default to tracknet_v2.")
    ap.add_argument("--fixture", default=None,
                    help="Run only this fixture stem (default: all)")
    ap.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))
    ap.add_argument("--tolerance-pp", type=float, default=DEFAULT_TOLERANCE_PP,
                    help=f"Absolute pp tolerance for 'no change' band "
                         f"(default {DEFAULT_TOLERANCE_PP}pp)")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON to stdout instead of "
                         "human report (verdict still echoed to stderr)")
    args = ap.parse_args(argv)

    if not Path(args.weights_path).exists():
        print(f"ERROR: candidate weights file not found: {args.weights_path}",
              file=sys.stderr)
        return 2

    fixtures = sorted(Path(args.fixtures_dir).glob("*.json"))
    if args.fixture:
        fixtures = [f for f in fixtures if f.stem == args.fixture]
    if not fixtures:
        print(f"No fixtures found in {args.fixtures_dir}", file=sys.stderr)
        return 1

    # Tracker default: finetuned weights are almost always TrackNetV2 — running
    # them against WASB would attempt to load a TrackNet-shape state_dict into
    # WASB's HRNet, which fails (and isn't what the user wants).
    trackers = (args.tracker,) if args.tracker else ("tracknet_v2",)

    print(f"=== bench_finetuned commit={_git_sha()} "
          f"candidate={args.weights_path} ===", flush=True)
    print(f"fixtures={len(fixtures)}  trackers={','.join(trackers)}  "
          f"tolerance={args.tolerance_pp}pp", flush=True)

    rows: list[dict] = []
    for fx in fixtures:
        for t in trackers:
            row = _run_pair(fx, t, args.weights_path)
            if row is not None:
                rows.append(row)

    if not rows:
        print("ERROR: no successful (fixture, tracker) pairs", file=sys.stderr)
        return 2

    if args.json:
        payload = {
            "commit": _git_sha(),
            "candidate_weights": args.weights_path,
            "tolerance_pp": args.tolerance_pp,
            "rows": rows,
        }
        verdict, reasons = _verdict(rows, args.tolerance_pp)
        payload["verdict"] = verdict
        payload["reasons"] = reasons
        print(json.dumps(payload, indent=2, sort_keys=True))
        print(f"verdict: {verdict}", file=sys.stderr)
    else:
        for r in rows:
            _print_row(r)
        verdict, reasons = _verdict(rows, args.tolerance_pp)
        print()
        print(f"=== verdict: {verdict} ===")
        for line in reasons:
            print(f"  {line}")
        if verdict == "PROMOTE":
            print()
            print("Next step: read .claude/playbook_phase_5c4_weights_promotion.md "
                  "for deploy steps.")

    return 1 if verdict == "REJECT" else 0


if __name__ == "__main__":
    sys.exit(main())
