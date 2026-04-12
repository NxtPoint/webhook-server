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

Regression / golden datasets:
    golden-list                          — list registered golden snapshots
    golden-snapshot <task_id> --name N   — capture a known-good baseline
    golden-check <name>                  — validate current data against snapshot

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
    if args.cmd == "golden-list":
        return cmd_golden_list(args)
    if args.cmd == "golden-snapshot":
        return cmd_golden_snapshot(args)
    if args.cmd == "golden-check":
        return cmd_golden_check(args)
    if args.cmd == "training-bench":
        return cmd_training_bench(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
