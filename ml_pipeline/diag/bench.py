"""Run the serve detector against ALL fixtures and compare to a baseline.

This is the regression detector. After a detector edit, run:

    python -m ml_pipeline.diag.bench

It loads every `*.pkl.gz` in `ml_pipeline/fixtures/`, runs the prod
pipeline against each, and prints per-fixture near/far/total + delta
versus `ml_pipeline/diag/bench_baseline.json`. A negative delta on any
axis means the change was a regression — surfaces "far improved, near
went backwards" silently slips that bit us before.

To accept a new set of numbers as the baseline (e.g. you legitimately
improved on all axes):

    python -m ml_pipeline.diag.bench --update-baseline

Baseline is committed in git so changes are visible in PRs.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pickle
import subprocess
import sys
from datetime import date
from pathlib import Path

from ml_pipeline.diag.replay_serves import replay


FIXTURES_DIR = Path("ml_pipeline/fixtures")
BASELINE_PATH = Path("ml_pipeline/diag/bench_baseline.json")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _load_fixture(path: Path) -> dict:
    with gzip.open(path, "rb") as f:
        return pickle.load(f)


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    with open(BASELINE_PATH) as f:
        return json.load(f)


def _save_baseline(data: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _run_one(path: Path) -> dict:
    fixture = _load_fixture(path)
    result = replay(fixture)
    return {
        "name": path.stem.replace(".pkl", ""),
        "task_id": fixture["task_id"],
        "near": [result["near_match"], result["near_total"]],
        "far": [result["far_match"], result["far_total"]],
        "total": [result["near_match"] + result["far_match"], result["total"]],
        "verdicts": result["verdict_counts"],
    }


def _format_delta(curr: list, base: list | None) -> str:
    if base is None:
        return ""
    delta = curr[0] - base[0]
    if delta > 0:
        return f"  (+{delta} vs baseline)"
    if delta < 0:
        return f"  ({delta} vs baseline) ⚠ REGRESSION"
    return "  (no change)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true",
                    help="Write current numbers as the new committed baseline")
    ap.add_argument("--fixtures-dir", default=str(FIXTURES_DIR),
                    help=f"Override fixtures dir (default {FIXTURES_DIR})")
    args = ap.parse_args(argv)

    fixtures = sorted(Path(args.fixtures_dir).glob("*.pkl.gz"))
    if not fixtures:
        print(f"No fixtures found in {args.fixtures_dir}", file=sys.stderr)
        print("Run: python -m ml_pipeline.diag.snapshot_task --task <T5_TID>",
              file=sys.stderr)
        return 1

    baseline = _load_baseline()
    base_fixtures = baseline.get("fixtures", {})

    results = []
    any_regression = False
    print(f"=== bench  {len(fixtures)} fixtures  commit={_git_sha()} ===")
    print()
    print(f"{'fixture':<12} {'near':>7} {'far':>7} {'total':>7}   delta")
    print("-" * 80)
    for fx in fixtures:
        r = _run_one(fx)
        results.append(r)
        base = base_fixtures.get(r["name"], {})
        near_d = _format_delta(r["near"], base.get("near"))
        far_d = _format_delta(r["far"], base.get("far"))
        tot_d = _format_delta(r["total"], base.get("total"))
        if "REGRESSION" in (near_d + far_d + tot_d):
            any_regression = True
        print(f"{r['name']:<12} "
              f"{r['near'][0]:>3}/{r['near'][1]:<3} "
              f"{r['far'][0]:>3}/{r['far'][1]:<3} "
              f"{r['total'][0]:>3}/{r['total'][1]:<3}"
              f"  near{near_d}  far{far_d}  total{tot_d}")

    print()
    if any_regression:
        print("⚠ REGRESSION DETECTED on at least one fixture. Investigate before pushing.")
    else:
        print("✓ No regressions vs committed baseline.")

    if args.update_baseline:
        new_baseline = {
            "updated_at": date.today().isoformat(),
            "commit": _git_sha(),
            "fixtures": {
                r["name"]: {"near": r["near"], "far": r["far"],
                            "total": r["total"], "verdicts": r["verdicts"]}
                for r in results
            },
        }
        _save_baseline(new_baseline)
        print()
        print(f"-> wrote new baseline to {BASELINE_PATH}")
        print("   Commit it: git add ml_pipeline/diag/bench_baseline.json && git commit")

    return 1 if (any_regression and not args.update_baseline) else 0


if __name__ == "__main__":
    sys.exit(main())
