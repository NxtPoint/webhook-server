"""Seed the local dev Postgres with real bronze rows for a handful of tasks.

Why this exists
---------------
Batches 3+ of the 2026-07-19 pipeline audit change silver DERIVATION logic
(serve legality, court geometry, zone bounds). Validating those changes means
rebuilding silver repeatedly and diffing every derived field before/after on
REAL matches -- which you cannot do in prod at any level of care. So we pull a
few real tasks into a disposable local database and iterate there.

Safety model
------------
- The SOURCE connection is only ever read from. Pass a READ-ONLY Postgres role
  (`GRANT SELECT` on bronze.*); then read-only is enforced by the engine rather
  than by this script's good behaviour.
- The TARGET defaults to the local docker DB on port 55433 and the script
  REFUSES to run against anything that looks like prod (see _assert_local).

Usage
-----
    docker compose -f devenv/docker-compose.yml up -d

    # see what's available (read-only)
    python -m devenv.seed_local --source-url "$RO_URL" --list

    # seed: newest 3 SportAI matches, or name them explicitly
    python -m devenv.seed_local --source-url "$RO_URL" --newest 3
    python -m devenv.seed_local --source-url "$RO_URL" --task <uuid> --task <uuid>

Then build silver/gold locally against the seeded data:

    DATABASE_URL=postgresql+psycopg://tf:tf@localhost:55433/tf_dev \
        python -c "import build_silver_v2 as b; print(b.build_silver_v2('<task>', replace=True))"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterable

from sqlalchemy import create_engine, text as sql_text

LOCAL_URL = "postgresql+psycopg://tf:tf@localhost:55433/tf_dev"

# Bronze tables carrying per-task rows. Order matters only for readability --
# there are no FKs between them. `session` and `submission_context` are the
# parents everything else hangs off.
TASK_TABLES = [
    "submission_context",
    "session",
    "player",
    "player_swing",
    "rally",
    "ball_position",
    "ball_bounce",
    "player_position",
    "session_confidences",
    "team_session",
    "bounce_heatmap",
    "thumbnail",
    "highlight",
    "unmatched_field",
    "debug_event",
]


def _normalise(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _assert_local(url: str) -> None:
    """Refuse to write anywhere that isn't the local dev container."""
    bad = ("render.com", "amazonaws.com", "azure", "supabase")
    if any(b in url for b in bad):
        sys.exit(f"REFUSING to write to what looks like a hosted DB: {url.split('@')[-1]}")
    if "localhost" not in url and "127.0.0.1" not in url:
        sys.exit(f"REFUSING to write to a non-local target: {url.split('@')[-1]}")
    if ":55432/" in url:
        sys.exit("REFUSING: :55432 is the CourtFlow local DB, not this project's dev DB.")


def _writable_columns(conn, schema: str, table: str) -> dict[str, str]:
    """Writable columns -> udt_name, excluding GENERATED columns.

    Two things bite here:

    1. Postgres rejects any INSERT that supplies a value for a GENERATED ALWAYS
       column, and bronze has seven (ball_bounce.court_x/court_y/image_x/image_y
       and ball_position.x/y/timestamp).
    2. jsonb columns come back from SELECT as Python dicts/lists, which psycopg
       cannot adapt as bind parameters. We need the type to know which columns
       to re-serialise and CAST on the way in -- hence returning types, not just
       names.
    """
    rows = conn.execute(sql_text("""
        SELECT column_name, udt_name
        FROM information_schema.columns
        WHERE table_schema = :s AND table_name = :t
          AND is_generated <> 'ALWAYS'
        ORDER BY ordinal_position
    """), {"s": schema, "t": table}).fetchall()
    return {r[0]: r[1] for r in rows}


def _source_column_defs(conn, schema: str, table: str) -> list[tuple[str, str]]:
    """(name, rendered type) for real, non-generated columns, from pg_catalog.

    `format_type` gives the exact declared type (numeric(5,1), timestamptz, …)
    which information_schema.data_type flattens.
    """
    rows = conn.execute(sql_text("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c     ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :s AND c.relname = :t
          AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = ''
        ORDER BY a.attnum
    """), {"s": schema, "t": table}).fetchall()
    return [(r[0], r[1]) for r in rows]


def _sync_missing_columns(src_conn, tgt_conn, table: str) -> list[str]:
    """Add columns the source has and the target lacks.

    Bronze schema is managed idempotently across several _init/_ensure_*
    functions (db_init.bronze_init, ingest_worker_app._ensure_schema,
    upload_app), so no single call reproduces production's real shape --
    submission_context's set-score columns are the case that caught us.
    Mirroring the source is the only way to be faithful.
    """
    # Existence must be tested against ALL columns, not just writable ones:
    # a column can exist on the target as GENERATED (so it is absent from
    # _writable_columns) while existing on the source as a plain column. That
    # is exactly the ball_position.x/y/timestamp divergence, and testing the
    # writable set would report a phantom "added" for a no-op ALTER.
    tgt = {r[0] for r in tgt_conn.execute(sql_text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'bronze' AND table_name = :t
    """), {"t": table}).fetchall()}
    added = []
    for name, typ in _source_column_defs(src_conn, "bronze", table):
        if name not in tgt:
            tgt_conn.execute(sql_text(
                f'ALTER TABLE bronze.{table} ADD COLUMN IF NOT EXISTS "{name}" {typ}'
            ))
            added.append(name)
    return added


def _table_exists(conn, schema: str, table: str) -> bool:
    return bool(conn.execute(sql_text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = :s AND table_name = :t
    """), {"s": schema, "t": table}).scalar())


def list_candidates(src, limit: int) -> None:
    with src.connect() as c:
        rows = c.execute(sql_text("""
            SELECT sc.task_id, sc.sport_type, sc.match_date,
                   COUNT(DISTINCT ps.id)  AS swings,
                   COUNT(DISTINCT bb.id)  AS bounces
            FROM bronze.submission_context sc
            LEFT JOIN bronze.player_swing ps ON ps.task_id = sc.task_id
            LEFT JOIN bronze.ball_bounce  bb ON bb.task_id = sc.task_id
            WHERE sc.deleted_at IS NULL
            GROUP BY sc.task_id, sc.sport_type, sc.match_date
            HAVING COUNT(DISTINCT ps.id) > 0
            ORDER BY sc.match_date DESC NULLS LAST
            LIMIT :n
        """), {"n": limit}).fetchall()
    if not rows:
        print("no candidate tasks found")
        return
    print(f"{'task_id':38} {'sport_type':22} {'date':12} {'swings':>7} {'bounces':>8}")
    for r in rows:
        print(f"{str(r[0]):38} {str(r[1] or ''):22} {str(r[2] or ''):12} {r[3]:>7} {r[4]:>8}")


def pick_newest(src, n: int, sport_type: str) -> list[str]:
    with src.connect() as c:
        rows = c.execute(sql_text("""
            SELECT sc.task_id
            FROM bronze.submission_context sc
            JOIN bronze.player_swing ps ON ps.task_id = sc.task_id
            WHERE sc.deleted_at IS NULL AND sc.sport_type = :st
            GROUP BY sc.task_id, sc.match_date
            HAVING COUNT(ps.id) > 20
            ORDER BY sc.match_date DESC NULLS LAST
            LIMIT :n
        """), {"n": n, "st": sport_type}).fetchall()
    return [str(r[0]) for r in rows]


def copy_task(src, tgt, task_id: str, batch: int = 2000) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TASK_TABLES:
        with src.connect() as sc, tgt.begin() as tc:
            if not (_table_exists(sc, "bronze", table) and _table_exists(tc, "bronze", table)):
                continue
            added = _sync_missing_columns(sc, tc, table)
            if added:
                print(f"  [schema] bronze.{table}: added {len(added)} missing column(s): "
                      f"{', '.join(added[:6])}{' …' if len(added) > 6 else ''}")

            src_cols = _writable_columns(sc, "bronze", table)
            tgt_cols = _writable_columns(tc, "bronze", table)
            cols = [c for c in tgt_cols if c in src_cols]
            if not cols:
                continue

            json_cols = {c for c in cols if tgt_cols[c] in ("json", "jsonb")}

            collist = ", ".join(f'"{c}"' for c in cols)
            rows = sc.execute(
                sql_text(f'SELECT {collist} FROM bronze.{table} WHERE task_id = :t'),
                {"t": task_id},
            ).mappings().all()
            if not rows:
                counts[table] = 0
                continue

            tc.execute(sql_text(f"DELETE FROM bronze.{table} WHERE task_id = :t"), {"t": task_id})
            # jsonb needs an explicit CAST: the driver sees a dict/list and has
            # no way to know it is destined for a json column.
            placeholders = ", ".join(
                (f"CAST(:{c} AS JSONB)" if c in json_cols else f":{c}") for c in cols
            )
            ins = sql_text(f'INSERT INTO bronze.{table} ({collist}) VALUES ({placeholders})')

            def _prep(r):
                d = dict(r)
                for c in json_cols:
                    v = d.get(c)
                    if v is not None and not isinstance(v, str):
                        d[c] = json.dumps(v)
                return d

            for i in range(0, len(rows), batch):
                tc.execute(ins, [_prep(r) for r in rows[i:i + batch]])
            counts[table] = len(rows)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-url", default=os.getenv("SEED_SOURCE_URL"),
                    help="READ-ONLY prod connection string (or set SEED_SOURCE_URL)")
    ap.add_argument("--target-url", default=os.getenv("SEED_TARGET_URL", LOCAL_URL))
    ap.add_argument("--task", action="append", default=[], help="task_id to copy (repeatable)")
    ap.add_argument("--newest", type=int, default=0, help="copy the newest N matches")
    ap.add_argument("--sport-type", default="tennis_singles")
    ap.add_argument("--list", action="store_true", help="list candidate tasks and exit")
    args = ap.parse_args()

    if not args.source_url:
        sys.exit("--source-url (or SEED_SOURCE_URL) is required")

    source_url = _normalise(args.source_url)
    target_url = _normalise(args.target_url)
    _assert_local(target_url)

    src = create_engine(source_url, pool_pre_ping=True)
    if args.list:
        list_candidates(src, 40)
        return 0

    # Create the bronze schema on the target using the repo's OWN init, so the
    # local shape can never drift from production's.
    os.environ["DATABASE_URL"] = target_url
    for mod in list(sys.modules):
        if mod in ("db_init",):
            del sys.modules[mod]
    import db_init  # noqa: E402  (import AFTER DATABASE_URL is pointed at the target)
    db_init.bronze_init()
    print(f"bronze schema ready on {target_url.split('@')[-1]}")

    # build_silver_v2 pass 3 reads ml_analysis.video_analysis_jobs even for
    # SportAI tasks (it resolves fps there, and simply finds no row). The table
    # must therefore EXIST locally or every build dies on an UndefinedTable.
    try:
        import ml_pipeline.db_schema as t5_schema  # noqa: E402
        t5_schema.ml_analysis_init()
        print("ml_analysis schema ready (needed by build_silver_v2 pass 3)")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: ml_analysis_init failed ({type(e).__name__}: {e}); "
              "silver builds will fail until ml_analysis.video_analysis_jobs exists")

    tgt = create_engine(target_url, pool_pre_ping=True)

    tasks = list(args.task)
    if args.newest:
        tasks += pick_newest(src, args.newest, args.sport_type)
    tasks = list(dict.fromkeys(tasks))
    if not tasks:
        sys.exit("nothing to do: pass --task and/or --newest (or --list to browse)")

    total = 0
    for t in tasks:
        counts = copy_task(src, tgt, t)
        n = sum(counts.values())
        total += n
        detail = "  ".join(f"{k}={v}" for k, v in counts.items() if v)
        print(f"\n{t}\n  {detail or '(no rows)'}\n  -> {n} rows")

    print(f"\nseeded {len(tasks)} task(s), {total} rows total")
    print("\nnext:")
    print(f'  set DATABASE_URL={target_url}')
    print('  python -c "import build_silver_v2 as b; print(b.build_silver_v2(\'<task>\', replace=True))"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
