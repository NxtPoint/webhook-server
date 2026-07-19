"""Compare silver.point_detail between environments, or across a code change.

Two jobs:

1. `--against-prod` — prove the local rebuild reproduces production exactly.
   Until that passes, no local before/after result means anything.
2. `--save` / `--vs` — fingerprint local silver before a fix, then diff after.
   This is the gate for the audit's batches 3-4, which rewrite derived values:
   the diff must show ONLY the fields the fix was meant to change.

Usage
-----
    SEED_SOURCE_URL=$(cat devenv/.env.local) python -m devenv.diff_silver \
        --task <uuid> --against-prod

    python -m devenv.diff_silver --task <uuid> --save .claude/tmp/before.json
    #   ... apply a fix, rebuild silver ...
    python -m devenv.diff_silver --task <uuid> --vs .claude/tmp/before.json

Comparison is per-column over rows matched on `id`, so a changed value is
attributed to the exact field that changed rather than showing up as a wholesale
row difference.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from sqlalchemy import create_engine, text as sql_text

LOCAL_URL = "postgresql+psycopg://tf:tf@localhost:55433/tf_dev"

# Derived columns worth watching. Base facts (ids, timestamps, raw coords) are
# compared too, but these are the ones the audit's fixes are expected to move.
DERIVED = [
    "serve_d", "server_end_d", "serve_side_d", "serve_try_ix_in_point",
    "serve_location", "serve_bucket_d", "point_number", "game_number",
    "shot_ix_in_point", "shot_phase_d", "shot_outcome_d", "ace_d",
    "service_winner_d", "point_winner_player_id", "game_winner_player_id",
    "exclude_d", "rally_location_hit", "rally_location_bounce",
    "aggression_d", "depth_d", "stroke_d", "rally_length", "rally_length_point",
]


def _normalise(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def fetch(engine, task_id: str) -> dict[str, dict]:
    with engine.connect() as c:
        cols = [r[0] for r in c.execute(sql_text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='silver' AND table_name='point_detail'
            ORDER BY ordinal_position
        """)).fetchall()]
        if not cols:
            sys.exit("silver.point_detail not found on one side")
        collist = ", ".join(f'"{c}"' for c in cols)
        rows = c.execute(sql_text(
            f"SELECT {collist} FROM silver.point_detail "
            f"WHERE task_id = CAST(:t AS uuid) AND COALESCE(model,'sportai')='sportai'"
        ), {"t": task_id}).mappings().all()
    return {str(r["id"]): dict(r) for r in rows}


def summarise(rows: dict[str, dict]) -> dict:
    """Distribution fingerprint — survives id churn after a bronze re-ingest."""
    out = {"row_count": len(rows)}
    for col in DERIVED:
        vals = [r.get(col) for r in rows.values() if col in r]
        if not vals:
            continue
        out[col] = dict(Counter(str(v) for v in vals).most_common())
    return out


def compare(a: dict[str, dict], b: dict[str, dict], label_a: str, label_b: str) -> int:
    ids_a, ids_b = set(a), set(b)
    print(f"rows: {label_a}={len(ids_a)}  {label_b}={len(ids_b)}")
    only_a, only_b = ids_a - ids_b, ids_b - ids_a
    if only_a:
        print(f"  ! {len(only_a)} row(s) only in {label_a}")
    if only_b:
        print(f"  ! {len(only_b)} row(s) only in {label_b}")

    shared = ids_a & ids_b
    if not shared:
        print("  no shared ids - comparing distributions instead")
        sa, sb = summarise(a), summarise(b)
        diffs = [k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k)]
        for k in sorted(diffs):
            print(f"  {k}:\n    {label_a}: {sa.get(k)}\n    {label_b}: {sb.get(k)}")
        return 1 if diffs else 0

    cols = sorted(set().union(*(set(r) for r in list(a.values())[:1] or [{}])))
    mismatches: Counter = Counter()
    examples: dict[str, tuple] = {}
    for i in shared:
        for col in cols:
            va, vb = a[i].get(col), b[i].get(col)
            if va != vb:
                mismatches[col] += 1
                examples.setdefault(col, (i, va, vb))

    if not mismatches and not only_a and not only_b:
        print("  IDENTICAL - every column matches on every shared row")
        return 0

    print(f"\n  {len(mismatches)} column(s) differ across {len(shared)} shared rows:")
    for col, n in mismatches.most_common():
        i, va, vb = examples[col]
        pct = 100.0 * n / len(shared)
        print(f"    {col:26} {n:5} rows ({pct:5.1f}%)  e.g. id={i}: {label_a}={va!r} {label_b}={vb!r}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--local-url", default=os.getenv("SEED_TARGET_URL", LOCAL_URL))
    ap.add_argument("--against-prod", action="store_true")
    ap.add_argument("--save")
    ap.add_argument("--vs")
    args = ap.parse_args()

    local = create_engine(_normalise(args.local_url), pool_pre_ping=True)
    rows = fetch(local, args.task)

    if args.against_prod:
        src = os.getenv("SEED_SOURCE_URL")
        if not src:
            sys.exit("SEED_SOURCE_URL required for --against-prod")
        prod = create_engine(_normalise(src), pool_pre_ping=True)
        return compare(fetch(prod, args.task), rows, "prod", "local")

    if args.save:
        os.makedirs(os.path.dirname(args.save) or ".", exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(summarise(rows), f, indent=2, default=str)
        print(f"saved fingerprint of {len(rows)} rows -> {args.save}")
        return 0

    if args.vs:
        with open(args.vs, encoding="utf-8") as f:
            before = json.load(f)
        after = summarise(rows)
        keys = sorted(set(before) | set(after))
        changed = [k for k in keys if before.get(k) != after.get(k)]
        if not changed:
            print("no change vs baseline")
            return 0
        print(f"{len(changed)} field(s) changed vs baseline:")
        for k in changed:
            print(f"\n  {k}\n    before: {before.get(k)}\n    after : {after.get(k)}")
        return 1

    print(json.dumps(summarise(rows), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
