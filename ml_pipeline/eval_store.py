"""
ml_pipeline/eval_store.py — Evaluation result persistence for T5 ML pipeline.

Appends evaluation results to ml_pipeline/eval_history.jsonl (git-tracked).
Each entry records timestamp, docker_image_tag, task IDs, and key metrics
so we can track pipeline quality over time and across docker image versions.

Functions:
    record_reconciliation(sportai_tid, t5_tid, results_dict)  — store reconcile run
    record_golden_check(name, passed, details)                — store golden check run
    record_component_eval(task_id, component, passed, metrics) — store per-component eval
    show_history(last_n=10)                                   — print formatted table
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

EVAL_HISTORY_FILE = Path(__file__).parent / "eval_history.jsonl"


# ============================================================
# Internal helpers
# ============================================================

def _image_tag() -> str:
    """Return docker image tag from env var, or 'local' if not set."""
    return os.environ.get("DOCKER_IMAGE_TAG") or os.environ.get("IMAGE_TAG") or "local"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(entry: Dict[str, Any]) -> None:
    """Append a single JSON entry to the JSONL file."""
    with EVAL_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _load_history(last_n: Optional[int] = None):
    """Load entries from eval_history.jsonl. Returns list (oldest first)."""
    if not EVAL_HISTORY_FILE.exists():
        return []
    lines = EVAL_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if last_n is not None:
        entries = entries[-last_n:]
    return entries


# ============================================================
# Public API
# ============================================================

def record_reconciliation(
    sportai_tid: str,
    t5_tid: str,
    results_dict: Dict[str, Any],
    image_tag: Optional[str] = None,
) -> None:
    """
    Record a reconciliation run (SportAI vs T5 comparison).

    results_dict should contain any subset of:
        serve_precision, serve_recall, ball_found_pct, player_count_match,
        total_rows, points_detected, games_detected,
        stroke_distribution (dict), speed_avg_kmh
    """
    entry = {
        "type": "reconciliation",
        "timestamp": _now_iso(),
        "docker_image_tag": image_tag or _image_tag(),
        "sportai_tid": sportai_tid,
        "t5_tid": t5_tid,
        "metrics": {
            "serve_precision": results_dict.get("serve_precision"),
            "serve_recall": results_dict.get("serve_recall"),
            "ball_found_pct": results_dict.get("ball_found_pct"),
            "player_count_match": results_dict.get("player_count_match"),
            "total_rows": results_dict.get("total_rows"),
            "points_detected": results_dict.get("points_detected"),
            "games_detected": results_dict.get("games_detected"),
            "stroke_distribution": results_dict.get("stroke_distribution"),
            "speed_avg_kmh": results_dict.get("speed_avg_kmh"),
        },
    }
    _append(entry)


def record_golden_check(
    name: str,
    passed: bool,
    details: Dict[str, Any],
    image_tag: Optional[str] = None,
) -> None:
    """
    Record a golden-check run.

    details should contain per-metric pass/fail and actual vs expected values.
    """
    entry = {
        "type": "golden_check",
        "timestamp": _now_iso(),
        "docker_image_tag": image_tag or _image_tag(),
        "name": name,
        "passed": passed,
        "details": details,
    }
    _append(entry)


def record_component_eval(
    task_id: str,
    component: str,
    passed: bool,
    metrics: Dict[str, Any],
    image_tag: Optional[str] = None,
) -> None:
    """
    Record a per-component evaluation run (ball / player / court).

    component: one of 'ball', 'player', 'court'
    metrics: component-specific key metrics dict
    """
    entry = {
        "type": "component_eval",
        "timestamp": _now_iso(),
        "docker_image_tag": image_tag or _image_tag(),
        "task_id": task_id,
        "component": component,
        "passed": passed,
        "metrics": metrics,
    }
    _append(entry)


def show_history(last_n: int = 10) -> None:
    """Print a formatted table of the most recent evaluation results."""
    entries = _load_history(last_n=last_n)
    if not entries:
        print("  (no eval history — run reconcile, golden-check, or component evals to populate)")
        return

    # Column widths
    W_TS = 20
    W_TYPE = 18
    W_TAG = 12
    W_SUBJECT = 20
    W_RESULT = 8

    header = (
        f"  {'timestamp':<{W_TS}} "
        f"{'type':<{W_TYPE}} "
        f"{'image_tag':<{W_TAG}} "
        f"{'subject':<{W_SUBJECT}} "
        f"{'result':<{W_RESULT}} "
        f"key_metrics"
    )
    print()
    print("=" * 110)
    print("  EVAL HISTORY")
    print("=" * 110)
    print(header)
    print("-" * 110)

    for e in entries:
        ts = e.get("timestamp", "?")[:19].replace("T", " ")
        etype = e.get("type", "?")[:W_TYPE]
        tag = str(e.get("docker_image_tag", "?"))[:W_TAG]

        etype_val = e.get("type", "")
        if etype_val == "reconciliation":
            subject = f"{e.get('sportai_tid','?')[:8]}/{e.get('t5_tid','?')[:8]}"
            result = "n/a"
            m = e.get("metrics", {})
            key = (
                f"prec={_fmt(m.get('serve_precision'))} "
                f"rec={_fmt(m.get('serve_recall'))} "
                f"ball%={_fmt(m.get('ball_found_pct'))} "
                f"rows={m.get('total_rows')}"
            )
        elif etype_val == "golden_check":
            subject = str(e.get("name", "?"))[:W_SUBJECT]
            result = "PASS" if e.get("passed") else "FAIL"
            key = _summarise_golden_details(e.get("details", {}))
        elif etype_val == "component_eval":
            component = e.get("component", "?")
            subject = f"{component}:{e.get('task_id','?')[:8]}"
            result = "PASS" if e.get("passed") else "FAIL"
            key = _summarise_component_metrics(component, e.get("metrics", {}))
        else:
            subject = "?"
            result = "?"
            key = str(e.get("metrics", ""))[:60]

        print(
            f"  {ts:<{W_TS}} "
            f"{etype:<{W_TYPE}} "
            f"{tag:<{W_TAG}} "
            f"{subject:<{W_SUBJECT}} "
            f"{result:<{W_RESULT}} "
            f"{key}"
        )

    print("-" * 110)
    print(f"  Showing last {len(entries)} entries from {EVAL_HISTORY_FILE}")


def _fmt(val) -> str:
    """Format a float to 2dp, or '-' if None."""
    if val is None:
        return "-"
    try:
        return f"{float(val):.2f}"
    except (TypeError, ValueError):
        return str(val)


def _summarise_golden_details(details: Dict[str, Any]) -> str:
    if not details:
        return ""
    fail_keys = [k for k, v in details.items() if isinstance(v, dict) and not v.get("ok", True)]
    if fail_keys:
        return f"FAILED: {', '.join(fail_keys[:4])}"
    return f"{len(details)} checks passed"


def _summarise_component_metrics(component: str, metrics: Dict[str, Any]) -> str:
    if component == "ball":
        return (
            f"det%={_fmt(metrics.get('detection_rate_pct'))} "
            f"bounces={metrics.get('bounce_count')} "
            f"speed_avg={_fmt(metrics.get('speed_avg_kmh'))}"
        )
    if component == "player":
        return (
            f"players={metrics.get('unique_player_ids')} "
            f"coord_var={_fmt(metrics.get('coord_variance_avg'))}"
        )
    if component == "court":
        return (
            f"success%={_fmt(metrics.get('homography_success_rate_pct'))} "
            f"kp={metrics.get('avg_keypoint_count')}"
        )
    return str(metrics)[:80]
