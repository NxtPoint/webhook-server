# ============================================================
# ffmpeg_trim_worker.py
# ============================================================
# FFmpeg-based video trimming worker. Runs as a detached subprocess
# spawned by video_worker_app.py after it returns 202 to the caller.
#
# Flow:
#   1. Resolve the source: stream it from S3 (presigned URL) or, when it is
#      small enough to fit /tmp comfortably, download it once.
#   2. Probe duration / audio / fps (works on a URL — metadata only).
#   3. Normalise EDL segments (clamp to [0, duration], discard empties).
#   4. Encode the keep segments using ONE SEEK INPUT PER SEGMENT
#      (`-ss <start> -t <dur> -i <src>`) + the concat filter, so ffmpeg
#      decodes only the footage we keep — not the whole source.
#   5. Above TRIM_SEEK_INPUTS_PER_PASS segments this runs as several passes
#      producing part files, then joins them with the concat demuxer (-c copy).
#   6. Upload the final file to S3 as trimmed/{task_id}/review.mp4.
#   7. The caller (video_worker_app) POSTs the completion callback.
#
# Main entry: run_ffmpeg_trim(task_id, s3_bucket, s3_key, edl)
#
# ---------------- WHY SEEK INPUTS (2026-07-26, second iteration) ------------
# Iteration 1 fixed a /tmp blowout: the original worker downloaded the whole
# source + wrote N per-segment re-encodes + the output into Render's 2 GB /tmp,
# so long matches got the instance SIGKILLed. That was replaced by streaming the
# source and running ONE `trim`-filter graph over a single input.
#
# That fix exposed a worse bottleneck: a single-input `trim` graph must DECODE
# THE ENTIRE SOURCE to reach the later segments. On df594aea (74 min, 8.0 GB)
# that blew past TRIM_ENCODE_TIMEOUT_S=3600 and failed outright.
#
# Now each kept segment is its own INPUT with `-ss` before `-i`, which seeks
# (HTTP range request when streaming) instead of decoding forward. Only the
# ~30% of footage actually kept is decoded. `-ss` as an *input* option is
# keyframe-based but ffmpeg's default accurate_seek then decodes forward from
# that keyframe to the exact timestamp, so cuts stay frame-accurate while the
# skipped footage is never touched.
#
# Measured shape of the failing match: 86 segments, 23.8 min kept of 73.9 min
# (32%), source 8.0 GB. 8.0 GB is why "download once, seek locally" is NOT the
# default — it cannot fit a 2 GB /tmp (see _resolve_source).
# ============================================================

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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
FFMPEG_TIMEOUT_S = int(os.getenv("FFMPEG_TIMEOUT_S", "1800"))        # legacy per-command ceiling
FFPROBE_TIMEOUT_S = int(os.getenv("FFPROBE_TIMEOUT_S", "60"))        # 1 min probe
MIN_DISK_FREE_MB = int(os.getenv("TRIM_MIN_DISK_FREE_MB", "500"))    # 500 MB minimum

# Source strategy. TRIM_STREAM_INPUT=1 (default) allows streaming from S3;
# set it to 0 to force the download-once path (rollback / local-seek behaviour).
TRIM_STREAM_INPUT = os.getenv("TRIM_STREAM_INPUT", "1").strip().lower() in ("1", "true", "yes", "y")

# Whole-trim wall-clock budget: every ffmpeg pass shares this deadline, so N
# passes can never add up to more than one budget.
TRIM_ENCODE_TIMEOUT_S = int(os.getenv("TRIM_ENCODE_TIMEOUT_S", "3600"))
TRIM_PRESIGN_EXPIRY_S = int(os.getenv("TRIM_PRESIGN_EXPIRY_S", "21600"))  # 6h — covers a long encode

# Seek inputs per ffmpeg invocation. Each open input costs a socket + a demuxer
# index, and the worker runs on a 512 MB Render starter instance, so a 100-input
# single pass is a real OOM risk. Chunking bounds concurrent inputs without
# costing extra HTTP header reads (the moov is read once per input either way).
# 0 = no chunking (all segments in one pass) — escape hatch, no redeploy needed.
TRIM_SEEK_INPUTS_PER_PASS = int(os.getenv("TRIM_SEEK_INPUTS_PER_PASS", "12"))

# Only download-and-seek-locally when the source is at most this big. Local
# seeks are quicker and avoid N HTTP connections, but /tmp is 2 GB total and
# must also hold the output.
TRIM_LOCAL_COPY_MAX_MB = int(os.getenv("TRIM_LOCAL_COPY_MAX_MB", "1500"))

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
            f"cmd={_redact(cmd)}"
        )

    if p.returncode != 0:
        raise RuntimeError(
            "Command failed\n"
            f"returncode={p.returncode}\n"
            f"cmd={_redact(cmd)}\n"
            f"stdout={p.stdout}\n"
            f"stderr={p.stderr[-4000:]}"
        )
    return p.stdout.strip()


def _redact(cmd: Sequence[str]) -> str:
    """Collapse presigned URLs (long, credential-bearing, and repeated once per
    seek input) so a failure message stays readable and keeps the signature out
    of logs / trim_error."""
    parts: List[str] = []
    for a in cmd:
        if a.startswith("http") and ("X-Amz-Signature" in a or "Signature=" in a):
            parts.append("<presigned-url>")
        else:
            parts.append(a)
    return " ".join(parts)


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


def _chunk(items: Sequence[Any], size: int) -> Iterator[List[Any]]:
    """Split into consecutive chunks. size <= 0 means one chunk of everything."""
    if size <= 0 or size >= len(items):
        yield list(items)
        return
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


# ============================================================
# S3 helpers
# ============================================================

_bucket_region_cache: Dict[str, str] = {}


def _bucket_region(bucket: str) -> str:
    """Resolve the bucket's real region. The worker's default AWS_REGION may not
    match the bucket (worker=us-east-1, bucket=eu-north-1), and a presigned URL is
    region+signature specific — signing for the wrong region 400s.

    Detection order: explicit S3_BUCKET_REGION env → the `x-amz-bucket-region`
    header (returned even on a 403, so it works WITHOUT s3:GetBucketLocation) →
    get_bucket_location → AWS_REGION → us-east-1."""
    if bucket in _bucket_region_cache:
        return _bucket_region_cache[bucket]

    def _hdr_region(meta: dict) -> str:
        return (meta or {}).get("HTTPHeaders", {}).get("x-amz-bucket-region", "") or ""

    region = (os.getenv("S3_BUCKET_REGION") or "").strip()
    if not region:
        try:
            resp = s3.head_bucket(Bucket=bucket)
            region = _hdr_region(resp.get("ResponseMetadata", {}))
        except ClientError as ce:
            region = _hdr_region(ce.response.get("ResponseMetadata", {}))
        except Exception:
            region = ""
    if not region:
        try:
            region = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or ""
        except Exception:
            region = ""
    region = region or (os.getenv("AWS_REGION") or "us-east-1").strip()
    _bucket_region_cache[bucket] = region
    return region


def _presigned_get_url(bucket: str, key: str, expires: int) -> str:
    """SigV4 presigned GET in the bucket's own region. Newer regions (eu-north-1
    etc.) reject SigV2, and ffmpeg reads a static URL, so this must be exact."""
    region = _bucket_region(bucket)
    client = boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )


def _source_size_mb(bucket: str, key: str) -> Optional[float]:
    """Source size in MB, or None if it can't be read (never fatal — an unknown
    size simply means we stream rather than risk filling /tmp)."""
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        return float(head["ContentLength"]) / (1024 * 1024)
    except Exception as e:
        log.warning("TRIM head_object failed for s3://%s/%s: %s", bucket, key, e)
        return None


# ============================================================
# Probing
# ============================================================

@dataclass(frozen=True)
class _SourceInfo:
    duration_s: float
    has_audio: bool
    fps: Optional[float]


def _parse_fps(raw: str) -> Optional[float]:
    """ffprobe reports frame rates as a rational ('30000/1001')."""
    raw = (raw or "").strip().splitlines()[0].strip() if raw.strip() else ""
    if not raw or raw in ("0/0", "N/A"):
        return None
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return None
            fps = float(num) / den_f
        else:
            fps = float(raw)
    except Exception:
        return None
    return fps if 1.0 <= fps <= 240.0 else None


def _probe_source(src: str) -> _SourceInfo:
    """Probe a source (local path OR http(s) URL) for duration / audio / fps.

    Works on a streamed presigned URL — ffprobe reads only the container
    metadata via range requests, so no full download is needed."""
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

    fps_out = ""
    try:
        fps_out = _run([
            FFPROBE_BIN, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            src,
        ], timeout=FFPROBE_TIMEOUT_S)
    except Exception as e:
        # fps is an optimisation for multi-pass uniformity, never load-bearing
        log.warning("TRIM fps probe failed (continuing without): %s", e)

    return _SourceInfo(
        duration_s=duration,
        has_audio=bool(a_out.strip()),
        fps=_parse_fps(fps_out),
    )


# ============================================================
# Command construction
# (checked by video_pipeline/tests/test_trim_cmd.py — pure arg math, no ffmpeg;
#  end-to-end against a real ffmpeg: video_pipeline/tests/e2e_trim_docker.py)
# ============================================================

def _build_concat_filter(n: int, has_audio: bool) -> str:
    """Concat filter over N seek inputs.

    Each input is ALREADY cut to its segment by `-ss`/`-t`, so there is no
    trim/setpts here and nothing has to be split off one decoded stream — that
    is exactly what let the old single-input graph decode the whole source."""
    if n < 1:
        raise ValueError("concat filter needs at least one input")
    labels = "".join(f"[{i}:v][{i}:a]" if has_audio else f"[{i}:v]" for i in range(n))
    tail = "concat=n=%d:v=1:a=%d%s" % (n, 1 if has_audio else 0,
                                       "[outv][outa]" if has_audio else "[outv]")
    return labels + tail


def _build_pass_cmd(
    *,
    src: str,
    segments: Sequence[Tuple[float, float]],
    has_audio: bool,
    filter_script: Path,
    out_path: Path,
    stream_input: bool,
    force_fps: Optional[float],
) -> List[str]:
    """One ffmpeg invocation: N seek inputs → concat → one encoded file.

    `-ss` BEFORE `-i` is the whole point: it seeks (HTTP range request when
    streaming) rather than decoding forward from 0. `-t <duration>` is used
    instead of `-to` because, as an input option, `-t` is unambiguously
    relative to the seek point."""
    cmd = [FFMPEG_BIN, "-y", "-hide_banner", "-nostdin"]

    for start_s, end_s in segments:
        dur = round(end_s - start_s, 3)
        if dur <= 0:
            raise ValueError(f"non-positive segment duration: {start_s}..{end_s}")
        if stream_input:
            # Survive transient network hiccups on each HTTP input.
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10"]
        cmd += ["-ss", f"{start_s:.3f}", "-t", f"{dur:.3f}", "-i", src]

    cmd += ["-filter_complex_script", str(filter_script), "-map", "[outv]"]
    if has_audio:
        cmd += ["-map", "[outa]"]

    cmd += ["-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", VIDEO_CRF,
            "-pix_fmt", "yuv420p"]
    if force_fps:
        # Multi-pass only: the parts are joined with `-c copy`, so every part
        # must share a frame rate or the join drifts (phone .mov sources are
        # often variable-frame-rate).
        # -fps_mode (not the deprecated -vsync): available since ffmpeg 5.0 and
        # still current in 7.x, so it survives a base-image bump.
        cmd += ["-r", f"{force_fps:.6f}", "-fps_mode", "cfr"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
        if force_fps:
            cmd += ["-ar", "48000", "-ac", "2"]

    cmd += ["-movflags", "+faststart", str(out_path)]
    return cmd


def _build_concat_demuxer_cmd(list_file: Path, out_path: Path) -> List[str]:
    """Join already-encoded parts without re-encoding."""
    return [
        FFMPEG_BIN, "-y", "-hide_banner", "-nostdin",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(out_path),
    ]


def _write_concat_list(parts: Sequence[Path], list_file: Path) -> None:
    list_file.write_text(
        "".join(f"file '{p.name}'\n" for p in parts),
        encoding="utf-8",
    )


# ============================================================
# Source resolution
# ============================================================

def _resolve_source(
    *, task_id: str, s3_bucket: str, s3_key: str, workdir: Path, free_mb: float,
) -> Tuple[str, bool]:
    """Decide how ffmpeg reads the source. Returns (src, stream_input).

    Downloading once and seeking locally is the faster, more predictable option
    — no N HTTP connections, no per-input container-header re-read — but /tmp is
    2 GB and must also hold the output, so it is only safe for a small source.
    The match that motivated this rewrite is 8.0 GB, so streaming is the
    default for anything sizeable.
    """
    if not TRIM_STREAM_INPUT:
        log.info("FFMPEG TRIM task_id=%s TRIM_STREAM_INPUT=0 → forced download", task_id)
        local = workdir / "source_input"
        s3.download_file(s3_bucket, s3_key, str(local))
        return str(local), False

    size_mb = _source_size_mb(s3_bucket, s3_key)
    # Keep MIN_DISK_FREE_MB headroom, and leave room for the output too.
    room_mb = free_mb - MIN_DISK_FREE_MB
    fits_locally = (
        size_mb is not None
        and size_mb <= TRIM_LOCAL_COPY_MAX_MB
        and (size_mb * 2.0) < room_mb   # source + generous output allowance
    )

    if fits_locally:
        log.info(
            "FFMPEG TRIM task_id=%s source %.0fMB fits /tmp (free %.0fMB) → download once, local seeks",
            task_id, size_mb, free_mb,
        )
        local = workdir / "source_input"
        s3.download_file(s3_bucket, s3_key, str(local))
        return str(local), False

    log.info(
        "FFMPEG TRIM task_id=%s streaming source from s3://%s/%s (size=%s free=%.0fMB)",
        task_id, s3_bucket, s3_key,
        f"{size_mb:.0f}MB" if size_mb is not None else "unknown", free_mb,
    )
    return _presigned_get_url(s3_bucket, s3_key, TRIM_PRESIGN_EXPIRY_S), True


# ============================================================
# Public worker API
# ============================================================

def run_ffmpeg_trim(*, task_id: str, s3_bucket: str, s3_key: str, edl: dict) -> dict:
    """
    Trim the keep segments out of an S3 source and upload one review.mp4.

    Decodes ONLY the kept footage (one seek input per segment), so runtime scales
    with the highlight length rather than the match length.

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

    started = time.monotonic()
    deadline = started + TRIM_ENCODE_TIMEOUT_S

    def _remaining(minimum: int = 60) -> int:
        """Per-command timeout drawn from the ONE whole-trim budget, so N passes
        can never sum past TRIM_ENCODE_TIMEOUT_S."""
        return max(minimum, int(deadline - time.monotonic()))

    with tempfile.TemporaryDirectory(prefix=f"trim_{task_id[:8]}_") as td_raw:
        td = Path(td_raw)
        out = td / "review.mp4"

        # --------------------------
        # Disk guard
        # --------------------------
        free_mb = shutil.disk_usage(td).free / (1024 * 1024)
        if free_mb < MIN_DISK_FREE_MB:
            raise RuntimeError(
                f"Insufficient disk space: {free_mb:.0f}MB free, need at least {MIN_DISK_FREE_MB}MB"
            )

        # --------------------------
        # Source + probe
        # --------------------------
        src, stream_input = _resolve_source(
            task_id=task_id, s3_bucket=s3_bucket, s3_key=s3_key,
            workdir=td, free_mb=free_mb,
        )

        info = _probe_source(src)
        log.info(
            "FFMPEG TRIM task_id=%s source_duration=%.3fs has_audio=%s fps=%s streaming=%s",
            task_id, info.duration_s, info.has_audio,
            f"{info.fps:.3f}" if info.fps else "unknown", stream_input,
        )

        # --------------------------
        # Normalize segments
        # --------------------------
        valid_segments = _normalize_segments(segments, info.duration_s)
        if not valid_segments:
            raise ValueError("No valid segments remain after normalization/clamping")

        total_keep = _sum_segment_durations(valid_segments)
        batches = list(_chunk(valid_segments, TRIM_SEEK_INPUTS_PER_PASS))
        multipass = len(batches) > 1

        log.info(
            "FFMPEG TRIM task_id=%s segments=%d total_keep=%.1fs (%.1f%% of source) "
            "removing=%.1fs passes=%d inputs_per_pass=%d",
            task_id, len(valid_segments), total_keep,
            100.0 * total_keep / info.duration_s,
            info.duration_s - total_keep, len(batches),
            TRIM_SEEK_INPUTS_PER_PASS if multipass else len(valid_segments),
        )

        # Force CFR only when the parts will be joined with `-c copy`; a single
        # pass needs no cross-part uniformity, so leave its behaviour untouched.
        force_fps = info.fps if multipass else None
        if multipass and not force_fps:
            log.warning(
                "FFMPEG TRIM task_id=%s multi-pass with UNKNOWN source fps — parts "
                "cannot be frame-rate normalised, so a variable-frame-rate source "
                "may drift across the concat join", task_id,
            )

        # --------------------------
        # Encode: N seek inputs per pass → concat
        # --------------------------
        parts: List[Path] = []
        for i, batch in enumerate(batches):
            target = out if not multipass else (td / f"part_{i:04d}.mp4")
            fscript = td / f"filter_{i:04d}.txt"
            fscript.write_text(
                _build_concat_filter(len(batch), info.has_audio), encoding="utf-8"
            )

            cmd = _build_pass_cmd(
                src=src, segments=batch, has_audio=info.has_audio,
                filter_script=fscript, out_path=target,
                stream_input=stream_input, force_fps=force_fps,
            )

            t0 = time.monotonic()
            _run(cmd, timeout=_remaining())
            if not target.exists() or target.stat().st_size == 0:
                raise RuntimeError(f"pass {i + 1}/{len(batches)} produced no output")

            batch_keep = _sum_segment_durations(batch)
            log.info(
                "FFMPEG TRIM task_id=%s pass %d/%d done segments=%d keep=%.1fs in %.1fs "
                "(%.0fMB free, %ds budget left)",
                task_id, i + 1, len(batches), len(batch), batch_keep,
                time.monotonic() - t0,
                shutil.disk_usage(td).free / (1024 * 1024),
                _remaining(0),
            )
            parts.append(target)

        # --------------------------
        # Join parts (stream copy — no second encode)
        # --------------------------
        if multipass:
            list_file = td / "parts.txt"
            _write_concat_list(parts, list_file)
            _run(_build_concat_demuxer_cmd(list_file, out), timeout=_remaining())
            # Reclaim /tmp before the upload.
            for p in parts:
                try:
                    p.unlink()
                except Exception:
                    pass

        if not out.exists():
            raise RuntimeError("Final trimmed output was not created")

        trimmed_duration_s = _probe_duration(out)
        if trimmed_duration_s <= 0:
            raise RuntimeError("Trimmed output duration is invalid")

        out_key = OUTPUT_KEY_TEMPLATE.format(task_id=task_id)
        out_mb = out.stat().st_size / (1024 * 1024)
        log.info(
            "FFMPEG TRIM task_id=%s uploading to s3://%s/%s (%.1fs, %.0fMB)",
            task_id, s3_bucket, out_key, trimmed_duration_s, out_mb,
        )

        s3.upload_file(
            str(out),
            s3_bucket,
            out_key,
            ExtraArgs={
                "ContentType": "video/mp4",
            },
        )

        seconds_removed = max(0.0, round(info.duration_s - trimmed_duration_s, 3))

        log.info(
            "FFMPEG TRIM DONE task_id=%s source=%.1fs trimmed=%.1fs removed=%.1fs "
            "segments=%d passes=%d elapsed=%.1fs",
            task_id, info.duration_s, trimmed_duration_s, seconds_removed,
            len(valid_segments), len(batches), time.monotonic() - started,
        )

        return {
            "task_id": str(task_id),
            "status": "completed",
            "output_s3_key": out_key,
            "source_duration_s": round(info.duration_s, 3),
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
