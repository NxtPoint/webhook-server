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


def analyse_video(video_path: str, device: str = None, practice: bool = False):
    """Public API: analyse a tennis video and return structured results."""
    from ml_pipeline.pipeline import TennisAnalysisPipeline
    pipeline = TennisAnalysisPipeline(device=device, practice=practice)
    return pipeline.process(video_path)


def _run_local(video_path: str, practice: bool = False):
    """Local dev mode: analyse a file and print stats."""
    result = analyse_video(video_path, practice=practice)
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


def _probe_video_codec(source_path: str) -> str:
    """Return the video codec name (e.g. 'h264', 'hevc', 'prores')."""
    import subprocess
    ffprobe_bin = os.environ.get("FFPROBE_BIN", "ffprobe")
    try:
        result = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", source_path],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip().lower()
    except Exception:
        return "unknown"


def _transcode_to_mp4(source_path: str) -> str:
    """
    Compress video for browser streaming.
    Scales to 720p max height, CRF 28, ultrafast preset for speed.
    Output is much smaller than source (~80-90% reduction for raw phone footage).
    """
    import subprocess
    out_fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(out_fd)

    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "ffmpeg")
    codec = _probe_video_codec(source_path)
    logger.info(f"Source codec: {codec}")

    # Always compress for streaming — scale to 720p, CRF 28 for small file size
    cmd = [
        ffmpeg_bin, "-y",
        "-i", source_path,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-vf", "scale=-2:720",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        out_path,
    ]

    logger.info(f"Compressing for streaming: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed (rc={result.returncode}): {result.stderr[-500:]}")

    src_size = os.path.getsize(source_path)
    out_size = os.path.getsize(out_path)
    logger.info(f"Complete: {src_size} → {out_size} bytes ({out_size/src_size*100:.0f}%)")
    return out_path


def _run_batch(job_id: str, s3_key: str, practice: bool = False):
    """
    AWS Batch mode: download video from S3, run pipeline, save results to DB,
    upload heatmaps to S3, transcode to MP4, clean up source.
    """
    import boto3
    from sqlalchemy import text as sql_text
    from ml_pipeline.db_schema import ml_analysis_init, _get_engine
    from ml_pipeline.db_writer import MLDBWriter
    from ml_pipeline.pipeline import TennisAnalysisPipeline
    from ml_pipeline.heatmaps import generate_all_heatmaps
    from ml_pipeline.bronze_export import export_bronze_to_s3

    s3_bucket = os.environ["S3_BUCKET"]
    aws_region = os.environ.get("AWS_REGION", "us-east-1")

    # S3 bucket region can differ from the compute region when a job runs
    # in a fallback region (e.g. us-east-1 Batch executing against a bucket
    # that lives in eu-north-1). Pinning the S3 client to the compute region
    # produces a spurious 404 on head_object for cross-region buckets
    # because HeadObject does not follow the 301 redirect transparently.
    # Resolve the bucket's home region once, fall back to AWS_REGION on
    # failure so single-region deployments still work.
    try:
        _loc_client = boto3.client("s3", region_name="us-east-1")
        _loc = _loc_client.get_bucket_location(Bucket=s3_bucket)
        s3_region = _loc.get("LocationConstraint") or "us-east-1"
        if s3_region != aws_region:
            logger.info(
                f"S3 bucket {s3_bucket} lives in {s3_region}; compute region is "
                f"{aws_region}. Pinning S3 client to {s3_region}."
            )
    except Exception as e:
        logger.warning(
            f"get_bucket_location failed for {s3_bucket}: {e}; "
            f"falling back to AWS_REGION={aws_region}"
        )
        s3_region = aws_region

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
        s3 = boto3.client("s3", region_name=s3_region)
        ext = os.path.splitext(s3_key)[1] or ".mp4"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(tmp_fd)

        logger.info(f"Downloading s3://{s3_bucket}/{s3_key} → {tmp_path}")
        s3.download_file(s3_bucket, s3_key, tmp_path)
        logger.info(f"Download complete ({os.path.getsize(tmp_path)} bytes)")

        # 2. Run pipeline (with live debug frame S3 upload context)
        pipeline = TennisAnalysisPipeline(progress_callback=on_progress, practice=practice)
        # Enable LIVE debug frame upload — user can inspect frames mid-run
        # and cancel bad runs without waiting for full ML processing
        pipeline.player_tracker.set_debug_upload_context(s3, s3_bucket, job_id)
        result = pipeline.process(tmp_path)

        # 2b. Far-baseline ROI pose extraction (ViTPose-Base on YOLOv8m-det
        # crops). Supplements bronze pid=1 with high-quality keypoints on
        # the 30-50 px far-player body where full-frame YOLOv8x-pose
        # under-resolves. Writes to ml_analysis.player_detections_roi
        # (source='far_vitpose'), consumed by serve_detector's merge
        # logic on the Render side. Failure is non-fatal.
        if not practice:
            try:
                on_progress("roi_pose", 78)
                from ml_pipeline.roi_extractors import extract_far_pose
                court_det = getattr(pipeline, "court_detector", None)
                n_pose = extract_far_pose(
                    video_path=tmp_path,
                    job_id=job_id,
                    engine=engine,
                    fps=getattr(result, "video_fps", 25.0) or 25.0,
                    sample_every=2,
                    court_detector=court_det,
                )
                logger.info(f"ROI pose: wrote {n_pose} rows")
            except Exception as e:
                logger.warning(f"ROI pose extraction failed (non-fatal): {e}")

        # 3. Export results to S3 as gzipped JSON (fast — single PUT)
        # The Render-side ingest worker (ml_pipeline.bronze_ingest_t5) downloads
        # and bulk-inserts into ml_analysis.* in the same region as the DB.
        on_progress("saving_results", 82)
        db.save_job_metadata(job_id, result)

        # Extract task_id from job row if present
        with engine.begin() as conn:
            row = conn.execute(sql_text(
                "SELECT task_id FROM ml_analysis.video_analysis_jobs WHERE job_id = :jid"
            ), {"jid": job_id}).fetchone()
            task_id = row[0] if row else None

        bronze_s3_key = export_bronze_to_s3(
            job_id=job_id,
            task_id=task_id,
            result=result,
            s3_client=s3,
            s3_bucket=s3_bucket,
            practice=practice,
        )
        # Record the S3 key on the job row so the ingest worker can find it
        with engine.begin() as conn:
            conn.execute(sql_text("""
                UPDATE ml_analysis.video_analysis_jobs
                SET bronze_s3_key = :bkey, updated_at = now()
                WHERE job_id = :jid
            """), {"jid": job_id, "bkey": bronze_s3_key})

        # 4. Generate and upload heatmaps
        on_progress("generating_heatmaps", 88)
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

        # 4b. Upload debug frames (YOLO bbox overlays) for visual inspection
        debug_dir = "/tmp/debug_frames"
        if os.path.exists(debug_dir):
            try:
                debug_files = sorted(os.listdir(debug_dir))
                logger.info(f"Uploading {len(debug_files)} debug frames to S3")
                for fname in debug_files:
                    local_path = os.path.join(debug_dir, fname)
                    if not fname.endswith(".jpg"):
                        continue
                    s3_debug_key = f"debug/{job_id}/{fname}"
                    try:
                        s3.upload_file(
                            local_path, s3_bucket, s3_debug_key,
                            ExtraArgs={"ContentType": "image/jpeg"},
                        )
                    except Exception as e:
                        logger.warning(f"Debug frame upload failed {fname}: {e}")
                    # Clean up local file
                    try:
                        os.unlink(local_path)
                    except Exception:
                        pass
                logger.info(f"Debug frames uploaded to s3://{s3_bucket}/debug/{job_id}/")
            except Exception as e:
                logger.warning(f"Debug frame upload step failed (non-fatal): {e}")

        # 5. Transcode to MP4 + upload to trimmed/{job_id}/practice.mp4
        on_progress("transcoding", 92)
        mp4_path = None
        try:
            mp4_path = _transcode_to_mp4(tmp_path)
            trimmed_key = f"trimmed/{job_id}/practice.mp4"
            s3.upload_file(mp4_path, s3_bucket, trimmed_key,
                           ExtraArgs={"ContentType": "video/mp4"})
            logger.info(f"Uploaded trimmed: s3://{s3_bucket}/{trimmed_key}")

            # (trimmed key is recorded in submission_context below — no need to
            # duplicate it on video_analysis_jobs)

            # Also update submission_context so Locker Room can find the footage
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    UPDATE bronze.submission_context
                    SET trim_status = 'completed',
                        trim_output_s3_key = :tkey
                    WHERE task_id = :jid
                """), {"jid": job_id, "tkey": trimmed_key})

        except Exception as e:
            logger.warning(f"Transcode failed (non-fatal): {e}")
        finally:
            if mp4_path and os.path.exists(mp4_path):
                os.unlink(mp4_path)

        # 6. Delete raw source from S3 (MOV cleanup)
        try:
            s3.delete_object(Bucket=s3_bucket, Key=s3_key)
            logger.info(f"Deleted raw source: s3://{s3_bucket}/{s3_key}")
        except Exception as e:
            logger.warning(f"Source cleanup failed (non-fatal): {e}")

        # 7. Record cost and mark complete
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
    parser.add_argument("--practice", action="store_true",
                        help="Practice mode: lower FPS + less frequent detection for faster processing")

    args = parser.parse_args()

    if args.job_id and args.s3_key:
        _run_batch(args.job_id, args.s3_key, practice=args.practice)
    elif args.video_path:
        _run_local(args.video_path, practice=args.practice)
    else:
        parser.print_help()
        sys.exit(1)
