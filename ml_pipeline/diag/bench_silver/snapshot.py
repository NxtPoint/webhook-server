"""Snapshot a task's bronze + ml_analysis rows to a portable .sql.gz fixture.

STUB — designed but not yet implemented. Captured here so the next session
inherits a working CLI surface even if the implementation is empty.

This tool runs on Render shell (or anywhere with DATABASE_URL pointing at
production). It produces two files:

  ml_pipeline/fixtures_silver/<task_short>_bronze.sql.gz
    pg_dump --data-only output of every row keyed to ``task_id`` across
    bronze.* and ml_analysis.*. Gzipped. Size: ~1-5 MB per task.

  ml_pipeline/fixtures_silver/<task_short>_silver_baseline.json
    Snapshot of expected silver.point_detail rows for this task — row count,
    serve count, stroke distribution, depth distribution. The bench's
    comparison target. Schema in
    .claude/strategy/silver_bench_design_2026-05-21.md §3.

Both should be uploaded to s3://nextpoint-prod-uploads/fixtures/silver/
after capture for CI / portability.

Usage (Render shell):
    python -m ml_pipeline.diag.bench_silver.snapshot --task <T5_TID>

See `.claude/strategy/silver_bench_design_2026-05-21.md` §4 for the
required pg_dump table list and the WHERE-clause filter pattern.
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Snapshot a task's bronze + ml_analysis state for the silver bench."
    )
    ap.add_argument("--task", required=True, help="T5 task_id to snapshot")
    ap.add_argument(
        "--output-dir", default="ml_pipeline/fixtures_silver",
        help="Directory for output files (default ml_pipeline/fixtures_silver)",
    )
    ap.add_argument(
        "--model", default="t5", choices=["t5", "sportai", "both"],
        help="Which model's silver rows to capture in the baseline (default t5)",
    )
    args = ap.parse_args(argv)

    print(f"=== snapshot.py STUB — args: task={args.task[:8]} "
          f"out={args.output_dir} model={args.model} ===",
          file=sys.stderr)
    print("NOT YET IMPLEMENTED. See "
          ".claude/strategy/silver_bench_design_2026-05-21.md §4 for the design.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
