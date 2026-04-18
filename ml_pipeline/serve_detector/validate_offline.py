"""Offline validation — run detector against local pose JSONL + DB ball data,
compare to SportAI ground truth.

Usage (repo root, .venv active, DATABASE_URL set):

    python -m ml_pipeline.serve_detector.validate_offline \\
        ml_pipeline/diag/local_poses_081e089c.jsonl

Reports precision, recall, timestamp alignment error, and writes the
fired ServeEvent list to stdout for visual review against the SportAI
ground truth (14 near-player + 10 far-player serves on task 4a194ff3).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Sequence

from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.serve_detector import SignalSource
from ml_pipeline.serve_detector.detector import detect_serves_offline


T5_TID = "081e089c-f7b1-49ce-b51c-d623bcc60953"
SA_TID = "4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb"


def _load_pose_jsonl(path: Path, fps: float, role: str) -> list:
    """Read the local pose dump, filter to a role, attach court projection.

    The local dump doesn't know court coords — we use a pixel-y
    threshold as a stand-in for the near/far-baseline zone prior.
    Near baseline = pixel cy > 700 → court_y ≈ 24 (we just stamp it as 23.77
    for the spatial prior). We assume pixel cy > 700 = near, < 400 = far.
    """
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r["role"] != role:
                continue
            cy_px = r["cy"]
            # Stand-in court_y so _baseline_zone() accepts this row.
            # Real pipeline supplies court_y from homography.
            if role == "near":
                # Approximate: pixel cy 700 ≈ court_y 22 (inside baseline zone)
                # pixel cy 1000 ≈ court_y 25 (past baseline)
                # Linear map py 700..1000 → cy 22..25 (roughly)
                if cy_px >= 700:
                    court_y = 22.0 + (cy_px - 700) / 300.0 * 3.0
                else:
                    # Near player mid-court: use court_y ~=15-21 proportional,
                    # BUT detector's baseline-zone filter will reject these.
                    # That's actually what we want — serves come from baseline.
                    court_y = 15.0 + (cy_px - 400) / 300.0 * 6.0
                court_y = max(0, min(30, court_y))
            else:  # far
                # pixel cy ~250 ≈ court_y ~0
                court_y = max(-3, min(10, (cy_px - 250) / 300.0 * 10.0))
            rows.append({
                "frame_idx": r["frame_idx"],
                "keypoints": r["kps"],
                "court_y": court_y,
                "court_x": None,
                "bbox": tuple(r["bbox"]),
            })
    return rows


def _load_ball_rows(conn, task_id: str) -> list:
    rs = conn.execute(sql_text("""
        SELECT frame_idx, x, y, is_bounce, court_x, court_y
        FROM ml_analysis.ball_detections
        WHERE job_id = :tid ORDER BY frame_idx
    """), {"tid": task_id}).mappings().all()
    return [dict(r) for r in rs]


def _load_sa_truth(conn) -> list:
    rs = conn.execute(sql_text("""
        SELECT ball_hit_s, serve_side_d,
               CASE WHEN ball_hit_location_y > 22 THEN 'NEAR'
                    WHEN ball_hit_location_y < 2 THEN 'FAR'
                    ELSE '?' END AS server_role
        FROM silver.point_detail
        WHERE task_id = :tid AND model = 'sportai' AND serve_d = TRUE
        ORDER BY ball_hit_s
    """), {"tid": SA_TID}).mappings().all()
    return [dict(r) for r in rs]


def _align(predicted: list, truth: list, tolerance_s: float = 4.0):
    """Greedy one-to-one alignment by nearest timestamp within tolerance.
    Returns (matches: [(pred, tru)], false_positives: [pred], misses: [tru])."""
    used_tru = [False] * len(truth)
    matches = []
    fps_pred = []
    for p in predicted:
        best_i, best_gap = None, 1e9
        for i, t in enumerate(truth):
            if used_tru[i]:
                continue
            gap = abs(p.ts - float(t["ball_hit_s"]))
            if gap < best_gap:
                best_i, best_gap = i, gap
        if best_i is not None and best_gap <= tolerance_s:
            used_tru[best_i] = True
            matches.append((p, truth[best_i], p.ts - float(truth[best_i]["ball_hit_s"])))
        else:
            fps_pred.append(p)
    misses = [truth[i] for i in range(len(truth)) if not used_tru[i]]
    return matches, fps_pred, misses


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pose_jsonl")
    ap.add_argument("--task-id", default=T5_TID)
    ap.add_argument("--left-handed", action="store_true")
    ap.add_argument("--tolerance", type=float, default=4.0,
                    help="Seconds of tolerance for aligning predicted to SA ground truth")
    args = ap.parse_args(argv)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2
    engine = create_engine(db_url)

    with engine.connect() as conn:
        fps = conn.execute(sql_text(
            "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs WHERE job_id=:t"
        ), {"t": args.task_id}).scalar() or 25.0
        ball_rows = _load_ball_rows(conn, args.task_id)
        sa_truth = _load_sa_truth(conn)

    pose_near = _load_pose_jsonl(Path(args.pose_jsonl), fps, "near")
    pose_far = _load_pose_jsonl(Path(args.pose_jsonl), fps, "far")
    print(f"Loaded: fps={fps} near_pose={len(pose_near)} far_pose={len(pose_far)} "
          f"ball_rows={len(ball_rows)} sa_truth={len(sa_truth)}")

    events = detect_serves_offline(
        task_id=args.task_id,
        pose_rows_near=pose_near,
        pose_rows_far=pose_far,
        ball_rows=ball_rows,
        is_left_handed=args.left_handed,
        fps=fps,
    )

    print()
    print("=" * 80)
    print(f"DETECTED {len(events)} SERVE EVENTS")
    print("=" * 80)
    print(f"{'ts':>7} {'pid':>3} {'source':>16} {'conf':>5} {'pose':>4} {'toss':>4} "
          f"{'bounce':>6} {'state':>14} {'cy':>5}")
    for e in events:
        cy = f"{e.hitter_court_y:.1f}" if e.hitter_court_y is not None else "-"
        print(f"{e.ts:>7.2f} {e.player_id:>3} {e.source.value:>16} "
              f"{e.confidence:>5.2f} {int(e.pose_score or 0):>4} "
              f"{str(e.has_ball_toss):>4} {str(e.bounce_frame or '-'):>6} "
              f"{e.rally_state:>14} {cy:>5}")

    print()
    print("=" * 80)
    print("SPORTAI GROUND TRUTH")
    print("=" * 80)
    print(f"{'ts':>7} {'side':>6} {'server':>6}")
    for t in sa_truth:
        print(f"{float(t['ball_hit_s']):>7.2f} {t['serve_side_d']:>6} {t['server_role']:>6}")

    # Alignment
    matches, false_positives, misses = _align(events, sa_truth, args.tolerance)
    tp = len(matches)
    fp = len(false_positives)
    fn = len(misses)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    mean_err = (sum(abs(g) for _, _, g in matches) / tp) if tp else 0.0

    print()
    print("=" * 80)
    print("ALIGNMENT REPORT")
    print("=" * 80)
    print(f"True positives:  {tp} / {len(sa_truth)} SA serves")
    print(f"False positives: {fp} (detected but no SA match)")
    print(f"False negatives: {fn} (SA serves we missed)")
    print(f"Precision:       {precision:.1%}")
    print(f"Recall:          {recall:.1%}")
    print(f"F1:              {f1:.1%}")
    print(f"Mean ts error:   {mean_err:.2f} s (on matched pairs)")
    print()
    if misses:
        print("Missed SA serves:")
        for m in misses:
            print(f"  ts={float(m['ball_hit_s']):.2f} side={m['serve_side_d']} server={m['server_role']}")
    if false_positives:
        print("False-positive detections:")
        for p in false_positives:
            print(f"  ts={p.ts:.2f} pid={p.player_id} source={p.source.value} conf={p.confidence:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
