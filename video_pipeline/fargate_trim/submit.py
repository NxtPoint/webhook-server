# ============================================================
# submit.py — main-API side: hand a trim to AWS Batch (Fargate)
# ============================================================
# Drop-in replacement for the POST to the always-on Render video-worker.
# Same inputs, same completion callback, so upload_app / ingest_worker /
# video_trim_api are unchanged apart from choosing this backend.
#
# The work order (including the EDL, which can be hundreds of segments) goes to
# S3 and the job is handed only its URI — no argv or env size limit. The
# callback URL + its key travel as container env because the API knows them and
# the job must not need database access.
# ============================================================

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import boto3

log = logging.getLogger(__name__)

AWS_REGION = (os.getenv("TRIM_BATCH_REGION") or os.getenv("AWS_REGION") or "eu-north-1").strip()
JOB_QUEUE = (os.getenv("TRIM_BATCH_QUEUE") or "ten-fifty5-trim-queue").strip()
JOB_DEF = (os.getenv("TRIM_BATCH_JOB_DEF") or "ten-fifty5-video-trim").strip()

# Where work orders live. Same bucket as the media by default.
JOB_PREFIX = (os.getenv("TRIM_JOB_PREFIX") or "trim-jobs").strip().strip("/")

# Per-job sizing. Fargate bills per second, so a bigger box costs roughly the
# same total and simply finishes sooner — the opposite of an always-on service,
# where a bigger box costs more every hour it sits idle. Valid Fargate vCPU
# values: 0.25/0.5/1/2/4/8/16; memory must be compatible with the vCPU choice.
JOB_VCPU = (os.getenv("TRIM_BATCH_VCPU") or "16").strip()
JOB_MEMORY_MB = (os.getenv("TRIM_BATCH_MEMORY_MB") or "32768").strip()

# Wall-clock ceiling enforced by Batch itself, independent of the in-process
# TRIM_ENCODE_TIMEOUT_S. Stops a pathological job billing indefinitely.
JOB_TIMEOUT_S = int(os.getenv("TRIM_BATCH_TIMEOUT_S", "3600"))

# Batch retries the whole job on infrastructure failures (spot-style
# interruptions, image pull hiccups). The trim is idempotent — it overwrites the
# same output key — so a retry is safe.
JOB_ATTEMPTS = int(os.getenv("TRIM_BATCH_ATTEMPTS", "2"))


def _client(service: str):
    return boto3.client(service, region_name=AWS_REGION)


def submit_trim_job(
    *,
    task_id: str,
    s3_bucket: str,
    s3_key: str,
    edl: Dict[str, Any],
    callback_url: str,
    callback_ops_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the work order to S3 and submit the Batch job.

    Returns a dict shaped like the old worker's HTTP response so callers can
    treat both backends identically.
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not (edl or {}).get("segments"):
        raise ValueError("edl has no segments")

    job_key = f"{JOB_PREFIX}/{task_id}.json"
    payload = {
        "task_id": task_id,
        "s3_bucket": s3_bucket,
        "s3_key": s3_key,
        "edl": edl,
    }
    _client("s3").put_object(
        Bucket=s3_bucket,
        Key=job_key,
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )

    env = [
        {"name": "TRIM_JOB_S3", "value": f"s3://{s3_bucket}/{job_key}"},
        {"name": "TRIM_CALLBACK_URL", "value": callback_url or ""},
    ]
    if callback_ops_key:
        env.append({"name": "TRIM_CALLBACK_OPS_KEY", "value": callback_ops_key})

    # Batch job names allow [A-Za-z0-9_-] only, max 128.
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in task_id)[:100]

    resp = _client("batch").submit_job(
        jobName=f"trim-{safe}",
        jobQueue=JOB_QUEUE,
        jobDefinition=JOB_DEF,
        containerOverrides={
            "environment": env,
            "resourceRequirements": [
                {"type": "VCPU", "value": JOB_VCPU},
                {"type": "MEMORY", "value": JOB_MEMORY_MB},
            ],
        },
        timeout={"attemptDurationSeconds": JOB_TIMEOUT_S},
        retryStrategy={"attempts": JOB_ATTEMPTS},
    )

    log.info("TRIM BATCH submitted task_id=%s job_id=%s queue=%s vcpu=%s",
             task_id, resp.get("jobId"), JOB_QUEUE, JOB_VCPU)

    return {
        "ok": True,
        "accepted": True,
        "task_id": task_id,
        "status": "accepted",
        "backend": "batch",
        "job_id": resp.get("jobId"),
        "job_name": resp.get("jobName"),
    }


def describe_trim_job(job_id: str) -> Dict[str, Any]:
    """Diagnostic helper: current Batch status for a submitted trim."""
    jobs = _client("batch").describe_jobs(jobs=[job_id]).get("jobs") or []
    if not jobs:
        return {"job_id": job_id, "status": "NOT_FOUND"}
    j = jobs[0]
    return {
        "job_id": job_id,
        "status": j.get("status"),
        "status_reason": j.get("statusReason"),
        "created_at": j.get("createdAt"),
        "started_at": j.get("startedAt"),
        "stopped_at": j.get("stoppedAt"),
        "log_stream": (j.get("container") or {}).get("logStreamName"),
    }
