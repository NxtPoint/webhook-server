# ============================================================
# runner.py — container entrypoint for the Fargate trim job
# ============================================================
# Replaces the always-on Render video-worker for the actual encode. The Flask
# worker's job was only ever "receive a request, run run_ffmpeg_trim, POST a
# callback" — none of which needs a server sitting idle 24/7. Here the same
# three steps run as a Batch job that exists for the duration of one trim.
#
# Reads its work order from S3 (written by fargate_trim.submit), so there is no
# argv/env size limit on the EDL — a long match can carry hundreds of segments.
# The callback URL and its auth arrive as env vars from the submitter, which is
# also the only place a secret appears.
#
# Exit codes matter: a non-zero exit marks the Batch job FAILED, which is what
# makes a job visible as broken in the Batch console. The completion callback is
# still POSTed on failure so the DB never sits at 'accepted' forever.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict

import boto3
import requests

from video_pipeline.ffmpeg_trim_worker import run_ffmpeg_trim

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fargate_trim")

CALLBACK_TIMEOUT_S = int(os.getenv("VIDEO_TRIM_CALLBACK_TIMEOUT_S", "20"))
CALLBACK_MAX_RETRIES = int(os.getenv("VIDEO_TRIM_CALLBACK_MAX_RETRIES", "4"))
CALLBACK_RETRY_BASE_S = float(os.getenv("VIDEO_TRIM_CALLBACK_RETRY_BASE_S", "2.0"))


def _load_job(job_s3_uri: str) -> Dict[str, Any]:
    """Fetch the work order. s3://bucket/key -> dict."""
    if not job_s3_uri.startswith("s3://"):
        raise ValueError(f"TRIM_JOB_S3 must be an s3:// URI, got {job_s3_uri!r}")
    bucket, _, key = job_s3_uri[len("s3://"):].partition("/")
    if not bucket or not key:
        raise ValueError(f"malformed TRIM_JOB_S3: {job_s3_uri!r}")
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def _delete_job(job_s3_uri: str) -> None:
    """Best-effort cleanup — a stale work order is harmless but untidy."""
    try:
        bucket, _, key = job_s3_uri[len("s3://"):].partition("/")
        boto3.client("s3").delete_object(Bucket=bucket, Key=key)
    except Exception as e:
        log.warning("could not delete work order %s: %s", job_s3_uri, e)


def _callback(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> None:
    """POST the result, with retries. The main API may be mid-redeploy, and a
    lost callback strands the row at 'accepted' until a sweep notices — which is
    exactly the failure this pipeline kept hitting, so retry generously."""
    if not url:
        log.warning("no callback_url — result not reported: %s", payload.get("status"))
        return

    hdrs = {"Content-Type": "application/json"}
    hdrs.update({k: str(v) for k, v in (headers or {}).items() if v is not None})

    last: Exception | None = None
    for attempt in range(1, CALLBACK_MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, headers=hdrs, timeout=CALLBACK_TIMEOUT_S)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            log.info("callback ok (attempt %d) status=%s", attempt, payload.get("status"))
            return
        except Exception as e:
            last = e
            if attempt < CALLBACK_MAX_RETRIES:
                wait = CALLBACK_RETRY_BASE_S * (2 ** (attempt - 1))
                log.warning("callback attempt %d/%d failed (%s) — retrying in %.1fs",
                            attempt, CALLBACK_MAX_RETRIES, e, wait)
                time.sleep(wait)
    raise RuntimeError(f"callback failed after {CALLBACK_MAX_RETRIES} attempts: {last}")


def main() -> int:
    job_uri = (os.getenv("TRIM_JOB_S3") or "").strip()
    if not job_uri:
        log.error("TRIM_JOB_S3 env var is required")
        return 2

    callback_url = (os.getenv("TRIM_CALLBACK_URL") or "").strip()
    callback_key = (os.getenv("TRIM_CALLBACK_OPS_KEY") or "").strip()
    callback_headers = {"Authorization": f"Bearer {callback_key}"} if callback_key else {}

    started = time.monotonic()
    task_id = "?"
    try:
        job = _load_job(job_uri)
        task_id = str(job["task_id"])
        log.info("TRIM JOB start task_id=%s bucket=%s key=%s segments=%d",
                 task_id, job["s3_bucket"], job["s3_key"],
                 len((job.get("edl") or {}).get("segments") or []))

        result = run_ffmpeg_trim(
            task_id=task_id,
            s3_bucket=job["s3_bucket"],
            s3_key=job["s3_key"],
            edl=job["edl"],
        )

        elapsed = time.monotonic() - started
        log.info("TRIM JOB done task_id=%s in %.1fmin -> %s",
                 task_id, elapsed / 60.0, result.get("output_s3_key"))

        _callback(callback_url, callback_headers, {
            "task_id": task_id,
            "status": "completed",
            "output_s3_key": result["output_s3_key"],
            "source_duration_s": result["source_duration_s"],
            "trimmed_duration_s": result["trimmed_duration_s"],
            "segment_count": result["segment_count"],
            "seconds_removed": result["seconds_removed"],
        })
        _delete_job(job_uri)
        return 0

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        elapsed = time.monotonic() - started
        log.exception("TRIM JOB FAILED task_id=%s after %.1fmin: %s", task_id, elapsed / 60.0, err)
        try:
            _callback(callback_url, callback_headers, {
                "task_id": task_id,
                "status": "failed",
                "error": err[:2000],
            })
        except Exception:
            log.exception("could not report failure for task_id=%s", task_id)
        # Leave the work order in place on failure — it is the record of what
        # was attempted, and a re-submit can reuse it.
        return 1


if __name__ == "__main__":
    sys.exit(main())
