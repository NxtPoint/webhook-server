"""Ball-tracker bench — runs BOTH trackers against ALL fixtures and reports deltas.

The ball-layer equivalent of `ml_pipeline/diag/bench.py` (serve detector). After
a `ball_tracker.py` or `wasb_ball_tracker.py` edit, run:

    python -m ml_pipeline.diag.bench_ball

It loads every `*.json` manifest in `ml_pipeline/fixtures_ball/`, runs both
tracknet_v2 and wasb (where available), and prints per-fixture / per-tracker
metrics vs `ml_pipeline/diag/bench_ball_baseline.json`.

Metrics reported per (fixture, tracker):
  - post_filter_rate: detections that survive the >100px-jump filter (this
    is the production-aligned signal — what ends up in ml_analysis.ball_detections)
  - post_filter_sa_recall: SA-bounce-anchor recall on the same filtered set
  - trajectory_coherence_pct: % of consecutive RAW detections that form a
    coherent trajectory — high when the tracker is following a ball, low
    when output is dominated by motion-fallback noise
  - tier_dist (TrackNetV2 only): % of detections from each tier; `delta` =
    frame-delta Hough motion fallback (the noise tier)
  - det_rate (raw) and sa_bounce_recall (raw) kept for compatibility

Regression test: a drop on any of {post_filter_rate, post_filter_sa_recall,
trajectory_coherence_pct} flags REGRESSION. Raw det_rate is tracked but its
drop alone is NOT a regression (WASB legitimately rejects low-confidence
frames that TrackNetV2's fallback would have kept as noise).

To accept current numbers as the new baseline:

    python -m ml_pipeline.diag.bench_ball --update-baseline

To scope:

    python -m ml_pipeline.diag.bench_ball --tracker wasb        # only WASB
    python -m ml_pipeline.diag.bench_ball --fixture a798eff0    # only this task
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from ml_pipeline.diag.replay_ball import _load_fixture, replay


FIXTURES_DIR = Path("ml_pipeline/fixtures_ball")
BASELINE_PATH = Path("ml_pipeline/diag/bench_ball_baseline.json")
DEFAULT_TRACKERS = ("tracknet_v2", "wasb")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
    except Exception:
        return "unknown"


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    with open(BASELINE_PATH) as f:
        return json.load(f)


def _save_baseline(data: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _format_delta(curr: float | None, base: float | None, *, pct: bool = True) -> str:
    """Return a delta string with [!] REGRESSION tag on negative metric moves.

    Both detection_rate and sa_bounce_recall are "higher is better" — a drop
    flags REGRESSION. Pass pct=True for percentage-point delta (the natural
    framing for rates / recalls), pct=False for absolute counts.
    """
    if base is None or curr is None:
        return ""
    delta = curr - base
    if pct:
        eps, sign = 0.0005, "%"
        scale = 100.0
    else:
        eps, sign = 0.5, ""
        scale = 1.0
    if abs(delta) < eps:
        return "  (no change)"
    s = f"{delta*scale:+.1f}{sign}" if pct else f"{int(delta):+d}"
    if delta < -eps:
        return f"  ({s}) [!] REGRESSION"
    return f"  ({s})"


def _run_one(fixture_path: Path, tracker: str) -> dict | None:
    """Run one (fixture, tracker) pair. Returns metrics dict, or None if the
    tracker is unavailable (e.g. WASB weights missing).
    """
    fixture = _load_fixture(str(fixture_path))
    try:
        return replay(fixture, tracker_name=tracker)
    except FileNotFoundError as e:
        print(f"[skip] {fixture_path.stem}/{tracker}: {e}", file=sys.stderr)
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true",
                    help="Write current numbers as the new committed baseline")
    ap.add_argument("--tracker", default=None,
                    help="Run only this tracker (default: all)")
    ap.add_argument("--fixture", default=None,
                    help="Run only this fixture stem (default: all)")
    ap.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))
    args = ap.parse_args(argv)

    fixtures = sorted(Path(args.fixtures_dir).glob("*.json"))
    if args.fixture:
        fixtures = [f for f in fixtures if f.stem == args.fixture]
    if not fixtures:
        print(f"No fixtures found in {args.fixtures_dir}", file=sys.stderr)
        print("Hand-write one, or run snapshot_task_ball.py on Render shell.",
              file=sys.stderr)
        return 1

    trackers = (args.tracker,) if args.tracker else DEFAULT_TRACKERS

    baseline = _load_baseline()
    base_fixtures = baseline.get("fixtures", {})

    any_regression = False
    results: dict = {}

    print(f"=== bench_ball  fixtures={len(fixtures)}  "
          f"trackers={','.join(trackers)}  commit={_git_sha()} ===")
    print()
    print(f"{'fixture':<14} {'tracker':<12} {'post_rate':>10} {'post_recall':>12} "
          f"{'coherence':>10} {'raw_rate':>9} {'runtime':>8}")
    print("-" * 110)

    for fx in fixtures:
        results[fx.stem] = {}
        for t in trackers:
            m = _run_one(fx, t)
            if m is None:
                continue
            results[fx.stem][m["tracker"]] = m

            base = base_fixtures.get(fx.stem, {}).get(m["tracker"], {})
            # Regression test: post-filter metrics + coherence are the verdict.
            # Raw det_rate is informational only (WASB legitimately drops noisy
            # frames that TrackNet's fallback would have kept).
            pr_d = _format_delta(
                m["post_filter_rate"], base.get("post_filter_rate"),
            )
            psr_d = _format_delta(
                m["post_filter_sa_recall"], base.get("post_filter_sa_recall"),
            )
            coh_d = _format_delta(
                m["trajectory_coherence_pct"], base.get("trajectory_coherence_pct"),
            )
            if "REGRESSION" in (pr_d + psr_d + coh_d):
                any_regression = True

            psr = m["post_filter_sa_recall"]
            psr_str = f"{psr:.2%}" if psr is not None else "n/a"
            coh = m["trajectory_coherence_pct"]
            coh_str = f"{coh:.2%}" if coh is not None else "n/a"
            print(f"{fx.stem:<14} {m['tracker']:<12} "
                  f"{m['post_filter_rate']:>9.2%} "
                  f"{psr_str:>12} "
                  f"{coh_str:>10} "
                  f"{m['detection_rate']:>8.2%} "
                  f"{m['runtime_sec']:>7.1f}s")

            # Show deltas + tier breakdown as a second indented line for clarity.
            tier_str = ""
            if "tier_dist" in m:
                td = m["tier_dist"]
                n = max(1, m["detections"])
                tier_str = (f"  tiers: hough={td['tier1_hough']} "
                            f"cc={td['tier2_cc']} argmax={td['tier3_argmax']} "
                            f"delta={td['delta_fallback_hits']} "
                            f"({100*td['delta_fallback_hits']/n:.0f}% fallback)")
            if any(d for d in (pr_d, psr_d, coh_d)) or tier_str:
                print(f"  {'':<26} "
                      f"post_rate{pr_d}  post_recall{psr_d}  coherence{coh_d}"
                      f"{tier_str}")

    print()
    if any_regression:
        print("[!] REGRESSION DETECTED on at least one (fixture, tracker). Investigate before pushing.")
    else:
        print("[OK] No regressions vs committed baseline.")

    if args.update_baseline:
        new_baseline = {
            "updated_at": date.today().isoformat(),
            "commit": _git_sha(),
            "fixtures": {
                stem: {
                    t: {
                        # Verdict metrics (regression-tracked):
                        "post_filter_rate": m["post_filter_rate"],
                        "post_filter_sa_recall": m["post_filter_sa_recall"],
                        "trajectory_coherence_pct": m["trajectory_coherence_pct"],
                        # Informational counts:
                        "post_filter_detections": m["post_filter_detections"],
                        "post_filter_sa_hits": m["post_filter_sa_hits"],
                        # Raw (untracked but recorded):
                        "detection_rate": m["detection_rate"],
                        "sa_bounce_recall": m["sa_bounce_recall"],
                        "detections": m["detections"],
                        "sa_bounce_hits": m["sa_bounce_hits"],
                        # Context:
                        "sa_bounce_total": m["sa_bounce_total"],
                        "frames_processed": m["frames_processed"],
                        "max_pixel_jump_px": m["max_pixel_jump_px"],
                        **({"tier_dist": m["tier_dist"]}
                           if "tier_dist" in m else {}),
                    }
                    for t, m in tracker_results.items()
                }
                for stem, tracker_results in results.items() if tracker_results
            },
        }
        _save_baseline(new_baseline)
        print()
        print(f"-> wrote new baseline to {BASELINE_PATH}")
        print("   Commit it: git add ml_pipeline/diag/bench_ball_baseline.json "
              "&& git commit")

    return 1 if (any_regression and not args.update_baseline) else 0


if __name__ == "__main__":
    sys.exit(main())
