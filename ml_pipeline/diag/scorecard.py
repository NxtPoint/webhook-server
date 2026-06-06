"""Per-run 18-field scorecard -- run after ANY probe/run of a dual-submit pair.

Measures a T5 run's bronze (`ml_analysis.*`) field-by-field against the
paired SportAI task. Promoted from `.claude/tmp/scorecard.py` (2026-06-06,
Job 2 of the post-far-court-fix arc) -- kills the silent-regression class
where a deploy improves one field and quietly degrades another.

Read-only: never writes. If `stroke_events` is empty (probe jobs skip the
Render ingest step that runs the stroke detector), it says so and how to
populate -- it does NOT auto-run detectors.

Usage:
    python -m ml_pipeline.diag.scorecard <job_id> [--sportai-tid <uuid>]

Serve recall/precision is deliberately not duplicated here -- run
`python -m ml_pipeline.harness eval-serve <job_id> --sportai-tid <sa>`
(it executes the detector with current local code; this tool only counts
persisted serve_events).

Output is plain ASCII -- this runs on the PS5.1 cp1252 console and the
Render shell alike.

Field map (the 18-base-field audit: docs/_investigation/bronze_silver_18_audit.md):
  WHO   identity pollution (court_y>13 in pid-1) + off-court static cluster
  WHERE player position dy vs SA (near/far, paired +/-0.2s)
  WHEN  stroke_events ts-alignment vs SA ball_hit_s (pid-strict, 0.5s/1.0s)
  bounce CNN count / NULL-coord rate / xy error vs SA floor bounces
  serve  persisted serve_events count (eval via harness)
  presence: ball speed coverage, swing_type_events (classifier gate)
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import sys

from sqlalchemy import create_engine, text as sql_text

DEFAULT_SA = "ba4812be-75af-4f8b-a15b-63941849f882"
SA_NEAR_PID, SA_FAR_PID = 22, 122


def _engine():
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("DB_URL"))
    if not url:
        print("DATABASE_URL required", file=sys.stderr)
        raise SystemExit(2)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return create_engine(url)


def _fps(conn, job_id: str) -> float:
    r = conn.execute(sql_text(
        "SELECT total_frames, video_duration_sec FROM ml_analysis.video_analysis_jobs "
        "WHERE job_id::text = :t"), {"t": job_id}).fetchone()
    if r and r[0] and r[1]:
        return r[0] / r[1]
    return 25.0


def section_identity(conn, job_id: str) -> None:
    print("-- WHO: identity --")
    rows = conn.execute(sql_text("""
        SELECT player_id, COUNT(*),
               ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY court_y)::numeric, 1),
               COUNT(*) FILTER (WHERE court_y > 13)
        FROM ml_analysis.player_detections
        WHERE job_id::text = :t AND court_y IS NOT NULL GROUP BY 1 ORDER BY 1
    """), {"t": job_id}).fetchall()
    for pid, n, med, bad in rows:
        extra = (f"  court_y>13: {bad} ({100*bad/n:.0f}%) <- near-half pollution"
                 if pid == 1 else "")
        print(f"  pid={pid}: n={n} median_court_y={med}{extra}")
    # Off-court static FP: pid-1 rows whose court_x sits outside the court
    # (doubles court x is 0..10.97 in SA convention; tolerate 1.5m slack)
    # -- spectators/umpire tracked into the far player id. Found on
    # ea1e500c: 886 rows locked at (x~-4.8, y~+6.0) -- evades the
    # court_y>13 pollution metric and drags far position p90.
    fp = conn.execute(sql_text("""
        WITH far AS (
            SELECT court_x, court_y FROM ml_analysis.player_detections
            WHERE job_id::text = :t AND player_id = 1
              AND court_x IS NOT NULL AND court_y IS NOT NULL
        )
        SELECT COUNT(*) FILTER (WHERE court_x < -1.5 OR court_x > 12.5), COUNT(*)
        FROM far
    """), {"t": job_id}).fetchone()
    if fp and fp[1]:
        print(f"  pid=1 off-court-x rows: {fp[0]}/{fp[1]} ({100*fp[0]/fp[1]:.0f}%)"
              f"{' <- static-FP suspect (spectator band)' if fp[0]/fp[1] > 0.2 else ''}")


def section_position(conn, job_id: str, sa_tid: str, fps: float) -> None:
    print("-- WHERE: player position dy vs SA --")
    for side, pid, sa_pid in (("FAR", 1, SA_FAR_PID), ("NEAR", 0, SA_NEAR_PID)):
        t5p = conn.execute(sql_text("""
            SELECT frame_idx, court_y FROM ml_analysis.player_detections
            WHERE job_id::text = :t AND player_id = :p AND court_y IS NOT NULL
            ORDER BY frame_idx"""), {"t": job_id, "p": pid}).fetchall()
        t5l = [(x[0] / fps, float(x[1])) for x in t5p]
        ts_list = [p[0] for p in t5l]
        sa = conn.execute(sql_text("""
            SELECT timestamp, court_y FROM bronze.player_position
            WHERE task_id::text = :s AND player_id = :p
              AND court_y IS NOT NULL AND timestamp IS NOT NULL
            ORDER BY timestamp"""), {"s": sa_tid, "p": str(sa_pid)}).fetchall()
        dys = []
        for ts_sa, sy in sa[::3]:
            i = bisect.bisect_left(ts_list, float(ts_sa))
            for j in (i - 1, i):
                if 0 <= j < len(ts_list) and abs(ts_list[j] - float(ts_sa)) < 0.2:
                    dys.append(t5l[j][1] - float(sy))
                    break
        dys.sort()
        if dys:
            n = len(dys)
            print(f"  {side:5} n={n} med {dys[n//2]:+.2f}m  "
                  f"p10 {dys[n//10]:+.2f}  p90 {dys[9*n//10]:+.2f}")
        else:
            print(f"  {side:5} no paired samples")


def section_strokes(conn, job_id: str, sa_tid: str) -> None:
    print("-- WHEN/WHO: stroke_events ts-alignment vs SA ball_hit_s --")
    t5 = [(float(r[0]), int(r[1])) for r in conn.execute(sql_text(
        "SELECT ts, player_id FROM ml_analysis.stroke_events "
        "WHERE task_id::text = :t ORDER BY ts"), {"t": job_id}).fetchall()]
    if not t5:
        print("  stroke_events EMPTY -- probe jobs skip the Render ingest step; "
              "populate via ml_pipeline.stroke_detector.detect_strokes_for_task")
        return
    sas = [(float(r[0]), int(r[1])) for r in conn.execute(sql_text(
        "SELECT ball_hit_s, CASE WHEN player_id = :np THEN 0 ELSE 1 END "
        "FROM bronze.player_swing WHERE task_id::text = :s "
        "AND player_id IN (:np, :fp) AND ball_hit_s IS NOT NULL ORDER BY 1"),
        {"s": sa_tid, "np": SA_NEAR_PID, "fp": SA_FAR_PID}).fetchall()]
    n_near = sum(1 for _, p in t5 if p == 0)
    n_far = sum(1 for _, p in t5 if p == 1)
    sa_near = sum(1 for _, p in sas if p == 0)
    sa_far = sum(1 for _, p in sas if p == 1)
    print(f"  emitted: near {n_near} (SA {sa_near})  far {n_far} (SA {sa_far})")
    for tol in (0.5, 1.0):
        used: set = set()
        hit = {0: 0, 1: 0}
        tot = {0: 0, 1: 0}
        for ts_sa, pid in sas:
            tot[pid] += 1
            best, bd = None, tol + 1
            for j, (ts5, p5) in enumerate(t5):
                if j in used or p5 != pid:
                    continue
                d = abs(ts5 - ts_sa)
                if d <= tol and d < bd:
                    best, bd = j, d
            if best is not None:
                used.add(best)
                hit[pid] += 1
        print(f"  tol={tol}s: near {hit[0]}/{tot[0]}  far {hit[1]}/{tot[1]}  "
              f"t5-unmatched {len(t5)-len(used)}/{len(t5)}")


def section_bounces(conn, job_id: str, sa_tid: str) -> None:
    print("-- WHERE-bounced: CNN ball_bounces vs SA floor --")
    r = conn.execute(sql_text(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE court_y IS NULL) "
        "FROM ml_analysis.ball_bounces WHERE job_id::text = :t"), {"t": job_id}).fetchone()
    sa_b = conn.execute(sql_text(
        "SELECT timestamp, court_x, court_y FROM bronze.ball_bounce "
        "WHERE task_id::text = :s AND type = 'floor' ORDER BY timestamp"),
        {"s": sa_tid}).fetchall()
    print(f"  CNN bounces: {r[0]} (NULL coords {r[1]} = {100*r[1]/max(r[0],1):.0f}%)  "
          f"SA floor: {len(sa_b)}")
    t5b = conn.execute(sql_text(
        "SELECT ts, court_x, court_y FROM ml_analysis.ball_bounces "
        "WHERE job_id::text = :t AND court_x IS NOT NULL ORDER BY ts"),
        {"t": job_id}).fetchall()
    used: set = set()
    errs = []
    for ts_sa, sx, sy in sa_b:
        best, bd = None, 0.6
        for i, (ts5, _x5, _y5) in enumerate(t5b):
            if i in used:
                continue
            dt = abs(float(ts5) - float(ts_sa))
            if dt < bd:
                bd, best = dt, i
        if best is not None:
            used.add(best)
            _, x5, y5 = t5b[best]
            errs.append(math.hypot(float(x5) - float(sx), float(y5) - float(sy)))
    errs.sort()
    if errs:
        n = len(errs)
        print(f"  xy error (matched {n}/{len(sa_b)} within 0.6s): "
              f"median {errs[n//2]:.2f}m  p90 {errs[9*n//10]:.2f}m")


def section_presence(conn, job_id: str) -> None:
    print("-- presence: speed / swing_type / serve_events --")
    sp = conn.execute(sql_text(
        "SELECT COUNT(*), COUNT(speed_kmh) FROM ml_analysis.ball_detections "
        "WHERE job_id::text = :t"), {"t": job_id}).fetchone()
    print(f"  ball speed: {sp[1]}/{sp[0]} detections carry speed_kmh")
    st = conn.execute(sql_text(
        "SELECT COUNT(*) FROM ml_analysis.swing_type_events WHERE job_id::text = :t"),
        {"t": job_id}).scalar()
    print(f"  swing_type_events: {st} (0 expected while SWING_CLASSIFIER_ENABLED=0)")
    se = conn.execute(sql_text(
        "SELECT COUNT(*) FROM ml_analysis.serve_events WHERE task_id::text = :t"),
        {"t": job_id}).scalar()
    print(f"  serve_events persisted: {se} "
          f"(recall/precision: python -m ml_pipeline.harness eval-serve {job_id[:8]}...)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job_id", help="T5 job_id (full uuid)")
    ap.add_argument("--sportai-tid", default=DEFAULT_SA,
                    help=f"paired SportAI task (default {DEFAULT_SA[:8]}...)")
    args = ap.parse_args(argv)

    eng = _engine()
    with eng.connect() as conn:
        fps = _fps(conn, args.job_id)
        print(f"=== scorecard job={args.job_id[:8]} vs SA {args.sportai_tid[:8]} "
              f"fps={fps:.2f} ===")
        section_identity(conn, args.job_id)
        section_position(conn, args.job_id, args.sportai_tid, fps)
        section_strokes(conn, args.job_id, args.sportai_tid)
        section_bounces(conn, args.job_id, args.sportai_tid)
        section_presence(conn, args.job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
