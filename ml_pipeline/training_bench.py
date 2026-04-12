"""
ml_pipeline/training_bench.py — Event-level alignment and feature analysis
between SportAI and T5 results on silver.point_detail.

This is the systematic, code-driven version of the manual 24-serve analysis
in .claude/serve_ground_truth/sportai_4a194ff3_serves.csv.

Strategic goal: use SportAI as labeled training data to identify and fix
T5's detection gaps (serve detection, stroke classification, player ID, etc.)

All functions accept an open SQLAlchemy Connection and return plain dicts/lists
so the caller (CLI in harness.py) controls DB lifecycle.

Functions:
    align_events(conn, sportai_tid, t5_tid, window_s) -> dict
    analyze_serves(conn, sportai_tid, t5_tid, window_s) -> dict
    feature_report(conn, sportai_tid, t5_tid, window_s) -> dict
    extract_features(conn, sportai_tid, t5_tid, window_s) -> list[dict]
    export_csv(rows, path) -> None
"""

import csv
import math
import sys
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

# Default reference task IDs (same video, two pipelines)
DEFAULT_SPORTAI = "4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb"
DEFAULT_T5 = "911f0dce-56cd-4973-9e03-ea3d237cd3c0"

# Alignment window: SportAI event is matched to nearest T5 event within ±N seconds
DEFAULT_WINDOW_S = 1.0


# ============================================================
# Internal helpers
# ============================================================

def _load_silver(conn, task_id: str) -> List[Dict[str, Any]]:
    """Load all silver.point_detail rows for a task_id, ordered by ball_hit_s."""
    rows = conn.execute(text("""
        SELECT
            id,
            player_id,
            serve,
            serve_d,
            swing_type,
            stroke_d,
            volley,
            ball_speed,
            ball_hit_s,
            ball_hit_location_x,
            ball_hit_location_y,
            court_x,
            court_y,
            depth_d,
            aggression_d,
            point_number,
            game_number,
            set_number,
            shot_ix_in_point,
            shot_phase_d,
            serve_side_d,
            serve_bucket_d,
            rally_location_bounce,
            COALESCE(model, 'sportai') AS model
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
        ORDER BY ball_hit_s NULLS LAST, id
    """), {"tid": task_id}).mappings().all()
    return [dict(r) for r in rows]


def _nearest_t5(t5_rows: List[Dict], hit_s: float, window_s: float) -> Optional[Dict]:
    """Find the T5 row whose ball_hit_s is closest to hit_s within window_s.

    Skips rows where ball_hit_s is None. Returns None if no match found.
    """
    if hit_s is None:
        return None
    best = None
    best_dist = window_s + 1.0
    for row in t5_rows:
        t = row.get("ball_hit_s")
        if t is None:
            continue
        d = abs(float(t) - float(hit_s))
        if d <= window_s and d < best_dist:
            best = row
            best_dist = d
    return best


def _corr(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson correlation coefficient. Returns None if fewer than 2 pairs."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _mae(xs: List[float], ys: List[float]) -> Optional[float]:
    """Mean absolute error between two numeric lists."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if not pairs:
        return None
    return sum(abs(x - y) for x, y in pairs) / len(pairs)


# ============================================================
# 1. Event-level alignment
# ============================================================

def align_events(
    conn,
    sportai_tid: str,
    t5_tid: str,
    window_s: float = DEFAULT_WINDOW_S,
) -> Dict[str, Any]:
    """Match SportAI rows to T5 rows by ball_hit_s timestamp within ±window_s.

    Returns:
        {
            "sportai_count": int,
            "t5_count": int,
            "matched": int,
            "unmatched_sportai": list[dict],   # SportAI rows with no T5 match
            "unmatched_t5": list[dict],         # T5 rows never matched
            "pairs": list[{"sportai": dict, "t5": dict, "delta_s": float}]
        }
    """
    sp_rows = _load_silver(conn, sportai_tid)
    t5_rows = _load_silver(conn, t5_tid)

    matched_t5_ids = set()
    pairs = []
    unmatched_sp = []

    for sp in sp_rows:
        hit_s = sp.get("ball_hit_s")
        match = _nearest_t5(t5_rows, hit_s, window_s)
        if match and match["id"] not in matched_t5_ids:
            delta = abs(float(match["ball_hit_s"]) - float(hit_s)) if hit_s and match["ball_hit_s"] else None
            pairs.append({"sportai": sp, "t5": match, "delta_s": delta})
            matched_t5_ids.add(match["id"])
        else:
            unmatched_sp.append(sp)

    unmatched_t5 = [r for r in t5_rows if r["id"] not in matched_t5_ids]

    return {
        "sportai_count": len(sp_rows),
        "t5_count": len(t5_rows),
        "matched": len(pairs),
        "unmatched_sportai": unmatched_sp,
        "unmatched_t5": unmatched_t5,
        "pairs": pairs,
    }


# ============================================================
# 2. Serve detection analysis
# ============================================================

def analyze_serves(
    conn,
    sportai_tid: str,
    t5_tid: str,
    window_s: float = DEFAULT_WINDOW_S,
) -> Dict[str, Any]:
    """Analyse T5 serve detection against SportAI ground truth.

    For each SportAI serve (serve_d=TRUE), look for a matching T5 event.
    Compute recall, precision, and detail on misses.

    Returns:
        {
            "sportai_serves": int,
            "t5_serves": int,
            "tp": int,           # SportAI serve → T5 matched AND T5 serve_d=TRUE
            "fp": int,           # T5 serve_d=TRUE with no SportAI match
            "fn": int,           # SportAI serve_d=TRUE with no T5 serve match
            "recall": float,     # tp / sportai_serves
            "precision": float,  # tp / t5_serves
            "missed_serves": list[dict],   # SportAI serves that T5 missed
            "false_positives": list[dict], # T5 serves with no SportAI match
        }
    """
    alignment = align_events(conn, sportai_tid, t5_tid, window_s)
    pairs = alignment["pairs"]

    t5_rows = _load_silver(conn, t5_tid)
    t5_serves = [r for r in t5_rows if r.get("serve_d")]

    matched_t5_serve_ids = set()

    tp = 0
    fn_list = []

    for pair in pairs:
        sp = pair["sportai"]
        t5 = pair["t5"]
        if not sp.get("serve_d"):
            continue
        # This is a SportAI serve — did T5 also flag it as a serve?
        if t5.get("serve_d"):
            tp += 1
            matched_t5_serve_ids.add(t5["id"])
        else:
            # Miss: T5 had an event at this timestamp but didn't call it a serve
            fn_list.append({
                "sportai_hit_s": sp.get("ball_hit_s"),
                "sportai_player": sp.get("player_id"),
                "t5_matched": True,
                "t5_hit_s": t5.get("ball_hit_s"),
                "t5_delta_s": pair.get("delta_s"),
                "t5_swing_type": t5.get("swing_type"),
                "t5_serve": t5.get("serve"),
                "t5_serve_d": t5.get("serve_d"),
                "t5_court_x": t5.get("court_x"),
                "t5_court_y": t5.get("court_y"),
                "t5_ball_speed": t5.get("ball_speed"),
            })

    # Unmatched SportAI serves (no T5 event at all in window)
    for sp in alignment["unmatched_sportai"]:
        if sp.get("serve_d"):
            fn_list.append({
                "sportai_hit_s": sp.get("ball_hit_s"),
                "sportai_player": sp.get("player_id"),
                "t5_matched": False,
                "t5_hit_s": None,
                "t5_delta_s": None,
                "t5_swing_type": None,
                "t5_serve": None,
                "t5_serve_d": None,
                "t5_court_x": None,
                "t5_court_y": None,
                "t5_ball_speed": None,
            })

    # False positives: T5 serves that matched no SportAI serve
    fp_list = []
    for t5s in t5_serves:
        if t5s["id"] not in matched_t5_serve_ids:
            # Find nearest SportAI event for context
            sp_rows = _load_silver(conn, sportai_tid)
            nearest_sp = _nearest_t5(sp_rows, t5s.get("ball_hit_s"), window_s * 2)
            fp_list.append({
                "t5_hit_s": t5s.get("ball_hit_s"),
                "t5_player": t5s.get("player_id"),
                "t5_swing_type": t5s.get("swing_type"),
                "nearest_sportai_hit_s": nearest_sp.get("ball_hit_s") if nearest_sp else None,
                "nearest_sportai_serve_d": nearest_sp.get("serve_d") if nearest_sp else None,
            })

    sportai_serves = sum(1 for p in pairs if p["sportai"].get("serve_d")) + sum(
        1 for sp in alignment["unmatched_sportai"] if sp.get("serve_d")
    )
    t5_serve_count = len(t5_serves)
    recall = tp / sportai_serves if sportai_serves > 0 else 0.0
    precision = tp / t5_serve_count if t5_serve_count > 0 else 0.0

    return {
        "sportai_serves": sportai_serves,
        "t5_serves": t5_serve_count,
        "tp": tp,
        "fp": len(fp_list),
        "fn": len(fn_list),
        "recall": recall,
        "precision": precision,
        "missed_serves": fn_list,
        "false_positives": fp_list,
    }


# ============================================================
# 3. Feature correlation report
# ============================================================

def feature_report(
    conn,
    sportai_tid: str,
    t5_tid: str,
    window_s: float = DEFAULT_WINDOW_S,
) -> Dict[str, Any]:
    """Compare key silver fields across aligned event pairs.

    Categorical fields: agreement rate (% matching).
    Numeric fields: correlation coefficient + mean absolute error.

    Returns:
        {
            "pair_count": int,
            "categorical": {field: {"agree": int, "total": int, "rate": float}},
            "numeric": {field: {"corr": float|None, "mae": float|None, "n": int}},
        }
    """
    alignment = align_events(conn, sportai_tid, t5_tid, window_s)
    pairs = alignment["pairs"]

    categorical_fields = ["swing_type", "stroke_d", "serve_d", "depth_d", "aggression_d", "player_id"]
    numeric_fields = ["ball_speed", "court_x", "court_y", "ball_hit_location_x", "ball_hit_location_y"]

    cat_stats: Dict[str, Dict] = {f: {"agree": 0, "total": 0} for f in categorical_fields}
    num_data: Dict[str, Tuple[List, List]] = {f: ([], []) for f in numeric_fields}

    for pair in pairs:
        sp = pair["sportai"]
        t5 = pair["t5"]

        for f in categorical_fields:
            sv = sp.get(f)
            tv = t5.get(f)
            if sv is not None or tv is not None:
                cat_stats[f]["total"] += 1
                if sv == tv:
                    cat_stats[f]["agree"] += 1

        for f in numeric_fields:
            sv = sp.get(f)
            tv = t5.get(f)
            if sv is not None and tv is not None:
                try:
                    num_data[f][0].append(float(sv))
                    num_data[f][1].append(float(tv))
                except (TypeError, ValueError):
                    pass

    cat_out = {}
    for f, s in cat_stats.items():
        rate = s["agree"] / s["total"] if s["total"] > 0 else 0.0
        cat_out[f] = {"agree": s["agree"], "total": s["total"], "rate": rate}

    num_out = {}
    for f, (xs, ys) in num_data.items():
        num_out[f] = {
            "corr": _corr(xs, ys),
            "mae": _mae(xs, ys),
            "n": len(xs),
        }

    return {
        "pair_count": len(pairs),
        "categorical": cat_out,
        "numeric": num_out,
    }


# ============================================================
# 4. Raw feature extraction from ml_analysis.*
# ============================================================

def extract_features(
    conn,
    sportai_tid: str,
    t5_tid: str,
    window_s: float = DEFAULT_WINDOW_S,
    serves_only: bool = True,
) -> List[Dict[str, Any]]:
    """Extract RAW T5 detection data (ml_analysis.*) for each SportAI event.

    Maps ball_hit_s (seconds) → frame_idx using video FPS from
    ml_analysis.video_analysis_jobs. For each target frame, pulls:
      - ball_detections: court_x/y, speed_kmh, is_bounce
      - player_detections: both players' court positions and keypoints

    Args:
        conn:         open SQLAlchemy connection
        sportai_tid:  SportAI task_id (ground truth events)
        t5_tid:       T5 task_id (raw detections to extract from)
        window_s:     alignment window in seconds (passed to align_events)
        serves_only:  if True, only process SportAI events where serve_d=TRUE

    Returns list of dicts (one per SportAI event), suitable for CSV export.
    """
    # Look up the T5 job_id and FPS from ml_analysis.video_analysis_jobs
    job_row = conn.execute(text("""
        SELECT job_id, video_fps, total_frames
        FROM ml_analysis.video_analysis_jobs
        WHERE task_id = :tid
        ORDER BY created_at DESC
        LIMIT 1
    """), {"tid": t5_tid}).mappings().first()

    if not job_row:
        return []

    job_id = job_row["job_id"]
    fps = float(job_row["video_fps"] or 25.0)

    # Load SportAI events to iterate over
    sp_rows = _load_silver(conn, sportai_tid)
    if serves_only:
        sp_rows = [r for r in sp_rows if r.get("serve_d")]

    results = []
    for sp in sp_rows:
        hit_s = sp.get("ball_hit_s")
        target_frame = int(round(float(hit_s) * fps)) if hit_s is not None else None

        # Ball detection: nearest frame within ±0.5s (half-second = fps/2 frames)
        frame_tol = max(1, int(fps * 0.5))
        ball = None
        if target_frame is not None:
            ball = conn.execute(text("""
                SELECT frame_idx, court_x, court_y, speed_kmh, is_bounce, is_in
                FROM ml_analysis.ball_detections
                WHERE job_id = :jid
                  AND ABS(frame_idx - :f) <= :tol
                ORDER BY ABS(frame_idx - :f)
                LIMIT 1
            """), {"jid": job_id, "f": target_frame, "tol": frame_tol}).mappings().first()

        # Player detections: both players at that frame (or nearest)
        players = []
        if target_frame is not None:
            players = conn.execute(text("""
                SELECT player_id, frame_idx, court_x, court_y,
                       bbox_x1, bbox_y1, bbox_x2, bbox_y2
                FROM ml_analysis.player_detections
                WHERE job_id = :jid
                  AND ABS(frame_idx - :f) <= :tol
                ORDER BY ABS(frame_idx - :f), player_id
                LIMIT 4
            """), {"jid": job_id, "f": target_frame, "tol": frame_tol}).mappings().all()

        p_by_id: Dict[int, Dict] = {}
        for p in players:
            pid = p["player_id"]
            if pid not in p_by_id:
                p_by_id[pid] = dict(p)

        row = {
            # SportAI ground truth
            "sportai_hit_s": hit_s,
            "sportai_player_id": sp.get("player_id"),
            "sportai_swing_type": sp.get("swing_type"),
            "sportai_serve_d": sp.get("serve_d"),
            "sportai_stroke_d": sp.get("stroke_d"),
            "sportai_court_x": sp.get("court_x"),
            "sportai_court_y": sp.get("court_y"),
            "sportai_ball_speed": sp.get("ball_speed"),
            "sportai_depth_d": sp.get("depth_d"),
            "sportai_aggression_d": sp.get("aggression_d"),
            "sportai_point_number": sp.get("point_number"),
            "sportai_game_number": sp.get("game_number"),
            # T5 job metadata
            "t5_job_id": job_id,
            "t5_fps": fps,
            "t5_target_frame": target_frame,
            # T5 raw ball at that frame
            "t5_ball_frame": ball["frame_idx"] if ball else None,
            "t5_ball_court_x": ball["court_x"] if ball else None,
            "t5_ball_court_y": ball["court_y"] if ball else None,
            "t5_ball_speed_kmh": ball["speed_kmh"] if ball else None,
            "t5_ball_is_bounce": ball["is_bounce"] if ball else None,
            "t5_ball_found": ball is not None,
        }

        # Add player positions (player_id 0 and 1 are the two court sides)
        for pid in (0, 1):
            p = p_by_id.get(pid, {})
            row[f"t5_p{pid}_court_x"] = p.get("court_x")
            row[f"t5_p{pid}_court_y"] = p.get("court_y")
            row[f"t5_p{pid}_found"] = bool(p)

        results.append(row)

    return results


# ============================================================
# 5. CSV export
# ============================================================

def export_csv(rows: List[Dict[str, Any]], path: str) -> None:
    """Write extract_features() output to a CSV file."""
    if not rows:
        print("  (no rows to export)")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  Wrote {len(rows)} rows to {path}")


# ============================================================
# Print helpers (mirror harness.py style)
# ============================================================

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"


def _hr(title: str, char: str = "=", width: int = 78) -> None:
    print()
    print(char * width)
    print(f"  {title}")
    print(char * width)


def _sub(title: str) -> None:
    print()
    print(f"--- {title} ---")


def print_align(result: Dict) -> None:
    _hr("EVENT ALIGNMENT")
    print(f"  SportAI events : {result['sportai_count']}")
    print(f"  T5 events      : {result['t5_count']}")
    print(f"  Matched pairs  : {result['matched']}")
    print(f"  Unmatched SA   : {len(result['unmatched_sportai'])}")
    print(f"  Unmatched T5   : {len(result['unmatched_t5'])}")

    if result["matched"] > 0:
        deltas = [p["delta_s"] for p in result["pairs"] if p["delta_s"] is not None]
        if deltas:
            avg_d = sum(deltas) / len(deltas)
            max_d = max(deltas)
            print(f"  Avg delta_s    : {avg_d:.3f}s")
            print(f"  Max delta_s    : {max_d:.3f}s")

    match_pct = result["matched"] / result["sportai_count"] * 100 if result["sportai_count"] else 0
    tag = PASS if match_pct >= 50 else WARN
    print(f"\n  {tag} Match rate: {match_pct:.0f}% of SportAI events have a T5 pair")

    if result["unmatched_sportai"]:
        _sub("Unmatched SportAI events (sample, up to 10)")
        print(f"  {'hit_s':>8s}  {'player':>6s}  {'serve_d':>7s}  {'stroke_d':>10s}")
        for sp in result["unmatched_sportai"][:10]:
            print(f"  {str(sp.get('ball_hit_s') or '-'):>8s}  "
                  f"{str(sp.get('player_id') or '-'):>6s}  "
                  f"{'Y' if sp.get('serve_d') else 'N':>7s}  "
                  f"{str(sp.get('stroke_d') or '-'):>10s}")


def print_serves(result: Dict) -> None:
    _hr("SERVE DETECTION ANALYSIS")
    print(f"  SportAI serves (ground truth) : {result['sportai_serves']}")
    print(f"  T5 serves detected            : {result['t5_serves']}")
    print(f"  True positives (TP)           : {result['tp']}")
    print(f"  False negatives (FN, misses)  : {result['fn']}")
    print(f"  False positives (FP)          : {result['fp']}")

    recall_pct = result["recall"] * 100
    prec_pct = result["precision"] * 100
    r_tag = PASS if recall_pct >= 70 else (WARN if recall_pct >= 40 else FAIL)
    p_tag = PASS if prec_pct >= 70 else (WARN if prec_pct >= 40 else FAIL)
    print(f"\n  {r_tag} Recall    : {recall_pct:.1f}%")
    print(f"  {p_tag} Precision : {prec_pct:.1f}%")

    if result["missed_serves"]:
        _sub("Missed serves (FN) — SportAI serves T5 did not detect")
        print(f"  {'sa_hit_s':>8s}  {'sa_pid':>6s}  {'matched':>7s}  "
              f"{'t5_hit_s':>8s}  {'t5_swing':>10s}  {'t5_cx':>7s}  {'t5_cy':>7s}")
        for m in result["missed_serves"][:15]:
            print(
                f"  {str(m.get('sportai_hit_s') or '-'):>8s}  "
                f"{str(m.get('sportai_player') or '-'):>6s}  "
                f"{'Y' if m['t5_matched'] else 'N':>7s}  "
                f"{str(m.get('t5_hit_s') or '-'):>8s}  "
                f"{str(m.get('t5_swing_type') or '-'):>10s}  "
                f"{str(round(m['t5_court_x'], 2) if m.get('t5_court_x') is not None else '-'):>7s}  "
                f"{str(round(m['t5_court_y'], 2) if m.get('t5_court_y') is not None else '-'):>7s}"
            )

    if result["false_positives"]:
        _sub("False positives (FP) — T5 serves not in SportAI")
        print(f"  {'t5_hit_s':>8s}  {'t5_pid':>6s}  {'t5_swing':>10s}  "
              f"{'sa_nearest':>10s}  {'sa_serve_d':>10s}")
        for fp in result["false_positives"][:10]:
            print(
                f"  {str(fp.get('t5_hit_s') or '-'):>8s}  "
                f"{str(fp.get('t5_player') or '-'):>6s}  "
                f"{str(fp.get('t5_swing_type') or '-'):>10s}  "
                f"{str(fp.get('nearest_sportai_hit_s') or '-'):>10s}  "
                f"{'Y' if fp.get('nearest_sportai_serve_d') else 'N':>10s}"
            )


def print_features(result: Dict) -> None:
    _hr("FEATURE CORRELATION REPORT")
    print(f"  Aligned pairs: {result['pair_count']}")

    _sub("Categorical fields — agreement rate")
    print(f"  {'field':25s}  {'agree':>6s}  {'total':>6s}  {'rate':>7s}")
    for f, s in result["categorical"].items():
        rate_pct = s["rate"] * 100
        tag = PASS if rate_pct >= 70 else (WARN if rate_pct >= 40 else FAIL)
        print(f"  {tag} {f:22s}  {s['agree']:>6d}  {s['total']:>6d}  {rate_pct:>6.1f}%")

    _sub("Numeric fields — correlation + MAE")
    print(f"  {'field':28s}  {'n':>5s}  {'corr':>7s}  {'mae':>10s}")
    for f, s in result["numeric"].items():
        corr = f"{s['corr']:.3f}" if s["corr"] is not None else "  n/a"
        mae = f"{s['mae']:.3f}" if s["mae"] is not None else "       n/a"
        n = s["n"]
        corr_ok = s["corr"] is not None and s["corr"] >= 0.7
        tag = PASS if corr_ok else (INFO if s["corr"] is not None else WARN)
        print(f"  {tag} {f:25s}  {n:>5d}  {corr:>7s}  {mae:>10s}")


def print_extract(rows: List[Dict]) -> None:
    _hr(f"RAW FEATURE EXTRACTION — {len(rows)} events")
    found_ball = sum(1 for r in rows if r.get("t5_ball_found"))
    found_p0 = sum(1 for r in rows if r.get("t5_p0_found"))
    found_p1 = sum(1 for r in rows if r.get("t5_p1_found"))
    total = len(rows)
    print(f"  Ball found       : {found_ball}/{total} ({found_ball/total*100:.0f}%)" if total else "  (empty)")
    print(f"  Player-0 found   : {found_p0}/{total} ({found_p0/total*100:.0f}%)" if total else "")
    print(f"  Player-1 found   : {found_p1}/{total} ({found_p1/total*100:.0f}%)" if total else "")

    if rows:
        _sub("Sample rows (up to 8)")
        print(f"  {'sa_hit_s':>8s}  {'ball_cx':>7s}  {'ball_cy':>7s}  "
              f"{'speed_kph':>9s}  {'bounce':>6s}  {'p0_cx':>6s}  {'p1_cx':>6s}")
        for r in rows[:8]:
            print(
                f"  {str(r.get('sportai_hit_s') or '-'):>8s}  "
                f"{str(round(r['t5_ball_court_x'], 2) if r.get('t5_ball_court_x') is not None else '-'):>7s}  "
                f"{str(round(r['t5_ball_court_y'], 2) if r.get('t5_ball_court_y') is not None else '-'):>7s}  "
                f"{str(round(r['t5_ball_speed_kmh'], 1) if r.get('t5_ball_speed_kmh') is not None else '-'):>9s}  "
                f"{'Y' if r.get('t5_ball_is_bounce') else 'N':>6s}  "
                f"{str(round(r['t5_p0_court_x'], 2) if r.get('t5_p0_court_x') is not None else '-'):>6s}  "
                f"{str(round(r['t5_p1_court_x'], 2) if r.get('t5_p1_court_x') is not None else '-'):>6s}"
            )
