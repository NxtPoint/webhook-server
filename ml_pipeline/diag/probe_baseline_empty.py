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
                COUNT(*) FILTER (WHERE keypoints IS NOT NULL
                                 AND court_y IS NOT NULL) AS rows_kpts_and_courty,
                COUNT(*) FILTER (WHERE keypoints IS NOT NULL
                                 AND court_y BETWEEN -3.5 AND 4.5) AS rows_kpts_in_far_bz,
                COUNT(*) FILTER (WHERE keypoints IS NOT NULL
                                 AND court_y BETWEEN -5.0 AND 4.5) AS rows_kpts_in_far_bz_wide,
                MIN(court_y) FILTER (WHERE keypoints IS NOT NULL) AS cy_min_kpts,
                MAX(court_y) FILTER (WHERE keypoints IS NOT NULL) AS cy_max_kpts,
                AVG(court_y) FILTER (WHERE keypoints IS NOT NULL) AS cy_avg_kpts
            FROM ml_analysis.{table}
            WHERE job_id = :t
              AND player_id = :pid
              AND frame_idx BETWEEN :lo AND :hi
        """), {"t": task, "pid": pid, "lo": lo, "hi": hi}).mappings().one()
        out["tables"][label] = dict(r)
    return out


def _classify(probe: dict) -> str:
    """Classify based on KEYPOINT-rows specifically. _load_pose_rows (the path
    the detector uses) requires keypoints IS NOT NULL, so what matters is
    not the broader row population but the keypoint subset's court_y state."""
    bronze = probe["tables"].get("bronze", {})
    roi = probe["tables"].get("ROI", {})
    kpts_total = (bronze.get("rows_kpts") or 0) + (roi.get("rows_kpts") or 0)
    kpts_with_cy = ((bronze.get("rows_kpts_and_courty") or 0)
                    + (roi.get("rows_kpts_and_courty") or 0))
    kpts_in_bz = ((bronze.get("rows_kpts_in_far_bz") or 0)
                  + (roi.get("rows_kpts_in_far_bz") or 0))
    kpts_in_bz_wide = ((bronze.get("rows_kpts_in_far_bz_wide") or 0)
                       + (roi.get("rows_kpts_in_far_bz_wide") or 0))

    if kpts_total < 5:
        return "detection_miss"
    if kpts_with_cy == 0:
        # Pose extractor produced keypoints but court projection couldn't
        # map them — homography / calibration fix territory.
        return "kpts_without_courty"
    if kpts_in_bz_wide > 0 and kpts_in_bz == 0:
        # Slack widening would catch them
        return "fixable_by_widening_slack"
    if kpts_in_bz == 0:
        # cy values present on keypoint rows but outside even the wide zone
        return "kpts_outside_baseline_zone"
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
        print(f"{'ts':>7} {'src':>7} {'rows':>5} {'kpts':>5} "
              f"{'k+cy':>5} {'k_bz':>5} {'k_bzW':>6} "
              f"{'cy_min_k':>9} {'cy_max_k':>9} {'cy_avg_k':>9}  classify")
        print()
        print("  rows    = total rows  |  kpts = rows w/ keypoints")
        print("  k+cy    = rows w/ keypoints AND court_y populated")
        print("  k_bz    = rows w/ keypoints AND court_y in [-3.5, 4.5]  (current far zone)")
        print("  k_bzW   = rows w/ keypoints AND court_y in [-5.0, 4.5]  (widened slack)")
        print("  cy_*_k  = court_y stats restricted to keypoint rows only")
        print("-" * 130)
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
                      f"{_fmt(t.get('rows_kpts_and_courty')):>5} "
                      f"{_fmt(t.get('rows_kpts_in_far_bz')):>5} "
                      f"{_fmt(t.get('rows_kpts_in_far_bz_wide')):>6} "
                      f"{_fmt(t.get('cy_min_kpts')):>9} "
                      f"{_fmt(t.get('cy_max_kpts')):>9} "
                      f"{_fmt(t.get('cy_avg_kpts')):>9}  "
                      f"{cls if label == 'bronze' else ''}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
