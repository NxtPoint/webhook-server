"""Silver-builder bench orchestrator.

Spins up local Docker Postgres, restores bronze fixtures, runs the silver
builder against each, and compares the result to the locked baseline JSON.
Exits non-zero on any regression. Same shape as `ml_pipeline/diag/bench.py`
(serve bench) but works on a DB instead of a Python pickle.

Flow (per `.claude/strategy/silver_bench_design_2026-05-21.md` §2):

  1. Start bench Postgres (db_helper.start()).
  2. For each `<task>_bronze.sql.gz` in fixtures dir:
     a. Reset DB state (TRUNCATE relevant tables, idempotent).
     b. Apply schema (bronze_init equivalents + silver ensure_schema +
        ml_analysis_init for ml_analysis.* tables).
     c. Restore the fixture via `gunzip | docker exec -i psql`.
     d. Call build_silver_match_t5(task_id, engine=bench_engine).
     e. Query the resulting silver.point_detail.
     f. Compare to the matching `_silver_baseline.json`.
  3. Print per-fixture verdicts. Exit 1 on any regression.

Usage:
    python -m ml_pipeline.diag.bench_silver --setup        # start container
    python -m ml_pipeline.diag.bench_silver                # run all fixtures
    python -m ml_pipeline.diag.bench_silver --task 880dff02  # one fixture
    python -m ml_pipeline.diag.bench_silver --update-baseline  # lock current
    python -m ml_pipeline.diag.bench_silver --teardown    # stop + remove
    python -m ml_pipeline.diag.bench_silver --status      # container status

The bench is empty until a fixture lands in `ml_pipeline/fixtures_silver/`
(via `python -m ml_pipeline.diag.bench_silver.snapshot --task <TID>` on
Render shell, then uploaded to S3 + downloaded locally). When no fixtures
are present the bench returns 0 with a "no fixtures" notice — that's the
expected first-time state, not a regression.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from ml_pipeline.diag.bench_silver import db_helper


logger = logging.getLogger(__name__)


FIXTURES_DIR = Path("ml_pipeline/fixtures_silver")
TASK_TABLES_TO_TRUNCATE = [
    # Order doesn't matter for TRUNCATE; included for completeness.
    "silver.point_detail",
    "ml_analysis.serve_events",
    "ml_analysis.player_detections",
    "ml_analysis.ball_detections",
    "ml_analysis.video_analysis_jobs",
    "bronze.player_position",
    "bronze.ball_position",
    "bronze.ball_bounce",
    "bronze.rally",
    "bronze.player_swing",
    "bronze.submission_context",
]


def _discover_fixtures(task_filter: str | None = None) -> list[tuple[str, Path, Path]]:
    """Return [(task_short, bronze_sql_gz_path, baseline_json_path), ...].

    Skips any fixture missing its baseline JSON sibling — bench can't compare
    without one. Filters by task_filter (prefix-match on task_short) if given.
    """
    if not FIXTURES_DIR.exists():
        return []
    fixtures = []
    for path in sorted(FIXTURES_DIR.glob("*_bronze.sql.gz")):
        short = path.name.removesuffix("_bronze.sql.gz")
        baseline = FIXTURES_DIR / f"{short}_silver_baseline.json"
        if not baseline.exists():
            logger.warning("fixture %s has no baseline JSON, skipping", path.name)
            continue
        if task_filter and not short.startswith(task_filter[:8]):
            continue
        fixtures.append((short, path, baseline))
    return fixtures


def _bench_engine():
    """Build a SQLAlchemy engine pointing at the bench Postgres."""
    from sqlalchemy import create_engine
    return create_engine(db_helper.connection_string(), future=True)


# ---------------------------------------------------------------------------
# Extra column DDL — kept in sync with upload_app.py:_ensure_submission_context_schema
# and video_pipeline/video_trim_api.py:_ensure_trim_columns. Inlined here because
# importing those modules transitively requires VIDEO_WORKER_BASE_URL + other
# service env vars the bench shouldn't need. If the prod DDL diverges, the bench
# restore will fail with a "column does not exist" error on COPY — that's the
# canary to update this list.
# ---------------------------------------------------------------------------
_EXTRA_SUBMISSION_CONTEXT_DDL = [
    "CREATE TABLE IF NOT EXISTS bronze.submission_context (task_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS email TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS customer_name TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS match_date DATE",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS start_time TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS location TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_name TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_name TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_utr TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_utr TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS video_url TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS share_url TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS raw_meta JSONB",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS session_id TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_status_at TIMESTAMPTZ",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS last_result_url TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_started_at TIMESTAMPTZ",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ingest_error TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notified_at TIMESTAMPTZ",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_status TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS wix_notify_error TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ses_notified_at TIMESTAMPTZ",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS ses_notify_error TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set1_games INT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set1_games INT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set2_games INT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set2_games INT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_a_set3_games INT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS player_b_set3_games INT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS first_server TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS sport_type TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
    # Trim columns — owned by video_pipeline.video_trim_api on the prod side
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS s3_bucket TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS s3_key TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_status TEXT",
    "ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS trim_output_s3_key TEXT",
]


def _ensure_schema(engine):
    """Apply bronze + ml_analysis + silver schema to the bench DB.

    Avoids importing upload_app / video_trim_api (they require service env
    vars the bench shouldn't need). Uses db_init internals + ml_analysis_init
    + inline submission_context DDL.
    """
    os.environ["DATABASE_URL"] = db_helper.connection_string()

    # Bronze core (raw_result, session, array tables, ball/swing typed cols)
    from db_init import _create_core, _add_typed_columns, _add_indexes
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        _create_core(conn)
        # submission_context + trim columns BEFORE _add_typed_columns (which
        # references the column set via ALTER … ADD IF NOT EXISTS).
        for ddl in _EXTRA_SUBMISSION_CONTEXT_DDL:
            conn.execute(sql_text(ddl))
        _add_typed_columns(conn)
        _add_indexes(conn)

    # ml_analysis schema (video_analysis_jobs, ball_detections, ...)
    from ml_pipeline.db_schema import ml_analysis_init
    ml_analysis_init(engine)

    # Silver schema (point_detail table + all columns)
    from build_silver_v2 import ensure_schema as silver_ensure_schema
    with engine.begin() as conn:
        silver_ensure_schema(conn)

    # serve_events DDL
    try:
        from ml_pipeline.serve_detector.schema import init_serve_events_schema
        with engine.begin() as conn:
            init_serve_events_schema(conn)
    except Exception as e:
        logger.warning("serve_events schema init failed (non-fatal): %s", e)


def _truncate_task_tables(engine):
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        for table in TASK_TABLES_TO_TRUNCATE:
            try:
                conn.execute(sql_text(f"TRUNCATE {table} RESTART IDENTITY CASCADE"))
            except Exception as e:
                # First-time setup: table may not exist yet
                logger.debug("truncate %s skipped: %s", table, e)


def _restore_fixture(sql_gz_path: Path) -> None:
    """Pipe the gzipped SQL fixture through `docker exec -i psql`."""
    if not sql_gz_path.exists():
        raise FileNotFoundError(sql_gz_path)
    with gzip.open(sql_gz_path, "rb") as gz:
        # Stream to psql via stdin. `-v ON_ERROR_STOP=1` makes COPY/SQL
        # errors fatal instead of silently continuing.
        proc = subprocess.run(
            [
                "docker", "exec", "-i", db_helper.CONTAINER_NAME,
                "psql",
                "-U", db_helper.PG_USER,
                "-d", db_helper.PG_DB,
                "-v", "ON_ERROR_STOP=1",
                "--quiet",
            ],
            stdin=gz,
            capture_output=True,
            text=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"fixture restore failed for {sql_gz_path}: "
            f"stderr={proc.stderr.decode('utf-8', errors='replace')[:2000]}"
        )


def _run_silver_builder(engine, task_id: str) -> dict:
    """Run build_silver_match_t5 against the bench engine for one task."""
    from ml_pipeline.build_silver_match_t5 import build_silver_match_t5
    return build_silver_match_t5(task_id=task_id, replace=True, engine=engine)


def _measure_current(engine, task_id: str, model: str) -> dict:
    """Re-use snapshot's _baseline_for_task to compute current numbers."""
    from ml_pipeline.diag.bench_silver.snapshot import _baseline_for_task
    with engine.connect() as conn:
        return _baseline_for_task(conn, task_id, model)


def _compare(current: dict, baseline: dict, tolerance: dict) -> list[str]:
    """Return a list of regression strings. Empty list = green."""
    regressions = []
    for key, exp in baseline.items():
        cur = current.get(key)
        tol = tolerance.get(key, {})

        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            abs_tol = tol.get("abs", 0)
            if cur is None:
                regressions.append(f"{key}: expected {exp}, got None")
                continue
            if abs(float(cur) - float(exp)) > abs_tol:
                regressions.append(f"{key}: expected {exp}, got {cur} (abs tol={abs_tol})")
            continue

        if isinstance(exp, dict):
            per_class_pct = tol.get("per_class_pct")
            for k, v in exp.items():
                cur_v = (cur or {}).get(k, 0)
                if per_class_pct is None:
                    if cur_v != v:
                        regressions.append(f"{key}.{k}: expected {v}, got {cur_v}")
                else:
                    abs_drift = abs(cur_v - v)
                    rel_drift = abs_drift / max(v, 1) * 100
                    if rel_drift > per_class_pct:
                        regressions.append(
                            f"{key}.{k}: expected {v}, got {cur_v} ({rel_drift:.1f}% drift > {per_class_pct}%)"
                        )
            # Also flag unexpected new keys
            for k in (cur or {}):
                if k not in exp:
                    cur_v = cur[k]
                    if cur_v:  # don't complain about a 0-count new key
                        regressions.append(f"{key}.{k}: unexpected new key with count {cur_v}")
            continue

        # Scalar non-numeric (e.g. None for timestamps when no serves)
        if cur != exp:
            regressions.append(f"{key}: expected {exp!r}, got {cur!r}")

    return regressions


def run_bench(task_filter: str | None = None, update_baseline: bool = False) -> int:
    """Top-level bench loop. Returns process exit code (0 green, 1 regression)."""
    fixtures = _discover_fixtures(task_filter)
    if not fixtures:
        if task_filter:
            print(f"no fixtures matched task_filter={task_filter!r}")
        else:
            print(f"no fixtures in {FIXTURES_DIR} — capture one via "
                  f"`python -m ml_pipeline.diag.bench_silver.snapshot --task <TID>` "
                  f"on Render shell")
        return 0

    db_helper.start()
    engine = _bench_engine()
    _ensure_schema(engine)

    overall_regressions = 0
    summary: list[tuple[str, int]] = []  # (task_short, regression_count)

    for short, bronze_path, baseline_path in fixtures:
        print(f"\n=== {short} ===")
        baseline_doc = json.loads(baseline_path.read_text())
        task_id = baseline_doc["task_id"]
        model = baseline_doc.get("model", "t5")

        _truncate_task_tables(engine)
        _restore_fixture(bronze_path)

        try:
            result = _run_silver_builder(engine, task_id)
        except Exception as e:
            print(f"  [!] silver builder raised: {e}")
            overall_regressions += 1
            summary.append((short, -1))
            continue
        print(f"  builder result: {result}")

        current = _measure_current(engine, task_id, model)
        regressions = _compare(
            current=current,
            baseline=baseline_doc["expected"],
            tolerance=baseline_doc.get("tolerance", {}),
        )

        if update_baseline:
            baseline_doc["expected"] = current
            baseline_path.write_text(json.dumps(baseline_doc, indent=2))
            print(f"  [updated baseline] {baseline_path.name}")
            summary.append((short, 0))
            continue

        if regressions:
            for r in regressions:
                print(f"  [!] {r}")
            overall_regressions += len(regressions)
            summary.append((short, len(regressions)))
        else:
            print(f"  [OK] {current['row_count_total']} silver rows "
                  f"({current['row_count_active']} active, "
                  f"{current['serve_count_active']} serves)")
            summary.append((short, 0))

    print("\n=== summary ===")
    for short, n in summary:
        verdict = "OK" if n == 0 else f"REGRESSION ({n} issues)" if n > 0 else "ERROR"
        print(f"  {short}: {verdict}")

    return 1 if overall_regressions else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Silver-builder bench harness (Docker Postgres + locked baselines)."
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
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

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

    return run_bench(
        task_filter=args.task,
        update_baseline=args.update_baseline,
    )


if __name__ == "__main__":
    sys.exit(main())
