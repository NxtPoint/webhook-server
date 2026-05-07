"""Point-completeness reconciler: SportAI truth vs T5 silver, per stroke.

For each SportAI point N (grouped by `point_number`), find the T5 strokes
whose `ts` falls within `[min_sa_ts - 0.5s, max_sa_ts + 0.5s]` (the SA
point's time span padded by 0.5s either side). Then per SA stroke, decide:

  match    — T5 has a stroke at the same time (±0.5s) with same `stroke_d`
  partial  — T5 has a stroke at the same time but different `stroke_d`
  missing  — no T5 stroke within ±0.5s of this SA stroke

Per-SA-point verdict:

  full_match — every SA stroke in the point is `match`
  partial    — at least one match, at least one missing/partial
  missing    — no SA stroke matches anything in T5

Single summary number = `full_match` count / total SA points.

This is the Phase 4 metric on the North Star ladder. SA truth is canonical:
we DO NOT try to detect T5 point boundaries here. The T5 strokes are
matched to SA point time spans only.

CLI:
    python -m ml_pipeline.diag.audit_points_reconcile

Defaults to the a798eff0 ↔ 2c1ad953 fixture pair. Override with
`--task <T5_TID> --sa <SA_TID>` for future fixtures.

The summary verdict counts and the headline X/Y are also written to a
baseline file (`points_reconcile_baseline.json`) when `--update-baseline`
is passed, alongside `bench_baseline.json`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine, text as sql_text


DEFAULT_T5 = "a798eff0-551f-4b5a-838f-7933866a727c"
DEFAULT_SA = "2c1ad953-b65b-41b4-9999-975964ff92e1"

BASELINE_PATH = Path("ml_pipeline/diag/points_reconcile_baseline.json")

# Time tolerance (s) for matching a T5 stroke to an SA stroke.
STROKE_TOL_S = 0.5
# Padding (s) added to either side of the SA point's time span when
# pulling T5 strokes — covers small timing offsets at point edges.
POINT_PAD_S = 0.5


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psyc" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _load_sa(conn, sa_tid: str) -> list[dict]:
    rows = conn.execute(sql_text("""
        SELECT point_number, ball_hit_s AS ts, player_id,
               COALESCE(stroke_d, '(null)') AS stroke,
               COALESCE(serve_d, FALSE) AS serve_d,
               shot_ix_in_point
        FROM silver.point_detail
        WHERE task_id = CAST(:t AS uuid)
          AND model = 'sportai'
          AND point_number IS NOT NULL
          AND ball_hit_s IS NOT NULL
        ORDER BY point_number, ball_hit_s, id
    """), {"t": sa_tid}).mappings().all()
    return [dict(r) for r in rows]


def _load_t5(conn, t5_tid: str, honor_exclude: bool = False) -> list[dict]:
    extra = "AND COALESCE(exclude_d, FALSE) = FALSE" if honor_exclude else ""
    rows = conn.execute(sql_text(f"""
        SELECT id, ball_hit_s AS ts, player_id,
               COALESCE(stroke_d, '(null)') AS stroke,
               COALESCE(serve_d, FALSE) AS serve_d
        FROM silver.point_detail
        WHERE task_id = CAST(:t AS uuid)
          AND model = 't5'
          AND ball_hit_s IS NOT NULL
          {extra}
        ORDER BY ball_hit_s, id
    """), {"t": t5_tid}).mappings().all()
    return [dict(r) for r in rows]


def _classify_sa_stroke(sa: dict, t5_in_window: list[dict]) -> tuple[str, dict | None]:
    """Return (verdict, matched_t5_row_or_None).

    verdict in {match, partial, missing}.
    Greedy nearest-in-time pick of a T5 stroke within ±STROKE_TOL_S; if
    same `stroke` class → match, else → partial. If nothing in window →
    missing.
    """
    sa_ts = sa["ts"]
    candidates = [t for t in t5_in_window
                  if abs(t["ts"] - sa_ts) <= STROKE_TOL_S
                  and not t.get("_consumed")]
    if not candidates:
        return ("missing", None)
    candidates.sort(key=lambda t: abs(t["ts"] - sa_ts))
    chosen = candidates[0]
    chosen["_consumed"] = True
    if chosen["stroke"] == sa["stroke"]:
        return ("match", chosen)
    return ("partial", chosen)


def reconcile(sa_rows: list[dict], t5_rows: list[dict]) -> dict:
    """Group SA by point_number; classify each SA stroke against T5.

    Returns:
      {
        "per_point": [ {point_number, sa_count, t5_count_in_window,
                         verdict, stroke_verdicts: [...],
                         top_failure_mode } ],
        "summary": { full_match, partial, missing, total_points,
                     stroke_match, stroke_partial, stroke_missing,
                     stroke_total },
        "extras": int,            # T5 strokes outside any SA point window
      }
    """
    # Bucket SA rows by point_number, preserving order
    by_point: dict[int, list[dict]] = {}
    for r in sa_rows:
        by_point.setdefault(r["point_number"], []).append(r)

    per_point = []
    stroke_match = stroke_partial = stroke_missing = 0
    full_match_pts = partial_pts = missing_pts = 0
    consumed_t5_ids: set = set()

    # Track which T5 rows fall into any SA point window (for extras count)
    t5_in_any_window: set = set()

    for pn, sa_strokes in sorted(by_point.items()):
        ts_min = min(r["ts"] for r in sa_strokes) - POINT_PAD_S
        ts_max = max(r["ts"] for r in sa_strokes) + POINT_PAD_S

        # Get T5 strokes whose ts falls in this SA point's time span.
        # A fresh window per point — reset _consumed flags.
        t5_window = []
        for t in t5_rows:
            if ts_min <= t["ts"] <= ts_max:
                t5_in_any_window.add(t["id"])
                # Make a shallow per-point copy so consumption is local
                t5_window.append({**t, "_consumed": False})

        verdicts = []
        per_stroke_failures: list[str] = []
        for sa in sa_strokes:
            v, matched = _classify_sa_stroke(sa, t5_window)
            verdicts.append({
                "sa_ts": sa["ts"],
                "sa_stroke": sa["stroke"],
                "verdict": v,
                "t5_ts": matched["ts"] if matched else None,
                "t5_stroke": matched["stroke"] if matched else None,
            })
            if v == "match":
                stroke_match += 1
            elif v == "partial":
                stroke_partial += 1
                per_stroke_failures.append(
                    f"{sa['stroke']}->{matched['stroke']}"
                )
            else:
                stroke_missing += 1
                per_stroke_failures.append(f"miss:{sa['stroke']}")

        # Per-point verdict
        if all(v["verdict"] == "match" for v in verdicts):
            point_verdict = "full_match"
            full_match_pts += 1
        elif any(v["verdict"] == "match" for v in verdicts):
            point_verdict = "partial"
            partial_pts += 1
        else:
            point_verdict = "missing"
            missing_pts += 1

        # Top failure mode
        if per_stroke_failures:
            top = Counter(per_stroke_failures).most_common(1)[0][0]
        else:
            top = "-"

        per_point.append({
            "point_number": pn,
            "sa_count": len(sa_strokes),
            "t5_in_window": len(t5_window),
            "verdict": point_verdict,
            "top_failure": top,
            "stroke_verdicts": verdicts,
        })

    extras = len(t5_rows) - len(t5_in_any_window)

    return {
        "per_point": per_point,
        "summary": {
            "full_match": full_match_pts,
            "partial": partial_pts,
            "missing": missing_pts,
            "total_points": len(by_point),
            "stroke_match": stroke_match,
            "stroke_partial": stroke_partial,
            "stroke_missing": stroke_missing,
            "stroke_total": stroke_match + stroke_partial + stroke_missing,
        },
        "extras": extras,
        "t5_total": len(t5_rows),
    }


def _print_report(result: dict, *, sa_tid: str, t5_tid: str) -> None:
    print("=== audit_points_reconcile ===")
    print(f"  SA (truth):  {sa_tid}")
    print(f"  T5 (test):   {t5_tid}")
    print(f"  stroke tol:  +/-{STROKE_TOL_S}s   point pad: +/-{POINT_PAD_S}s")
    print()
    print(f"{'pt':>3} {'sa_n':>4} {'t5_n':>4}  {'verdict':<11} {'top_failure':<28}")
    print("-" * 60)
    for p in result["per_point"]:
        print(f"{p['point_number']:>3} {p['sa_count']:>4} {p['t5_in_window']:>4}  "
              f"{p['verdict']:<11} {p['top_failure']:<28}")
    print()
    s = result["summary"]
    print("=== POINT SUMMARY ===")
    print(f"  full_match   {s['full_match']:>3} / {s['total_points']}")
    print(f"  partial      {s['partial']:>3} / {s['total_points']}")
    print(f"  missing      {s['missing']:>3} / {s['total_points']}")
    print()
    print("=== STROKE SUMMARY ===")
    print(f"  match        {s['stroke_match']:>3} / {s['stroke_total']}")
    print(f"  partial      {s['stroke_partial']:>3} / {s['stroke_total']}")
    print(f"  missing      {s['stroke_missing']:>3} / {s['stroke_total']}")
    print()
    print(f"  T5 strokes outside ANY SA point window: "
          f"{result['extras']} / {result['t5_total']}")
    print()
    print(f"=== HEADLINE: {s['full_match']}/{s['total_points']} points fully reconcile ===")


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    with open(BASELINE_PATH) as f:
        return json.load(f)


def _save_baseline(data: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=DEFAULT_T5,
                    help=f"T5 task_id to verify (default {DEFAULT_T5})")
    ap.add_argument("--sa", default=DEFAULT_SA,
                    help=f"SportAI truth task_id (default {DEFAULT_SA})")
    ap.add_argument("--update-baseline", action="store_true",
                    help="Write current numbers as the new committed baseline")
    ap.add_argument("--honor-exclude", action="store_true",
                    help="Drop T5 rows where exclude_d=TRUE before reconciling "
                         "(post-Phase-3 active view; off by default to preserve "
                         "the unfiltered baseline interpretation)")
    args = ap.parse_args(argv)

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2

    engine = create_engine(_normalize_db_url(db_url))

    with engine.connect() as conn:
        sa_rows = _load_sa(conn, args.sa)
        t5_rows = _load_t5(conn, args.task, honor_exclude=args.honor_exclude)

    if not sa_rows:
        print(f"No SA rows for task_id={args.sa}", file=sys.stderr)
        return 2

    result = reconcile(sa_rows, t5_rows)
    _print_report(result, sa_tid=args.sa, t5_tid=args.task)

    if args.update_baseline:
        s = result["summary"]
        # Use fixture short name (first 8 chars of T5 task) — matches
        # bench_baseline naming convention.
        fx_name = args.task.split("-")[0]
        baseline = {
            "updated_at": date.today().isoformat(),
            "commit": _git_sha(),
            "fixtures": {
                fx_name: {
                    "sa_task_id": args.sa,
                    "t5_task_id": args.task,
                    "points": [s["full_match"], s["total_points"]],
                    "strokes": [s["stroke_match"], s["stroke_total"]],
                    "verdicts": {
                        "full_match": s["full_match"],
                        "partial": s["partial"],
                        "missing": s["missing"],
                    },
                    "stroke_verdicts": {
                        "match": s["stroke_match"],
                        "partial": s["stroke_partial"],
                        "missing": s["stroke_missing"],
                    },
                    "t5_extras_outside_points": result["extras"],
                    "t5_total": result["t5_total"],
                }
            },
        }
        _save_baseline(baseline)
        print()
        print(f"-> wrote new baseline to {BASELINE_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
