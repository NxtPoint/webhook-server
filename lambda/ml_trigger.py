"""
lambda/ml_trigger.py — Lambda function triggered by S3 ObjectCreated on the videos/ prefix.

Creates a video_analysis_jobs row in PostgreSQL and submits an AWS Batch job.

Environment variables (set on the Lambda function):
    DATABASE_URL    — PostgreSQL connection string
    S3_BUCKET       — Source S3 bucket name
    BATCH_JOB_QUEUE — AWS Batch job queue name
    BATCH_JOB_DEF   — AWS Batch job definition name (or ARN)
    AWS_REGION      — AWS region (default: us-east-1)
"""

import os
import json
import uuid
import logging
import urllib.parse

import boto3
import psycopg

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BATCH_JOB_QUEUE = os.environ["BATCH_JOB_QUEUE"]
BATCH_JOB_DEF = os.environ["BATCH_JOB_DEF"]
DATABASE_URL = os.environ["DATABASE_URL"]
S3_BUCKET = os.environ.get("S3_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

batch_client = boto3.client("batch", region_name=REGION)


def handler(event, context):
    """
    S3 ObjectCreated trigger handler.

    Processes each S3 record:
      1. Extract the S3 key from the event
      2. Generate a unique job_id
      3. Insert a row into ml_analysis.video_analysis_jobs
      4. Submit an AWS Batch job
    """
    results = []

    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name", "")
        raw_key = s3_info.get("object", {}).get("key", "")
        s3_key = urllib.parse.unquote_plus(raw_key)

        if not s3_key.startswith("videos/"):
            logger.info(f"Skipping non-videos/ key: {s3_key}")
            continue

        # Skip zero-byte or marker objects
        size = s3_info.get("object", {}).get("size", 0)
        if size == 0:
            logger.info(f"Skipping zero-byte object: {s3_key}")
            continue

        job_id = str(uuid.uuid4())
        logger.info(f"Processing s3://{bucket}/{s3_key} → job_id={job_id}")

        # Extract task_id from key if present: videos/{task_id}/filename.mp4
        parts = s3_key.split("/")
        task_id = parts[1] if len(parts) >= 3 else None

        # 1. Create job row in PostgreSQL
        try:
            _create_job_row(job_id, s3_key, task_id)
        except Exception as e:
            logger.exception(f"Failed to create job row for {s3_key}")
            raise

        # 2. Submit AWS Batch job
        try:
            batch_response = batch_client.submit_job(
                jobName=f"ml-pipeline-{job_id[:8]}",
                jobQueue=BATCH_JOB_QUEUE,
                jobDefinition=BATCH_JOB_DEF,
                containerOverrides={
                    "command": [
                        "python", "-m", "ml_pipeline",
                        "--job-id", job_id,
                        "--s3-key", s3_key,
                    ],
                    "environment": [
                        {"name": "JOB_ID", "value": job_id},
                        {"name": "S3_KEY", "value": s3_key},
                    ],
                },
                tags={
                    "Project": "TEN-FIFTY5",
                    "Environment": "production",
                    "JobId": job_id,
                },
            )
            batch_job_id = batch_response["jobId"]
            logger.info(f"Submitted Batch job: {batch_job_id}")

            # Update job row with Batch job ID
            _update_batch_id(job_id, batch_job_id)

        except Exception as e:
            logger.exception(f"Failed to submit Batch job for {s3_key}")
            _mark_job_failed(job_id, f"Batch submit failed: {e}")
            raise

        results.append({
            "job_id": job_id,
            "s3_key": s3_key,
            "batch_job_id": batch_job_id,
        })

    return {
        "statusCode": 200,
        "body": json.dumps({"jobs": results}),
    }


def _get_conn():
    """Get a psycopg3 connection."""
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url)


def _create_job_row(job_id: str, s3_key: str, task_id: str = None):
    """Insert a new job row with status=queued."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ml_analysis.video_analysis_jobs
                    (job_id, s3_key, task_id, status, current_stage, progress_pct)
                VALUES (%s, %s, %s, 'queued', 'queued', 0)
                ON CONFLICT (job_id) DO NOTHING
            """, (job_id, s3_key, task_id))
        conn.commit()
    logger.info(f"Created job row: {job_id}")


def _update_batch_id(job_id: str, batch_job_id: str):
    """Update job row with the AWS Batch job ID."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ml_analysis.video_analysis_jobs
                SET batch_job_id = %s, updated_at = now()
                WHERE job_id = %s
            """, (batch_job_id, job_id))
        conn.commit()


def _mark_job_failed(job_id: str, error: str):
    """Mark job as failed."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ml_analysis.video_analysis_jobs
                    SET status = 'failed', error_message = %s, updated_at = now()
                    WHERE job_id = %s
                """, (error[:2000], job_id))
            conn.commit()
    except Exception:
        logger.exception("Failed to mark job as failed in DB")
