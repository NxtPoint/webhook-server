"""Silver-builder bench orchestrator.

STUB — designed but not yet implemented. CLI surface stubbed so the next
session inherits a working argument shape even if the implementation is
empty.

Flow (per `.claude/strategy/silver_bench_design_2026-05-21.md` §2):

  1. Start bench Postgres (via db_helper.start()).
  2. For each .sql.gz fixture in ml_pipeline/fixtures_silver/:
     a. Reset / clean DB state.
     b. Apply schema (db_init.bronze_init equivalent on the local engine,
        plus build_silver_v2.ensure_schema, plus ml_pipeline/db_schema
        for ml_analysis.* tables).
     c. Restore the bronze fixture via gunzip | docker exec psql.
     d. Run build_silver_match_t5(task_id, engine=local_engine).
     e. Query the resulting silver.point_detail.
     f. Compare to the matching _silver_baseline.json.
  3. Print per-fixture deltas. Exit 1 on any regression.
  4. With --update-baseline, overwrite the JSON baselines.
  5. With --teardown, stop the container.

Usage:
    python -m ml_pipeline.diag.bench_silver --setup
    python -m ml_pipeline.diag.bench_silver
    python -m ml_pipeline.diag.bench_silver --update-baseline
    python -m ml_pipeline.diag.bench_silver --teardown
    python -m ml_pipeline.diag.bench_silver --task <TID> --diff  # row-level diff
"""
from __future__ import annotations

import argparse
import logging
import sys

from ml_pipeline.diag.bench_silver import db_helper


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Silver-builder bench harness (STUB)."
    )
    ap.add_argument("--setup", action="store_true",
                    help="Start the bench Postgres container and exit")
    ap.add_argument("--teardown", action="store_true",
                    help="Stop + remove the bench Postgres container and exit")
    ap.add_argument("--status", action="store_true",
                    help="Print bench Postgres container status and exit")
    ap.add_argument("--update-baseline", action="store_true",
                    help="Lock current bench results as the new baseline")
    ap.add_argument("--task", default=None,
                    help="Run only this fixture stem (default: all fixtures)")
    ap.add_argument("--diff", action="store_true",
                    help="Print a row-level diff vs baseline for the failing task(s)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    # Plumbing commands (db_helper passthrough) — these ARE implemented.
    if args.status:
        if db_helper.is_running():
            print(f"running on {db_helper.connection_string()}")
        elif db_helper.is_present():
            print("stopped (container exists)")
        else:
            print("absent")
        return 0
    if args.setup:
        url = db_helper.start()
        print(url)
        return 0
    if args.teardown:
        db_helper.stop(remove=True)
        return 0

    # Bench logic — STUB.
    print("=== bench.py STUB — bench orchestrator not yet implemented ===",
          file=sys.stderr)
    print("Design: .claude/strategy/silver_bench_design_2026-05-21.md §2",
          file=sys.stderr)
    print("Steps to implement next (from the design spec §7):",
          file=sys.stderr)
    print("  - snapshot.py (capture fixture on Render shell)", file=sys.stderr)
    print("  - schema init on the bench DB (db_init.bronze_init equivalent)",
          file=sys.stderr)
    print("  - fixture restore via docker exec psql", file=sys.stderr)
    print("  - call build_silver_match_t5(task_id, engine=local_engine)",
          file=sys.stderr)
    print("  - query + compare to baseline JSON", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
