# technique/api_client.py — Call the SportAI Technique API.
#
# The Technique API accepts a video file + metadata via multipart/form-data
# and returns a streaming JSON response. We read lines until status == "done"
# (or "failed"/"error"), then return the final parsed payload.
#
# Env vars:
#   TECHNIQUE_API_BASE  — base URL (required, no default)
#   TECHNIQUE_API_TOKEN — bearer token (optional, for authenticated endpoints)

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Optional

import requests

log = logging.getLogger(__name__)

TECHNIQUE_API_BASE = (os.getenv("TECHNIQUE_API_BASE") or "").strip().rstrip("/")
TECHNIQUE_API_TOKEN = (os.getenv("TECHNIQUE_API_TOKEN") or "").strip()

# Timeout for the streaming POST (technique API processes 30-120s)
TECHNIQUE_API_TIMEOUT_S = int(os.getenv("TECHNIQUE_API_TIMEOUT_S", "300"))


def call_technique_api(
    video_bytes: bytes,
    filename: str,
    sport: str = "tennis",
    swing_type: str = "forehand_drive",
    dominant_hand: str = "right",
    player_height_mm: int = 1800,
    uid: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
) -> dict:
    """
    POST video + metadata to the Technique API and collect the streaming response.

    Returns the final JSON payload (with status "done" or "failed").
    Raises RuntimeError on connection/protocol errors.
    """
    if not TECHNIQUE_API_BASE:
        raise RuntimeError("TECHNIQUE_API_BASE env var is required for technique analysis")

    url = f"{TECHNIQUE_API_BASE}/process"
    uid = uid or str(uuid.uuid4())

    metadata = {
        "uid": uid,
        "sport": sport,
        "swing_type": swing_type,
        "dominant_hand": dominant_hand,
        "player_height_mm": player_height_mm,
        "store_data": False,
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    headers = {}
    if TECHNIQUE_API_TOKEN:
        headers["Authorization"] = f"Bearer {TECHNIQUE_API_TOKEN}"

    files = {
        "file": (filename, video_bytes, "video/mp4"),
    }
    data = {
        "metadata": json.dumps(metadata),
    }

    log.info(
        "TECHNIQUE API CALL url=%s uid=%s sport=%s swing_type=%s height=%s file_size=%d",
        url, uid, sport, swing_type, player_height_mm, len(video_bytes),
    )

    try:
        resp = requests.post(
            url,
            headers=headers,
            files=files,
            data=data,
            timeout=TECHNIQUE_API_TIMEOUT_S,
            stream=True,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Technique API request failed: {e}") from e

    return _consume_streaming_response(resp, uid)


def _consume_streaming_response(resp: requests.Response, uid: str) -> dict:
    """
    Read streaming JSON lines from the technique API response.
    Each line is a JSON object with a "status" field.
    We collect lines until status is "done", "failed", or "error".
    Returns the final JSON object.
    """
    final_payload = None

    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue

        line = raw_line.strip()
        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("TECHNIQUE API non-JSON line uid=%s: %s", uid, line[:200])
            continue

        status = (obj.get("status") or "").lower()
        log.info("TECHNIQUE API STREAM uid=%s status=%s", uid, status)

        if status == "done":
            final_payload = obj
            break
        elif status in ("failed", "error"):
            errors = obj.get("errors", [])
            warnings = obj.get("warnings", [])
            log.error(
                "TECHNIQUE API %s uid=%s errors=%s warnings=%s",
                status.upper(), uid, errors, warnings,
            )
            final_payload = obj
            break
        elif status == "processing":
            continue
        else:
            # Unknown status — keep reading
            log.debug("TECHNIQUE API unknown status uid=%s status=%s", uid, status)
            continue

    if final_payload is None:
        raise RuntimeError(
            f"Technique API stream ended without terminal status for uid={uid}"
        )

    return final_payload
