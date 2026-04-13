"""
ml_pipeline/training/export_labels.py — Extract training labels from the database.

Two label sources:
  1. T5 ball detections — from ml_analysis.ball_detections (per-frame pixel coords)
  2. SportAI hit positions — from bronze.player_swing joined via submission_context
     (ball_hit_location_x/y per swing event, with timestamp)

Usage (standalone):
    python -m ml_pipeline.training.export_labels ball <task_id> <output.json>
    python -m ml_pipeline.training.export_labels sportai <task_id> <output.json>

Usage (via harness subcommands):
    python -m ml_pipeline.harness export-ball-labels <task_id> <output.json>
    python -m ml_pipeline.harness export-sportai-labels <task_id> <output.json>
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import text

logger = logging.getLogger(__name__)


def _get_engine():
    """Return the shared SQLAlchemy engine (same pattern as harness.py)."""
    from db_init import engine
    return engine


# ============================================================
# T5 ball detections export
# ============================================================

def export_ball_labels(task_id: str, output_path: str) -> List[Dict[str, Any]]:
    """
    Extract ball positions from ml_analysis.ball_detections for a given task_id.

    Looks up the job_id via ml_analysis.video_analysis_jobs, then exports
    all ball detection rows in frame order.

    Args:
        task_id: The submission_context task_id (UUID string).
        output_path: Path to write the JSON output file.

    Returns:
        List of dicts: [{frame_idx, x, y, confidence}, ...]

    Raises:
        ValueError: if no T5 job is found for the task_id.
    """
    engine = _get_engine()

    with engine.connect() as conn:
        # Resolve task_id -> job_id
        job = conn.execute(text("""
            SELECT job_id::text AS job_id, total_frames, video_fps
            FROM ml_analysis.video_analysis_jobs
            WHERE task_id = CAST(:t AS uuid)
            ORDER BY created_at DESC
            LIMIT 1
        """), {"t": task_id}).mappings().first()

        if not job:
            raise ValueError(f"No T5 job found for task_id={task_id}")

        job_id = job["job_id"]
        total_frames = job["total_frames"] or 0
        video_fps = job["video_fps"] or 25.0
        logger.info(
            "export_ball_labels: task_id=%s job_id=%s total_frames=%d fps=%.1f",
            task_id[:8], job_id[:8], total_frames, video_fps,
        )

        rows = conn.execute(text("""
            SELECT
                frame_idx,
                pixel_x    AS x,
                pixel_y    AS y,
                confidence
            FROM ml_analysis.ball_detections
            WHERE job_id = CAST(:j AS uuid)
            ORDER BY frame_idx
        """), {"j": job_id}).mappings().all()

    labels = [
        {
            "frame_idx": int(r["frame_idx"]),
            "x": float(r["x"]) if r["x"] is not None else None,
            "y": float(r["y"]) if r["y"] is not None else None,
            "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
        }
        for r in rows
    ]

    output = {
        "source": "t5_ball_detections",
        "task_id": task_id,
        "job_id": job_id,
        "total_frames": total_frames,
        "video_fps": video_fps,
        "label_count": len(labels),
        "labels": labels,
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))

    logger.info(
        "export_ball_labels: wrote %d labels to %s", len(labels), output_path
    )
    print(f"[INFO] Exported {len(labels)} ball labels to {output_path}")
    return labels


# ============================================================
# SportAI hit positions export
# ============================================================

def export_sportai_labels(task_id: str, output_path: str) -> List[Dict[str, Any]]:
    """
    Extract SportAI ball hit positions from bronze.player_swing for a given task_id.

    Joins via bronze.submission_context to filter by task_id. Returns hit events
    with timestamps and ball_hit_location_x/y coordinates (raw SportAI pixel coords
    in the original video frame).

    Args:
        task_id: The submission_context task_id (UUID string).
        output_path: Path to write the JSON output file.

    Returns:
        List of dicts: [{timestamp, ball_hit_location_x, ball_hit_location_y,
                         player_id, swing_type, ball_speed}, ...]

    Raises:
        ValueError: if no SportAI data is found for the task_id.
    """
    engine = _get_engine()

    with engine.connect() as conn:
        # Verify the task exists and is SportAI
        ctx = conn.execute(text("""
            SELECT task_id::text AS task_id, sport_type, s3_key
            FROM bronze.submission_context
            WHERE task_id = CAST(:t AS uuid)
        """), {"t": task_id}).mappings().first()

        if not ctx:
            raise ValueError(f"No submission_context row found for task_id={task_id}")

        logger.info(
            "export_sportai_labels: task_id=%s sport_type=%s",
            task_id[:8], ctx["sport_type"],
        )

        rows = conn.execute(text("""
            SELECT
                ps.ball_hit_s            AS timestamp,
                ps.ball_hit_location_x   AS ball_hit_location_x,
                ps.ball_hit_location_y   AS ball_hit_location_y,
                ps.player_id,
                ps.swing_type,
                ps.ball_speed
            FROM bronze.player_swing ps
            JOIN bronze.raw_result rr ON rr.id = ps.raw_result_id
            JOIN bronze.submission_context sc ON sc.task_id = rr.task_id
            WHERE sc.task_id = CAST(:t AS uuid)
              AND ps.ball_hit_location_x IS NOT NULL
              AND ps.ball_hit_location_y IS NOT NULL
            ORDER BY ps.ball_hit_s
        """), {"t": task_id}).mappings().all()

    if not rows:
        raise ValueError(
            f"No SportAI swing data with ball positions found for task_id={task_id}. "
            "Ensure bronze ingest has run for this task."
        )

    labels = [
        {
            "timestamp": float(r["timestamp"]) if r["timestamp"] is not None else None,
            "ball_hit_location_x": float(r["ball_hit_location_x"]),
            "ball_hit_location_y": float(r["ball_hit_location_y"]),
            "player_id": str(r["player_id"]) if r["player_id"] is not None else None,
            "swing_type": str(r["swing_type"]) if r["swing_type"] is not None else None,
            "ball_speed": float(r["ball_speed"]) if r["ball_speed"] is not None else None,
        }
        for r in rows
    ]

    output = {
        "source": "sportai_player_swing",
        "task_id": task_id,
        "sport_type": ctx["sport_type"],
        "label_count": len(labels),
        "labels": labels,
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))

    logger.info(
        "export_sportai_labels: wrote %d labels to %s", len(labels), output_path
    )
    print(f"[INFO] Exported {len(labels)} SportAI hit labels to {output_path}")
    return labels


# ============================================================
# CLI entry point (standalone)
# ============================================================

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    p = argparse.ArgumentParser(
        prog="ml_pipeline.training.export_labels",
        description="Export training labels from the database",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ball = sub.add_parser(
        "ball",
        help="Export T5 ball detections from ml_analysis.ball_detections",
    )
    p_ball.add_argument("task_id", help="Task UUID")
    p_ball.add_argument("output", help="Output JSON path")

    p_sportai = sub.add_parser(
        "sportai",
        help="Export SportAI hit positions from bronze.player_swing",
    )
    p_sportai.add_argument("task_id", help="Task UUID")
    p_sportai.add_argument("output", help="Output JSON path")

    args = p.parse_args()

    try:
        if args.cmd == "ball":
            export_ball_labels(args.task_id, args.output)
        elif args.cmd == "sportai":
            export_sportai_labels(args.task_id, args.output)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
