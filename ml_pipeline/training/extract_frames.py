"""
ml_pipeline/training/extract_frames.py — Extract video frames for TrackNet training.

Saves frames as frame_000000.jpg, frame_000001.jpg, … in the output directory.
Frame indices are sequential from 0 and correspond directly to the frame_idx
values stored in ml_analysis.ball_detections.

Two extraction modes:
  1. Local video file: extract_frames(video_path, output_dir, fps=25)
  2. S3 key:          extract_frames_s3(s3_key, output_dir, fps=25)
     Downloads to a temp file, then extracts, then deletes the temp file.

Usage (standalone):
    python -m ml_pipeline.training.extract_frames <video_path> <output_dir> [--fps 25]
    python -m ml_pipeline.training.extract_frames s3://bucket/key <output_dir> [--fps 25]

Usage (via harness subcommand):
    python -m ml_pipeline.harness extract-frames <video_path_or_s3_key> <output_dir> [--fps 25]
"""

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import cv2

logger = logging.getLogger(__name__)

_DEFAULT_FPS = 25
_FRAME_JPEG_QUALITY = 95   # JPEG quality for saved frames (95 = high quality, ~smaller than PNG)


# ============================================================
# Core extraction
# ============================================================

def extract_frames(
    video_path: str,
    output_dir: str,
    fps: float = _DEFAULT_FPS,
) -> int:
    """
    Extract frames from a local video file at the given FPS rate.

    Saves frames as frame_{idx:06d}.jpg in output_dir. The idx starts at 0
    and increments for each *sampled* frame (not the original frame number),
    matching the convention used by ml_pipeline/video_preprocessor.py and
    the frame_idx stored in ml_analysis.ball_detections.

    Args:
        video_path: Path to the video file (.mp4, .mov, .avi, .mkv).
        output_dir: Directory to write frame JPEGs into (created if absent).
        fps:        Target extraction rate. Frames are sampled from the source
                    video at this rate (skipping frames if source FPS > target).

    Returns:
        Number of frames extracted.

    Raises:
        FileNotFoundError: if the video file does not exist.
        ValueError: if the video cannot be opened.
    """
    video_path = str(video_path)
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    source_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_source_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    logger.info(
        "extract_frames: source=%s  source_fps=%.2f  source_frames=%d  target_fps=%.2f",
        video_path, source_fps, total_source_frames, fps,
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Frame interval: how many source frames to advance per target frame
    # If source is 30fps and target is 25fps, advance ~1.2 source frames per output
    frame_interval = source_fps / fps if fps < source_fps else 1.0

    extracted = 0
    source_frame_idx = 0.0
    last_read_idx = -1

    jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, _FRAME_JPEG_QUALITY]

    while True:
        # Seek to the next source frame to read
        read_idx = int(source_frame_idx)
        if read_idx >= total_source_frames:
            break

        # Only seek when we've jumped past the current position
        if read_idx != last_read_idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, read_idx)

        ok, frame = cap.read()
        if not ok:
            break

        last_read_idx = read_idx

        frame_path = out_dir / f"frame_{extracted:06d}.jpg"
        cv2.imwrite(str(frame_path), frame, jpeg_params)

        extracted += 1
        source_frame_idx += frame_interval

    cap.release()

    logger.info("extract_frames: extracted %d frames to %s", extracted, output_dir)
    print(f"[INFO] Extracted {extracted} frames to {output_dir}")
    return extracted


def extract_frames_s3(
    s3_key: str,
    output_dir: str,
    fps: float = _DEFAULT_FPS,
    bucket: Optional[str] = None,
    region: Optional[str] = None,
) -> int:
    """
    Download a video from S3 and extract frames.

    Downloads the video to a temporary file, extracts frames, then deletes
    the temp file.  The temp file is always cleaned up even on error.

    Args:
        s3_key:     S3 key (e.g. "incoming/abc123/video.mp4").  Can also be a
                    full s3:// URI, in which case bucket is parsed from it.
        output_dir: Directory to write frame JPEGs into.
        fps:        Target extraction rate.
        bucket:     S3 bucket name.  Defaults to S3_BUCKET env var.
        region:     AWS region.  Defaults to AWS_REGION env var (or eu-north-1).

    Returns:
        Number of frames extracted.

    Raises:
        ValueError: if bucket cannot be determined.
        botocore.exceptions.ClientError: on S3 download failure.
    """
    import boto3

    # Parse s3:// URI if provided
    if s3_key.startswith("s3://"):
        without_scheme = s3_key[len("s3://"):]
        parts = without_scheme.split("/", 1)
        if len(parts) != 2 or not parts[1]:
            raise ValueError(f"Cannot parse S3 URI: {s3_key}")
        bucket = bucket or parts[0]
        s3_key = parts[1]

    bucket = bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError(
            "S3 bucket not specified. Pass --bucket or set S3_BUCKET env var."
        )

    region = region or os.environ.get("AWS_REGION", "eu-north-1")

    logger.info(
        "extract_frames_s3: s3://%s/%s  region=%s  fps=%.2f  output=%s",
        bucket, s3_key, region, fps, output_dir,
    )

    s3 = boto3.client("s3", region_name=region)

    # Infer file extension from key for the temp file
    ext = Path(s3_key).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name

    try:
        print(f"[INFO] Downloading s3://{bucket}/{s3_key} ...")
        s3.download_file(bucket, s3_key, tmp_path)
        file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        logger.info("Downloaded %.1f MB to temp file %s", file_size_mb, tmp_path)
        print(f"[INFO] Downloaded {file_size_mb:.1f} MB")

        return extract_frames(tmp_path, output_dir, fps=fps)

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
            logger.info("Deleted temp file %s", tmp_path)


# ============================================================
# CLI entry point (standalone)
# ============================================================

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    p = argparse.ArgumentParser(
        prog="ml_pipeline.training.extract_frames",
        description="Extract video frames for TrackNet training",
    )
    p.add_argument(
        "video",
        help="Path to video file, or s3://bucket/key, or a bare S3 key (requires --bucket)",
    )
    p.add_argument("output_dir", help="Directory to write frame JPEGs into")
    p.add_argument("--fps", type=float, default=_DEFAULT_FPS, help=f"Frame extraction rate (default {_DEFAULT_FPS})")
    p.add_argument("--bucket", default=None, help="S3 bucket (required for S3 keys without s3:// prefix)")
    p.add_argument("--region", default=None, help="AWS region (default: AWS_REGION env or eu-north-1)")

    args = p.parse_args()

    try:
        if args.video.startswith("s3://") or (args.bucket is not None and not os.path.isfile(args.video)):
            n = extract_frames_s3(
                s3_key=args.video,
                output_dir=args.output_dir,
                fps=args.fps,
                bucket=args.bucket,
                region=args.region,
            )
        else:
            n = extract_frames(args.video, args.output_dir, fps=args.fps)

        print(f"[INFO] Done — {n} frames extracted")

    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
