"""Query silver.point_detail for serve-vs-overhead rows to test the
'hit_y drift through silver eps gate' theory for a task.

For a task where we see over-counted Overhead rows, this shows whether
the Overhead rows are sitting just inside the 1.5m eps gate (drift
confirmed) or at genuine mid-court positions (real overheads).

Usage (Render shell):
    python -m ml_pipeline.diag.query_stroke_drift \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    args = ap.parse_args(argv)

    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("DB_URL"))
    if not url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    engine = create_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT stroke_d, swing_type,
                   ROUND(ball_hit_location_y::numeric, 2) AS hy,
                   ROUND(court_y::numeric, 2) AS player_court_y,
                   ROUND(ball_hit_s::numeric, 1) AS ts,
                   player_id
            FROM silver.point_detail
            WHERE task_id = :tid
              AND stroke_d IN ('Serve', 'Overhead')
            ORDER BY ball_hit_s
        """), {"tid": args.task}).fetchall()

    print(f"{'stroke':<9} {'swing':<12} {'hy':>6} {'plr_cy':>7} {'ts':>7} {'pid':>3}")
    print("-" * 52)
    for r in rows:
        print(f"{r.stroke_d:<9} {str(r.swing_type)[:12]:<12} "
              f"{str(r.hy):>6} {str(r.player_court_y):>7} "
              f"{str(r.ts):>7} {r.player_id:>3}")

    # Summary: how many Overheads are sitting just inside the 22.27 cut?
    near_cut = 0
    far_cut = 0
    real_overhead = 0
    for r in rows:
        if r.stroke_d != "Overhead" or r.hy is None:
            continue
        hy = float(r.hy)
        # Near baseline failing gate: 20.77 < hy < 22.27 (i.e. 0-1.5m inside cut)
        if 20.77 < hy < 22.27:
            near_cut += 1
        elif 1.5 < hy < 3.0:
            far_cut += 1
        else:
            real_overhead += 1

    total_overhead = sum(1 for r in rows if r.stroke_d == "Overhead")
    print()
    print(f"OVERHEAD breakdown ({total_overhead} total):")
    print(f"  just inside near-cut (20.77 < hy < 22.27):  {near_cut}")
    print(f"  just inside far-cut  (1.50 < hy < 3.00):    {far_cut}")
    print(f"  genuine mid-court (outside drift band):     {real_overhead}")
    print()
    if near_cut + far_cut >= total_overhead * 0.7:
        print("VERDICT: drift-through-gate HIGHLY LIKELY — most Overheads are")
        print("sitting just inside the 1.5m eps gate. Bronze fix (feet instead")
        print("of center in map_to_court) should reclassify these to Serve.")
    elif real_overhead >= total_overhead * 0.7:
        print("VERDICT: drift NOT the cause — Overheads are at genuine mid-court")
        print("positions. Investigate stroke_d fallback logic or swing_type.")
    else:
        print("VERDICT: mixed — some drift, some genuine. Inspect per-row.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
