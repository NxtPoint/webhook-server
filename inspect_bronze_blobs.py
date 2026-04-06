# inspect_bronze_blobs.py
# Utility to discover the structure of unextracted JSONB blobs in bronze.
# Usage:
#   python inspect_bronze_blobs.py <task_id>
#   python inspect_bronze_blobs.py <task_id> --table team_session
#   python inspect_bronze_blobs.py --latest

import json
import sys
from typing import Any, Dict, Optional

from sqlalchemy import text
from db_init import engine


def _summarize(val: Any, depth: int = 0, max_depth: int = 2) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return f"bool ({val})"
    if isinstance(val, (int, float)):
        return f"{type(val).__name__} ({val})"
    if isinstance(val, str):
        return f"str (len={len(val)}) {val[:80]!r}" if len(val) > 0 else "str (empty)"
    if isinstance(val, list):
        if not val:
            return "list (empty)"
        sample = _summarize(val[0], depth + 1, max_depth) if depth < max_depth else "..."
        return f"list (len={len(val)}) [{sample}, ...]"
    if isinstance(val, dict):
        if depth >= max_depth:
            return f"dict ({len(val)} keys)"
        inner = []
        for k, v in list(val.items())[:15]:
            inner.append(f"  {'  ' * depth}{k!r}: {_summarize(v, depth + 1, max_depth)}")
        if len(val) > 15:
            inner.append(f"  {'  ' * depth}... +{len(val) - 15} more keys")
        return "dict {\n" + "\n".join(inner) + "\n" + "  " * depth + "}"
    return type(val).__name__


# Singleton tables (task_id PK, data JSONB)
SINGLETONS = [
    "session_confidences",
    "thumbnail",
    "highlight",
    "team_session",
    "bounce_heatmap",
    "submission_context",
]

# Array tables where we inspect per-row data + annotations
ARRAY_SAMPLES = [
    ("player_swing", "annotations"),
    ("player_swing", "rally"),
    ("player_swing", "ball_trajectory"),
    ("player_swing", "ball_impact_location"),
]


def inspect_singletons(task_id: str, table_filter: Optional[str] = None):
    with engine.connect() as conn:
        for tbl in SINGLETONS:
            if table_filter and tbl != table_filter:
                continue

            row = conn.execute(
                text(f"SELECT data FROM bronze.{tbl} WHERE task_id = :tid LIMIT 1"),
                {"tid": task_id},
            ).mappings().first()

            print(f"\n{'='*60}")
            print(f"bronze.{tbl}  task_id={task_id}")
            print("=" * 60)

            if not row or row["data"] is None:
                print("  (no data)")
                continue

            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)

            print(_summarize(data))


def inspect_array_blobs(task_id: str):
    with engine.connect() as conn:
        for tbl, col in ARRAY_SAMPLES:
            row = conn.execute(
                text(f"""
                    SELECT {col}
                    FROM bronze.{tbl}
                    WHERE task_id = :tid AND {col} IS NOT NULL
                    LIMIT 1
                """),
                {"tid": task_id},
            ).mappings().first()

            print(f"\n{'='*60}")
            print(f"bronze.{tbl}.{col}  task_id={task_id}")
            print("=" * 60)

            if not row or row[col] is None:
                print("  (no data)")
                continue

            data = row[col]
            if isinstance(data, str):
                data = json.loads(data)

            print(_summarize(data))


def get_latest_task_id() -> Optional[str]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT task_id FROM bronze.session ORDER BY created_at DESC LIMIT 1")
        ).first()
        return row[0] if row else None


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Inspect bronze JSONB blob structures")
    p.add_argument("task_id", nargs="?", help="task UUID (omit with --latest)")
    p.add_argument("--latest", action="store_true", help="use most recent task_id")
    p.add_argument("--table", help="inspect only this singleton table")
    args = p.parse_args()

    tid = args.task_id
    if args.latest:
        tid = get_latest_task_id()
        if not tid:
            print("No tasks found in bronze.session")
            sys.exit(1)
        print(f"Using latest task_id: {tid}")

    if not tid:
        print("Usage: python inspect_bronze_blobs.py <task_id> [--table <name>]")
        print("       python inspect_bronze_blobs.py --latest")
        sys.exit(1)

    inspect_singletons(tid, table_filter=args.table)
    if not args.table:
        inspect_array_blobs(tid)
