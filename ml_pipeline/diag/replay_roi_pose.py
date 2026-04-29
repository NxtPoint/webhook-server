"""Re-run extract_far_pose on specific frame ranges with the rally gate
disabled, writing to a debug source tag.

Used to test the hypothesis that the rally gate inside extract_far_pose
(idle_threshold_s=3) is what's blocking ROI pose extraction during the
serve windows for FAR misses on a798eff0 (458.08, 463.52, 584.92).

Usage on Render (DATABASE_URL + AWS creds set):

    python -m ml_pipeline.diag.replay_roi_pose \\
        --task a798eff0-551f-4b5a-838f-7933866a727c \\
        --ranges 11300-11700,14500-14800 \\
        --source-tag far_nogate_test

Then inspect the new rows:

    python -m ml_pipeline.diag.probe_roi_coverage \\
        --task a798eff0-551f-4b5a-838f-7933866a727c \\
        --ts 458.08,463.52,584.92 --player 1 --dump-rows

(probe_roi_coverage shows ALL ROI rows — far_vitpose AND far_nogate_test
will appear side-by-side in the per-frame dump because both go to
ml_analysis.player_detections_roi. Filter on `source` in the dump to
distinguish them.)

Outcomes:
  - New rows appear in baseline zone for the miss windows → rally gate
    was the entire problem; relax the gate in production.
  - New rows appear but court_y is NULL or out of zone → wrong-body
    detection; ROI extractor can't see the actual far player here.
  - No new rows even with gate disabled → YOLO can't find a person in
    the far ROI at all; player is occluded / off-camera.

Runtime: ~1-2 s/frame on CPU (YOLO + ViTPose). For 400 + 300 frames at
sample_every=2 = 350 sampled frames, expect ~6-12 minutes total.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from typing import List, Tuple

from sqlalchemy import create_engine, text as sql_text


logger = logging.getLogger("replay_roi_pose")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _get_engine():
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("DB_URL"))
    if not url:
        raise RuntimeError("DATABASE_URL (or POSTGRES_URL / DB_URL) required")
    return create_engine(_normalize_db_url(url))


def _s3_head_ok(bucket: str, key: str) -> bool:
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def _resolve_video_s3(conn, task_id: str,
                      sportai_fallback_tid: str = None,
                      trimmed_fallback: bool = True) -> Tuple[str, str]:
    """Resolve an S3 location for the task's video.

    Strategy:
      1. submission_context.s3_bucket/s3_key — primary upload (often deleted
         after ingest by cleanup)
      2. submission_context.trim_output_s3_key — trimmed point-clips only,
         skipped here because we need the full video for mid-rally frames
      3. SportAI dual-submit fallback — sportai_fallback_tid's s3_key
      4. Raise with attempts.
    """
    row = conn.execute(sql_text("""
        SELECT s3_bucket, s3_key
        FROM bronze.submission_context
        WHERE task_id = :tid
    """), {"tid": task_id}).fetchone()
    if row is None:
        raise RuntimeError(f"no submission_context row for task {task_id}")
    attempts = []
    bucket = row[0] or os.environ.get("S3_BUCKET")
    key = row[1]
    if bucket and key:
        attempts.append((bucket, key, "primary submission_context"))
        if _s3_head_ok(bucket, key):
            return bucket, key
        logger.warning("primary s3://%s/%s not found (HeadObject 404)", bucket, key)

    if sportai_fallback_tid:
        sa_row = conn.execute(sql_text("""
            SELECT s3_bucket, s3_key
            FROM bronze.submission_context
            WHERE task_id = :tid
        """), {"tid": sportai_fallback_tid}).fetchone()
        if sa_row:
            sa_bucket = sa_row[0] or os.environ.get("S3_BUCKET")
            sa_key = sa_row[1]
            if sa_bucket and sa_key:
                attempts.append((sa_bucket, sa_key, "SportAI fallback"))
                if _s3_head_ok(sa_bucket, sa_key):
                    logger.info("using SportAI fallback s3://%s/%s",
                                sa_bucket, sa_key)
                    return sa_bucket, sa_key
                logger.warning("SportAI fallback s3://%s/%s not found",
                               sa_bucket, sa_key)

    tried = "\n".join(f"    - s3://{b}/{k}  ({src})"
                      for b, k, src in attempts) or "    (none — no rows found)"
    raise RuntimeError(
        f"video not found in S3 for task {task_id}. Tried:\n{tried}\n"
        f"Options:\n"
        f"  - pass --s3-bucket <bucket> --s3-key <key> explicitly\n"
        f"  - pass --video <local_path> if you have the file locally\n"
        f"  - pass --sportai <SA_TID> if dual-submit ref is different"
    )


def _download_video_to_tmp(bucket: str, key: str) -> str:
    import boto3
    s3 = boto3.client("s3")
    tmp_path = os.path.join(tempfile.gettempdir(),
                            f"replay_roi_{os.path.basename(key)}")
    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
        logger.info("video already on disk: %s (%.1f MB)",
                    tmp_path, os.path.getsize(tmp_path) / 1e6)
        return tmp_path
    logger.info("downloading s3://%s/%s -> %s", bucket, key, tmp_path)
    t0 = time.time()
    s3.download_file(bucket, key, tmp_path)
    dt = time.time() - t0
    size_mb = os.path.getsize(tmp_path) / 1e6
    logger.info("download took %.1fs, %.1f MB", dt, size_mb)
    return tmp_path


def _parse_ranges(s: str) -> List[Tuple[int, int]]:
    out = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            raise ValueError(f"bad range token: {chunk!r}")
        a, b = chunk.split("-", 1)
        out.append((int(a.strip()), int(b.strip())))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="T5 task_id")
    ap.add_argument("--ranges", required=True,
                    help="Comma-sep frame ranges, e.g. 11300-11700,14500-14800")
    ap.add_argument("--source-tag", default="far_nogate_test",
                    help="ROI table source tag (default far_nogate_test)")
    ap.add_argument("--video", default=None,
                    help="Local video path (otherwise download from S3)")
    ap.add_argument("--s3-bucket", default=None,
                    help="Override S3 bucket (with --s3-key) — bypass DB lookup")
    ap.add_argument("--s3-key", default=None,
                    help="Override S3 key (with --s3-bucket) — bypass DB lookup")
    ap.add_argument("--sportai", default="2c1ad953-b65b-41b4-9999-975964ff92e1",
                    help="SportAI task_id for dual-submit fallback when the "
                         "T5 task's video has been deleted")
    ap.add_argument("--rally-gate", action="store_true",
                    help="Keep rally gate ON (default: gate OFF — that's "
                         "the point of this diag)")
    ap.add_argument("--sample-every", type=int, default=2)
    ap.add_argument("--det-conf", type=float, default=0.15)
    ap.add_argument("--cleanup", action="store_true",
                    help="DELETE all rows for this (job_id, source-tag) and "
                         "exit. Use after the diag run is done to avoid "
                         "polluting future production detector runs.")
    args = ap.parse_args(argv)

    # --cleanup mode: just delete the debug rows and exit
    if args.cleanup:
        engine = _get_engine()
        with engine.begin() as conn:
            n = conn.execute(sql_text("""
                DELETE FROM ml_analysis.player_detections_roi
                WHERE job_id = :t AND source = :s
            """), {"t": args.task, "s": args.source_tag}).rowcount
        print(f"deleted {n} rows  (job_id={args.task[:8]}, source={args.source_tag})")
        return 0

    ranges = _parse_ranges(args.ranges)
    if not ranges:
        print("no ranges supplied", file=sys.stderr)
        return 2

    engine = _get_engine()

    # Get video — local path > explicit s3 args > DB lookup with SA fallback
    video_path = args.video
    if video_path is None:
        if args.s3_bucket and args.s3_key:
            bucket, key = args.s3_bucket, args.s3_key
            logger.info("using explicit s3 override: s3://%s/%s", bucket, key)
        else:
            with engine.connect() as conn:
                bucket, key = _resolve_video_s3(
                    conn, args.task,
                    sportai_fallback_tid=args.sportai,
                )
        video_path = _download_video_to_tmp(bucket, key)
    if not os.path.exists(video_path):
        print(f"video not found: {video_path}", file=sys.stderr)
        return 2

    # Pick up fps for logging
    with engine.connect() as conn:
        fps = conn.execute(sql_text(
            "SELECT COALESCE(video_fps, 25.0) FROM ml_analysis.video_analysis_jobs "
            "WHERE job_id = :t"
        ), {"t": args.task}).scalar() or 25.0

    # Build court detector once and reuse across ranges
    from ml_pipeline.court_detector import CourtDetector
    import cv2
    logger.info("calibrating court detector on first 300 frames")
    court_detector = CourtDetector()
    cap = cv2.VideoCapture(video_path)
    try:
        for i in range(301):
            ok, f = cap.read()
            if not ok:
                break
            court_detector.detect(f, i)
    finally:
        cap.release()
    if (court_detector._locked_detection is None
            and court_detector._best_detection is None):
        print("court calibration failed — cannot project ROI", file=sys.stderr)
        return 2
    logger.info("court calibration done")

    # Run extract_far_pose for each range. bounces=None disables the rally
    # gate. replace=False so multiple ranges accumulate under the same tag.
    # First range uses replace=True to wipe any prior debug rows for clean
    # start.
    from ml_pipeline.roi_extractors import extract_far_pose
    bounces = None  # explicit — gate is OFF unless --rally-gate
    if args.rally_gate:
        # Reload bronze bounces if user explicitly asked to keep gate on
        with engine.connect() as conn:
            rows = conn.execute(sql_text("""
                SELECT frame_idx FROM ml_analysis.ball_detections
                WHERE job_id = :t AND is_bounce = TRUE
                ORDER BY frame_idx
            """), {"t": args.task}).scalars().all()

        class _B:
            def __init__(self, fi):
                self.frame_idx = fi
                self.is_bounce = True
        bounces = [_B(r) for r in rows]
        logger.info("rally gate ON with %d bronze bounces", len(bounces))
    else:
        logger.info("rally gate OFF (bounces=None)")

    total_written = 0
    for i, (frame_from, frame_to) in enumerate(ranges):
        is_first = (i == 0)
        logger.info("=== range %d/%d: frames %d-%d (%.2f-%.2fs) ===",
                    i + 1, len(ranges), frame_from, frame_to,
                    frame_from / fps, frame_to / fps)
        n = extract_far_pose(
            video_path=video_path,
            job_id=args.task,
            engine=engine,
            fps=fps,
            sample_every=args.sample_every,
            det_conf=args.det_conf,
            source_tag=args.source_tag,
            court_detector=court_detector,
            bounces=bounces,
            frame_from=frame_from,
            frame_to=frame_to,
            replace=is_first,  # only first range wipes prior rows
        )
        logger.info("range wrote %d rows", n)
        total_written += n

    print(f"=== replay_roi_pose: wrote {total_written} total rows "
          f"(source={args.source_tag}) ===")
    print()
    print("Next: inspect the new rows with:")
    print(f"  python -m ml_pipeline.diag.probe_roi_coverage \\")
    print(f"      --task {args.task} \\")
    print(f"      --ts 458.08,463.52,584.92 --player 1 --dump-rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
