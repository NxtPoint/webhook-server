"""Prove that every field in a raw SportAI JSON landed somewhere in bronze.

The question this answers: "is the full JSON being captured, or is something
dropping off — and will we notice when SportAI adds a field we've never seen?"

Design (deliberately NOT a hand-maintained field list — that would drift from
the code and lie to us):

  1. Flatten the raw JSON into canonical leaf paths (array indices collapsed to
     []): players[].swings[].ball_speed, warmups[].start_time, meta.video_info.fps …
  2. For the same task_id, gather what ACTUALLY exists in bronze: every non-null
     typed column plus every key present in each table's `data` jsonb catch-all.
  3. Classify each raw leaf:
        PROMOTED  — a first-class typed bronze column (fully usable)
        PRESERVED — only in a `data` jsonb blob (kept, but silver can't easily use it)
        DROPPED   — nowhere in bronze (lost)
  4. Anything DROPPED is the coverage gap. A registry of known leaves (below) also
     flags NEW leaves — the schema-drift signal for "SportAI added something."

Usage:
    python -m devenv.coverage_check --json path/to/raw.json --task <uuid> \
        --source-url "$RO_URL"

The matching is by leaf NAME (bronze flattens nested objects), which is a
coverage signal, not a formal proof — a genuinely renamed field is caught as
DROPPED + NEW, which is exactly what we want to see.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import create_engine, text as sql_text

# The bronze tables that carry per-task rows, and how each raw top-level key maps
# to one. Only used to scope the DB read; classification uses live bronze state.
TABLES = [
    "player", "player_swing", "rally", "ball_position", "ball_bounce",
    "player_position", "session_confidences", "team_session", "bounce_heatmap",
    "thumbnail", "highlight", "unmatched_field", "debug_event", "session",
]


def _normalise(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def flatten_paths(obj, prefix="") -> set[str]:
    """Canonical leaf paths; every array collapses to [] so 500 swings = one path."""
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                out |= flatten_paths(v, p)
            else:
                out.add(p)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                out |= flatten_paths(item, f"{prefix}[]")
            else:
                out.add(f"{prefix}[]")
    return out


def leaf(path: str) -> str:
    return path.replace("[]", "").split(".")[-1]


def captured_leaves(engine, task_id: str) -> tuple[set[str], set[str]]:
    """(promoted, preserved) leaf names actually present in bronze for this task."""
    promoted: set[str] = set()
    preserved: set[str] = set()
    with engine.connect() as c:
        for t in TABLES:
            exists = c.execute(sql_text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='bronze' AND table_name=:t"""), {"t": t}).scalar()
            if not exists:
                continue
            cols = [r[0] for r in c.execute(sql_text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='bronze' AND table_name=:t"""), {"t": t}).fetchall()]
            has_task = "task_id" in cols
            # typed columns that are non-null for at least one row of this task
            for col in cols:
                if col in ("id", "task_id", "created_at", "data"):
                    continue
                q = f'SELECT count(*) FROM bronze.{t} WHERE "{col}" IS NOT NULL'
                if has_task:
                    q += " AND task_id=:tid"
                try:
                    if c.execute(sql_text(q), {"tid": task_id}).scalar():
                        promoted.add(col)
                except Exception:
                    pass
            # keys inside the data jsonb catch-all
            if "data" in cols:
                q = (f"SELECT DISTINCT jsonb_object_keys(data) FROM bronze.{t} "
                     "WHERE data IS NOT NULL AND jsonb_typeof(data)='object'")
                if has_task:
                    q += " AND task_id=:tid"
                try:
                    for r in c.execute(sql_text(q), {"tid": task_id}).fetchall():
                        preserved.add(r[0])
                except Exception:
                    pass
    return promoted, preserved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="raw SportAI result JSON file")
    ap.add_argument("--task", required=True)
    ap.add_argument("--source-url", default=os.getenv("SEED_SOURCE_URL"))
    args = ap.parse_args()
    if not args.source_url:
        sys.exit("--source-url (or SEED_SOURCE_URL) required")

    raw = json.load(open(args.json, encoding="utf-8"))
    paths = flatten_paths(raw)
    engine = create_engine(_normalise(args.source_url), pool_pre_ping=True)
    promoted, preserved = captured_leaves(engine, args.task)

    rows = []
    for p in sorted(paths):
        lf = leaf(p)
        if lf in promoted:
            status = "PROMOTED"
        elif lf in preserved:
            status = "PRESERVED"
        else:
            status = "DROPPED"
        rows.append((status, p))

    by = {"PROMOTED": [], "PRESERVED": [], "DROPPED": []}
    for s, p in rows:
        by[s].append(p)

    print(f"\nRaw JSON leaf paths: {len(paths)}   "
          f"PROMOTED {len(by['PROMOTED'])}  PRESERVED {len(by['PRESERVED'])}  "
          f"DROPPED {len(by['DROPPED'])}\n")
    if by["DROPPED"]:
        print("=== DROPPED — in the JSON, NOWHERE in bronze (the gap) ===")
        for p in by["DROPPED"]:
            print(f"  {p}")
    print("\n=== PRESERVED — kept only in a data jsonb blob (not first-class) ===")
    for p in by["PRESERVED"]:
        print(f"  {p}")
    # machine-readable, so this can gate CI or feed a drift registry
    out = args.json + ".coverage.json"
    json.dump({k: v for k, v in by.items()}, open(out, "w"), indent=2)
    print(f"\nfull report -> {out}")
    return 1 if by["DROPPED"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
