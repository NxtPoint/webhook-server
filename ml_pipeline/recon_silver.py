"""
ml_pipeline/recon_silver.py — Reconciliation test harness for silver.point_detail.

Compares two task_ids (typically SportAI vs T5) across the 18 bronze base fields
and key derived fields. Designed to be run from the Render shell.

Usage (from Render shell):
    python -m ml_pipeline.recon_silver
        (uses reference task IDs hardcoded below)

    python -m ml_pipeline.recon_silver SPORTAI_TID T5_TID
        (explicit task IDs)

    python -m ml_pipeline.recon_silver SPORTAI_TID T5_TID --mode=summary
    python -m ml_pipeline.recon_silver SPORTAI_TID T5_TID --mode=rows
    python -m ml_pipeline.recon_silver SPORTAI_TID T5_TID --mode=distributions
    python -m ml_pipeline.recon_silver SPORTAI_TID T5_TID --mode=coverage
    python -m ml_pipeline.recon_silver SPORTAI_TID T5_TID --mode=all        (default)

Modes:
    summary       — row counts, player counts, point/game counts, time range
    coverage      — how many rows have each field populated
    distributions — stroke, serve, zone, depth breakdowns
    rows          — first 15 rows side by side
    speed         — ball_speed stats (avg/max/min/distribution)
    all           — everything
"""

import sys
from typing import Optional

from sqlalchemy import text

# Reference task IDs — update when we have new matches to compare
DEFAULT_SPORTAI = "4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb"
DEFAULT_T5      = "9052c6e1-d428-4511-8595-35cd9fec3984"


def _get_engine():
    from db_init import engine
    return engine


def _hr(title: str, char: str = "=", width: int = 80):
    print()
    print(char * width)
    print(f"  {title}")
    print(char * width)


def _sub(title: str):
    print()
    print(f"--- {title} ---")


def run_summary(conn, sportai_tid: str, t5_tid: str):
    _hr("SUMMARY — row counts, players, points, games")
    for label, tid in [("SPORTAI", sportai_tid), ("T5", t5_tid)]:
        r = conn.execute(text("""
            SELECT
              count(*) AS total_rows,
              count(DISTINCT player_id) AS players,
              count(DISTINCT point_number) FILTER (WHERE point_number IS NOT NULL) AS points,
              count(DISTINCT game_number) FILTER (WHERE game_number IS NOT NULL) AS games,
              count(DISTINCT set_number) FILTER (WHERE set_number IS NOT NULL) AS sets,
              count(*) FILTER (WHERE serve_d) AS serves_d,
              count(*) FILTER (WHERE serve) AS serves_raw,
              count(*) FILTER (WHERE ace_d) AS aces,
              round(min(ball_hit_s)::numeric, 1) AS t_start,
              round(max(ball_hit_s)::numeric, 1) AS t_end,
              COALESCE(model, 'sportai') AS model
            FROM silver.point_detail
            WHERE task_id = CAST(:tid AS uuid)
            GROUP BY model
        """), {"tid": tid}).mappings().first()
        print(f"\n{label:8s} ({tid[:8]})")
        if not r:
            print("  NO ROWS")
            continue
        for k, v in dict(r).items():
            print(f"  {k:20s} = {v}")


def run_coverage(conn, sportai_tid: str, t5_tid: str):
    _hr("FIELD COVERAGE — how many rows have each field populated")
    header = f"{'Field':30s} {'SPORTAI':>12s} {'T5':>12s} {'% MATCH':>10s}"
    print()
    print(header)
    print("-" * len(header))

    fields = [
        # 18 base fields
        ("id", "base"),
        ("player_id", "base"),
        ("valid", "base"),
        ("serve", "base"),
        ("swing_type", "base"),
        ("volley", "base"),
        ("is_in_rally", "base"),
        ("ball_player_distance", "base"),
        ("ball_speed", "base"),
        ("ball_impact_type", "base"),
        ("ball_hit_s", "base"),
        ("ball_hit_location_x", "base"),
        ("ball_hit_location_y", "base"),
        ("type", "base"),
        ("timestamp", "base"),
        ("court_x", "base"),
        ("court_y", "base"),
        # Derived fields
        ("serve_d", "derived"),
        ("server_end_d", "derived"),
        ("serve_side_d", "derived"),
        ("point_number", "derived"),
        ("game_number", "derived"),
        ("set_number", "derived"),
        ("server_id", "derived"),
        ("shot_ix_in_point", "derived"),
        ("shot_phase_d", "derived"),
        ("shot_outcome_d", "derived"),
        ("point_winner_player_id", "derived"),
        ("game_winner_player_id", "derived"),
        ("serve_location", "derived"),
        ("rally_location_hit", "derived"),
        ("rally_location_bounce", "derived"),
        ("ball_hit_x_norm", "derived"),
        ("ball_hit_y_norm", "derived"),
        ("ball_bounce_x_norm", "derived"),
        ("ball_bounce_y_norm", "derived"),
        ("serve_bucket_d", "derived"),
        ("rally_length", "derived"),
        ("stroke_d", "derived"),
        ("aggression_d", "derived"),
        ("depth_d", "derived"),
    ]

    def get_counts(tid: str):
        counts = {}
        sel = ", ".join([f"count({f[0]}) AS {f[0]}" for f in fields])
        sel += ", count(*) AS total"
        r = conn.execute(text(
            f"SELECT {sel} FROM silver.point_detail WHERE task_id = CAST(:tid AS uuid)"
        ), {"tid": tid}).mappings().first()
        return dict(r) if r else {"total": 0}

    sp = get_counts(sportai_tid)
    t5 = get_counts(t5_tid)

    print(f"{'[TOTAL ROWS]':30s} {sp.get('total', 0):>12d} {t5.get('total', 0):>12d}")
    print()
    print("BRONZE BASE FIELDS:")
    base_done = False
    for fname, ftype in fields:
        if ftype == "derived" and not base_done:
            print()
            print("DERIVED FIELDS:")
            base_done = True
        sp_n = sp.get(fname, 0)
        t5_n = t5.get(fname, 0)
        sp_pct = (sp_n / sp.get('total', 1) * 100) if sp.get('total', 0) else 0
        t5_pct = (t5_n / t5.get('total', 1) * 100) if t5.get('total', 0) else 0
        match = ""
        if sp.get('total', 0) and t5.get('total', 0):
            if sp_pct > 0:
                diff = abs(sp_pct - t5_pct)
                match = f"{100-diff:.0f}%"
        print(f"  {fname:28s} {sp_n:>5d}({sp_pct:>3.0f}%) {t5_n:>5d}({t5_pct:>3.0f}%) {match:>10s}")


def run_distributions(conn, sportai_tid: str, t5_tid: str):
    _hr("DISTRIBUTIONS")

    for field in ["stroke_d", "serve_bucket_d", "rally_location_bounce", "depth_d", "aggression_d"]:
        _sub(field)
        print(f"{'value':15s} {'SPORTAI':>10s} {'T5':>10s}")
        sp_rows = conn.execute(text(
            f"SELECT {field} AS v, count(*) AS n FROM silver.point_detail "
            f"WHERE task_id = CAST(:tid AS uuid) GROUP BY v ORDER BY n DESC"
        ), {"tid": sportai_tid}).mappings().all()
        t5_rows = conn.execute(text(
            f"SELECT {field} AS v, count(*) AS n FROM silver.point_detail "
            f"WHERE task_id = CAST(:tid AS uuid) GROUP BY v ORDER BY n DESC"
        ), {"tid": t5_tid}).mappings().all()
        sp_map = {str(r["v"]): r["n"] for r in sp_rows}
        t5_map = {str(r["v"]): r["n"] for r in t5_rows}
        keys = sorted(set(list(sp_map.keys()) + list(t5_map.keys())))
        for k in keys:
            print(f"{k:15s} {sp_map.get(k, 0):>10d} {t5_map.get(k, 0):>10d}")


def run_speed(conn, sportai_tid: str, t5_tid: str):
    _hr("BALL SPEED COMPARISON (m/s)")
    for label, tid in [("SPORTAI", sportai_tid), ("T5", t5_tid)]:
        r = conn.execute(text("""
            SELECT
              count(ball_speed) AS populated,
              round(avg(ball_speed)::numeric, 1) AS avg_ms,
              round(max(ball_speed)::numeric, 1) AS max_ms,
              round(min(ball_speed)::numeric, 1) AS min_ms,
              round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ball_speed)::numeric, 1) AS median_ms,
              round((avg(ball_speed) * 3.6)::numeric, 1) AS avg_kmh,
              round((max(ball_speed) * 3.6)::numeric, 1) AS max_kmh
            FROM silver.point_detail
            WHERE task_id = CAST(:tid AS uuid) AND ball_speed IS NOT NULL
        """), {"tid": tid}).mappings().first()
        print(f"\n{label}:")
        for k, v in dict(r).items():
            print(f"  {k:12s} = {v}")


def run_rows(conn, sportai_tid: str, t5_tid: str, limit: int = 15):
    _hr(f"ROW-BY-ROW SAMPLE (first {limit} by ball_hit_s)")

    cols = ("id, player_id, serve, swing_type, volley, "
            "round(ball_speed::numeric,1) AS speed, "
            "round(ball_hit_s::numeric,2) AS hit_s, "
            "round(ball_hit_location_x::numeric,2) AS hx, "
            "round(ball_hit_location_y::numeric,2) AS hy, "
            "round(court_x::numeric,2) AS bx, "
            "round(court_y::numeric,2) AS by_, "
            "stroke_d, point_number, game_number, shot_ix_in_point")

    for label, tid in [("SPORTAI", sportai_tid), ("T5", t5_tid)]:
        _sub(f"{label} ({tid[:8]})")
        rows = conn.execute(text(
            f"SELECT {cols} FROM silver.point_detail "
            f"WHERE task_id = CAST(:tid AS uuid) "
            f"ORDER BY ball_hit_s, id LIMIT :lim"
        ), {"tid": tid, "lim": limit}).mappings().all()
        if not rows:
            print("  NO ROWS")
            continue
        # Compact column headers
        hdr = f"{'id':>4s} {'pid':>3s} {'srv':>3s} {'type':>6s} {'vly':>3s} {'spd':>5s} {'hit_s':>6s} {'hx':>6s} {'hy':>6s} {'bx':>6s} {'by':>6s} {'stroke':>10s} {'pt':>3s} {'gm':>3s} {'shot':>4s}"
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            print(f"{r['id']:>4} {str(r['player_id']):>3} {'T' if r['serve'] else 'F':>3} "
                  f"{str(r['swing_type'] or '-'):>6s} {'T' if r['volley'] else 'F':>3} "
                  f"{str(r['speed'] or '-'):>5s} {str(r['hit_s'] or '-'):>6s} "
                  f"{str(r['hx'] or '-'):>6s} {str(r['hy'] or '-'):>6s} "
                  f"{str(r['bx'] or '-'):>6s} {str(r['by_'] or '-'):>6s} "
                  f"{str(r['stroke_d'] or '-'):>10s} "
                  f"{str(r['point_number'] or '-'):>3s} "
                  f"{str(r['game_number'] or '-'):>3s} "
                  f"{str(r['shot_ix_in_point'] or '-'):>4s}")


def main():
    args = sys.argv[1:]
    mode = "all"
    sportai_tid = DEFAULT_SPORTAI
    t5_tid = DEFAULT_T5

    positional = []
    for a in args:
        if a.startswith("--mode="):
            mode = a.split("=", 1)[1]
        else:
            positional.append(a)

    if len(positional) >= 2:
        sportai_tid = positional[0]
        t5_tid = positional[1]

    print(f"SPORTAI: {sportai_tid}")
    print(f"T5:      {t5_tid}")
    print(f"MODE:    {mode}")

    engine = _get_engine()
    with engine.connect() as conn:
        if mode in ("summary", "all"):
            run_summary(conn, sportai_tid, t5_tid)
        if mode in ("coverage", "all"):
            run_coverage(conn, sportai_tid, t5_tid)
        if mode in ("distributions", "all"):
            run_distributions(conn, sportai_tid, t5_tid)
        if mode in ("speed", "all"):
            run_speed(conn, sportai_tid, t5_tid)
        if mode in ("rows", "all"):
            run_rows(conn, sportai_tid, t5_tid)


if __name__ == "__main__":
    main()
