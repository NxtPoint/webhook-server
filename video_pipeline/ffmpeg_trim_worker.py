# ============================================================
# ffmpeg_trim_worker.py
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
FFMPEG_TIMEOUT_S = int(os.getenv("FFMPEG_TIMEOUT_S", "1800"))        # 30 min per segment
FFPROBE_TIMEOUT_S = int(os.getenv("FFPROBE_TIMEOUT_S", "60"))        # 1 min probe
MIN_DISK_FREE_MB = int(os.getenv("TRIM_MIN_DISK_FREE_MB", "500"))    # 500 MB minimum

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


def _write_concat_file(concat_path: Path, segment_files: List[Path]) -> None:
    """
    FFmpeg concat demuxer expects one file line per segment.
    Use resolved POSIX paths and escape single quotes safely.
    """
    lines: List[str] = []
    for sf in segment_files:
        resolved = sf.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{resolved}'")

    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sum_segment_durations(valid_segments: List[Tuple[float, float]]) -> float:
    return round(sum((e - s) for s, e in valid_segments), 3)


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

        src = td / "source_input"
        out = td / "review.mp4"
        concat_list = td / "concat.txt"

        # --------------------------
        # Disk space guard
        # --------------------------
        disk = shutil.disk_usage(td)
        free_mb = disk.free // (1024 * 1024)
        if free_mb < MIN_DISK_FREE_MB:
            raise RuntimeError(
                f"Insufficient disk space: {free_mb}MB free, need at least {MIN_DISK_FREE_MB}MB"
            )

        # --------------------------
        # Download + probe source
        # --------------------------
        log.info("FFMPEG TRIM task_id=%s downloading s3://%s/%s", task_id, s3_bucket, s3_key)
        s3.download_file(s3_bucket, s3_key, str(src))
        source_duration_s = _probe_duration(src)
        log.info("FFMPEG TRIM task_id=%s source_duration=%.3fs", task_id, source_duration_s)

        # --------------------------
        # Normalize segments
        # --------------------------
        valid_segments = _normalize_segments(segments, source_duration_s)
        if not valid_segments:
            raise ValueError("No valid segments remain after normalization/clamping")

        total_keep = sum(e - s for s, e in valid_segments)
        log.info(
            "FFMPEG TRIM task_id=%s segments=%d total_keep=%.3fs removing=%.3fs",
            task_id, len(valid_segments), total_keep, source_duration_s - total_keep,
        )

        # --------------------------
        # Render each segment
        # Re-encode each clip so concat is stable and deterministic
        # --------------------------
        segment_files: List[Path] = []

        for i, (s, e) in enumerate(valid_segments, start=1):
            seg_file = td / f"seg_{i:03d}.mp4"
            log.info("FFMPEG TRIM task_id=%s encoding segment %d/%d (%.3f-%.3fs)", task_id, i, len(valid_segments), s, e)

            _run([
                FFMPEG_BIN,
                "-y",
                "-ss", f"{s:.3f}",
                "-to", f"{e:.3f}",
                "-i", str(src),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", VIDEO_PRESET,
                "-crf", VIDEO_CRF,
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", AUDIO_BITRATE,
                "-movflags", "+faststart",
                str(seg_file),
            ])

            if not seg_file.exists():
                raise RuntimeError(f"Segment file was not created: {seg_file}")

            segment_files.append(seg_file)

        if not segment_files:
            raise ValueError("No segment files were created")

        # --------------------------
        # Concat
        # Since clips are normalized to same codec/container settings,
        # concat copy is acceptable and fast.
        # --------------------------
        _write_concat_file(concat_list, segment_files)

        _run([
            FFMPEG_BIN,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out),
        ])

        if not out.exists():
            raise RuntimeError("Final trimmed output was not created")

        trimmed_duration_s = _probe_duration(out)
        if trimmed_duration_s <= 0:
            raise RuntimeError("Trimmed output duration is invalid")

        # Prefer actual output duration as truth
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