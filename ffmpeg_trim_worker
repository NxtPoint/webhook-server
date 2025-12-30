# ============================================================
# ffmpeg_trim_worker.py
# ============================================================

import subprocess
import tempfile
from pathlib import Path

import boto3

FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

# Output quality baseline (storage-optimized)
VIDEO_CRF = "28"
VIDEO_PRESET = "veryfast"
AUDIO_BITRATE = "96k"

s3 = boto3.client("s3")

def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)


def _probe_duration(path: Path) -> float:
    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return float(p.stdout.strip())


def run_ffmpeg_trim(*, task_id: str, s3_bucket: str, s3_key: str, edl: dict) -> dict:
    segments = edl.get("segments") or []
    if not segments:
        raise ValueError("EDL has no segments")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        src = td / "source.mp4"
        out = td / "trimmed.mp4"
        concat_list = td / "concat.txt"

        s3.download_file(s3_bucket, s3_key, str(src))

        segment_files: list[Path] = []

        for i, seg in enumerate(segments, start=1):
            s = float(seg["start_s"])
            e = float(seg["end_s"])
            if e <= s:
                raise ValueError(f"Invalid segment: start_s={s} end_s={e}")

            seg_file = td / f"seg_{i:03d}.mp4"
            segment_files.append(seg_file)

            # Accurate trim (ss after -i), re-encode to consistent output profile
            _run([
                FFMPEG_BIN, "-y",
                "-i", str(src),
                "-ss", f"{s}",
                "-to", f"{e}",
                "-map", "0:v:0",
                "-map", "0:a?",                 # audio optional
                "-c:v", "libx264",
                "-preset", VIDEO_PRESET,
                "-crf", VIDEO_CRF,
                "-c:a", "aac",
                "-b:a", AUDIO_BITRATE,
                "-movflags", "+faststart",
                str(seg_file),
            ])

        with open(concat_list, "w", encoding="utf-8") as f:
            for sf in segment_files:
                f.write(f"file '{sf.as_posix()}'\n")

        # Concat demuxer (stream copy works because we encoded segments identically)
        _run([
            FFMPEG_BIN, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(out),
        ])

        duration_s = _probe_duration(out)

        out_key = s3_key.replace(".mp4", "_trimmed.mp4")
        s3.upload_file(str(out), s3_bucket, out_key)

        return {
            "task_id": task_id,
            "status": "completed",
            "output_s3_key": out_key,
            "duration_s": duration_s,
        }
