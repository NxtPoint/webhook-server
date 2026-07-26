# ============================================================
# ffmpeg_trim_worker.py
# ============================================================
# FFmpeg-based video trimming worker. Runs as a detached subprocess
# spawned by video_worker_app.py after it returns 202 to the caller.
#
# Flow:
#   1. Download source video from S3 (wix-uploads/ prefix) to a temp file.
#   2. Probe video duration with ffprobe to validate EDL segment bounds.
#   3. Normalise EDL segments (clamp to [0, duration], discard empties).
#   4. Re-encode each keep segment with configurable CRF/preset (H.264).
#   5. Concatenate all clip files into a single review.mp4 via FFmpeg concat.
#   6. Upload the final file to S3 as trimmed/{task_id}/review.mp4.
#   7. POST a completion callback to VIDEO_TRIM_CALLBACK_URL with
#      { task_id, status: "completed"|"failed", output_s3_key }.
#
# Main entry: run_ffmpeg_trim(task_id, s3_key, edl, callback_url)
# Called from video_worker_app.py subprocess launch.
# ============================================================

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boto3

log = logging.getLogger(__name__)

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")

VIDEO_CRF = os.getenv("VIDEO_CRF", "28")
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "veryfast")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "96k")

# Hard floor to avoid useless micro-segments that can make ffmpeg unstable
MIN_KEEP_SEGMENT_S = float(os.getenv("MIN_KEEP_SEGMENT_S", "0.25"))

# Single deterministic output naming
OUTPUT_KEY_TEMPLATE = "trimmed/{task_id}/review.mp4"

# Safety ceilings
FFMPEG_TIMEOUT_S = int(os.getenv("FFMPEG_TIMEOUT_S", "1800"))        # legacy per-segment ceiling
FFPROBE_TIMEOUT_S = int(os.getenv("FFPROBE_TIMEOUT_S", "60"))        # 1 min probe
MIN_DISK_FREE_MB = int(os.getenv("TRIM_MIN_DISK_FREE_MB", "500"))    # 500 MB minimum

# Streaming single-pass trim (2026-07-26). The old path downloaded the full
# source + wrote N per-segment files + the output all to /tmp, which blew past
# Render's 2 GB /tmp limit on long matches → instance killed → orphaned trim.
# Now: stream the source straight from S3 (presigned URL, nothing downloaded)
# and do ONE trim+concat filtergraph pass → only the final clip touches /tmp.
TRIM_STREAM_INPUT = os.getenv("TRIM_STREAM_INPUT", "1").strip().lower() in ("1", "true", "yes", "y")
TRIM_ENCODE_TIMEOUT_S = int(os.getenv("TRIM_ENCODE_TIMEOUT_S", "3600"))   # whole single-pass encode
TRIM_PRESIGN_EXPIRY_S = int(os.getenv("TRIM_PRESIGN_EXPIRY_S", "21600"))  # 6h — covers a long encode

s3 = boto3.client("s3")


# ============================================================
# Low-level process helpers
# ============================================================

def _run(cmd: List[str], *, timeout: int | None = None) -> str:
    """
    Run a subprocess and return stdout.
    Raise RuntimeError with full stderr/stdout context on failure.
    """
    effective_timeout = timeout or FFMPEG_TIMEOUT_S
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Command timed out after {effective_timeout}s\n"
            f"cmd={' '.join(cmd)}"
        )

    if p.returncode != 0:
        raise RuntimeError(
            "Command failed\n"
            f"returncode={p.returncode}\n"
            f"cmd={' '.join(cmd)}\n"
            f"stdout={p.stdout}\n"
            f"stderr={p.stderr}"
        )
    return p.stdout.strip()


def _probe_duration(path: Path) -> float:
    if not path.exists():
        raise FileNotFoundError(f"File not found for duration probe: {path}")

    out = _run([
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ], timeout=FFPROBE_TIMEOUT_S)

    try:
        duration = float(out)
    except Exception as e:
        raise RuntimeError(f"Could not parse ffprobe duration output: {out!r}") from e

    if duration <= 0:
        raise RuntimeError(f"Invalid probed duration: {duration}")

    return duration


# ============================================================
# Segment helpers
# ============================================================

def _normalize_segments(
    segments: List[Dict[str, Any]],
    source_duration_s: float,
) -> List[Tuple[float, float]]:
    """
    Normalize, clamp, de-duplicate and remove invalid/tiny segments.
    Assumes timeline merge already happened upstream, but still hardens input.
    """
    cleaned: List[Tuple[float, float]] = []

    for raw in segments:
        try:
            s = max(0.0, float(raw["start_s"]))
            e = min(float(source_duration_s), float(raw["end_s"]))
        except Exception as e:
            raise ValueError(f"Invalid segment payload: {raw!r}") from e

        if e <= s:
            continue

        if (e - s) < MIN_KEEP_SEGMENT_S:
            continue

        cleaned.append((round(s, 3), round(e, 3)))

    if not cleaned:
        return []

    cleaned.sort(key=lambda x: (x[0], x[1]))

    # De-duplicate exact repeats
    deduped: List[Tuple[float, float]] = []
    prev: Tuple[float, float] | None = None
    for seg in cleaned:
        if prev is None or seg != prev:
            deduped.append(seg)
        prev = seg

    return deduped


def _sum_segment_durations(valid_segments: List[Tuple[float, float]]) -> float:
    return round(sum((e - s) for s, e in valid_segments), 3)


def _presigned_get_url(bucket: str, key: str, expires: int) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )


def _probe_source(src: str) -> Tuple[float, bool]:
    """Probe a source (local path OR http(s) URL) for (duration_s, has_audio).

    Works on a streamed presigned URL — ffprobe reads only the metadata via
    range requests, so no full download is needed to clamp segments."""
    dur_out = _run([
        FFPROBE_BIN, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        src,
    ], timeout=FFPROBE_TIMEOUT_S)
    try:
        duration = float(dur_out)
    except Exception as e:
        raise RuntimeError(f"Could not parse ffprobe duration: {dur_out!r}") from e
    if duration <= 0:
        raise RuntimeError(f"Invalid probed duration: {duration}")

    a_out = _run([
        FFPROBE_BIN, "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        src,
    ], timeout=FFPROBE_TIMEOUT_S)
    return duration, bool(a_out.strip())


def _build_filter_script(valid_segments: List[Tuple[float, float]], has_audio: bool) -> str:
    """Single-pass trim+concat filtergraph.

    Explicit split/asplit so the ONE input stream can feed every segment (no
    reliance on ffmpeg auto-split), keeping the whole trim to a single
    decode+encode pass — no per-segment temp files, so /tmp only ever holds the
    final clip. Written to a script file (-filter_complex_script) so a 100+
    segment graph never hits a command-line length limit."""
    n = len(valid_segments)
    lines: List[str] = []
    lines.append("[0:v]split=%d%s" % (n, "".join(f"[vs{i}]" for i in range(n))))
    if has_audio:
        lines.append("[0:a]asplit=%d%s" % (n, "".join(f"[as{i}]" for i in range(n))))
    concat_in: List[str] = []
    for i, (s, e) in enumerate(valid_segments):
        lines.append(f"[vs{i}]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        concat_in.append(f"[v{i}]")
        if has_audio:
            lines.append(f"[as{i}]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
            concat_in.append(f"[a{i}]")
    tail = "concat=n=%d:v=1:a=%d%s" % (n, 1 if has_audio else 0,
                                        "[outv][outa]" if has_audio else "[outv]")
    lines.append("".join(concat_in) + tail)
    return ";\n".join(lines)


# ============================================================
# Public worker API
# ============================================================

def run_ffmpeg_trim(*, task_id: str, s3_bucket: str, s3_key: str, edl: dict) -> dict:
    """
    Download source from S3, trim keep segments, concatenate, upload output to S3.

    Returns:
      {
        task_id,
        status,
        output_s3_key,
        source_duration_s,
        trimmed_duration_s,
        segment_count,
        seconds_removed
      }
    """
    if not task_id or not str(task_id).strip():
        raise ValueError("task_id is required")

    if not s3_bucket or not str(s3_bucket).strip():
        raise ValueError("s3_bucket is required")

    if not s3_key or not str(s3_key).strip():
        raise ValueError("s3_key is required")

    if not isinstance(edl, dict):
        raise ValueError("edl must be a dict")

    segments = edl.get("segments") or []
    if not isinstance(segments, list) or not segments:
        raise ValueError("EDL has no segments")

    with tempfile.TemporaryDirectory(prefix=f"trim_{task_id[:8]}_") as td_raw:
        td = Path(td_raw)
        out = td / "review.mp4"
        filter_script = td / "filter.txt"
        downloaded_src = td / "source_input"

        # --------------------------
        # Disk guard — when streaming, only the OUTPUT lands in /tmp, so this is
        # easily satisfied; it still catches a genuinely full volume.
        # --------------------------
        free_mb = shutil.disk_usage(td).free // (1024 * 1024)
        if free_mb < MIN_DISK_FREE_MB:
            raise RuntimeError(
                f"Insufficient disk space: {free_mb}MB free, need at least {MIN_DISK_FREE_MB}MB"
            )

        # --------------------------
        # Source: stream from S3 (default — nothing downloaded, /tmp-safe) or,
        # with TRIM_STREAM_INPUT=0, fall back to a full download.
        # --------------------------
        input_prefix_args: List[str] = []
        if TRIM_STREAM_INPUT:
            src = _presigned_get_url(s3_bucket, s3_key, TRIM_PRESIGN_EXPIRY_S)
            # Survive transient network hiccups mid-encode on the HTTP input.
            input_prefix_args = ["-reconnect", "1", "-reconnect_streamed", "1",
                                 "-reconnect_delay_max", "10"]
            log.info("FFMPEG TRIM task_id=%s streaming source from s3://%s/%s", task_id, s3_bucket, s3_key)
        else:
            log.info("FFMPEG TRIM task_id=%s downloading source s3://%s/%s", task_id, s3_bucket, s3_key)
            s3.download_file(s3_bucket, s3_key, str(downloaded_src))
            src = str(downloaded_src)

        source_duration_s, has_audio = _probe_source(src)
        log.info("FFMPEG TRIM task_id=%s source_duration=%.3fs has_audio=%s",
                 task_id, source_duration_s, has_audio)

        # --------------------------
        # Normalize segments
        # --------------------------
        valid_segments = _normalize_segments(segments, source_duration_s)
        if not valid_segments:
            raise ValueError("No valid segments remain after normalization/clamping")

        total_keep = sum(e - s for s, e in valid_segments)
        log.info(
            "FFMPEG TRIM task_id=%s segments=%d total_keep=%.3fs removing=%.3fs (single-pass)",
            task_id, len(valid_segments), total_keep, source_duration_s - total_keep,
        )

        # --------------------------
        # ONE trim+concat pass. Reads the source once (streamed), writes only the
        # final clip — no per-segment temp files, so /tmp stays tiny and it beats
        # the 2 GB limit that killed long-match trims.
        # --------------------------
        filter_script.write_text(_build_filter_script(valid_segments, has_audio), encoding="utf-8")

        cmd = [FFMPEG_BIN, "-y", *input_prefix_args, "-i", src,
               "-filter_complex_script", str(filter_script),
               "-map", "[outv]"]
        if has_audio:
            cmd += ["-map", "[outa]"]
        cmd += ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF, "-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
        cmd += ["-movflags", "+faststart", str(out)]

        _run(cmd, timeout=TRIM_ENCODE_TIMEOUT_S)

        if not out.exists():
            raise RuntimeError("Final trimmed output was not created")

        trimmed_duration_s = _probe_duration(out)
        if trimmed_duration_s <= 0:
            raise RuntimeError("Trimmed output duration is invalid")

        out_key = OUTPUT_KEY_TEMPLATE.format(task_id=task_id)
        log.info("FFMPEG TRIM task_id=%s uploading to s3://%s/%s (%.3fs)", task_id, s3_bucket, out_key, trimmed_duration_s)

        s3.upload_file(
            str(out),
            s3_bucket,
            out_key,
            ExtraArgs={
                "ContentType": "video/mp4",
            },
        )

        seconds_removed = max(0.0, round(source_duration_s - trimmed_duration_s, 3))

        log.info(
            "FFMPEG TRIM DONE task_id=%s source=%.1fs trimmed=%.1fs removed=%.1fs segments=%d",
            task_id, source_duration_s, trimmed_duration_s, seconds_removed, len(valid_segments),
        )

        return {
            "task_id": str(task_id),
            "status": "completed",
            "output_s3_key": out_key,
            "source_duration_s": round(source_duration_s, 3),
            "trimmed_duration_s": round(trimmed_duration_s, 3),
            "segment_count": int(len(valid_segments)),
            "seconds_removed": seconds_removed,
        }


# ============================================================
# Optional local smoke entry
# ============================================================

if __name__ == "__main__":
    sample = {
        "task_id": "example-task",
        "segments": [
            {"start_s": 5.0, "end_s": 11.5},
            {"start_s": 20.0, "end_s": 27.0},
        ],
    }
    print(json.dumps(sample, indent=2))
    print("Import and call run_ffmpeg_trim(task_id=..., s3_bucket=..., s3_key=..., edl=...)")