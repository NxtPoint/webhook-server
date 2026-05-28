"""Bounce detector bench — runs detector against corpus tasks and reports
recall / precision / spatial error vs the SportAI ball-position labels.

The bounce-layer equivalent of `bench` (serve) and `bench_ball` (tracker).
LOCAL-ONLY — not CI-gated. The serve `bench.py` is the only CI gate
(`.github/workflows/bench.yml`), and per CLAUDE.md rule #9 we don't widen
that gate. This bench runs against the live prod DB (the dev box's IP is
allowlisted) plus S3-hosted corpus labels from `training/labels/`.

Usage:
    python -m ml_pipeline.diag.bench_bounce
    python -m ml_pipeline.diag.bench_bounce --task <UUID>      # single task
    python -m ml_pipeline.diag.bench_bounce --threshold 0.45   # override

v0 expectations (STOPGAP-untrained):
  - recall ~0%, precision ~0%
  - the bench MUST run end-to-end without exceptions
  - this proves the plumbing works (DB reads + pre-gates + features + scoring)
  - once trained weights land, baseline values lock in `bench_baseline_bounce.json`

Metrics per task:
  - candidates           : raw is_bounce flags pulled from bronze
  - pre_gate_kept        : candidates that passed all three gates
  - emitted              : bounces written by the detector after NMS
  - corpus_labels        : floor-type labels available for this task
  - matched              : labels matched within (1.0 m, 0.2 s) of an emitted bounce
  - recall_pct           : matched / corpus_labels
  - precision_pct        : matched / emitted (NaN if emitted == 0)
  - spatial_err_mean_m   : mean distance between matched pairs
  - spatial_err_median_m : median distance between matched pairs
  - spatial_err_p90_m    : 90th percentile distance
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text as sql_text


BASELINE_PATH = Path("ml_pipeline/diag/bench_baseline_bounce.json")
S3_BUCKET = "nextpoint-prod-uploads"
S3_LABEL_PREFIX = "training/labels/"

# Match tolerance per ADR §"Threshold defaults":
#   Spatial TP tolerance 1.0 m in-bounds, ±0.2 s
DEFAULT_DIST_TOL_M = 1.0
DEFAULT_TIME_TOL_S = 0.2

logger = logging.getLogger("bench_bounce")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
    except Exception:
        return "unknown"


def _load_corpus_tasks(conn) -> list[dict]:
    """List all (t5_task_id, sa_task_id, label_s3_key) ball_position rows
    from ml_analysis.training_corpus."""
    rows = conn.execute(sql_text("""
        SELECT t5_task_id, sa_task_id, label_s3_key, label_count
        FROM ml_analysis.training_corpus
        WHERE label_kind = 'ball_position'
        ORDER BY created_at
    """)).mappings().all()
    return [dict(r) for r in rows]


def _fetch_labels_json(s3_uri: str) -> dict:
    """Fetch a labels JSON from s3:// — returns the parsed dict."""
    assert s3_uri.startswith("s3://"), s3_uri
    bucket, key = s3_uri[5:].split("/", 1)
    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"failed to fetch {s3_uri}: {exc}") from exc
    return json.loads(obj["Body"].read())


def _floor_labels(labels_doc: dict) -> list[dict]:
    """Return only the floor-type labels (= ground bounces). Drops 'swing'."""
    return [
        l for l in labels_doc.get("labels", [])
        if l.get("type") == "floor"
        and l.get("court_x") is not None
        and l.get("court_y") is not None
    ]


def _match_labels_to_events(
    labels: list[dict],
    events: list,
    dist_tol_m: float,
    time_tol_s: float,
) -> tuple[int, list[float]]:
    """Greedy match: for each label, find the closest unmatched event in
    time within ±time_tol_s, and confirm it's within dist_tol_m. Returns
    (n_matched, list_of_match_distances_m).

    Greedy by label order — fine for v0 metrics (precision/recall don't
    change vs Hungarian for the small label counts we have).
    """
    if not labels or not events:
        return (0, [])
    used: set[int] = set()
    matched = 0
    distances: list[float] = []
    for lbl in labels:
        lbl_ts = float(lbl.get("timestamp"))
        lbl_cx = float(lbl["court_x"])
        lbl_cy = float(lbl["court_y"])
        best_idx = None
        best_dt = math.inf
        for j, ev in enumerate(events):
            if j in used:
                continue
            dt = abs(ev.ts - lbl_ts)
            if dt > time_tol_s:
                continue
            if ev.court_x is None or ev.court_y is None:
                continue
            d = math.sqrt(
                (ev.court_x - lbl_cx) ** 2 + (ev.court_y - lbl_cy) ** 2
            )
            if d > dist_tol_m:
                continue
            # Pick the closest in time first (PES convention)
            if dt < best_dt:
                best_dt = dt
                best_idx = j
        if best_idx is not None:
            used.add(best_idx)
            matched += 1
            ev = events[best_idx]
            distances.append(math.sqrt(
                (ev.court_x - lbl_cx) ** 2 + (ev.court_y - lbl_cy) ** 2
            ))
    return (matched, distances)


def _run_one(
    *,
    engine,
    t5_task_id: str,
    sa_task_id: str,
    label_s3_key: str,
    threshold_override: Optional[float],
    dist_tol_m: float,
    time_tol_s: float,
) -> dict:
    """Run detector + reconcile against corpus labels for one task."""
    # Import here so the bench can fail fast on import errors with a clear msg.
    from ml_pipeline.bounce_detector.detector import (
        _load_ball_rows,
        _load_rally_states_by_frame,
        _load_wrist_positions,
        detect_bounces_offline,
    )

    with engine.connect() as conn:
        fps = conn.execute(sql_text(
            "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
            "WHERE job_id = :t OR task_id = :t LIMIT 1"
        ), {"t": t5_task_id}).scalar() or 25.0
        ball_rows = _load_ball_rows(conn, t5_task_id)
        if not ball_rows:
            return {
                "task": t5_task_id[:8],
                "error": "no ball_detections rows",
            }
        last_frame_idx = max(int(r["frame_idx"]) for r in ball_rows)
        wrists_by_frame = _load_wrist_positions(conn, t5_task_id)
        rally_by_frame = _load_rally_states_by_frame(
            conn, t5_task_id, fps, last_frame_idx,
        )

    events = detect_bounces_offline(
        task_id=t5_task_id,
        fps=fps,
        ball_rows=ball_rows,
        wrists_by_frame=wrists_by_frame,
        rally_by_frame=rally_by_frame,
        weights_path=None,
        threshold_override=threshold_override,
    )

    labels_doc = _fetch_labels_json(label_s3_key)
    floor_labels = _floor_labels(labels_doc)
    matched, distances = _match_labels_to_events(
        floor_labels, events, dist_tol_m=dist_tol_m, time_tol_s=time_tol_s,
    )

    recall_pct = (100.0 * matched / len(floor_labels)) if floor_labels else 0.0
    precision_pct = (100.0 * matched / len(events)) if events else float("nan")

    raw_candidates = sum(1 for r in ball_rows if r.get("is_bounce"))

    return {
        "task": t5_task_id[:8],
        "task_id": t5_task_id,
        "sa_task_id": sa_task_id,
        "fps": fps,
        "candidates": raw_candidates,
        "emitted": len(events),
        "corpus_floor_labels": len(floor_labels),
        "matched": matched,
        "recall_pct": round(recall_pct, 2),
        "precision_pct": (round(precision_pct, 2) if not math.isnan(precision_pct) else None),
        "spatial_err_mean_m": (round(statistics.mean(distances), 3) if distances else None),
        "spatial_err_median_m": (round(statistics.median(distances), 3) if distances else None),
        "spatial_err_p90_m": (round(_p90(distances), 3) if distances else None),
    }


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(round(0.9 * (len(s) - 1))))
    return s[idx]


def _save_baseline(data: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _ensure_baseline_seed() -> None:
    """Create the baseline file as an empty stub if missing. v0 doesn't
    lock-in numbers (untrained model) but committing the stub means the
    next session has a place to write trained-weight metrics into."""
    if BASELINE_PATH.exists():
        return
    stub = {
        "_note": "bench_bounce baseline — v0 STOPGAP, no values locked yet. "
                 "Next session: train v1, run `python -m ml_pipeline.diag.bench_bounce "
                 "--update-baseline`, commit.",
        "updated_at": date.today().isoformat(),
        "commit": _git_sha(),
        "fixtures": {},
    }
    _save_baseline(stub)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=None,
                    help="Restrict to one T5 task_id (default: all corpus tasks)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="CNN threshold override (default uses STOPGAP 1.1 untrained, "
                         "0.55 trained)")
    ap.add_argument("--dist-tol-m", type=float, default=DEFAULT_DIST_TOL_M)
    ap.add_argument("--time-tol-s", type=float, default=DEFAULT_TIME_TOL_S)
    ap.add_argument("--update-baseline", action="store_true",
                    help="Write current results as the locked baseline (use after "
                         "training v1 + manual review)")
    ap.add_argument("--json-out", default=None,
                    help="Optional path to dump the per-task JSON report")
    args = ap.parse_args(argv)

    _ensure_baseline_seed()

    from db_init import engine
    with engine.connect() as conn:
        corpus_tasks = _load_corpus_tasks(conn)

    if args.task:
        corpus_tasks = [t for t in corpus_tasks if t["t5_task_id"] == args.task]
        if not corpus_tasks:
            print(f"No corpus row for task_id={args.task}", file=sys.stderr)
            return 1

    if not corpus_tasks:
        print("No ball_position rows in ml_analysis.training_corpus. "
              "Run dual-submit to populate. See "
              "ml_pipeline/training/label_ball_positions.py.",
              file=sys.stderr)
        return 1

    print(f"=== bench_bounce  {len(corpus_tasks)} corpus tasks  "
          f"commit={_git_sha()} ===")
    print(f"    dist_tol_m={args.dist_tol_m}  time_tol_s={args.time_tol_s}")
    print(f"    threshold_override={args.threshold} "
          f"(default UNTRAINED=1.1, TRAINED=0.55)")
    print()
    print(f"{'task':<10} {'fps':>6} {'cand':>6} {'emit':>6} "
          f"{'floor':>6} {'match':>6} {'rec%':>6} {'prec%':>6} "
          f"{'mean_m':>8} {'med_m':>8} {'p90_m':>8}")
    print("-" * 88)

    results: list[dict] = []
    for t in corpus_tasks:
        r = _run_one(
            engine=engine,
            t5_task_id=t["t5_task_id"],
            sa_task_id=t["sa_task_id"],
            label_s3_key=t["label_s3_key"],
            threshold_override=args.threshold,
            dist_tol_m=args.dist_tol_m,
            time_tol_s=args.time_tol_s,
        )
        results.append(r)
        if "error" in r:
            print(f"{r['task']:<10} ERROR: {r['error']}")
            continue
        prec_str = f"{r['precision_pct']:.1f}" if r["precision_pct"] is not None else "  -- "
        mean_str = f"{r['spatial_err_mean_m']:.2f}" if r["spatial_err_mean_m"] is not None else "  -- "
        med_str = f"{r['spatial_err_median_m']:.2f}" if r["spatial_err_median_m"] is not None else "  -- "
        p90_str = f"{r['spatial_err_p90_m']:.2f}" if r["spatial_err_p90_m"] is not None else "  -- "
        print(
            f"{r['task']:<10} {r['fps']:>6.1f} {r['candidates']:>6} "
            f"{r['emitted']:>6} {r['corpus_floor_labels']:>6} "
            f"{r['matched']:>6} {r['recall_pct']:>6.1f} "
            f"{prec_str:>6} {mean_str:>8} {med_str:>8} {p90_str:>8}"
        )

    print()
    json_report = {
        "commit": _git_sha(),
        "generated_at": date.today().isoformat(),
        "dist_tol_m": args.dist_tol_m,
        "time_tol_s": args.time_tol_s,
        "threshold_override": args.threshold,
        "tasks": results,
    }
    print(json.dumps(json_report, indent=2, default=str))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(json_report, indent=2, default=str))
        print(f"\n-> wrote JSON report to {args.json_out}")

    if args.update_baseline:
        new_baseline = {
            "updated_at": date.today().isoformat(),
            "commit": _git_sha(),
            "fixtures": {r["task"]: r for r in results if "error" not in r},
        }
        _save_baseline(new_baseline)
        print(f"\n-> wrote new baseline to {BASELINE_PATH}")
        print("   Commit it: git add ml_pipeline/diag/bench_baseline_bounce.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
