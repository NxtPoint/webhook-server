"""Run ROI-targeted ball detection around candidate serve times.

The bronze TrackNet pass runs at 640×360 on the full 1920×1080 frame.
At that scale a ball in a service box (especially the FAR service box on
the distant half of the court) is ~1-2 px — below TrackNet's effective
detection floor. Consequence on task `8a5e0b5e`:

  * every confirmed near-player serve has bounce_court_x/y = NULL
  * far-player serves are 0/10 because the bounce-first detector
    has no anchors

This tool runs TrackNet again on a TIGHT CROP covering both service
boxes, upsampled to 640×360. Effective ball size goes 1-2 px → 3-6 px,
which TrackNet can reliably detect. We run in short WINDOWS around known
serve timestamps (from the SportAI ground-truth reference task) so
runtime stays reasonable.

Output is written to a new table `ml_analysis.ball_detections_roi`.
The serve_detector is augmented (in a separate follow-up commit) to
merge rows from this table into its ball_rows so the augmented
bounces feed bounce-first far-player detection + near-player bounce
linking.

Usage on Render:

    # 1. Run ROI extraction on all SA-GT serve windows (~1-2 min per serve)
    python -m ml_pipeline.diag.extract_roi_bounces \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088

    # 2. Re-run silver / eval-serve to pick up the new bounces
    python -m ml_pipeline.harness rerun-silver \\
        8a5e0b5e-58a5-4236-a491-0fb7b3a25088
    python -m ml_pipeline.harness eval-serve \\
        8a5e0b5e-58a5-4236-a491-0fb7b3a25088

    # 3. Reconcile
    python -m ml_pipeline.diag.reconcile_serves_strict \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088

The tool is idempotent: re-running replaces rows for the same
(job_id, source) combination.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from typing import List, Optional, Tuple

import numpy as np
from sqlalchemy import create_engine, text as sql_text

logger = logging.getLogger("extract_roi_bounces")


# ---------------------------------------------------------------------------
# Court geometry
# ---------------------------------------------------------------------------
COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
FAR_SERVICE_LINE_M = HALF_Y - SERVICE_LINE_FROM_NET_M   # 5.485
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M  # 18.285

DEFAULT_SPORTAI_REF = "2c1ad953-b65b-41b4-9999-975964ff92e1"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

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


def _init_roi_schema(conn) -> None:
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS ml_analysis.ball_detections_roi (
            id              BIGSERIAL PRIMARY KEY,
            job_id          TEXT NOT NULL,
            frame_idx       INTEGER NOT NULL,
            x               DOUBLE PRECISION NOT NULL,
            y               DOUBLE PRECISION NOT NULL,
            court_x         DOUBLE PRECISION,
            court_y         DOUBLE PRECISION,
            is_bounce       BOOLEAN NOT NULL DEFAULT FALSE,
            source          TEXT NOT NULL DEFAULT 'roi_far',
            window_serve_ts DOUBLE PRECISION,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS idx_ball_detections_roi_job_bounce
            ON ml_analysis.ball_detections_roi (job_id) WHERE is_bounce = TRUE;
    """))


def _s3_head_ok(bucket: str, key: str) -> bool:
    import boto3
    from botocore.exceptions import ClientError
    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def _get_video_s3(conn, task_id: str,
                  sportai_fallback_tid: Optional[str] = None) -> Tuple[str, str]:
    """Resolve the S3 location of the task's video.

    Strategy:
      1. Read s3_bucket/s3_key from bronze.submission_context for task_id
      2. HeadObject — if it exists, return it
      3. If the fallback (SportAI) task_id is provided, try its s3_key
         (dual-submit matches share video content with the SA upload)
      4. Raise with a helpful message pointing at all the things we tried
    """
    row = conn.execute(sql_text("""
        SELECT s3_bucket, s3_key
        FROM bronze.submission_context
        WHERE task_id = :tid
    """), {"tid": task_id}).fetchone()
    if row is None:
        raise RuntimeError(f"no submission_context row for task {task_id}")
    bucket = row[0] or os.environ.get("S3_BUCKET")
    key = row[1]
    attempts = []
    if bucket and key:
        attempts.append((bucket, key, "submission_context"))
        if _s3_head_ok(bucket, key):
            return bucket, key

    # Fallback: SportAI reference task (dual-submit scenario — same video)
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
                attempts.append((sa_bucket, sa_key, "sportai fallback"))
                if _s3_head_ok(sa_bucket, sa_key):
                    logger.info(
                        "using SportAI fallback s3://%s/%s (primary object missing)",
                        sa_bucket, sa_key,
                    )
                    return sa_bucket, sa_key

    tried = "\n".join(f"    - s3://{b}/{k}  (from {src})" for b, k, src in attempts)
    raise RuntimeError(
        f"video not found in S3 for task {task_id}. Tried:\n{tried}\n"
        f"Options:\n"
        f"  - pass --s3-bucket <bucket> --s3-key <key> explicitly\n"
        f"  - pass --video <local_path> if you have the file locally\n"
        f"  - check `aws s3 ls s3://{bucket or '<bucket>'}/ --recursive "
        f"| grep <identifier>` for the real path"
    )


def _get_sa_serve_times(conn, sportai_tid: str) -> List[dict]:
    rows = conn.execute(sql_text("""
        SELECT ball_hit_s AS ts,
               CASE WHEN ball_hit_location_y > 22 THEN 'NEAR'
                    WHEN ball_hit_location_y < 2 THEN 'FAR'
                    ELSE '?' END AS role
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND serve_d = TRUE
          AND ball_hit_s IS NOT NULL
        ORDER BY ball_hit_s
    """), {"tid": sportai_tid}).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Video / S3
# ---------------------------------------------------------------------------

def _download_video_to_tmp(bucket: str, key: str) -> str:
    import boto3
    s3 = boto3.client("s3")
    # Give it a predictable name so repeat runs on the same task can reuse
    tmp_path = os.path.join(tempfile.gettempdir(),
                            f"roi_bounce_{os.path.basename(key)}")
    if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
        logger.info("video already on disk: %s (%.1f MB)",
                    tmp_path, os.path.getsize(tmp_path) / 1e6)
        return tmp_path
    logger.info("downloading s3://%s/%s -> %s", bucket, key, tmp_path)
    t0 = time.time()
    s3.download_file(bucket, key, tmp_path)
    logger.info("download took %.1fs, %.1f MB",
                time.time() - t0, os.path.getsize(tmp_path) / 1e6)
    return tmp_path


# ---------------------------------------------------------------------------
# Court ROI
# ---------------------------------------------------------------------------

def _calibrate_court(video_path: str, n_frames: int = 300):
    import cv2
    from ml_pipeline.court_detector import CourtDetector

    detector = CourtDetector()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        for idx in range(n_frames + 1):
            ok, frame = cap.read()
            if not ok:
                break
            detector.detect(frame, idx)
    finally:
        cap.release()

    if detector._locked_detection is None and detector._best_detection is None:
        raise RuntimeError("court_calibration failed — no detection produced")

    logger.info(
        "court_calibration: locked=%s best_validated_inliers=%d calibration=%s",
        detector._locked_detection is not None,
        detector._best_validated_inliers,
        detector._calibration is not None,
    )
    return detector


def _service_box_pixel_roi(detector, frame_shape, pad_px: int = 40):
    """Project the service-box rectangle from metres to pixels.

    Covers court_x in [-1, DOUBLES_WIDTH+1], court_y in
    [FAR_SERVICE_LINE_M-1.5, NEAR_SERVICE_LINE_M+1.5] — i.e. both
    service boxes plus a small margin."""
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj_calib

    corners_m = [
        (-1.0, FAR_SERVICE_LINE_M - 1.5),
        (COURT_WIDTH_DOUBLES_M + 1.0, FAR_SERVICE_LINE_M - 1.5),
        (COURT_WIDTH_DOUBLES_M + 1.0, NEAR_SERVICE_LINE_M + 1.5),
        (-1.0, NEAR_SERVICE_LINE_M + 1.5),
    ]

    pixel_corners = []
    calib = detector._calibration
    for (mx, my) in corners_m:
        p = None
        if calib is not None:
            p = proj_calib(mx, my, calib)
        if p is None:
            best = (detector._locked_detection
                    or detector._best_validated_detection
                    or detector._best_detection)
            if best is not None and best.homography is not None:
                H_inv = np.linalg.inv(best.homography)
                pt = H_inv @ np.array([mx, my, 1.0])
                if pt[2] != 0:
                    p = (pt[0] / pt[2], pt[1] / pt[2])
        if p is None:
            raise RuntimeError(f"cannot project court ({mx},{my}) to pixel")
        pixel_corners.append(p)

    xs = [p[0] for p in pixel_corners]
    ys = [p[1] for p in pixel_corners]
    h, w = frame_shape[:2]
    x0 = max(0, int(min(xs) - pad_px))
    y0 = max(0, int(min(ys) - pad_px))
    x1 = min(w, int(max(xs) + pad_px))
    y1 = min(h, int(max(ys) + pad_px))
    logger.info(
        "service_box_pixel_roi: crop=(%d,%d) to (%d,%d), size=%dx%d",
        x0, y0, x1, y1, x1 - x0, y1 - y0,
    )
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# ROI ball detection over windows
# ---------------------------------------------------------------------------

def _run_roi_window(
    video_path: str,
    start_frame: int,
    end_frame: int,
    roi: Tuple[int, int, int, int],
) -> list:
    """Run a fresh BallTracker on the ROI crop for frames [start, end).

    Returns the list of BallDetection objects WITH is_bounce set by
    velocity-reversal analysis. Coordinates are crop-pixel (not full-frame)."""
    import cv2
    from ml_pipeline.ball_tracker import BallTracker

    x0, y0, x1, y1 = roi
    tracker = BallTracker()

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    try:
        for idx in range(start_frame, end_frame):
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[y0:y1, x0:x1]
            tracker.detect_frame(crop, idx)
    finally:
        cap.release()

    tracker.interpolate_gaps()
    # detect_bounces without a court_detector — we'll project after
    tracker.detect_bounces()
    return tracker.detections


def _project_to_court(dets, roi, detector):
    """Map crop-pixel detections back to full-frame pixels, then to court metres."""
    x0, y0, _x1, _y1 = roi
    out = []
    for d in dets:
        full_x = d.x + float(x0)
        full_y = d.y + float(y0)
        court = detector.to_court_coords(full_x, full_y, strict=False)
        if court is None:
            cx = cy = None
        else:
            cx, cy = court
        out.append({
            "frame_idx": d.frame_idx,
            "x": full_x,
            "y": full_y,
            "court_x": cx,
            "court_y": cy,
            "is_bounce": d.is_bounce,
        })
    return out


def _in_service_box_zone(cx, cy) -> bool:
    if cx is None or cy is None:
        return False
    if not (-1.5 <= cx <= COURT_WIDTH_DOUBLES_M + 1.5):
        return False
    return FAR_SERVICE_LINE_M - 1.5 <= cy <= NEAR_SERVICE_LINE_M + 1.5


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _replace_existing_rows(conn, task_id: str, source: str) -> None:
    n = conn.execute(sql_text("""
        DELETE FROM ml_analysis.ball_detections_roi
        WHERE job_id = :tid AND source = :src
    """), {"tid": task_id, "src": source}).rowcount
    if n:
        logger.info("deleted %d prior ROI rows for (task=%s, source=%s)",
                    n, task_id, source)


def _insert_rows(conn, task_id: str, source: str, rows: list) -> int:
    if not rows:
        return 0
    conn.execute(sql_text("""
        INSERT INTO ml_analysis.ball_detections_roi
            (job_id, frame_idx, x, y, court_x, court_y,
             is_bounce, source, window_serve_ts)
        VALUES
            (:job_id, :frame_idx, :x, :y, :court_x, :court_y,
             :is_bounce, :source, :window_serve_ts)
    """), [
        {
            "job_id": task_id,
            "frame_idx": r["frame_idx"],
            "x": r["x"],
            "y": r["y"],
            "court_x": r["court_x"],
            "court_y": r["court_y"],
            "is_bounce": r["is_bounce"],
            "source": source,
            "window_serve_ts": r.get("window_serve_ts"),
        }
        for r in rows
    ])
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True,
                    help="T5 task_id to extract ROI bounces for")
    ap.add_argument("--video", default=None,
                    help="Local video path. If omitted, downloads from S3 via "
                         "bronze.submission_context.s3_bucket/s3_key, "
                         "falling back to the SportAI reference task's key "
                         "when the primary object is missing.")
    ap.add_argument("--s3-bucket", default=None,
                    help="Override S3 bucket (skips submission_context lookup)")
    ap.add_argument("--s3-key", default=None,
                    help="Override S3 key (skips submission_context lookup)")
    ap.add_argument("--sportai", default=DEFAULT_SPORTAI_REF,
                    help=f"SA reference task_id for serve times (default {DEFAULT_SPORTAI_REF[:8]})")
    ap.add_argument("--window-s", type=float, default=2.5,
                    help="Half-window in seconds around each SA serve time")
    ap.add_argument("--fps", type=float, default=25.0,
                    help="Video fps (default 25.0)")
    ap.add_argument("--source-tag", default="roi_far",
                    help="Value to write to source column (for experimentation)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do everything except write rows to the DB")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    engine = _get_engine()

    # --- DB reads --------------------------------------------------------
    with engine.connect() as conn:
        sa_serves = _get_sa_serve_times(conn, args.sportai)
        bucket = key = None
        if not args.video:
            if args.s3_bucket and args.s3_key:
                bucket, key = args.s3_bucket, args.s3_key
                logger.info("using explicit S3 override: s3://%s/%s", bucket, key)
            else:
                bucket, key = _get_video_s3(
                    conn, args.task, sportai_fallback_tid=args.sportai,
                )

    if not sa_serves:
        logger.error("no SA serves found for sportai task %s", args.sportai)
        return 1
    logger.info("SA GT: %d serves (NEAR=%d  FAR=%d  other=%d)",
                len(sa_serves),
                sum(1 for s in sa_serves if s["role"] == "NEAR"),
                sum(1 for s in sa_serves if s["role"] == "FAR"),
                sum(1 for s in sa_serves if s["role"] not in ("NEAR", "FAR")))

    # --- Video -----------------------------------------------------------
    video_path = args.video
    if not video_path:
        video_path = _download_video_to_tmp(bucket, key)
    if not os.path.exists(video_path):
        logger.error("video not found: %s", video_path)
        return 2

    # --- Calibration + ROI ----------------------------------------------
    detector = _calibrate_court(video_path)
    import cv2
    cap = cv2.VideoCapture(video_path)
    ok, first_frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("cannot read any frame for ROI computation")
    roi = _service_box_pixel_roi(detector, first_frame.shape)

    # --- Per-window ROI ball tracking -----------------------------------
    all_rows: list = []
    window_frames = int(round(args.window_s * args.fps))
    for i, s in enumerate(sa_serves):
        ts = float(s["ts"])
        center_frame = int(round(ts * args.fps))
        start_f = max(0, center_frame - window_frames)
        end_f = center_frame + window_frames
        logger.info(
            "[%d/%d] serve ts=%.2fs role=%s -> frames [%d, %d)",
            i + 1, len(sa_serves), ts, s["role"], start_f, end_f,
        )
        t0 = time.time()
        dets = _run_roi_window(video_path, start_f, end_f, roi)
        projected = _project_to_court(dets, roi, detector)
        # Filter to service-box zone AND attach the serve ts that triggered us.
        kept = []
        for r in projected:
            if not _in_service_box_zone(r["court_x"], r["court_y"]):
                continue
            r["window_serve_ts"] = ts
            kept.append(r)
        n_bounces = sum(1 for r in kept if r["is_bounce"])
        logger.info(
            "  processed %d frames in %.1fs -> %d dets in service-box zone, %d bounces",
            end_f - start_f, time.time() - t0, len(kept), n_bounces,
        )
        all_rows.extend(kept)

    # --- Summary ---------------------------------------------------------
    n_total = len(all_rows)
    n_bounces = sum(1 for r in all_rows if r["is_bounce"])
    far_sb_bounces = sum(
        1 for r in all_rows
        if r["is_bounce"] and r["court_y"] is not None
        and r["court_y"] <= HALF_Y
    )
    near_sb_bounces = sum(
        1 for r in all_rows
        if r["is_bounce"] and r["court_y"] is not None
        and r["court_y"] > HALF_Y
    )
    logger.info("")
    logger.info("=== SUMMARY ===")
    logger.info("  total detections in service-box zone: %d", n_total)
    logger.info("  total bounces: %d", n_bounces)
    logger.info("    - in FAR service box (y <= %.1f): %d",
                HALF_Y, far_sb_bounces)
    logger.info("    - in NEAR service box (y > %.1f): %d",
                HALF_Y, near_sb_bounces)

    # --- Persist ---------------------------------------------------------
    if args.dry_run:
        logger.info("dry-run: not writing to DB")
        return 0

    with engine.begin() as conn:
        _init_roi_schema(conn)
        _replace_existing_rows(conn, args.task, args.source_tag)
        n = _insert_rows(conn, args.task, args.source_tag, all_rows)
        logger.info("wrote %d rows to ml_analysis.ball_detections_roi "
                    "(task=%s, source=%s)", n, args.task[:8], args.source_tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
