"""
ml_pipeline/db_writer.py — Save pipeline results to PostgreSQL.

Writes AnalysisResult data into ml_analysis.* tables.
Also provides update_job_progress() for real-time stage tracking.
"""

import logging
from sqlalchemy import create_engine, text as sql_text

logger = logging.getLogger(__name__)


class MLDBWriter:
    """Writes ML pipeline results to the ml_analysis schema."""

    def __init__(self, engine):
        self.engine = engine

    def update_job_progress(self, job_id: str, stage: str, progress_pct: int):
        """Update job status with current stage and progress."""
        with self.engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET current_stage = :stage,
                    progress_pct = :pct,
                    status = CASE WHEN :stage = 'complete' THEN 'complete' ELSE 'processing' END,
                    updated_at = now()
                WHERE job_id = :job_id
            """), {"job_id": job_id, "stage": stage, "pct": progress_pct})

    def mark_failed(self, job_id: str, error_message: str):
        """Mark a job as failed with error details."""
        with self.engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET status = 'failed',
                    error_message = :err,
                    updated_at = now()
                WHERE job_id = :job_id
            """), {"job_id": job_id, "err": error_message[:2000]})

    def save_job_metadata(self, job_id: str, result):
        """Save video metadata and processing stats to the job row."""
        meta = result.video_metadata
        params = {
            "job_id": job_id,
            "video_duration_sec": meta.duration_sec if meta else None,
            "video_fps": meta.fps if meta else None,
            "video_width": meta.width if meta else None,
            "video_height": meta.height if meta else None,
            "video_codec": meta.codec if meta else None,
            "video_file_size": meta.file_size_bytes if meta else None,
            "total_frames": result.total_frames_processed,
            "frame_errors": result.frame_errors,
            "processing_time_sec": result.processing_time_sec,
            "ms_per_frame": result.ms_per_frame,
            "court_detected": result.court_detected,
            "court_confidence": result.court_confidence,
            "court_used_fallback": result.court_used_fallback,
        }
        with self.engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET video_duration_sec = :video_duration_sec,
                    video_fps = :video_fps,
                    video_width = :video_width,
                    video_height = :video_height,
                    video_codec = :video_codec,
                    video_file_size = :video_file_size,
                    total_frames = :total_frames,
                    frame_errors = :frame_errors,
                    processing_time_sec = :processing_time_sec,
                    ms_per_frame = :ms_per_frame,
                    court_detected = :court_detected,
                    court_confidence = :court_confidence,
                    court_used_fallback = :court_used_fallback,
                    updated_at = now()
                WHERE job_id = :job_id
            """), params)

    def save_heatmap_keys(self, job_id: str, ball_key: str, player_keys: dict):
        """Update job row with S3 heatmap keys."""
        import json
        with self.engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET ball_heatmap_s3_key = :ball_key,
                    player_heatmap_s3_keys = :player_keys,
                    updated_at = now()
                WHERE job_id = :job_id
            """), {
                "job_id": job_id,
                "ball_key": ball_key,
                "player_keys": json.dumps(player_keys),
            })

    def save_batch_cost(self, job_id: str, batch_job_id: str, duration_sec: float,
                        cost_usd: float, batch_job_arn: str = None):
        """Record Batch job cost information."""
        with self.engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET batch_job_id = :batch_job_id,
                    batch_end_at = now(),
                    batch_duration_sec = :duration_sec,
                    estimated_cost_usd = :cost_usd,
                    batch_job_arn = :batch_job_arn,
                    updated_at = now()
                WHERE job_id = :job_id
            """), {
                "job_id": job_id,
                "batch_job_id": batch_job_id,
                "duration_sec": duration_sec,
                "cost_usd": cost_usd,
                "batch_job_arn": batch_job_arn,
            })
