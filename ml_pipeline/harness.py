"""
ml_pipeline/harness.py — Test harness for the T5 ML pipeline + silver builder.

A single CLI entry point with subcommands for validation, reconciliation,
ops, and regression testing. Designed to run as one-liners from the Render shell
(no multi-line python -c indent issues).

Usage:
    python -m ml_pipeline.harness <command> [args]

Quality checks:
    validate-bronze <job_id>             — sanity-check ml_analysis.* data
    validate-silver <task_id>            — sanity-check silver.point_detail data
    validate <task_id>                   — both bronze + silver checks

Reconciliation:
    reconcile                            — full SportAI vs T5 comparison (uses defaults)
    reconcile <sportai_tid> <t5_tid>     — explicit task IDs
    reconcile <sportai_tid> <t5_tid> --mode=summary|coverage|distributions|speed|rows

Training bench (event-level alignment + feature analysis):
    training-bench align [sportai_tid] [t5_tid] [--window 1.0]
                                         — match events by timestamp, report coverage
    training-bench serves [sportai_tid] [t5_tid] [--window 1.0]
                                         — serve detection recall/precision vs ground truth
    training-bench features [sportai_tid] [t5_tid] [--window 1.0]
                                         — field-by-field agreement + numeric correlation
    training-bench extract-serves [sportai_tid] [t5_tid] [--window 1.0] [--csv PATH]
                                         — raw ml_analysis.* data at each serve timestamp

Operational:
    list-jobs [--limit 20]               — recent T5 batch jobs
    list-matches [--limit 20] [--source sportai|t5]  — recent silver matches
    rerun-silver <task_id>               — rebuild silver from existing bronze
    rerun-ingest <task_id>               — re-download bronze from S3 + rebuild silver
    dual-submit <sportai_task_id>        — submit existing SportAI video to T5 pipeline

Regression / golden datasets:
    golden-list                          — list registered golden snapshots
    golden-snapshot <task_id> --name N   — capture a known-good baseline
    golden-check <name>                  — validate current data against snapshot

Eval store:
    eval-history [--last N]              — show recent evaluation history

Per-component evaluation:
    eval-ball <task_id>                  — ball detection quality: rate, speed, bounces
    eval-player <task_id>                — player detection: IDs, coord variance, path
    eval-court <task_id>                 — court detection: homography success, keypoints

Exit codes:
    0 = all checks passed
    1 = one or more checks failed
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


# ============================================================
# Path to golden datasets (lives in repo so it's version-controlled)
# ============================================================

GOLDEN_FILE = Path(__file__).parent / "golden_datasets.json"


# ============================================================
# Output helpers
# ============================================================

# Use ASCII characters only for Render shell compatibility (no unicode codecs)
PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"


def hr(title: str, char: str = "=", width: int = 78) -> None:
    print()
    print(char * width)
    print(f"  {title}")
    print(char * width)


def sub(title: str) -> None:
    print()
    print(f"--- {title} ---")


def check(name: str, ok: bool, value: Any = None, expected: Any = None) -> bool:
    """Print a check result line. Returns the ok flag."""
    tag = PASS if ok else FAIL
    line = f"  {tag} {name:40s}"
    if value is not None:
        line += f" value={value}"
    if expected is not None:
        line += f" expected={expected}"
    print(line)
    return ok


# ============================================================
# Engine helper
# ============================================================

def get_engine():
    from db_init import engine
    return engine


# ============================================================
# Bronze validation (ml_analysis.*)
# ============================================================

def validate_bronze(job_id: str) -> bool:
    hr(f"BRONZE VALIDATION  job_id={job_id[:8]}")
    engine = get_engine()
    all_ok = True

    with engine.connect() as conn:
        # Job row
        job = conn.execute(text("""
            SELECT job_id, task_id, status, video_fps, video_duration_sec,
                   total_frames, court_detected, court_confidence,
                   processing_time_sec, bronze_s3_key
            FROM ml_analysis.video_analysis_jobs
            WHERE job_id = :j
        """), {"j": job_id}).mappings().first()

        if not job:
            print(f"  {FAIL} job_row_exists                       no row found")
            return False
        all_ok &= check("job_row_exists", True)
        all_ok &= check("job_status_complete", job["status"] == "complete", job["status"])
        all_ok &= check("court_detected", bool(job["court_detected"]), job["court_detected"])
        all_ok &= check(
            "court_confidence_>=0.5",
            (job["court_confidence"] or 0) >= 0.5,
            job["court_confidence"],
        )
        all_ok &= check("bronze_s3_key_set", bool(job["bronze_s3_key"]), job["bronze_s3_key"])
        all_ok &= check("video_fps_set", bool(job["video_fps"]), job["video_fps"])
        all_ok &= check("total_frames_>0", (job["total_frames"] or 0) > 0, job["total_frames"])

        # Ball detections
        sub("ball_detections")
        ball = conn.execute(text("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE is_bounce) AS bounces,
                   count(*) FILTER (WHERE is_in IS TRUE) AS in_count,
                   count(*) FILTER (WHERE is_in IS FALSE) AS out_count,
                   count(court_x) AS court_x_pop,
                   count(speed_kmh) AS speed_pop,
                   round(avg(speed_kmh)::numeric, 1) AS avg_spd,
                   round(max(speed_kmh)::numeric, 1) AS max_spd,
                   min(court_x) AS min_cx, max(court_x) AS max_cx,
                   min(court_y) AS min_cy, max(court_y) AS max_cy
            FROM ml_analysis.ball_detections
            WHERE job_id = :j
        """), {"j": job_id}).mappings().first()
        all_ok &= check("ball_total_>0", ball["total"] > 0, ball["total"])
        all_ok &= check("ball_bounces_>=10", ball["bounces"] >= 10, ball["bounces"])
        all_ok &= check("ball_court_x_populated", ball["court_x_pop"] > 0, ball["court_x_pop"])
        all_ok &= check(
            "ball_court_x_in_range",
            ball["min_cx"] is None or (ball["min_cx"] >= -1.0 and ball["max_cx"] <= 12.0),
            f"[{ball['min_cx']}, {ball['max_cx']}]",
        )
        all_ok &= check(
            "ball_court_y_in_range",
            ball["min_cy"] is None or (ball["min_cy"] >= -1.0 and ball["max_cy"] <= 25.0),
            f"[{ball['min_cy']}, {ball['max_cy']}]",
        )
        all_ok &= check(
            "ball_speed_realistic",
            ball["max_spd"] is None or (10 <= ball["max_spd"] <= 300),
            f"avg={ball['avg_spd']} max={ball['max_spd']} (km/h)",
        )

        # Player detections
        sub("player_detections")
        ply = conn.execute(text("""
            SELECT count(*) AS total,
                   count(DISTINCT player_id) AS players,
                   count(court_x) AS court_x_pop,
                   count(keypoints) AS kp_pop
            FROM ml_analysis.player_detections
            WHERE job_id = :j
        """), {"j": job_id}).mappings().first()
        all_ok &= check("player_total_>0", ply["total"] > 0, ply["total"])
        all_ok &= check("player_distinct_=2", ply["players"] == 2, ply["players"], expected=2)
        all_ok &= check("player_court_x_pop_>0", ply["court_x_pop"] > 0, ply["court_x_pop"])
        all_ok &= check("player_keypoints_pop_>0", ply["kp_pop"] > 0, ply["kp_pop"])

        # Match analytics
        sub("match_analytics")
        ma = conn.execute(text("""
            SELECT bounce_count, bounces_in, bounces_out, max_speed_kmh,
                   avg_speed_kmh, rally_count, serve_count, player_count
            FROM ml_analysis.match_analytics
            WHERE job_id = :j
        """), {"j": job_id}).mappings().first()
        if ma:
            all_ok &= check("ma_bounce_count_>0", (ma["bounce_count"] or 0) > 0, ma["bounce_count"])
            all_ok &= check("ma_player_count_=2", (ma["player_count"] or 0) == 2, ma["player_count"], expected=2)
        else:
            all_ok &= check("ma_row_exists", False)

    return all_ok


# ============================================================
# Silver validation (silver.point_detail)
# ============================================================

def validate_silver(task_id: str) -> bool:
    hr(f"SILVER VALIDATION  task_id={task_id[:8]}")
    engine = get_engine()
    all_ok = True

    with engine.connect() as conn:
        # Total + base coverage
        r = conn.execute(text("""
            SELECT count(*) AS total,
                   count(DISTINCT player_id) AS players,
                   count(DISTINCT point_number) AS points,
                   count(DISTINCT game_number) AS games,
                   COALESCE(model, 'sportai') AS model
            FROM silver.point_detail
            WHERE task_id = CAST(:t AS uuid)
            GROUP BY model
        """), {"t": task_id}).mappings().first()

        if not r:
            print(f"  {FAIL} silver_rows_exist                    no rows found")
            return False

        all_ok &= check("silver_total_>0", r["total"] > 0, r["total"])
        all_ok &= check("silver_players_=2", r["players"] == 2, r["players"], expected=2)
        all_ok &= check("silver_points_>0", (r["points"] or 0) > 0, r["points"])
        all_ok &= check("silver_games_>0", (r["games"] or 0) > 0, r["games"])
        print(f"  {INFO} model                                    {r['model']}")

        # Base field coverage
        sub("base field coverage")
        bf = conn.execute(text("""
            SELECT count(*) AS total,
                   count(player_id) AS pid,
                   count(serve) AS serve,
                   count(swing_type) AS swing_type,
                   count(ball_speed) AS ball_speed,
                   count(ball_hit_s) AS ball_hit_s,
                   count(ball_hit_location_x) AS hit_x,
                   count(ball_hit_location_y) AS hit_y,
                   count(court_x) AS court_x,
                   count(court_y) AS court_y
            FROM silver.point_detail
            WHERE task_id = CAST(:t AS uuid)
        """), {"t": task_id}).mappings().first()
        for f in ("pid", "serve", "swing_type", "ball_hit_s", "hit_x", "hit_y", "court_x", "court_y"):
            pct = (bf[f] / bf["total"] * 100) if bf["total"] else 0
            all_ok &= check(f"base_{f}_>=80%", pct >= 80, f"{pct:.0f}%")

        # Derived field coverage
        sub("derived field coverage")
        df = conn.execute(text("""
            SELECT count(serve_d) AS serve_d,
                   count(point_number) AS point_num,
                   count(game_number) AS game_num,
                   count(stroke_d) AS stroke,
                   count(rally_location_bounce) AS zone_b,
                   count(depth_d) AS depth,
                   count(*) FILTER (WHERE serve_d) AS serves
            FROM silver.point_detail
            WHERE task_id = CAST(:t AS uuid)
        """), {"t": task_id}).mappings().first()
        all_ok &= check("derived_point_number_pop", (df["point_num"] or 0) > 0, df["point_num"])
        all_ok &= check("derived_game_number_pop", (df["game_num"] or 0) > 0, df["game_num"])
        all_ok &= check("derived_serve_d_pop", (df["serves"] or 0) > 0, df["serves"])
        all_ok &= check("derived_stroke_pop", (df["stroke"] or 0) > 0, df["stroke"])

    return all_ok


# ============================================================
# Validate (both)
# ============================================================

def cmd_validate(task_id: str) -> int:
    """Run both bronze + silver validation."""
    bronze_ok = validate_bronze(task_id)
    silver_ok = validate_silver(task_id)
    print()
    print(f"BRONZE: {'PASS' if bronze_ok else 'FAIL'}")
    print(f"SILVER: {'PASS' if silver_ok else 'FAIL'}")
    return 0 if (bronze_ok and silver_ok) else 1


# ============================================================
# Reconciliation (delegates to recon_silver)
# ============================================================

def cmd_reconcile(args: argparse.Namespace) -> int:
    from ml_pipeline import recon_silver
    sportai = args.sportai_tid or recon_silver.DEFAULT_SPORTAI
    t5 = args.t5_tid or recon_silver.DEFAULT_T5
    mode = args.mode

    print(f"SPORTAI: {sportai}")
    print(f"T5:      {t5}")
    print(f"MODE:    {mode}")

    engine = get_engine()
    with engine.connect() as conn:
        if mode in ("summary", "all"):
            recon_silver.run_summary(conn, sportai, t5)
        if mode in ("coverage", "all"):
            recon_silver.run_coverage(conn, sportai, t5)
        if mode in ("distributions", "all"):
            recon_silver.run_distributions(conn, sportai, t5)
        if mode in ("speed", "all"):
            recon_silver.run_speed(conn, sportai, t5)
        if mode in ("rows", "all"):
            recon_silver.run_rows(conn, sportai, t5)
    return 0


# ============================================================
# List jobs / matches
# ============================================================

def cmd_list_jobs(args: argparse.Namespace) -> int:
    hr("RECENT T5 BATCH JOBS")
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT j.job_id, j.task_id, j.status, j.created_at,
                   j.processing_time_sec, j.court_detected,
                   j.court_confidence, sc.sport_type
            FROM ml_analysis.video_analysis_jobs j
            LEFT JOIN bronze.submission_context sc ON sc.task_id = j.task_id
            ORDER BY j.created_at DESC
            LIMIT :lim
        """), {"lim": args.limit}).mappings().all()

        if not rows:
            print("  (no jobs)")
            return 0

        print(f"  {'job_id':>10s} {'created':>16s} {'status':>10s} {'sport_type':>20s} {'court':>5s} {'time':>7s}")
        for r in rows:
            ts = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "-"
            ptime = f"{r['processing_time_sec']:.0f}s" if r["processing_time_sec"] else "-"
            court = "Y" if r["court_detected"] else "N"
            print(
                f"  {str(r['job_id'])[:8]:>10s} {ts:>16s} {str(r['status']):>10s} "
                f"{str(r['sport_type'] or '-'):>20s} {court:>5s} {ptime:>7s}"
            )
    return 0


def cmd_list_matches(args: argparse.Namespace) -> int:
    hr("RECENT SILVER MATCHES")
    engine = get_engine()
    where = ""
    params = {"lim": args.limit}
    if args.source:
        where = "WHERE COALESCE(model, 'sportai') = :src"
        params["src"] = args.source
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT task_id::text AS tid,
                   COALESCE(model, 'sportai') AS model,
                   count(*) AS rows,
                   count(DISTINCT point_number) AS points,
                   count(DISTINCT game_number) AS games,
                   max(ball_hit_s) AS duration_s
            FROM silver.point_detail
            {where}
            GROUP BY tid, model
            ORDER BY max(ball_hit_s) DESC NULLS LAST
            LIMIT :lim
        """), params).mappings().all()

        if not rows:
            print("  (no matches)")
            return 0

        print(f"  {'task_id':>10s} {'model':>8s} {'rows':>6s} {'points':>7s} {'games':>6s} {'duration':>9s}")
        for r in rows:
            dur = f"{r['duration_s']:.0f}s" if r['duration_s'] else "-"
            print(
                f"  {r['tid'][:8]:>10s} {r['model']:>8s} {r['rows']:>6d} "
                f"{r['points'] or 0:>7d} {r['games'] or 0:>6d} {dur:>9s}"
            )
    return 0


# ============================================================
# Rerun commands
# ============================================================

def cmd_rerun_silver(args: argparse.Namespace) -> int:
    hr(f"RERUN SILVER  task_id={args.task_id}")
    engine = get_engine()

    # Determine model from task_id
    with engine.connect() as conn:
        st = conn.execute(text(
            "SELECT sport_type FROM bronze.submission_context WHERE task_id = :t"
        ), {"t": args.task_id}).scalar() or ""

    if st == "tennis_singles_t5":
        from ml_pipeline.build_silver_match_t5 import build_silver_match_t5
        result = build_silver_match_t5(task_id=args.task_id, replace=True, engine=engine)
        print(f"T5 silver result: {result}")
    elif st in ("serve_practice", "rally_practice"):
        from ml_pipeline.build_silver_practice import build_silver_practice
        result = build_silver_practice(task_id=args.task_id, replace=True, engine=engine)
        print(f"Practice silver result: {result}")
    else:
        # Assume SportAI
        from build_silver_v2 import build_silver_v2
        result = build_silver_v2(args.task_id, replace=True)
        print(f"SportAI silver result: {result}")
    return 0


def cmd_rerun_ingest(args: argparse.Namespace) -> int:
    hr(f"RERUN INGEST  task_id={args.task_id}")
    engine = get_engine()
    from ml_pipeline.bronze_ingest_t5 import ingest_bronze_t5
    bronze_result = ingest_bronze_t5(job_id=args.task_id, engine=engine, replace=True)
    print(f"Bronze ingest: {bronze_result}")
    return cmd_rerun_silver(args)


# ============================================================
# Golden datasets (regression testing)
# ============================================================

def _load_goldens() -> Dict[str, Any]:
    if not GOLDEN_FILE.exists():
        return {"goldens": []}
    return json.loads(GOLDEN_FILE.read_text())


def _save_goldens(data: Dict[str, Any]) -> None:
    GOLDEN_FILE.write_text(json.dumps(data, indent=2))


def _capture_metrics(task_id: str) -> Dict[str, Any]:
    """Capture key metrics from current silver data for snapshot."""
    engine = get_engine()
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT count(*) AS total,
                   count(DISTINCT player_id) AS players,
                   count(DISTINCT point_number) AS points,
                   count(DISTINCT game_number) AS games,
                   count(*) FILTER (WHERE serve_d) AS serves,
                   count(stroke_d) AS stroke_pop,
                   count(rally_location_bounce) AS zone_pop,
                   round(avg(ball_speed)::numeric, 2) AS avg_speed,
                   round(max(ball_speed)::numeric, 2) AS max_speed,
                   COALESCE(model, 'sportai') AS model
            FROM silver.point_detail
            WHERE task_id = CAST(:t AS uuid)
            GROUP BY model
        """), {"t": task_id}).mappings().first()
        return dict(r) if r else {}


def cmd_golden_list(args: argparse.Namespace) -> int:
    hr("GOLDEN DATASETS")
    data = _load_goldens()
    if not data.get("goldens"):
        print("  (no goldens registered — use 'golden-snapshot' to add one)")
        return 0
    for g in data["goldens"]:
        print(f"\n  name:       {g['name']}")
        print(f"  task_id:    {g['task_id']}")
        print(f"  model:      {g.get('model', '?')}")
        print(f"  captured:   {g.get('captured_at', '?')}")
        print(f"  metrics:    {g.get('metrics', {})}")
    return 0


def cmd_golden_snapshot(args: argparse.Namespace) -> int:
    hr(f"GOLDEN SNAPSHOT  task_id={args.task_id}  name={args.name}")
    metrics = _capture_metrics(args.task_id)
    if not metrics:
        print(f"  {FAIL} no silver data for task_id={args.task_id}")
        return 1

    data = _load_goldens()
    # Replace existing entry with same name
    data["goldens"] = [g for g in data.get("goldens", []) if g.get("name") != args.name]
    data["goldens"].append({
        "name": args.name,
        "task_id": args.task_id,
        "model": metrics.get("model", "?"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            k: (float(v) if hasattr(v, "real") and not isinstance(v, bool) else v)
            for k, v in metrics.items()
            if k != "model"
        },
    })
    _save_goldens(data)
    print(f"  {PASS} captured snapshot")
    print(f"  metrics: {metrics}")
    return 0


def cmd_golden_check(args: argparse.Namespace) -> int:
    hr(f"GOLDEN CHECK  name={args.name}")
    data = _load_goldens()
    golden = next((g for g in data.get("goldens", []) if g.get("name") == args.name), None)
    if not golden:
        print(f"  {FAIL} no golden named '{args.name}'")
        return 1

    current = _capture_metrics(golden["task_id"])
    if not current:
        print(f"  {FAIL} no current silver data for task_id={golden['task_id']}")
        return 1

    expected = golden["metrics"]
    print(f"  task_id: {golden['task_id']}")
    print(f"  captured_at: {golden.get('captured_at')}")
    print()

    # Compare each metric — allow small drift on counts (±5%), exact on player count
    all_ok = True
    for k in ("total", "points", "games", "serves", "stroke_pop", "zone_pop"):
        exp = expected.get(k)
        cur = current.get(k)
        if exp is None or cur is None:
            continue
        # Allow ±5% drift on counts
        if exp > 0:
            drift = abs(cur - exp) / exp
            ok = drift <= 0.05
        else:
            ok = cur == exp
        all_ok &= check(f"{k}", ok, f"current={cur} expected={exp}")

    # Player count must be exact
    all_ok &= check("players", current.get("players") == expected.get("players"),
                    f"current={current.get('players')} expected={expected.get('players')}")

    return 0 if all_ok else 1


# ============================================================
# Training bench
# ============================================================

def cmd_training_bench(args: argparse.Namespace) -> int:
    """Dispatch training-bench subcommands to ml_pipeline.training_bench."""
    from ml_pipeline import training_bench as tb

    sportai = args.sportai_tid or tb.DEFAULT_SPORTAI
    t5 = args.t5_tid or tb.DEFAULT_T5
    window = args.window

    print(f"SPORTAI : {sportai}")
    print(f"T5      : {t5}")
    print(f"WINDOW  : {window}s")

    engine = get_engine()

    if args.tb_cmd == "align":
        with engine.connect() as conn:
            result = tb.align_events(conn, sportai, t5, window)
        tb.print_align(result)
        return 0

    if args.tb_cmd == "serves":
        with engine.connect() as conn:
            result = tb.analyze_serves(conn, sportai, t5, window)
        tb.print_serves(result)
        return 0

    if args.tb_cmd == "features":
        with engine.connect() as conn:
            result = tb.feature_report(conn, sportai, t5, window)
        tb.print_features(result)
        return 0

    if args.tb_cmd == "extract-serves":
        with engine.connect() as conn:
            rows = tb.extract_features(conn, sportai, t5, window, serves_only=True)
        tb.print_extract(rows)
        if args.csv:
            tb.export_csv(rows, args.csv)
        return 0

    print(f"Unknown training-bench subcommand: {args.tb_cmd}")
    return 1


# ============================================================
# Dual-submit T5
# ============================================================

def cmd_dual_submit(args: argparse.Namespace) -> int:
    """
    Trigger a T5 dual-submit for an existing SportAI task_id.

    Reads the s3_key / email / player names from bronze.submission_context,
    submits a new T5 Batch job, and creates the submission_context row so
    auto-ingest can fire when the job completes.

    This calls upload_app._manual_dual_submit_t5() directly (no HTTP needed)
    when run on the Render shell (same process has DB access via db_init.engine).
    """
    hr(f"DUAL SUBMIT T5  sportai_task_id={args.sportai_task_id}")

    try:
        # Import from upload_app to reuse the same logic as the ops endpoint
        from upload_app import _manual_dual_submit_t5
        result = _manual_dual_submit_t5(args.sportai_task_id)
    except ImportError:
        # upload_app not importable (e.g. missing env vars) — call ops endpoint via HTTP
        import requests as _req
        ops_key = os.environ.get("OPS_KEY", "")
        base = os.environ.get("API_BASE_URL", "http://localhost:8000")
        resp = _req.post(
            f"{base}/ops/dual-submit-t5",
            json={"sportai_task_id": args.sportai_task_id},
            headers={"Authorization": f"Bearer {ops_key}"},
            timeout=30,
        )
        result = resp.json()

    status = result.get("status")
    if status == "submitted":
        print(f"  {PASS} submitted — t5_task_id={result.get('t5_task_id')}")
        print()
        print("  Next: poll task-status or run:")
        print(f"    python -m ml_pipeline.harness validate {result.get('t5_task_id')}")
        return 0
    elif status == "skipped":
        print(f"  {WARN} skipped — reason: {result.get('reason')}")
        return 0
    else:
        print(f"  {FAIL} unexpected result: {result}")
        return 1


# ============================================================
# Eval store history
# ============================================================

def cmd_eval_history(args: argparse.Namespace) -> int:
    from ml_pipeline.eval_store import show_history
    show_history(last_n=args.last)
    return 0


# ============================================================
# Per-component evaluation — ball
# ============================================================

# Thresholds for ball eval
_BALL_MIN_DETECTION_RATE_PCT = 5.0      # at least 5% of frames have ball detected
_BALL_MIN_BOUNCES = 5                   # need at least a few bounces to be useful
_BALL_MIN_COURT_COORD_PCT = 50.0        # at least half of detected balls have court coords
_BALL_MIN_SPEED_POP_PCT = 20.0          # at least 20% have a valid speed reading
_BALL_MAX_SPEED_KMH = 300.0             # sanity cap


def cmd_eval_ball(args: argparse.Namespace) -> int:
    """
    Evaluate ball detection quality for a T5 job.

    Looks up the job_id for the given task_id, then analyses
    ml_analysis.ball_detections and ml_analysis.video_analysis_jobs.
    """
    task_id = args.task_id
    hr(f"EVAL BALL  task_id={task_id[:8]}")
    engine = get_engine()
    all_ok = True

    with engine.connect() as conn:
        # Resolve task_id -> job_id (T5 job)
        job = conn.execute(text("""
            SELECT job_id::text AS job_id, total_frames, video_fps, video_duration_sec
            FROM ml_analysis.video_analysis_jobs
            WHERE task_id = CAST(:t AS uuid)
            ORDER BY created_at DESC
            LIMIT 1
        """), {"t": task_id}).mappings().first()

        if not job:
            print(f"  {FAIL} no T5 job found for task_id={task_id}")
            return 1

        job_id = job["job_id"]
        total_frames = job["total_frames"] or 0
        print(f"  {INFO} job_id={job_id[:8]}  total_frames={total_frames}  fps={job['video_fps']}")

        # Ball detection stats
        ball = conn.execute(text("""
            SELECT
                count(*)                                         AS detected_frames,
                count(*) FILTER (WHERE is_bounce IS TRUE)        AS bounce_count,
                count(court_x)                                   AS court_coord_pop,
                count(speed_kmh)                                 AS speed_pop,
                round(avg(speed_kmh)::numeric, 1)               AS speed_avg_kmh,
                round(percentile_cont(0.5) WITHIN GROUP
                      (ORDER BY speed_kmh)::numeric, 1)          AS speed_median_kmh,
                round(max(speed_kmh)::numeric, 1)               AS speed_max_kmh,
                round(min(speed_kmh)::numeric, 1)               AS speed_min_kmh,
                min(court_x)                                     AS court_x_min,
                max(court_x)                                     AS court_x_max,
                min(court_y)                                     AS court_y_min,
                max(court_y)                                     AS court_y_max
            FROM ml_analysis.ball_detections
            WHERE job_id = CAST(:j AS uuid)
        """), {"j": job_id}).mappings().first()

    detected = int(ball["detected_frames"] or 0)
    bounces = int(ball["bounce_count"] or 0)
    court_pop = int(ball["court_coord_pop"] or 0)
    speed_pop = int(ball["speed_pop"] or 0)
    speed_avg = ball["speed_avg_kmh"]
    speed_median = ball["speed_median_kmh"]
    speed_max = ball["speed_max_kmh"]

    # Detection rate
    det_rate_pct = (detected / total_frames * 100) if total_frames > 0 else 0.0
    court_pct = (court_pop / detected * 100) if detected > 0 else 0.0
    speed_pct = (speed_pop / detected * 100) if detected > 0 else 0.0

    sub("detection coverage")
    all_ok &= check(
        f"detection_rate_>={_BALL_MIN_DETECTION_RATE_PCT}%",
        det_rate_pct >= _BALL_MIN_DETECTION_RATE_PCT,
        f"{det_rate_pct:.1f}%  ({detected}/{total_frames} frames)",
    )
    all_ok &= check(
        f"bounce_count_>={_BALL_MIN_BOUNCES}",
        bounces >= _BALL_MIN_BOUNCES,
        bounces,
    )
    all_ok &= check(
        f"court_coord_pct_>={_BALL_MIN_COURT_COORD_PCT}%",
        court_pct >= _BALL_MIN_COURT_COORD_PCT,
        f"{court_pct:.1f}%  ({court_pop}/{detected})",
    )
    all_ok &= check(
        f"speed_pop_pct_>={_BALL_MIN_SPEED_POP_PCT}%",
        speed_pct >= _BALL_MIN_SPEED_POP_PCT,
        f"{speed_pct:.1f}%  ({speed_pop}/{detected})",
    )

    sub("speed distribution")
    speed_ok = (speed_max is None) or (0 < speed_max <= _BALL_MAX_SPEED_KMH)
    all_ok &= check(
        f"speed_max_<={_BALL_MAX_SPEED_KMH}km/h",
        speed_ok,
        f"max={speed_max}  avg={speed_avg}  median={speed_median} (km/h)",
    )

    sub("court coordinate range")
    cx_ok = ball["court_x_min"] is None or (
        float(ball["court_x_min"]) >= -2.0 and float(ball["court_x_max"]) <= 13.0
    )
    cy_ok = ball["court_y_min"] is None or (
        float(ball["court_y_min"]) >= -2.0 and float(ball["court_y_max"]) <= 26.0
    )
    all_ok &= check(
        "court_x_in_range[-2..13]",
        cx_ok,
        f"[{ball['court_x_min']}, {ball['court_x_max']}]",
    )
    all_ok &= check(
        "court_y_in_range[-2..26]",
        cy_ok,
        f"[{ball['court_y_min']}, {ball['court_y_max']}]",
    )

    # Persist to eval store
    metrics = {
        "detection_rate_pct": round(det_rate_pct, 2),
        "detected_frames": detected,
        "total_frames": total_frames,
        "bounce_count": bounces,
        "court_coord_pct": round(court_pct, 2),
        "speed_pop_pct": round(speed_pct, 2),
        "speed_avg_kmh": float(speed_avg) if speed_avg is not None else None,
        "speed_median_kmh": float(speed_median) if speed_median is not None else None,
        "speed_max_kmh": float(speed_max) if speed_max is not None else None,
    }
    from ml_pipeline.eval_store import record_component_eval
    record_component_eval(task_id=task_id, component="ball", passed=all_ok, metrics=metrics)

    print()
    print(f"BALL EVAL: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


# ============================================================
# Per-component evaluation — player
# ============================================================

# Thresholds for player eval
_PLAYER_EXPECTED_IDS = 2
_PLAYER_MIN_FRAMES_PER_PLAYER = 50       # each player should appear in >=50 frames
_PLAYER_MIN_COORD_VARIANCE = 0.5         # court_x variance — too low = fixed coords bug
_PLAYER_MAX_COORD_VARIANCE = 200.0       # sanity cap — extremely noisy tracking


def cmd_eval_player(args: argparse.Namespace) -> int:
    """
    Evaluate player detection quality for a T5 job.

    Checks: unique player IDs, frames per player, coordinate variance
    (a very low variance signals the "fixed coordinates" bug), path length.
    """
    task_id = args.task_id
    hr(f"EVAL PLAYER  task_id={task_id[:8]}")
    engine = get_engine()
    all_ok = True

    with engine.connect() as conn:
        job = conn.execute(text("""
            SELECT job_id::text AS job_id, total_frames
            FROM ml_analysis.video_analysis_jobs
            WHERE task_id = CAST(:t AS uuid)
            ORDER BY created_at DESC
            LIMIT 1
        """), {"t": task_id}).mappings().first()

        if not job:
            print(f"  {FAIL} no T5 job found for task_id={task_id}")
            return 1

        job_id = job["job_id"]
        total_frames = job["total_frames"] or 0
        print(f"  {INFO} job_id={job_id[:8]}  total_frames={total_frames}")

        # Per-player stats
        per_player = conn.execute(text("""
            SELECT
                player_id,
                count(*)                                  AS frame_count,
                round(var_pop(court_x)::numeric, 3)       AS var_x,
                round(var_pop(court_y)::numeric, 3)       AS var_y,
                round(avg(court_x)::numeric, 2)           AS avg_x,
                round(avg(court_y)::numeric, 2)           AS avg_y,
                count(court_x)                            AS court_x_pop,
                count(keypoints)                          AS kp_pop
            FROM ml_analysis.player_detections
            WHERE job_id = CAST(:j AS uuid)
            GROUP BY player_id
            ORDER BY player_id
        """), {"j": job_id}).mappings().all()

        # Overall summary
        summary = conn.execute(text("""
            SELECT
                count(DISTINCT player_id)                 AS unique_players,
                count(*)                                  AS total_detections,
                count(court_x)                            AS court_pop,
                count(keypoints)                          AS kp_pop
            FROM ml_analysis.player_detections
            WHERE job_id = CAST(:j AS uuid)
        """), {"j": job_id}).mappings().first()

    unique_players = int(summary["unique_players"] or 0)
    total_detections = int(summary["total_detections"] or 0)
    court_pop = int(summary["court_pop"] or 0)
    kp_pop = int(summary["kp_pop"] or 0)

    sub("player ID coverage")
    all_ok &= check(
        f"unique_player_ids_=={_PLAYER_EXPECTED_IDS}",
        unique_players == _PLAYER_EXPECTED_IDS,
        unique_players,
        expected=_PLAYER_EXPECTED_IDS,
    )
    all_ok &= check("total_detections_>0", total_detections > 0, total_detections)
    court_pct = (court_pop / total_detections * 100) if total_detections > 0 else 0.0
    all_ok &= check("court_coord_pop_>0", court_pop > 0, f"{court_pct:.1f}% ({court_pop}/{total_detections})")
    all_ok &= check("keypoints_pop_>0", kp_pop > 0, kp_pop)

    sub("per-player detail")
    variance_values = []
    for p in per_player:
        pid = p["player_id"]
        frames = int(p["frame_count"] or 0)
        vx = float(p["var_x"] or 0)
        vy = float(p["var_y"] or 0)
        coord_var = round((vx + vy) / 2, 3)
        variance_values.append(coord_var)

        frames_ok = frames >= _PLAYER_MIN_FRAMES_PER_PLAYER
        # Coord variance: low = fixed coords bug, very high = noisy tracking
        var_ok = _PLAYER_MIN_COORD_VARIANCE <= coord_var <= _PLAYER_MAX_COORD_VARIANCE
        all_ok &= check(
            f"player_{pid}_frames_>={_PLAYER_MIN_FRAMES_PER_PLAYER}",
            frames_ok,
            f"{frames} frames  avg_x={p['avg_x']} avg_y={p['avg_y']}",
        )
        all_ok &= check(
            f"player_{pid}_coord_variance_in_range",
            var_ok,
            f"var_avg={coord_var}  (var_x={vx} var_y={vy})",
        )

    coord_variance_avg = round(sum(variance_values) / len(variance_values), 3) if variance_values else 0.0

    # Persist to eval store
    metrics = {
        "unique_player_ids": unique_players,
        "total_detections": total_detections,
        "total_frames": total_frames,
        "court_coord_pop": court_pop,
        "keypoints_pop": kp_pop,
        "coord_variance_avg": coord_variance_avg,
        "per_player": [
            {
                "player_id": str(p["player_id"]),
                "frame_count": int(p["frame_count"] or 0),
                "var_x": float(p["var_x"] or 0),
                "var_y": float(p["var_y"] or 0),
                "avg_x": float(p["avg_x"] or 0),
                "avg_y": float(p["avg_y"] or 0),
            }
            for p in per_player
        ],
    }
    from ml_pipeline.eval_store import record_component_eval
    record_component_eval(task_id=task_id, component="player", passed=all_ok, metrics=metrics)

    print()
    print(f"PLAYER EVAL: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


# ============================================================
# Per-component evaluation — court
# ============================================================

# Thresholds for court eval
_COURT_MIN_SUCCESS_RATE_PCT = 30.0      # at least 30% of frames have valid homography
_COURT_MIN_AVG_KEYPOINTS = 4.0          # on average at least 4 keypoints detected
_COURT_MAX_REPROJ_ERROR = 15.0          # reprojection error threshold (pixels)


def cmd_eval_court(args: argparse.Namespace) -> int:
    """
    Evaluate court detection quality for a T5 job.

    Uses ml_analysis.video_analysis_jobs (job-level court stats).
    Player court coords are a proxy for homography quality: if they exist
    the homography was applied successfully.
    """
    task_id = args.task_id
    hr(f"EVAL COURT  task_id={task_id[:8]}")
    engine = get_engine()
    all_ok = True

    with engine.connect() as conn:
        job = conn.execute(text("""
            SELECT job_id::text AS job_id,
                   total_frames,
                   court_detected,
                   court_confidence,
                   processing_time_sec,
                   bronze_s3_key
            FROM ml_analysis.video_analysis_jobs
            WHERE task_id = CAST(:t AS uuid)
            ORDER BY created_at DESC
            LIMIT 1
        """), {"t": task_id}).mappings().first()

        if not job:
            print(f"  {FAIL} no T5 job found for task_id={task_id}")
            return 1

        job_id = job["job_id"]
        total_frames = job["total_frames"] or 0
        court_detected = bool(job["court_detected"])
        court_confidence = float(job["court_confidence"] or 0)
        print(f"  {INFO} job_id={job_id[:8]}  total_frames={total_frames}")
        print(f"  {INFO} court_detected={court_detected}  court_confidence={court_confidence:.3f}")

        # Use player_detections as a proxy for frame-level homography success:
        # frames where court_x IS NOT NULL had a valid homography applied
        proxy = conn.execute(text("""
            SELECT
                count(*)                                              AS total_player_frames,
                count(court_x) FILTER (WHERE court_x IS NOT NULL)    AS frames_with_court_coords,
                round(avg(
                    CASE WHEN court_x IS NOT NULL
                    THEN array_length(
                        ARRAY(
                            SELECT jsonb_array_elements_text(keypoints)
                        ), 1)
                    END
                )::numeric, 1)                                        AS avg_kp_elements
            FROM ml_analysis.player_detections
            WHERE job_id = CAST(:j AS uuid)
        """), {"j": job_id}).mappings().first()

        # Ball detections provide another signal: how many have court coords
        ball_proxy = conn.execute(text("""
            SELECT
                count(*)           AS total_ball_frames,
                count(court_x)     AS ball_with_court_coords
            FROM ml_analysis.ball_detections
            WHERE job_id = CAST(:j AS uuid)
        """), {"j": job_id}).mappings().first()

    total_player_frames = int(proxy["total_player_frames"] or 0)
    frames_with_coords = int(proxy["frames_with_court_coords"] or 0)
    total_ball_frames = int(ball_proxy["total_ball_frames"] or 0)
    ball_with_coords = int(ball_proxy["ball_with_court_coords"] or 0)

    # Homography success rate: player frames with court coords / total player frames
    homography_rate_pct = (
        frames_with_coords / total_player_frames * 100
    ) if total_player_frames > 0 else 0.0

    ball_coord_pct = (
        ball_with_coords / total_ball_frames * 100
    ) if total_ball_frames > 0 else 0.0

    sub("job-level court detection")
    all_ok &= check("court_detected", court_detected, court_detected)
    all_ok &= check(
        "court_confidence_>=0.5",
        court_confidence >= 0.5,
        f"{court_confidence:.3f}",
    )

    sub("frame-level homography quality (player coord proxy)")
    all_ok &= check(
        f"homography_success_>={_COURT_MIN_SUCCESS_RATE_PCT}%",
        homography_rate_pct >= _COURT_MIN_SUCCESS_RATE_PCT,
        f"{homography_rate_pct:.1f}%  ({frames_with_coords}/{total_player_frames} player frames)",
    )

    sub("ball court coord coverage")
    print(
        f"  {INFO} ball_coord_coverage                  "
        f"{ball_coord_pct:.1f}%  ({ball_with_coords}/{total_ball_frames})"
    )

    # Persist to eval store
    metrics = {
        "court_detected": court_detected,
        "court_confidence": court_confidence,
        "homography_success_rate_pct": round(homography_rate_pct, 2),
        "frames_with_court_coords": frames_with_coords,
        "total_player_frames": total_player_frames,
        "ball_coord_pct": round(ball_coord_pct, 2),
        "total_frames": total_frames,
        # avg_keypoint_count not easily computed without unnesting JSONB here;
        # store None so the field exists in the schema for future use
        "avg_keypoint_count": None,
    }
    from ml_pipeline.eval_store import record_component_eval
    record_component_eval(task_id=task_id, component="court", passed=all_ok, metrics=metrics)

    print()
    print(f"COURT EVAL: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


# ============================================================
# CLI dispatch
# ============================================================

def main():
    p = argparse.ArgumentParser(prog="ml_pipeline.harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_vb = sub.add_parser("validate-bronze")
    p_vb.add_argument("task_id")

    p_vs = sub.add_parser("validate-silver")
    p_vs.add_argument("task_id")

    p_v = sub.add_parser("validate")
    p_v.add_argument("task_id")

    p_r = sub.add_parser("reconcile")
    p_r.add_argument("sportai_tid", nargs="?")
    p_r.add_argument("t5_tid", nargs="?")
    p_r.add_argument("--mode", default="all",
                     choices=["all", "summary", "coverage", "distributions", "speed", "rows"])

    p_lj = sub.add_parser("list-jobs")
    p_lj.add_argument("--limit", type=int, default=20)

    p_lm = sub.add_parser("list-matches")
    p_lm.add_argument("--limit", type=int, default=20)
    p_lm.add_argument("--source", choices=["sportai", "t5"], default=None)

    p_rs = sub.add_parser("rerun-silver")
    p_rs.add_argument("task_id")

    p_ri = sub.add_parser("rerun-ingest")
    p_ri.add_argument("task_id")

    p_ds = sub.add_parser("dual-submit")
    p_ds.add_argument("sportai_task_id", help="Existing SportAI task_id to dual-submit to T5")

    p_gl = sub.add_parser("golden-list")

    p_gs = sub.add_parser("golden-snapshot")
    p_gs.add_argument("task_id")
    p_gs.add_argument("--name", required=True)

    p_gc = sub.add_parser("golden-check")
    p_gc.add_argument("name")

    # Training bench — event-level alignment + feature analysis
    p_tb = sub.add_parser("training-bench")
    tb_sub = p_tb.add_subparsers(dest="tb_cmd", required=True)

    def _add_tb_base(sp):
        sp.add_argument("sportai_tid", nargs="?", default=None)
        sp.add_argument("t5_tid", nargs="?", default=None)
        sp.add_argument("--window", type=float, default=1.0,
                        help="Alignment window in seconds (default 1.0)")

    p_tb_align = tb_sub.add_parser("align")
    _add_tb_base(p_tb_align)

    p_tb_serves = tb_sub.add_parser("serves")
    _add_tb_base(p_tb_serves)

    p_tb_feat = tb_sub.add_parser("features")
    _add_tb_base(p_tb_feat)

    p_tb_extract = tb_sub.add_parser("extract-serves")
    _add_tb_base(p_tb_extract)
    p_tb_extract.add_argument("--csv", default=None, metavar="PATH",
                              help="Write output to CSV file at PATH")

    p_eh = sub.add_parser("eval-history")
    p_eh.add_argument("--last", type=int, default=10,
                      help="Number of most recent entries to show (default 10)")

    p_eb = sub.add_parser("eval-ball")
    p_eb.add_argument("task_id")

    p_ep = sub.add_parser("eval-player")
    p_ep.add_argument("task_id")

    p_ec = sub.add_parser("eval-court")
    p_ec.add_argument("task_id")

    args = p.parse_args()

    if args.cmd == "validate-bronze":
        return 0 if validate_bronze(args.task_id) else 1
    if args.cmd == "validate-silver":
        return 0 if validate_silver(args.task_id) else 1
    if args.cmd == "validate":
        return cmd_validate(args.task_id)
    if args.cmd == "reconcile":
        return cmd_reconcile(args)
    if args.cmd == "list-jobs":
        return cmd_list_jobs(args)
    if args.cmd == "list-matches":
        return cmd_list_matches(args)
    if args.cmd == "rerun-silver":
        return cmd_rerun_silver(args)
    if args.cmd == "rerun-ingest":
        return cmd_rerun_ingest(args)
    if args.cmd == "dual-submit":
        return cmd_dual_submit(args)
    if args.cmd == "golden-list":
        return cmd_golden_list(args)
    if args.cmd == "golden-snapshot":
        return cmd_golden_snapshot(args)
    if args.cmd == "golden-check":
        return cmd_golden_check(args)
    if args.cmd == "training-bench":
        return cmd_training_bench(args)
    if args.cmd == "eval-history":
        return cmd_eval_history(args)
    if args.cmd == "eval-ball":
        return cmd_eval_ball(args)
    if args.cmd == "eval-player":
        return cmd_eval_player(args)
    if args.cmd == "eval-court":
        return cmd_eval_court(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
