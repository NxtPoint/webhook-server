"""
ml_pipeline/__main__.py — Entry point for the tennis ML analysis pipeline.

Usage:
    # Local mode
    python -m ml_pipeline <video_path>

    # AWS Batch mode (S3 input → DB output)
    python -m ml_pipeline --job-id <job_id> --s3-key <s3_key>
"""

import sys
import os
import argparse
import logging
import tempfile
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def analyse_video(video_path: str, device: str = None):
    """Public API: analyse a tennis video and return structured results."""
    from ml_pipeline.pipeline import TennisAnalysisPipeline
    pipeline = TennisAnalysisPipeline(device=device)
    return pipeline.process(video_path)


def _run_local(video_path: str):
    """Local dev mode: analyse a file and print stats."""
    result = analyse_video(video_path)
    print(f"\n{'='*60}")
    print(f"Frames processed:   {result.total_frames_processed}")
    print(f"Ball detection %:   {result.ball_detection_rate*100:.1f}%")
    print(f"Court detected:     {result.court_detected} (conf={result.court_confidence:.2f})")
    print(f"Players found:      {result.player_count}")
    print(f"Bounces:            {result.bounce_count} (in={result.bounces_in}, out={result.bounces_out})")
    print(f"Rallies:            {result.rally_count}")
    print(f"Avg rally length:   {result.avg_rally_length:.1f} bounces")
    print(f"Serves:             {result.serve_count}")
    print(f"First serve %:      {result.first_serve_pct:.1f}%")
    print(f"Max speed:          {result.max_speed_kmh:.1f} km/h")
    print(f"Avg speed:          {result.avg_speed_kmh:.1f} km/h")
    print(f"Processing time:    {result.processing_time_sec:.1f}s")
    print(f"ms/frame:           {result.ms_per_frame:.1f}")
    print(f"Frame errors:       {result.frame_errors}")
    print(f"{'='*60}")


def _run_batch(job_id: str, s3_key: str):
    """
    AWS Batch mode: download video from S3, run pipeline, save results to DB,
    upload heatmaps to S3, clean up temp file.
    """
    import boto3
    from sqlalchemy import text as sql_text
    from ml_pipeline.db_schema import ml_analysis_init, _get_engine
    from ml_pipeline.db_writer import MLDBWriter
    from ml_pipeline.pipeline import TennisAnalysisPipeline
    from ml_pipeline.heatmaps import generate_all_heatmaps

    s3_bucket = os.environ["S3_BUCKET"]
    aws_region = os.environ.get("AWS_REGION", "us-east-1")

    engine = _get_engine()
    ml_analysis_init(engine)
    db = MLDBWriter(engine)

    batch_start = time.time()
    batch_job_id = os.environ.get("AWS_BATCH_JOB_ID", "local")
    batch_job_arn = os.environ.get("AWS_BATCH_JOB_ARN")

    # Record batch start
    with engine.begin() as conn:
        conn.execute(sql_text("""
            UPDATE ml_analysis.video_analysis_jobs
            SET batch_job_id = :batch_job_id,
                batch_job_arn = :batch_job_arn,
                batch_start_at = now(),
                status = 'processing',
                updated_at = now()
            WHERE job_id = :job_id
        """), {"job_id": job_id, "batch_job_id": batch_job_id, "batch_job_arn": batch_job_arn})

    # Progress callback writes to DB
    def on_progress(stage: str, pct: int):
        db.update_job_progress(job_id, stage, pct)

    tmp_path = None
    try:
        # 1. Download from S3
        on_progress("downloading", 5)
        s3 = boto3.client("s3", region_name=aws_region)
        ext = os.path.splitext(s3_key)[1] or ".mp4"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(tmp_fd)

        logger.info(f"Downloading s3://{s3_bucket}/{s3_key} → {tmp_path}")
        s3.download_file(s3_bucket, s3_key, tmp_path)
        logger.info(f"Download complete ({os.path.getsize(tmp_path)} bytes)")

        # 2. Run pipeline
        pipeline = TennisAnalysisPipeline(progress_callback=on_progress)
        result = pipeline.process(tmp_path)

        # 3. Save results to DB
        on_progress("saving_results", 95)
        db.save_job_metadata(job_id, result)
        db.save_ball_detections(job_id, result.ball_detections)
        db.save_player_detections(job_id, result.player_detections)

        # Extract task_id from job row if present
        with engine.begin() as conn:
            row = conn.execute(sql_text(
                "SELECT task_id FROM ml_analysis.video_analysis_jobs WHERE job_id = :jid"
            ), {"jid": job_id}).fetchone()
            task_id = row[0] if row else None
        db.save_match_analytics(job_id, result, task_id=task_id)

        # 4. Generate and upload heatmaps
        on_progress("generating_heatmaps", 85)
        heatmaps = generate_all_heatmaps(result)
        ball_heatmap_key = None
        player_heatmap_keys = {}

        for filename, png_bytes in heatmaps.items():
            s3_heatmap_key = f"analysis/{job_id}/{filename}"
            s3.put_object(
                Bucket=s3_bucket,
                Key=s3_heatmap_key,
                Body=png_bytes,
                ContentType="image/png",
            )
            logger.info(f"Uploaded heatmap: s3://{s3_bucket}/{s3_heatmap_key}")

            if filename == "ball_heatmap.png":
                ball_heatmap_key = s3_heatmap_key
            else:
                player_heatmap_keys[filename] = s3_heatmap_key

        db.save_heatmap_keys(job_id, ball_heatmap_key, player_heatmap_keys)

        # 5. Record cost and mark complete
        batch_duration = time.time() - batch_start
        # G4dn.xlarge spot ≈ $0.1578/hr
        estimated_cost = (batch_duration / 3600) * 0.1578
        db.save_batch_cost(
            job_id, batch_job_id, batch_duration, estimated_cost,
            batch_job_arn=batch_job_arn,
        )
        on_progress("complete", 100)
        logger.info(f"Job {job_id} complete in {batch_duration:.0f}s (est. ${estimated_cost:.4f})")

    except Exception as e:
        logger.exception(f"Job {job_id} failed")
        db.mark_failed(job_id, str(e))
        sys.exit(1)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
            logger.info(f"Cleaned up temp file: {tmp_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tennis ML Analysis Pipeline")
    parser.add_argument("video_path", nargs="?", help="Local video file path")
    parser.add_argument("--job-id", help="ML analysis job ID (AWS Batch mode)")
    parser.add_argument("--s3-key", help="S3 object key of the video (AWS Batch mode)")

    args = parser.parse_args()

    if args.job_id and args.s3_key:
        _run_batch(args.job_id, args.s3_key)
    elif args.video_path:
        _run_local(args.video_path)
    else:
        parser.print_help()
        sys.exit(1)
