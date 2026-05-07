"""Audit T5-detected point boundaries against SA `point_number` truth.

Phase 2 reconciler. Reads:

    T5 side  (default a798eff0-...):
        ml_analysis.serve_events   → start frames + ts
        ml_analysis.ball_detections (is_bounce)  → rally evidence
        ml_analysis.video_analysis_jobs.video_fps  → fps

    SA side  (default 2c1ad953-...):
        silver.point_detail (model='sportai')
            point_number, serve_d, ball_hit_s, exclude_d
        → SA "point window" = [first event ts in point, last event ts in point]

Computes:
    1. T5 point boundaries via `detect_point_boundaries`.
    2. SA point windows from grouped point_number rows.
    3. Best-IOU assignment between the two sets.
    4. Per-SA-point match rate at the IOU threshold (default 0.5).

Output: per-point table + overall match rate. The success target is
≥80% per `docs/north_star.md` Phase 2.

NOT to be confused with `audit_points_reconcile.py` (agent RECONCILER's
parallel tool — different purpose, different metric).

Usage:
    python -m ml_pipeline.diag.audit_points
    python -m ml_pipeline.diag.audit_points --task <T5_uuid> --sportai <SA_uuid>
    python -m ml_pipeline.diag.audit_points --iou 0.4
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy import create_engine, text as sql_text


DEFAULT_T5 = "a798eff0-551f-4b5a-838f-7933866a727c"
DEFAULT_SA = "2c1ad953-b65b-41b4-9999-975964ff92e1"


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# ----------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------

@dataclass
class _Serve:
    frame_idx: int
    ts: float
    player_id: int


@dataclass
class _Bounce:
    frame_idx: int
    is_bounce: bool = True


def _load_t5_fps(conn, task_id: str) -> float:
    row = conn.execute(sql_text("""
        SELECT video_fps, total_frames, video_duration_sec
        FROM ml_analysis.video_analysis_jobs
        WHERE task_id = :tid OR job_id = :tid
        ORDER BY created_at DESC
        LIMIT 1
    """), {"tid": task_id}).mappings().first()
    if not row:
        raise RuntimeError(f"no video_analysis_jobs row for task_id={task_id}")
    fps = row.get("video_fps")
    if not fps and row.get("total_frames") and row.get("video_duration_sec"):
        fps = row["total_frames"] / row["video_duration_sec"]
    if not fps:
        fps = 25.0
    return float(fps)


def _t5_job_id(conn, task_id: str) -> str:
    """ml_analysis tables key on job_id, not task_id (job_id usually == task_id
    but the schema permits divergence). Resolve once."""
    row = conn.execute(sql_text("""
        SELECT job_id FROM ml_analysis.video_analysis_jobs
        WHERE task_id = :tid OR job_id = :tid
        ORDER BY created_at DESC
        LIMIT 1
    """), {"tid": task_id}).mappings().first()
    if row and row.get("job_id"):
        return row["job_id"]
    # Fall back to assuming task_id == job_id
    return task_id


def _load_t5_serves(conn, task_id: str) -> List[_Serve]:
    rows = conn.execute(sql_text("""
        SELECT frame_idx, ts, player_id
        FROM ml_analysis.serve_events
        WHERE task_id = CAST(:tid AS uuid)
        ORDER BY frame_idx
    """), {"tid": task_id}).mappings().all()
    return [_Serve(frame_idx=int(r["frame_idx"]),
                   ts=float(r["ts"]),
                   player_id=int(r["player_id"])) for r in rows]


def _load_t5_bounces(conn, job_id: str) -> List[_Bounce]:
    rows = conn.execute(sql_text("""
        SELECT frame_idx FROM ml_analysis.ball_detections
        WHERE job_id = :jid AND is_bounce = TRUE
        ORDER BY frame_idx
    """), {"jid": job_id}).all()
    return [_Bounce(frame_idx=int(r[0])) for r in rows]


@dataclass
class _SAPoint:
    point_number: int
    start_s: float       # first event ts in the point
    end_s: float         # last event ts in the point
    serve_count: int
    event_count: int


def _load_sa_points(conn, sa_tid: str) -> List[_SAPoint]:
    """Group SA silver rows by point_number to derive ground-truth windows."""
    rows = conn.execute(sql_text("""
        SELECT point_number,
               MIN(ball_hit_s)   AS start_s,
               MAX(ball_hit_s)   AS end_s,
               SUM(CASE WHEN serve_d THEN 1 ELSE 0 END) AS serves,
               COUNT(*)          AS events
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND point_number IS NOT NULL
          AND point_number > 0
          AND ball_hit_s IS NOT NULL
        GROUP BY point_number
        ORDER BY point_number
    """), {"tid": sa_tid}).mappings().all()
    return [_SAPoint(
        point_number=int(r["point_number"]),
        start_s=float(r["start_s"]),
        end_s=float(r["end_s"]),
        serve_count=int(r["serves"]),
        event_count=int(r["events"]),
    ) for r in rows]


# ----------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------

def _iou(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    """Temporal Intersection-over-Union for two closed intervals."""
    inter = max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))
    union = max(a_hi, b_hi) - min(a_lo, b_lo)
    if union <= 0:
        return 0.0
    return inter / union


def _frame_to_s(frame: int, fps: float) -> float:
    return frame / fps


@dataclass
class _PairResult:
    sa: _SAPoint
    t5_idx: Optional[int]
    t5_start_s: Optional[float]
    t5_end_s: Optional[float]
    iou: float
    end_reason: str
    matched: bool


def reconcile(
    sa_points: List[_SAPoint],
    t5_boundaries,
    t5_fps: float,
    iou_threshold: float = 0.5,
) -> List[_PairResult]:
    """For each SA point, find the T5 boundary with the highest IOU (in
    seconds-space). Mark matched if IOU >= threshold.

    `t5_boundaries` is List[PointBoundary] (the detailed shape so we can
    surface end_reason for failure analysis).
    """
    used = [False] * len(t5_boundaries)
    results: List[_PairResult] = []
    for sa in sa_points:
        best_i, best_iou = None, 0.0
        for i, t5p in enumerate(t5_boundaries):
            if used[i]:
                continue
            t5_lo = _frame_to_s(t5p.start_frame, t5_fps)
            t5_hi = _frame_to_s(t5p.end_frame, t5_fps)
            iou = _iou(sa.start_s, sa.end_s, t5_lo, t5_hi)
            if iou > best_iou:
                best_iou = iou
                best_i = i
        if best_i is not None and best_iou >= iou_threshold:
            used[best_i] = True
            t5p = t5_boundaries[best_i]
            results.append(_PairResult(
                sa=sa,
                t5_idx=best_i,
                t5_start_s=_frame_to_s(t5p.start_frame, t5_fps),
                t5_end_s=_frame_to_s(t5p.end_frame, t5_fps),
                iou=best_iou,
                end_reason=t5p.end_reason,
                matched=True,
            ))
        elif best_i is not None:
            t5p = t5_boundaries[best_i]
            results.append(_PairResult(
                sa=sa,
                t5_idx=best_i,
                t5_start_s=_frame_to_s(t5p.start_frame, t5_fps),
                t5_end_s=_frame_to_s(t5p.end_frame, t5_fps),
                iou=best_iou,
                end_reason=t5p.end_reason,
                matched=False,
            ))
        else:
            results.append(_PairResult(
                sa=sa, t5_idx=None,
                t5_start_s=None, t5_end_s=None,
                iou=0.0, end_reason="no_t5_match", matched=False,
            ))
    return results


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------

def _print_table(results: List[_PairResult], iou_threshold: float) -> None:
    print()
    header = (f"{'SA#':>4} {'sa_start':>9} {'sa_end':>9} "
              f"{'t5_start':>9} {'t5_end':>9} {'IOU':>6} "
              f"{'end_reason':>14}  match")
    print(header)
    print("-" * len(header))
    for r in results:
        sa_start = f"{r.sa.start_s:9.2f}"
        sa_end = f"{r.sa.end_s:9.2f}"
        t5_start = f"{r.t5_start_s:9.2f}" if r.t5_start_s is not None else "      n/a"
        t5_end = f"{r.t5_end_s:9.2f}" if r.t5_end_s is not None else "      n/a"
        iou = f"{r.iou:6.2f}" if r.iou > 0 else "  0.00"
        mark = "PASS " if r.matched else "MISS "
        print(f"{r.sa.point_number:>4} {sa_start} {sa_end} "
              f"{t5_start} {t5_end} {iou} {r.end_reason:>14}  {mark}")
    matched = sum(1 for r in results if r.matched)
    total = len(results)
    pct = 100.0 * matched / total if total else 0.0
    print()
    print(f"  match-rate (IOU >= {iou_threshold:.2f}): {matched}/{total} = {pct:.1f}%")
    # End-reason breakdown for the misses (helps Phase-3 prioritisation)
    miss_reasons = {}
    for r in results:
        if not r.matched:
            miss_reasons[r.end_reason] = miss_reasons.get(r.end_reason, 0) + 1
    if miss_reasons:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(miss_reasons.items()))
        print(f"  miss-reason breakdown: {reasons}")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", default=DEFAULT_T5,
                    help=f"T5 task_id (default {DEFAULT_T5})")
    ap.add_argument("--sportai", default=DEFAULT_SA,
                    help=f"SportAI reference task_id (default {DEFAULT_SA})")
    ap.add_argument("--iou", type=float, default=0.5,
                    help="IOU threshold for match (default 0.5)")
    ap.add_argument("--idle-gap", type=float, default=4.0,
                    help="Idle-gap seconds for point end (default 4.0)")
    args = ap.parse_args(argv)

    db_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
              or os.environ.get("DB_URL"))
    if not db_url:
        print("DATABASE_URL required", file=sys.stderr)
        return 2

    # Lazy-import so the module loads without DB libs in the path
    from ml_pipeline.point_structure import detect_point_boundaries  # noqa
    from ml_pipeline.point_structure.point_boundaries import (
        detect_point_boundaries_detailed,
    )

    engine = create_engine(_normalize_db_url(db_url))
    with engine.connect() as conn:
        fps = _load_t5_fps(conn, args.task)
        job_id = _t5_job_id(conn, args.task)
        serves = _load_t5_serves(conn, args.task)
        bounces = _load_t5_bounces(conn, job_id)
        sa_points = _load_sa_points(conn, args.sportai)

    print(f"  T5 task_id  = {args.task}")
    print(f"  T5 job_id   = {job_id}")
    print(f"  T5 fps      = {fps:.3f}")
    print(f"  T5 serves   = {len(serves)}")
    print(f"  T5 bounces  = {len(bounces)}")
    print(f"  SA task_id  = {args.sportai}")
    print(f"  SA points   = {len(sa_points)}")
    print(f"  idle_gap_s  = {args.idle_gap}")

    if not serves:
        print("  no T5 serves found — nothing to do", file=sys.stderr)
        return 1
    if not sa_points:
        print("  no SA points found — nothing to do", file=sys.stderr)
        return 1

    boundaries = detect_point_boundaries_detailed(
        serves=serves,
        ball_events=bounces,
        fps=fps,
        idle_gap_s=args.idle_gap,
    )
    print(f"  T5 points   = {len(boundaries)} (one per accepted serve)")

    results = reconcile(sa_points, boundaries, fps, iou_threshold=args.iou)
    _print_table(results, args.iou)
    return 0


if __name__ == "__main__":
    sys.exit(main())
