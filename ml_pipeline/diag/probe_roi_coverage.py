"""ROI pose coverage probe — what did extract_far_pose actually produce
for this task, and does it cover the windows where the bronze pipeline
left baseline-zone gaps?

Three sections of output:

  1. Task-wide ROI table coverage
     - Does ml_analysis.player_detections_roi have rows for this job_id?
     - Breakdown by source (far_vitpose / far_roi_pose / etc.)
     - court_y distribution: how many rows fall in the FAR baseline zone?

  2. Per-window detail (bronze vs ROI side-by-side)
     For each ts: keypoint-row counts, court_y populated counts, and
     court_y range. Mirrors probe_baseline_empty.py columns so they're
     directly comparable.

  3. For "kpts_outside_baseline_zone" windows: dump the actual bbox
     centers + court_y values for all keypoint rows in the window.
     This tells us whether we're tracking the wrong body (chair umpire,
     fan in stand) vs. a court-calibration drift on the right body.

Usage (Render shell, DATABASE_URL set):
    python -m ml_pipeline.diag.probe_roi_coverage \\
        --task a798eff0-551f-4b5a-838f-7933866a727c \\
        --ts 458.08,463.52,584.92 \\
        --player 1
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _table_exists(conn, schema: str, name: str) -> bool:
    return bool(conn.execute(sql_text("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = :s AND table_name = :n
        )
    """), {"s": schema, "n": name}).scalar())


def _task_wide_roi_stats(conn, task: str, pid: int) -> dict:
    out = {"missing_table": False, "by_source": [], "totals": {}}
    if not _table_exists(conn, "ml_analysis", "player_detections_roi"):
        out["missing_table"] = True
        return out

    rows = conn.execute(sql_text("""
        SELECT
            source,
            COUNT(*)                                           AS rows,
            COUNT(keypoints)                                   AS rows_kpts,
            COUNT(court_y)                                     AS rows_courty,
            COUNT(*) FILTER (WHERE court_y BETWEEN -3.5 AND 4.5)  AS in_far_bz,
            COUNT(*) FILTER (WHERE court_y BETWEEN -5.0 AND 4.5)  AS in_far_bz_wide,
            MIN(frame_idx)                                     AS frame_min,
            MAX(frame_idx)                                     AS frame_max,
            MIN(court_y)                                       AS cy_min,
            MAX(court_y)                                       AS cy_max,
            AVG(court_y)                                       AS cy_avg
        FROM ml_analysis.player_detections_roi
        WHERE job_id = :t AND player_id = :pid
        GROUP BY source
        ORDER BY source
    """), {"t": task, "pid": pid}).mappings().all()
    out["by_source"] = [dict(r) for r in rows]

    total = conn.execute(sql_text("""
        SELECT COUNT(*) AS rows
        FROM ml_analysis.player_detections_roi
        WHERE job_id = :t AND player_id = :pid
    """), {"t": task, "pid": pid}).scalar() or 0
    out["totals"] = {"rows": total}
    return out


def _window_detail(conn, task: str, ts: float, fps: float, win: float,
                   pid: int) -> dict:
    lo = int((ts - win) * fps)
    hi = int((ts + win) * fps)
    out = {"ts": ts, "lo": lo, "hi": hi, "tables": {}}
    for table, label in [("player_detections", "bronze"),
                         ("player_detections_roi", "ROI")]:
        if not _table_exists(conn, "ml_analysis", table):
            out["tables"][label] = {"missing_table": True}
            continue
        # Bronze has no `source` column; ROI has one. Branch the projection.
        if table == "player_detections_roi":
            r = conn.execute(sql_text(f"""
                SELECT
                    string_agg(DISTINCT source, ',') AS sources,
                    COUNT(*) AS rows,
                    COUNT(keypoints) AS rows_kpts,
                    COUNT(court_y) AS rows_courty,
                    COUNT(*) FILTER (WHERE keypoints IS NOT NULL
                                     AND court_y IS NOT NULL) AS rows_kpts_and_courty,
                    COUNT(*) FILTER (WHERE keypoints IS NOT NULL
                                     AND court_y BETWEEN -3.5 AND 4.5) AS rows_kpts_in_far_bz,
                    MIN(court_y) AS cy_min,
                    MAX(court_y) AS cy_max
                FROM ml_analysis.{table}
                WHERE job_id = :t AND player_id = :pid
                  AND frame_idx BETWEEN :lo AND :hi
            """), {"t": task, "pid": pid, "lo": lo, "hi": hi}).mappings().one()
        else:
            r = conn.execute(sql_text(f"""
                SELECT
                    NULL::text AS sources,
                    COUNT(*) AS rows,
                    COUNT(keypoints) AS rows_kpts,
                    COUNT(court_y) AS rows_courty,
                    COUNT(*) FILTER (WHERE keypoints IS NOT NULL
                                     AND court_y IS NOT NULL) AS rows_kpts_and_courty,
                    COUNT(*) FILTER (WHERE keypoints IS NOT NULL
                                     AND court_y BETWEEN -3.5 AND 4.5) AS rows_kpts_in_far_bz,
                    MIN(court_y) AS cy_min,
                    MAX(court_y) AS cy_max
                FROM ml_analysis.{table}
                WHERE job_id = :t AND player_id = :pid
                  AND frame_idx BETWEEN :lo AND :hi
            """), {"t": task, "pid": pid, "lo": lo, "hi": hi}).mappings().one()
        out["tables"][label] = dict(r)
    return out


def _row_dump(conn, task: str, ts: float, fps: float, win: float,
              pid: int, limit: int = 30) -> list:
    """For diagnosis when 'kpts_outside_baseline_zone': dump bbox centers +
    court_y for keypoint rows in the window. Lets us tell wrong-body from
    bad-projection."""
    lo = int((ts - win) * fps)
    hi = int((ts + win) * fps)
    out = []
    if _table_exists(conn, "ml_analysis", "player_detections"):
        r = conn.execute(sql_text(f"""
            SELECT 'bronze' AS src, frame_idx,
                   (bbox_x1 + bbox_x2)/2 AS bcx,
                   (bbox_y1 + bbox_y2)/2 AS bcy,
                   (bbox_x2 - bbox_x1) * (bbox_y2 - bbox_y1) AS area,
                   court_x, court_y
            FROM ml_analysis.player_detections
            WHERE job_id = :t AND player_id = :pid
              AND frame_idx BETWEEN :lo AND :hi
              AND keypoints IS NOT NULL
            ORDER BY frame_idx
            LIMIT :lim
        """), {"t": task, "pid": pid, "lo": lo, "hi": hi, "lim": limit}
        ).mappings().all()
        out.extend(dict(x) for x in r)
    if _table_exists(conn, "ml_analysis", "player_detections_roi"):
        r = conn.execute(sql_text(f"""
            SELECT source AS src, frame_idx,
                   (bbox_x1 + bbox_x2)/2 AS bcx,
                   (bbox_y1 + bbox_y2)/2 AS bcy,
                   (bbox_x2 - bbox_x1) * (bbox_y2 - bbox_y1) AS area,
                   court_x, court_y
            FROM ml_analysis.player_detections_roi
            WHERE job_id = :t AND player_id = :pid
              AND frame_idx BETWEEN :lo AND :hi
              AND keypoints IS NOT NULL
            ORDER BY frame_idx
            LIMIT :lim
        """), {"t": task, "pid": pid, "lo": lo, "hi": hi, "lim": limit}
        ).mappings().all()
        out.extend(dict(x) for x in r)
    return sorted(out, key=lambda r: (r["frame_idx"], r["src"]))


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--ts", required=True, help="Comma-separated ts list")
    ap.add_argument("--player", type=int, default=1)
    ap.add_argument("--win", type=float, default=2.0)
    ap.add_argument("--dump-rows", action="store_true",
                    help="Also dump per-frame bbox/court_y for each window")
    args = ap.parse_args(argv)

    ts_list = [float(t.strip()) for t in args.ts.split(",") if t.strip()]
    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2
    engine = create_engine(_normalize_db_url(db_url))

    with engine.connect() as conn:
        fps = conn.execute(sql_text(
            "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
            "WHERE job_id = :t"
        ), {"t": args.task}).scalar() or 25.0

        print(f"=== probe_roi_coverage  task={args.task[:8]}  pid={args.player}  "
              f"win=±{args.win}s  fps={fps:.2f} ===")
        print()

        # 1. Task-wide ROI coverage
        print("--- 1. Task-wide ml_analysis.player_detections_roi coverage ---")
        stats = _task_wide_roi_stats(conn, args.task, args.player)
        if stats["missing_table"]:
            print("  ROI table does not exist on this database — extract_far_pose "
                  "has never been deployed.")
        elif not stats["by_source"]:
            print(f"  ROI table exists but has 0 rows for job_id={args.task[:8]} pid={args.player}.")
            print("  Apr 23 ROI pose extractor either didn't run on this task, or ran but skipped "
                  "(check Batch logs for 'roi_pose:' lines).")
        else:
            print(f"  Total ROI rows for this (task,pid): {stats['totals']['rows']}")
            print()
            print(f"  {'source':<20} {'rows':>6} {'kpts':>6} {'cy':>6} "
                  f"{'in_bz':>6} {'in_bzW':>7} {'frames':>17} {'cy_min':>7} {'cy_max':>7} {'cy_avg':>7}")
            for r in stats["by_source"]:
                fr = f"{r['frame_min']}-{r['frame_max']}"
                print(f"  {r['source']:<20} {_fmt(r['rows']):>6} "
                      f"{_fmt(r['rows_kpts']):>6} {_fmt(r['rows_courty']):>6} "
                      f"{_fmt(r['in_far_bz']):>6} {_fmt(r['in_far_bz_wide']):>7} "
                      f"{fr:>17} {_fmt(r['cy_min']):>7} "
                      f"{_fmt(r['cy_max']):>7} {_fmt(r['cy_avg']):>7}")
        print()

        # 2. Per-window detail
        print("--- 2. Per-window detail ---")
        print(f"  {'ts':>7} {'src':>7} {'rows':>5} {'kpts':>5} "
              f"{'k+cy':>5} {'k_bz':>5} {'cy_min':>7} {'cy_max':>7}  sources")
        print("  " + "-" * 90)
        for ts in ts_list:
            w = _window_detail(conn, args.task, ts, fps, args.win, args.player)
            for label in ("bronze", "ROI"):
                t = w["tables"].get(label, {})
                if t.get("missing_table"):
                    print(f"  {ts:>7.2f} {label:>7}  (table missing)")
                    continue
                print(f"  {ts:>7.2f} {label:>7} "
                      f"{_fmt(t.get('rows')):>5} "
                      f"{_fmt(t.get('rows_kpts')):>5} "
                      f"{_fmt(t.get('rows_kpts_and_courty')):>5} "
                      f"{_fmt(t.get('rows_kpts_in_far_bz')):>5} "
                      f"{_fmt(t.get('cy_min')):>7} "
                      f"{_fmt(t.get('cy_max')):>7}  "
                      f"{t.get('sources') or ''}")
            print()

        # 3. Optional row dump
        if args.dump_rows:
            print("--- 3. Per-frame bbox/court_y dump ---")
            for ts in ts_list:
                rows = _row_dump(conn, args.task, ts, fps, args.win, args.player)
                print(f"  ts={ts:.2f}  ({len(rows)} rows)")
                if not rows:
                    print("    (no keypoint rows in window)")
                    continue
                print(f"    {'src':<14} {'frame':>6} {'bcx':>6} {'bcy':>6} {'area':>7}  "
                      f"{'court_x':>8} {'court_y':>8}")
                for r in rows:
                    print(f"    {r['src']:<14} {r['frame_idx']:>6} "
                          f"{_fmt(r['bcx']):>6} {_fmt(r['bcy']):>6} "
                          f"{_fmt(r['area']):>7}  "
                          f"{_fmt(r['court_x']):>8} {_fmt(r['court_y']):>8}")
                print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
