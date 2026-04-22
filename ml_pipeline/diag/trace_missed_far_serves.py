"""Trace WHY the bounce-first far-player serve detector missed specific
SportAI FAR-server timestamps. Complements `trace_missed_serves.py`
(which handles pid=0 pose-first gate diagnosis).

For each target SA serve timestamp in the ±window:
  - list all bronze bounces (is_bounce=TRUE) in the NEAR service box
    (court_y > HALF_Y) — these are the anchors the bounce-first detector
    needs
  - list all ROI-augmented bounces from ml_analysis.ball_detections_roi
    in the same zone
  - list rally state + time-since-last-bounce at the target
  - list any serve_events (near or far) already persisted
  - render a verdict pointing at the blocking gate

Usage (Render shell):
    python -m ml_pipeline.diag.trace_missed_far_serves \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088 \\
        --targets 23.6,67.2,115.4   # SA FAR serve timestamps
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text as sql_text


COURT_LENGTH_M = 23.77
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M  # 18.285
CROSS_PLAYER_DEDUP_S = 3.0
MIN_SERVE_GAP_S = 5.0

DEFAULT_SPORTAI_REF = "4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb"


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _fetch_bronze_bounces(conn, task_id, ts_lo, ts_hi, fps):
    rows = conn.execute(sql_text("""
        SELECT frame_idx, court_x, court_y
        FROM ml_analysis.ball_detections
        WHERE job_id = :tid
          AND is_bounce = TRUE
          AND frame_idx BETWEEN :lo AND :hi
        ORDER BY frame_idx
    """), {
        "tid": task_id,
        "lo": int(ts_lo * fps),
        "hi": int(ts_hi * fps),
    }).fetchall()
    return [(r.frame_idx / fps, r.court_x, r.court_y) for r in rows]


def _fetch_roi_bounces(conn, task_id, ts_lo, ts_hi, fps):
    """Fetch ROI-augmented bounces. Returns [] if the table doesn't exist."""
    try:
        rows = conn.execute(sql_text("""
            SELECT frame_idx, court_x, court_y, source
            FROM ml_analysis.ball_detections_roi
            WHERE job_id = :tid
              AND is_bounce = TRUE
              AND frame_idx BETWEEN :lo AND :hi
            ORDER BY frame_idx
        """), {
            "tid": task_id,
            "lo": int(ts_lo * fps),
            "hi": int(ts_hi * fps),
        }).fetchall()
    except Exception as exc:
        print(f"  (ball_detections_roi not available: {exc})")
        return []
    return [(r.frame_idx / fps, r.court_x, r.court_y, r.source) for r in rows]


def _fetch_existing_serves(conn, task_id, ts_lo, ts_hi):
    rows = conn.execute(sql_text("""
        SELECT ts, player_id, source, confidence,
               bounce_court_x, bounce_court_y
        FROM ml_analysis.serve_events
        WHERE task_id = CAST(:tid AS uuid)
          AND ts BETWEEN :lo AND :hi
        ORDER BY ts
    """), {"tid": task_id, "lo": ts_lo, "hi": ts_hi}).fetchall()
    return [
        (float(r.ts), int(r.player_id), r.source, float(r.confidence),
         r.bounce_court_x, r.bounce_court_y)
        for r in rows
    ]


def _idle_before(bronze_ts: list, t: float) -> float:
    """Seconds since the last bounce at or before `t`. Infinite if none."""
    prior = [b for b in bronze_ts if b <= t]
    if not prior:
        return float("inf")
    return t - max(prior)


def _classify_zone(court_y):
    if court_y is None:
        return "no_coords"
    if HALF_Y < court_y <= NEAR_SERVICE_LINE_M:
        return "near_service_box"
    if court_y > NEAR_SERVICE_LINE_M:
        return "behind_near_service_line"
    return "far_half"  # cy <= HALF_Y, irrelevant for far-player bounce-first


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--targets",
                    help="Comma-separated SA serve timestamps. If omitted, "
                         "pulls FAR-player SA serve times from silver.point_detail "
                         "of --sportai.")
    ap.add_argument("--sportai", default=DEFAULT_SPORTAI_REF)
    ap.add_argument("--window", type=float, default=3.0,
                    help="Seconds ± each target to inspect")
    ap.add_argument("--fps", type=float, default=25.0)
    args = ap.parse_args(argv)

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2

    engine = create_engine(_normalize_db_url(db_url))

    with engine.connect() as conn:
        if args.targets:
            targets = [float(x.strip()) for x in args.targets.split(",")]
        else:
            rows = conn.execute(sql_text("""
                SELECT ball_hit_s
                FROM silver.point_detail
                WHERE task_id = CAST(:tid AS uuid)
                  AND model = 'sportai'
                  AND serve_d = TRUE
                  AND ball_hit_location_y < 2   -- FAR server
                  AND ball_hit_s IS NOT NULL
                ORDER BY ball_hit_s
            """), {"tid": args.sportai}).fetchall()
            targets = [float(r.ball_hit_s) for r in rows]
            if not targets:
                print(f"no FAR-player SA serves found for {args.sportai}",
                      file=sys.stderr)
                return 1

        print(f"Tracing {len(targets)} FAR-player SA serve timestamps on "
              f"task {args.task[:8]} vs SA {args.sportai[:8]}")

        matched = 0
        for tgt in targets:
            print(f"\n{'='*80}")
            print(f"TARGET FAR-player SA serve ts = {tgt:.2f}s  (±{args.window}s)")
            print(f"{'='*80}")

            ts_lo = tgt - args.window
            ts_hi = tgt + args.window

            bronze = _fetch_bronze_bounces(conn, args.task, ts_lo, ts_hi, args.fps)
            roi = _fetch_roi_bounces(conn, args.task, ts_lo, ts_hi, args.fps)
            events = _fetch_existing_serves(conn, args.task, ts_lo, ts_hi)

            # Bounces split by zone
            bronze_nsb = [(ts, bx, by) for (ts, bx, by) in bronze
                          if by is not None and HALF_Y < by <= NEAR_SERVICE_LINE_M
                          and bx is not None]
            roi_nsb = [(ts, bx, by, src) for (ts, bx, by, src) in roi
                       if by is not None and HALF_Y < by <= NEAR_SERVICE_LINE_M
                       and bx is not None]

            print(f"  bronze bounces in window:         {len(bronze)}")
            print(f"    in NEAR service box (anchor):   {len(bronze_nsb)}")
            for ts, bx, by in bronze_nsb:
                dt = ts - tgt
                print(f"      bronze  ts={ts:.2f} (dt={dt:+.2f}s) court=({bx:.1f},{by:.1f})")
            print(f"  ROI bounces in window:            {len(roi)}")
            print(f"    in NEAR service box (anchor):   {len(roi_nsb)}")
            for ts, bx, by, src in roi_nsb:
                dt = ts - tgt
                print(f"      roi     ts={ts:.2f} (dt={dt:+.2f}s) court=({bx:.1f},{by:.1f})  [{src}]")

            print(f"  serve_events in window:           {len(events)}")
            for ts, pid, src, conf, bx, by in events:
                dt = ts - tgt
                loc = f"bounce=({bx},{by})" if bx is not None else "bounce=NULL"
                print(f"    event   ts={ts:.2f} (dt={dt:+.2f}s) pid={pid} "
                      f"source={src} conf={conf:.2f} {loc}")

            # Rally context: how much idle time leads into the target?
            bronze_ts = [t for (t, _x, _y) in bronze]
            idle = _idle_before(bronze_ts, tgt)

            near_events_close = [e for e in events if e[1] == 0 and abs(e[0] - tgt) < CROSS_PLAYER_DEDUP_S]
            any_far_match = any(e[1] == 1 and abs(e[0] - tgt) < 1.0 for e in events)

            print(f"  idle-time before target (bronze bounces only): {idle:.1f}s")

            # Verdict
            if any_far_match:
                matched += 1
                print(f"\n  VERDICT: FAR-player serve detected ✓")
                continue

            if near_events_close:
                print(f"\n  VERDICT: blocked by CROSS_PLAYER_DEDUP (near-player event "
                      f"within ±{CROSS_PLAYER_DEDUP_S}s). Loosen dedup? Check if the "
                      f"near event is a real serve or a FP.")
                continue

            total_anchors = len(bronze_nsb) + len(roi_nsb)
            if total_anchors == 0:
                print(f"\n  VERDICT: NO BOUNCE ANCHOR in the near service box within "
                      f"±{args.window}s. Bounce-first detector cannot fire. "
                      f"Run extract_roi_bounces, or widen window, or the serve actually "
                      f"bounces elsewhere (out-call / deep return).")
            elif idle < 8.0:
                print(f"\n  VERDICT: anchor present but rally_state=IN_RALLY "
                      f"(idle={idle:.1f}s < 8.0s threshold). Would need a longer "
                      f"idle or to lower the gate threshold.")
            elif total_anchors > 0 and len(roi_nsb) > 0 and len(bronze_nsb) == 0:
                print(f"\n  VERDICT: ONLY ROI bounces present — if this still doesn't "
                      f"trigger an event, check that ml_analysis.ball_detections_roi "
                      f"rows are being merged (look for 'ball_detections augmented' "
                      f"log line during rerun-silver).")
            else:
                print(f"\n  VERDICT: anchor + idle look OK but no event. Check "
                      f"MIN_SERVE_GAP_S cooldown, near-player near-match within "
                      f"{CROSS_PLAYER_DEDUP_S}s, or _detect_bounce_based_serves_far "
                      f"confidence threshold.")

        print(f"\n{'='*80}")
        print(f"SUMMARY: {matched}/{len(targets)} FAR-player SA serves already matched "
              f"in serve_events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
