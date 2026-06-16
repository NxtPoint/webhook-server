"""Line-level reconciliation: SA-active vs T5-active, one row per shot.

The standardized "how good are we really" tool (RULE 6, docs/north_star.md).
Matches each SportAI-active event to the nearest T5-active event within a time
tolerance, then scores agreement per base field. Read-only; never writes.

Why silver-grain (not raw bronze tables): silver is hit-driven (1 row = 1 shot)
and projects bronze VERBATIM, so a silver-t5 row's fields ARE the bronze facts —
this gives the "1 line, 12 fields" view without juggling 4 bronze tables or
hardcoding SportAI player-ids (which swap ends at changeovers). Reconciliation
is always SA-active vs T5-active (exclude_d IS NOT TRUE) — never raw counts.

Usage:
    python -m ml_pipeline.diag.recon_line <t5_task> --sa <sa_task> [--tol 1.0]
"""
from __future__ import annotations

import argparse
import statistics as st
import sys

from db_init import engine
from sqlalchemy import text as sql_text

# Coarse swing-type buckets so SA's 5-way and T5's 4-way labels compare.
_SWING = {
    "fh": "FH", "forehand": "FH",
    "1h_bh": "BH", "2h_bh": "BH", "bh": "BH", "backhand": "BH",
    "fh_overhead": "OH", "overhead": "OH", "smash": "OH",
    "other": "OTHER", "serve": "SERVE", "volley": "VOLLEY", "slice": "BH",
}
def _swing(v):
    return _SWING.get(str(v).strip().lower(), "OTHER") if v is not None else None


def _load(tid: str, model: str):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(sql_text("""
            SELECT ball_hit_s, ball_speed,
                   ball_hit_location_x AS hx, ball_hit_location_y AS hy,
                   court_x AS bx, court_y AS by_,
                   swing_type, serve, volley, player_id
            FROM silver.point_detail
            WHERE task_id = :t AND model = :m
              AND exclude_d IS NOT TRUE AND ball_hit_s IS NOT NULL
            ORDER BY ball_hit_s
        """), {"t": tid, "m": model}).mappings().all()]


def _dist(a, b, ax, ay, bx, by):
    try:
        return ((float(a[ax]) - float(b[bx])) ** 2 + (float(a[ay]) - float(b[by])) ** 2) ** 0.5
    except (TypeError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Line-level SA-active vs T5-active reconciliation")
    ap.add_argument("t5_task")
    ap.add_argument("--sa", required=True, help="paired SportAI task_id")
    ap.add_argument("--tol", type=float, default=1.0, help="match tolerance (s)")
    args = ap.parse_args(argv)

    sa = _load(args.sa, "sportai")
    t5 = _load(args.t5_task, "t5")
    print(f"=== recon_line  SA={args.sa[:8]}  T5={args.t5_task[:8]}  tol={args.tol}s ===")
    print(f"SA-active={len(sa)}  T5-active={len(t5)}  (T5 over-emit {len(t5)/max(1,len(sa)):.2f}x)")
    if not sa or not t5:
        print("  one side empty — nothing to match"); return 1

    # greedy nearest-in-time, one-to-one
    used = [False] * len(t5)
    pairs = []
    for s in sa:
        best, bi = args.tol + 1e9, -1
        for i, t in enumerate(t5):
            if used[i]:
                continue
            d = abs(float(s["ball_hit_s"]) - float(t["ball_hit_s"]))
            if d < best:
                best, bi = d, i
        if bi >= 0 and best <= args.tol:
            used[bi] = True
            pairs.append((s, t5[bi]))
    n = len(pairs)
    print(f"\nMATCHED {n}/{len(sa)} SA-active within {args.tol}s "
          f"(recall {100*n/max(1,len(sa)):.0f}%)  |  T5 unmatched (FP-ish) {used.count(False)}/{len(t5)}")
    if not n:
        return 0

    def pct(k):
        return f"{k}/{n} ({100*k/n:.0f}%)"

    serve_ok = sum(1 for s, t in pairs if bool(s["serve"]) == bool(t["serve"]))
    volley_ok = sum(1 for s, t in pairs if bool(s["volley"]) == bool(t["volley"]))
    swing_ok = sum(1 for s, t in pairs
                   if _swing(s["swing_type"]) and _swing(s["swing_type"]) == _swing(t["swing_type"]))
    print("\n-- categorical agreement (matched pairs) --")
    print(f"  serve  T/F : {pct(serve_ok)}")
    print(f"  volley T/F : {pct(volley_ok)}")
    print(f"  swing_type : {pct(swing_ok)}")

    print("\n-- coordinates (median error, matched & non-null) --")
    hd = [d for s, t in pairs if (d := _dist(s, t, 'hx', 'hy', 'hx', 'hy')) is not None]
    bd = [d for s, t in pairs if (d := _dist(s, t, 'bx', 'by_', 'bx', 'by_')) is not None]
    print(f"  ball_hit_xy : {len(hd)}/{n} pairs, median {st.median(hd):.2f}m" if hd else "  ball_hit_xy : no comparable pairs")
    print(f"  bounce_xy   : {len(bd)}/{n} pairs, median {st.median(bd):.2f}m" if bd else "  bounce_xy   : no comparable pairs (NULL court coords)")

    print("\n-- ball_speed --")
    sa_sp = sum(1 for s, _ in pairs if s["ball_speed"] is not None)
    t5_sp = sum(1 for _, t in pairs if t["ball_speed"] is not None)
    both = [(float(s["ball_speed"]), float(t["ball_speed"])) for s, t in pairs
            if s["ball_speed"] is not None and t["ball_speed"] is not None]
    print(f"  coverage on matched: SA {sa_sp}/{n}, T5 {t5_sp}/{n}")
    if both:
        diffs = [abs(a - b) for a, b in both]
        print(f"  SA median {st.median(a for a,_ in both):.1f} / T5 median {st.median(b for _,b in both):.1f} km/h; "
              f"median |diff| {st.median(diffs):.1f} km/h ({len(both)} pairs)")
    elif t5_sp == 0:
        print("  T5 carries NO per-shot ball_speed -> not comparable (DEV wiring gap: "
              "raw speed in ml_analysis.ball_detections never projected onto the shot).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
