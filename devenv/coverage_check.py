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

# SportAI top-level key -> the bronze table(s) that ingest it. Derived from
# ingest_bronze.py::ingest_bronze_strict. A raw top-level key that is NOT here is
# ingested NOWHERE -- that is both a coverage gap AND the schema-drift signal:
# when SportAI ships a new top-level key, it lands in neither this map nor bronze,
# so it surfaces immediately as a fully-dropped unknown.
TOPLEVEL_MAP = {
    "players":          ["player", "player_swing"],
    "ball_positions":   ["ball_position"],
    "ball_bounces":     ["ball_bounce"],
    "player_positions": ["player_position"],
    "confidences":      ["session_confidences"],
    "thumbnail_crops":  ["thumbnail"],
    "thumbnails":       ["thumbnail"],
    "highlights":       ["highlight"],
    "team_sessions":    ["team_session"],
    "bounce_heatmap":   ["bounce_heatmap"],
    "rallies":          ["rally"],
    "rally_events":     ["rally"],
    "unmatched":        ["unmatched_field"],
    "unmatched_fields": ["unmatched_field"],
    "debug_events":     ["debug_event"],
    "events_debug":     ["debug_event"],
}
# Known top-level keys SportAI sends that we deliberately/accidentally DON'T
# ingest. Listed so they are reported as DROPPED but not flagged as NEW drift.
KNOWN_NOT_INGESTED = {"meta", "debug_data", "warmups"}

TABLES = sorted({t for ts in TOPLEVEL_MAP.values() for t in ts})


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


def _jsonb_keys_recursive(c, table: str, has_task: bool, task_id: str) -> set[str]:
    """Every key nested anywhere in a table's `data` blob (object OR array), lowercased.

    Done in Python — a recursive SQL CTE can only self-reference once, so it can't
    descend both objects and arrays in one walk. flatten_paths handles both.
    """
    q = f"SELECT data FROM bronze.{table} WHERE data IS NOT NULL"
    if has_task:
        q += " AND task_id=:tid"
    keys: set[str] = set()
    try:
        for (blob,) in c.execute(sql_text(q), {"tid": task_id}).fetchall():
            if blob is None:
                continue
            for p in flatten_paths(blob):
                for seg in p.replace("[]", "").split("."):
                    if seg:
                        keys.add(seg.lower())
    except Exception:
        pass
    return keys


def captured_per_table(engine, task_id: str) -> dict:
    """{table: {"cols_all", "cols_nonnull", "data_keys"}} — all lowercased.

    Per-table so that classification only matches a raw key against the columns
    of the table that actually ingests it — avoiding cross-table name collisions
    (e.g. SportAI's top-level `meta` vs bronze `session.meta`).
    """
    out: dict = {}
    with engine.connect() as c:
        for t in TABLES:
            exists = c.execute(sql_text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='bronze' AND table_name=:t"""), {"t": t}).scalar()
            if not exists:
                out[t] = {"cols_all": set(), "cols_nonnull": set(), "data_keys": set()}
                continue
            cols = [r[0] for r in c.execute(sql_text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='bronze' AND table_name=:t"""), {"t": t}).fetchall()]
            has_task = "task_id" in cols
            cols_all, cols_nonnull = set(), set()
            for col in cols:
                if col in ("id", "task_id", "created_at", "data"):
                    continue
                cols_all.add(col.lower())
                q = f'SELECT count(*) FROM bronze.{t} WHERE "{col}" IS NOT NULL'
                if has_task:
                    q += " AND task_id=:tid"
                try:
                    if c.execute(sql_text(q), {"tid": task_id}).scalar():
                        cols_nonnull.add(col.lower())
                except Exception:
                    pass
            data_keys = _jsonb_keys_recursive(c, t, has_task, task_id) if "data" in cols else set()
            out[t] = {"cols_all": cols_all, "cols_nonnull": cols_nonnull, "data_keys": data_keys}
    return out


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
    per = captured_per_table(engine, args.task)

    def segs(p):
        return [s.lower() for s in p.replace("[]", "").split(".") if s]

    drift = set()  # top-level keys we've never seen in the map or known list
    rows = []
    for p in sorted(paths):
        top = p.split(".")[0].split("[")[0]
        tables = TOPLEVEL_MAP.get(top)
        if not tables:
            rows.append(("DROPPED", p, "  <NEW KEY>" if top not in KNOWN_NOT_INGESTED else ""))
            if top not in KNOWN_NOT_INGESTED:
                drift.add(top)
            continue
        # match this path's segments only against the table(s) that ingest `top`
        sg = segs(p)[1:]  # drop the top-level key itself
        cols_all = set().union(*(per[t]["cols_all"] for t in tables))
        cols_nn = set().union(*(per[t]["cols_nonnull"] for t in tables))
        data_keys = set().union(*(per[t]["data_keys"] for t in tables))
        prom = next((s for s in sg if s in cols_all), None)
        if prom:
            note = "  (column exists but NULL here)" if prom not in cols_nn else ""
            rows.append(("PROMOTED", p, note))
        elif any(s in data_keys for s in sg) or not sg:
            rows.append(("PRESERVED", p, ""))
        else:
            rows.append(("DROPPED", p, ""))

    if drift:
        print(f"\n*** SCHEMA DRIFT: {len(drift)} NEW top-level key(s) not in the map: {sorted(drift)} ***")

    by = {"PROMOTED": [], "PRESERVED": [], "DROPPED": []}
    for s, p, note in rows:
        by[s].append(p + note)

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
