"""Keep the raw SportAI JSON, and shout when SportAI's schema drifts.

Why: the raw result JSON is the source of truth. Until now it was thrown away
(`_persist_raw` was dead) and the re-fetch URL expires in an hour, so no past
match could be re-validated or re-ingested. This archives every payload to S3
(whole, gzipped) and flags any top-level key we've never handled — so when
SportAI adds data "somewhere we never had", we find out immediately instead of
silently dropping it.

Wired into ingest_worker_app._do_ingest right after the payload is downloaded.
Everything here is BEST-EFFORT: a failure logs and returns, never breaking an
ingest.
"""
from __future__ import annotations

import gzip
import json
import logging
import os

log = logging.getLogger(__name__)

# Every top-level key we currently know SportAI sends. The ingest handles the
# first group; `meta`/`debug_data`/`warmups` are known-but-not-ingested. A key
# outside this set is NEW — the drift signal.
KNOWN_TOPLEVEL = {
    # ingested
    "players", "ball_positions", "ball_bounces", "player_positions",
    "confidences", "thumbnail_crops", "thumbnails", "highlights",
    "team_sessions", "bounce_heatmap", "rallies", "rally_events",
    "unmatched", "unmatched_fields", "debug_events", "events_debug",
    # known, deliberately/historically NOT ingested
    "meta", "metadata", "debug_data", "warmups",
}

RAW_ARCHIVE_ENABLED = (os.getenv("RAW_ARCHIVE_ENABLED", "1").strip() != "0")
RAW_ARCHIVE_PREFIX = os.getenv("RAW_ARCHIVE_PREFIX", "raw-json").strip("/")


def archive_raw(task_id: str, payload: dict, *, bucket: str | None = None) -> str | None:
    """Store the whole payload to s3://<bucket>/<prefix>/<task_id>.json.gz.

    Returns the S3 key on success, None otherwise. Never raises.
    """
    if not RAW_ARCHIVE_ENABLED:
        return None
    bucket = bucket or os.getenv("S3_BUCKET")
    if not bucket:
        log.warning("RAW ARCHIVE skipped task_id=%s: no S3_BUCKET", task_id)
        return None
    try:
        import boto3  # local import — worker may not need it otherwise
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        key = f"{RAW_ARCHIVE_PREFIX}/{task_id}.json.gz"
        boto3.client("s3", region_name=os.getenv("AWS_REGION", "eu-north-1")).put_object(
            Bucket=bucket, Key=key, Body=body,
            ContentType="application/json", ContentEncoding="gzip",
        )
        log.info("RAW ARCHIVE stored task_id=%s key=%s bytes=%d", task_id, key, len(body))
        return key
    except Exception as e:  # noqa: BLE001
        log.warning("RAW ARCHIVE failed task_id=%s: %s", task_id, e)
        return None


def detect_drift(task_id: str, payload: dict) -> list[str]:
    """Return top-level keys not in KNOWN_TOPLEVEL; alert on any. Never raises."""
    try:
        new = sorted(k for k in payload.keys() if k not in KNOWN_TOPLEVEL)
        if new:
            log.warning("SCHEMA DRIFT task_id=%s: SportAI sent NEW top-level key(s) %s "
                        "— not ingested anywhere. Review and map them.", task_id, new)
            _alert(task_id, new)
        return new
    except Exception as e:  # noqa: BLE001
        log.warning("DRIFT check failed task_id=%s: %s", task_id, e)
        return []


def _alert(task_id: str, new_keys: list[str]) -> None:
    try:
        from coach_invite.video_complete_email import send_ops_email
        send_ops_email(
            subject=f"[SportAI schema drift] new key(s): {', '.join(new_keys)}",
            text_body=(f"Task {task_id} — SportAI's result JSON contains top-level key(s) "
                       f"we have never handled: {new_keys}.\n\n"
                       "The raw JSON is archived; nothing is lost, but these fields are not "
                       "ingested into bronze. Decide whether to map them "
                       "(devenv/coverage_check.py shows exactly what they contain)."),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("DRIFT alert email failed task_id=%s: %s", task_id, e)
