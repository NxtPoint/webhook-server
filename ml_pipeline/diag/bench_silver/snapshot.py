"""Snapshot a task's bronze + ml_analysis rows to a portable .sql.gz fixture.

This tool runs on Render shell (or anywhere with DATABASE_URL pointing at
production). It produces two files in --output-dir:

  <task_short>_bronze.sql.gz
    Per-table COPY ... FROM STDIN blocks (CSV format) carrying the rows for
    one task across bronze.* + ml_analysis.*. Gunzip + pipe through psql to
    restore. Size: ~1-5 MB per task. Filtered by task_id (or job_id for
    ml_analysis.ball_detections / player_detections).

  <task_short>_silver_baseline.json
    Snapshot of expected silver.point_detail for this task — row count,
    active vs excluded count, stroke distribution, outcome distribution,
    depth distribution, serve count, first/last serve timestamps. Schema in
    .claude/strategy/silver_bench_design_2026-05-21.md §3.

Upload both to s3://nextpoint-prod-uploads/fixtures/silver/ after capture
so CI + future sessions can fetch them without DB access.

Usage (Render shell):

    python -m ml_pipeline.diag.bench_silver.snapshot \\
        --task 880dff02-58bd-412c-9a29-5c5151004447

    # Then upload:
    aws s3 cp ml_pipeline/fixtures_silver/880dff02_bronze.sql.gz \\
              s3://nextpoint-prod-uploads/fixtures/silver/
    aws s3 cp ml_pipeline/fixtures_silver/880dff02_silver_baseline.json \\
              s3://nextpoint-prod-uploads/fixtures/silver/

Design notes:
- COPY (SELECT … WHERE …) TO STDOUT is the per-row filter mechanism.
  `pg_dump --where` only landed in Postgres 17; Render is on 15-16, so
  this hand-rolled approach is required.
- We write a plain SQL script (gzipped) so the bench can restore via
  `gunzip -c fixture.sql.gz | docker exec -i <pg> psql ...` — no special
  custom format, just plain SQL.
- Filter key is `task_id` for everything except `ml_analysis.ball_detections`
  / `player_detections` which only carry `job_id`. For T5 jobs the two are
  equal (`upload_app.py::_t5_submit` sets `job_id = task_id`), but we
  resolve via `video_analysis_jobs.job_id` to stay correct if that ever
  changes.
- `bronze.session` carries a session row but is rarely needed by the
  silver builder; kept out of the snapshot to keep file size down. Add it
  here if the bench surfaces a missing-dependency error.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

from sqlalchemy import create_engine, text as sql_text


logger = logging.getLogger("snapshot_silver")


# (table, filter_column) — order matters for restore (parents before children).
# submission_context is the natural anchor; bronze siblings + ml_analysis come
# in dependency-free order (no FKs across these tables in current schema).
SNAPSHOT_TABLES_BRONZE = [
    ("bronze.submission_context", "task_id"),
    ("bronze.player_swing",       "task_id"),
    ("bronze.rally",              "task_id"),
    ("bronze.ball_bounce",        "task_id"),
    ("bronze.ball_position",      "task_id"),
    ("bronze.player_position",    "task_id"),
]

SNAPSHOT_TABLES_ML = [
    ("ml_analysis.video_analysis_jobs", "job_id"),
    ("ml_analysis.ball_detections",     "job_id"),
    ("ml_analysis.player_detections",   "job_id"),
    ("ml_analysis.serve_events",        "task_id"),
]


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _get_engine(url_override: str | None = None):
    url = url_override or (
        os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or os.environ.get("DB_URL")
    )
    if not url:
        raise RuntimeError("DATABASE_URL required")
    return create_engine(_normalize_db_url(url))


def _columns(conn, schema: str, table: str) -> list[str]:
    rows = conn.execute(sql_text("""
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = :s AND table_name = :t
         ORDER BY ordinal_position
    """), {"s": schema, "t": table}).scalars().all()
    if not rows:
        raise RuntimeError(f"table {schema}.{table} has no columns or doesn't exist")
    return list(rows)


def _resolve_job_id(conn, task_id: str) -> str | None:
    """Look up job_id from video_analysis_jobs. For T5 this equals task_id."""
    row = conn.execute(sql_text("""
        SELECT job_id FROM ml_analysis.video_analysis_jobs
         WHERE job_id = :t OR task_id = :t
         ORDER BY created_at DESC
         LIMIT 1
    """), {"t": task_id}).first()
    return row[0] if row else None


def _dump_table(conn, table: str, filter_col: str, filter_value: str,
                out_handle) -> int:
    """Stream filtered rows from `table` to `out_handle` as a COPY ... FROM
    STDIN block. Returns the number of bytes written for the data section.
    """
    schema, tname = table.split(".", 1)
    cols = _columns(conn, schema, tname)
    col_list = ", ".join(f'"{c}"' for c in cols)

    out_handle.write(f"\n-- {table} (filter: {filter_col} = '{filter_value}')\n".encode())
    out_handle.write(f"COPY {table} ({col_list}) FROM STDIN WITH (FORMAT csv);\n".encode())

    # Get the raw psycopg connection (psycopg3) for COPY support.
    # Cast the column to text in the WHERE clause so the same code path works
    # for TEXT task_id columns (e.g. bronze.*) and UUID task_id columns (e.g.
    # ml_analysis.serve_events). Snapshot is not a hot path — losing the
    # index on the column is fine.
    raw = conn.connection.driver_connection
    copy_sql = (
        f"COPY (SELECT {col_list} FROM {table} "
        f"WHERE {filter_col}::text = %s) "
        f"TO STDOUT WITH (FORMAT csv)"
    )
    bytes_written = 0
    with raw.cursor() as cur:
        with cur.copy(copy_sql, [str(filter_value)]) as cp:
            for chunk in cp:
                # psycopg3 returns memoryview chunks; convert to bytes
                b = bytes(chunk)
                out_handle.write(b)
                bytes_written += len(b)
    out_handle.write(b"\\.\n")
    return bytes_written


def _baseline_for_task(conn, task_id: str, model: str) -> dict:
    """Query silver.point_detail and return a baseline dict per spec §3."""
    model_filter = "AND COALESCE(model, 'sportai') = :model" if model != "both" else ""

    row_count_total = conn.execute(sql_text(f"""
        SELECT count(*) FROM silver.point_detail
         WHERE task_id::text = :t {model_filter}
    """), {"t": task_id, "model": model}).scalar() or 0

    row_count_active = conn.execute(sql_text(f"""
        SELECT count(*) FROM silver.point_detail
         WHERE task_id::text = :t {model_filter}
           AND (exclude_d IS NULL OR exclude_d = FALSE)
    """), {"t": task_id, "model": model}).scalar() or 0

    serve_count_active = conn.execute(sql_text(f"""
        SELECT count(*) FROM silver.point_detail
         WHERE task_id::text = :t {model_filter}
           AND (exclude_d IS NULL OR exclude_d = FALSE)
           AND serve_d = TRUE
    """), {"t": task_id, "model": model}).scalar() or 0

    serve_count_total = conn.execute(sql_text(f"""
        SELECT count(*) FROM silver.point_detail
         WHERE task_id::text = :t {model_filter}
           AND serve_d = TRUE
    """), {"t": task_id, "model": model}).scalar() or 0

    def _dist(col: str, active_only: bool) -> dict:
        gate = "AND (exclude_d IS NULL OR exclude_d = FALSE)" if active_only else ""
        rows = conn.execute(sql_text(f"""
            SELECT {col} AS k, count(*) AS n
              FROM silver.point_detail
             WHERE task_id::text = :t {model_filter} {gate}
             GROUP BY {col}
        """), {"t": task_id, "model": model}).mappings().all()
        return {(r["k"] or "<null>"): int(r["n"]) for r in rows}

    serve_ts = conn.execute(sql_text(f"""
        SELECT MIN(ball_hit_s) AS first_s, MAX(ball_hit_s) AS last_s
          FROM silver.point_detail
         WHERE task_id::text = :t {model_filter}
           AND serve_d = TRUE
    """), {"t": task_id, "model": model}).mappings().first() or {}

    return {
        "row_count_total": int(row_count_total),
        "row_count_active": int(row_count_active),
        "row_count_excluded": int(row_count_total) - int(row_count_active),
        "serve_count_active": int(serve_count_active),
        "serve_count_total": int(serve_count_total),
        "stroke_distribution_active": _dist("stroke_d", active_only=True),
        "outcome_distribution": _dist("shot_outcome_d", active_only=False),
        "depth_distribution_active": _dist("depth_d", active_only=True),
        "first_serve_ts_s": (
            float(serve_ts["first_s"]) if serve_ts.get("first_s") is not None else None
        ),
        "last_serve_ts_s": (
            float(serve_ts["last_s"]) if serve_ts.get("last_s") is not None else None
        ),
    }


def _current_commit() -> str:
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        )
        return out.strip()
    except Exception:
        return "unknown"


def snapshot_fixture(
    task_id: str,
    output_dir: str = "ml_pipeline/fixtures_silver",
    model: str = "t5",
    engine=None,
) -> dict:
    """Capture bronze + ml_analysis fixture and silver baseline for one task.

    Returns a dict with paths + summary counts.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    short = task_id[:8]
    bronze_path = out_dir / f"{short}_bronze.sql.gz"
    baseline_path = out_dir / f"{short}_silver_baseline.json"

    if engine is None:
        engine = _get_engine()

    with engine.connect() as conn:
        job_id = _resolve_job_id(conn, task_id)
        if not job_id:
            raise RuntimeError(
                f"no ml_analysis.video_analysis_jobs row for task_id={task_id} — "
                "snapshot requires the T5 job to exist"
            )
        logger.info("snapshot task_id=%s resolved job_id=%s", task_id, job_id)

        total_bytes = 0
        # Write the gzipped SQL fixture
        with gzip.open(bronze_path, "wb") as gz:
            gz.write(
                f"-- silver bench fixture for task_id={task_id} (job_id={job_id})\n"
                f"-- captured by ml_pipeline.diag.bench_silver.snapshot\n"
                f"SET session_replication_role = 'replica';  -- skip triggers / FKs during restore\n".encode()
            )

            for table, filter_col in SNAPSHOT_TABLES_BRONZE:
                filter_value = task_id
                n = _dump_table(conn, table, filter_col, filter_value, gz)
                total_bytes += n
                logger.info("  dumped %s: %d bytes", table, n)

            for table, filter_col in SNAPSHOT_TABLES_ML:
                # ml_analysis.* uses job_id, except serve_events which is task_id
                filter_value = job_id if filter_col == "job_id" else task_id
                n = _dump_table(conn, table, filter_col, filter_value, gz)
                total_bytes += n
                logger.info("  dumped %s: %d bytes", table, n)

            gz.write(b"\nSET session_replication_role = 'origin';\n")

        # Capture silver baseline JSON
        expected = _baseline_for_task(conn, task_id, model)

    baseline = {
        "task_id": task_id,
        "job_id": job_id,
        "captured_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "captured_after_commit": _current_commit(),
        "model": model,
        "expected": expected,
        "tolerance": {
            "row_count_total": {"abs": 0, "comment": "exact — silver builder is deterministic"},
            "row_count_active": {"abs": 0},
            "serve_count_active": {"abs": 0},
            "stroke_distribution_active": {"per_class_pct": 5},
        },
    }
    baseline_path.write_text(json.dumps(baseline, indent=2))

    bronze_size = bronze_path.stat().st_size
    logger.info(
        "wrote %s (%d bytes gzipped, %d bytes uncompressed data)",
        bronze_path, bronze_size, total_bytes,
    )
    logger.info("wrote %s (%d silver rows total, %d active)",
                baseline_path, expected["row_count_total"], expected["row_count_active"])

    return {
        "task_id": task_id,
        "bronze_path": str(bronze_path),
        "baseline_path": str(baseline_path),
        "bronze_size_bytes": bronze_size,
        "silver_row_count": expected["row_count_total"],
    }


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

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
    ap.add_argument(
        "--db-url", default=None,
        help="Override DATABASE_URL (rarely needed)",
    )
    args = ap.parse_args(argv)

    engine = _get_engine(args.db_url)
    try:
        result = snapshot_fixture(
            task_id=args.task,
            output_dir=args.output_dir,
            model=args.model,
            engine=engine,
        )
    except Exception as e:
        logger.error("snapshot failed: %s", e)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
