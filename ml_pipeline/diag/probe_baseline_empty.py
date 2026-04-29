"""Bucket B probe: why does a serve window have zero baseline-zone rows?

For each (task, ts, player) triple, dump the player_detections +
player_detections_roi state in a ±2s window:

  rows         total rows in window
  rows_kpts    rows with non-NULL keypoints
  rows_courty  rows with non-NULL court_y
  cy_min/max   range of court_y values seen (so you can tell if the
               player is being detected but not in the baseline zone
               vs. court calibration is dropping them entirely)

Two buckets emerge from the output:

  detection_miss     rows=0 (or only a handful) — the player wasn't
                     detected at all in this window. Need better
                     pose extraction (training / model swap).

  calibration_miss   rows>>0 but rows_courty=0 OR all court_y outside
                     the baseline zone (cy < 5 or cy > 18 typically).
                     Detected fine, but the homography says they're
                     mid-court — fix in lens calibration / court CNN.

Usage:
    python -m ml_pipeline.diag.probe_baseline_empty \\
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


def _probe(conn, task: str, ts: float, fps: float, win: float, pid: int) -> dict:
    lo = int((ts - win) * fps)
    hi = int((ts + win) * fps)
    out = {"ts": ts, "lo": lo, "hi": hi, "pid": pid, "tables": {}}

    for table, label in [("player_detections", "bronze"),
                         ("player_detections_roi", "ROI")]:
        # Check table existence first (ROI may not exist on older tasks)
        exists = conn.execute(sql_text("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='ml_analysis' AND table_name=:t
            )
        """), {"t": table}).scalar()
        if not exists:
            out["tables"][label] = {"missing_table": True}
            continue
        r = conn.execute(sql_text(f"""
            SELECT
                COUNT(*) AS rows,
                COUNT(court_y) AS rows_courty,
                COUNT(*) FILTER (WHERE keypoints IS NOT NULL) AS rows_kpts,
                MIN(court_y) AS cy_min,
                MAX(court_y) AS cy_max,
                AVG(court_y) AS cy_avg,
                COUNT(*) FILTER (WHERE court_y < 5)  AS cy_lt_5,
                COUNT(*) FILTER (WHERE court_y BETWEEN 5 AND 18) AS cy_5_18,
                COUNT(*) FILTER (WHERE court_y > 18) AS cy_gt_18
            FROM ml_analysis.{table}
            WHERE job_id = CAST(:t AS uuid)
              AND player_id = :pid
              AND frame_idx BETWEEN :lo AND :hi
        """), {"t": task, "pid": pid, "lo": lo, "hi": hi}).mappings().one()
        out["tables"][label] = dict(r)
    return out


def _classify(probe: dict) -> str:
    bronze = probe["tables"].get("bronze", {})
    roi = probe["tables"].get("ROI", {})
    total = (bronze.get("rows") or 0) + (roi.get("rows") or 0)
    courty = (bronze.get("rows_courty") or 0) + (roi.get("rows_courty") or 0)
    if total < 5:
        return "detection_miss"
    if courty == 0:
        return "calibration_null_courty"
    # baseline zone is roughly cy < 5 (far) or cy > 18 (near). Mid-court
    # cy 5-18 is rejected. If ALL courty rows are mid-court that's a
    # calibration drift.
    bz = ((bronze.get("cy_lt_5") or 0) + (bronze.get("cy_gt_18") or 0)
          + (roi.get("cy_lt_5") or 0) + (roi.get("cy_gt_18") or 0))
    if bz == 0:
        return "calibration_midcourt_drift"
    return "baseline_present_other_gate"


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
    ap.add_argument("--player", type=int, default=1, help="0=near, 1=far")
    ap.add_argument("--win", type=float, default=2.0)
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
        print(f"=== probe_baseline_empty task={args.task[:8]} pid={args.player} "
              f"win=±{args.win}s fps={fps:.2f} ===")
        print()
        print(f"{'ts':>7} {'src':>7} {'rows':>5} {'kpts':>5} {'cy_n':>5} "
              f"{'cy_lt5':>6} {'cy_5_18':>7} {'cy_gt18':>7} "
              f"{'cy_min':>6} {'cy_max':>6} {'cy_avg':>6}  classify")
        print("-" * 110)
        for ts in ts_list:
            probe = _probe(conn, args.task, ts, fps, args.win, args.player)
            cls = _classify(probe)
            for label in ("bronze", "ROI"):
                t = probe["tables"].get(label, {})
                if t.get("missing_table"):
                    print(f"{ts:>7.2f} {label:>7}  (table missing)")
                    continue
                print(f"{ts:>7.2f} {label:>7} "
                      f"{_fmt(t.get('rows')):>5} "
                      f"{_fmt(t.get('rows_kpts')):>5} "
                      f"{_fmt(t.get('rows_courty')):>5} "
                      f"{_fmt(t.get('cy_lt_5')):>6} "
                      f"{_fmt(t.get('cy_5_18')):>7} "
                      f"{_fmt(t.get('cy_gt_18')):>7} "
                      f"{_fmt(t.get('cy_min')):>6} "
                      f"{_fmt(t.get('cy_max')):>6} "
                      f"{_fmt(t.get('cy_avg')):>6}  "
                      f"{cls if label == 'bronze' else ''}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
