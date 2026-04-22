"""ROI bounce extraction v2 — per-service-box TIGHT crops.

v1 (extract_roi_bounces.py) used one combined rectangle covering both
service boxes, ~1448×374 px, which TrackNet downsamples to 640×360. The
ball stayed 1-2 px — too small. Tested on 6 serves, 0 bounces landed in
the service-box court zone.

v2 runs TWO separate crops per serve:

  FSB crop — far service box rectangle (court_y ∈ [5.48, 11.88])
            Expected size after perspective ~ 900×150 px. Ball is tiny
            here (far from camera) — this is where NEAR-player serves
            bounce.

  NSB crop — near service box rectangle (court_y ∈ [11.88, 18.28])
            Expected ~1300×250 px. Closer to camera, ball is larger —
            this is where FAR-player serves bounce.

Each crop is run through TrackNet independently with tight padding
(0.5m metric). Crop → TrackNet at 640×360 gives effective zoom of
~2-3× depending on the source dimensions, which should make the ball
~3-6 px instead of the ~1-2 px in v1.

Only runs the RELEVANT crop for each serve:
  - SA GT role=NEAR → FSB crop (near-player bounce lands far)
  - SA GT role=FAR  → NSB crop (far-player bounce lands near)

Cuts compute in half vs v1 (one crop instead of two+combined).

Usage:
    python -m ml_pipeline.diag.extract_roi_bounces_v2 \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088 \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --sportai 1515aff7-1ec7-472d-8dba-8fff9f939ff1 \\
        --max-serves 3    # sanity check; drop for full 24-serve run
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

logger = logging.getLogger("extract_roi_bounces_v2")


# ---------------------------------------------------------------------------
# Court geometry
# ---------------------------------------------------------------------------
COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
FAR_SERVICE_LINE_M = HALF_Y - SERVICE_LINE_FROM_NET_M   # 5.485
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M  # 18.285

DEFAULT_SPORTAI_REF = "1515aff7-1ec7-472d-8dba-8fff9f939ff1"


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _get_engine():
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("DB_URL"))
    if not url:
        raise RuntimeError("DATABASE_URL required")
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


def _get_sa_serves(conn, sportai_tid: str) -> List[dict]:
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
# Court calibration + per-box ROI computation
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
        raise RuntimeError("court_calibration failed")
    logger.info(
        "court_calibration: locked=%s calibration=%s",
        detector._locked_detection is not None,
        detector._calibration is not None,
    )
    return detector


def _project_court_to_pixel(mx, my, detector):
    import cv2
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj_calib
    calib = detector._calibration
    if calib is not None:
        p = proj_calib(mx, my, calib)
        if p is not None:
            return p
    best = (detector._locked_detection
            or detector._best_validated_detection
            or detector._best_detection)
    if best is not None and best.homography is not None:
        H_inv = np.linalg.inv(best.homography)
        pt = H_inv @ np.array([mx, my, 1.0])
        if pt[2] != 0:
            return float(pt[0] / pt[2]), float(pt[1] / pt[2])
    return None


def _service_box_roi(detector, frame_shape, box: str, pad_m: float = 0.5):
    """Compute pixel ROI for ONE service box.

    box='FSB' → court_y in [FAR_SERVICE_LINE_M, HALF_Y]
    box='NSB' → court_y in [HALF_Y, NEAR_SERVICE_LINE_M]
    """
    if box == "FSB":
        y_lo, y_hi = FAR_SERVICE_LINE_M, HALF_Y
    elif box == "NSB":
        y_lo, y_hi = HALF_Y, NEAR_SERVICE_LINE_M
    else:
        raise ValueError(f"unknown box {box}")

    corners_m = [
        (-pad_m, y_lo - pad_m),
        (COURT_WIDTH_DOUBLES_M + pad_m, y_lo - pad_m),
        (COURT_WIDTH_DOUBLES_M + pad_m, y_hi + pad_m),
        (-pad_m, y_hi + pad_m),
    ]
    pixel_corners = []
    for mx, my in corners_m:
        p = _project_court_to_pixel(mx, my, detector)
        if p is None:
            raise RuntimeError(f"cannot project ({mx},{my}) to pixel")
        pixel_corners.append(p)
    xs = [p[0] for p in pixel_corners]
    ys = [p[1] for p in pixel_corners]
    h, w = frame_shape[:2]
    pad_px = 20  # small pixel padding on top of metric pad
    x0 = max(0, int(min(xs) - pad_px))
    y0 = max(0, int(min(ys) - pad_px))
    x1 = min(w, int(max(xs) + pad_px))
    y1 = min(h, int(max(ys) + pad_px))
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# Per-window ball tracking
# ---------------------------------------------------------------------------

def _run_roi_window(video_path, start_frame, end_frame, roi):
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
            if crop.size == 0:
                continue
            tracker.detect_frame(crop, idx)
    finally:
        cap.release()
    tracker.interpolate_gaps()
    tracker.detect_bounces()
    # Diagnostic: also report raw detection count & tier breakdown
    return tracker.detections, dict(tracker._diag)


def _project_dets_to_court(dets, roi, detector):
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


def _in_target_zone(cx, cy, box: str) -> bool:
    """Is the court coordinate inside the expected service box zone?"""
    if cx is None or cy is None:
        return False
    if not (-1.0 <= cx <= COURT_WIDTH_DOUBLES_M + 1.0):
        return False
    if box == "FSB":
        return FAR_SERVICE_LINE_M - 1.0 <= cy <= HALF_Y + 0.5
    if box == "NSB":
        return HALF_Y - 0.5 <= cy <= NEAR_SERVICE_LINE_M + 1.0
    return False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _replace_rows(conn, task_id, source):
    n = conn.execute(sql_text("""
        DELETE FROM ml_analysis.ball_detections_roi
        WHERE job_id = :tid AND source = :src
    """), {"tid": task_id, "src": source}).rowcount
    if n:
        logger.info("deleted %d prior rows (source=%s)", n, source)


def _insert_rows(conn, task_id, source, rows):
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
            "job_id": task_id, "frame_idx": r["frame_idx"],
            "x": r["x"], "y": r["y"],
            "court_x": r["court_x"], "court_y": r["court_y"],
            "is_bounce": r["is_bounce"], "source": source,
            "window_serve_ts": r.get("window_serve_ts"),
        } for r in rows
    ])
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--video", required=True,
                    help="Local video path (S3 download out of scope for v2 — pass explicit path)")
    ap.add_argument("--sportai", default=DEFAULT_SPORTAI_REF,
                    help=f"SA reference task_id (default {DEFAULT_SPORTAI_REF[:8]})")
    ap.add_argument("--window-s", type=float, default=1.5,
                    help="Half-window in seconds around each SA serve time")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--max-serves", type=int, default=None,
                    help="Only process first N serves (for sanity testing)")
    ap.add_argument("--only-role", choices=["NEAR", "FAR"], default=None,
                    help="Only process serves with this role")
    ap.add_argument("--source-tag", default="roi_v2")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not os.path.exists(args.video):
        logger.error("video not found: %s", args.video)
        return 2

    engine = _get_engine()
    with engine.connect() as conn:
        sa_serves = _get_sa_serves(conn, args.sportai)

    if args.only_role:
        sa_serves = [s for s in sa_serves if s["role"] == args.only_role]
    if args.max_serves:
        sa_serves = sa_serves[:args.max_serves]

    if not sa_serves:
        logger.error("no SA serves found for task %s (role=%s)",
                     args.sportai, args.only_role or "any")
        return 1
    logger.info("processing %d SA serves (NEAR=%d FAR=%d)",
                len(sa_serves),
                sum(1 for s in sa_serves if s["role"] == "NEAR"),
                sum(1 for s in sa_serves if s["role"] == "FAR"))

    detector = _calibrate_court(args.video)
    import cv2
    cap = cv2.VideoCapture(args.video)
    ok, first = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("cannot read first frame")

    # Compute BOTH crops once
    fsb_roi = _service_box_roi(detector, first.shape, "FSB")
    nsb_roi = _service_box_roi(detector, first.shape, "NSB")
    logger.info("FSB crop: (%d,%d)-(%d,%d) size=%dx%d",
                *fsb_roi, fsb_roi[2] - fsb_roi[0], fsb_roi[3] - fsb_roi[1])
    logger.info("NSB crop: (%d,%d)-(%d,%d) size=%dx%d",
                *nsb_roi, nsb_roi[2] - nsb_roi[0], nsb_roi[3] - nsb_roi[1])

    window_frames = int(round(args.window_s * args.fps))
    all_rows = []
    for i, s in enumerate(sa_serves):
        ts = float(s["ts"])
        center = int(round(ts * args.fps))
        start_f = max(0, center - window_frames)
        end_f = center + window_frames
        # Pick the RELEVANT crop: NEAR hitter → FSB (ball lands far),
        # FAR hitter → NSB (ball lands near)
        if s["role"] == "NEAR":
            box, roi = "FSB", fsb_roi
        elif s["role"] == "FAR":
            box, roi = "NSB", nsb_roi
        else:
            logger.info("[%d/%d] ts=%.2f role=? — skip", i + 1, len(sa_serves), ts)
            continue
        logger.info("[%d/%d] ts=%.2f role=%s → %s crop frames [%d,%d)",
                    i + 1, len(sa_serves), ts, s["role"], box, start_f, end_f)
        t0 = time.time()
        dets, diag = _run_roi_window(args.video, start_f, end_f, roi)
        projected = _project_dets_to_court(dets, roi, detector)

        # Report per-serve diagnostics: what did TrackNet see, where
        total_dets = len(projected)
        dets_in_zone = sum(1 for r in projected if _in_target_zone(r["court_x"], r["court_y"], box))
        total_bounces = sum(1 for r in projected if r["is_bounce"])
        bounces_in_zone = sum(
            1 for r in projected
            if r["is_bounce"] and _in_target_zone(r["court_x"], r["court_y"], box)
        )
        logger.info(
            "  [%d frames, %.1fs]  tracker: frames_inferred=%d heatmap_empty=%d "
            "tier1=%d tier3=%d none=%d delta_hits=%d",
            end_f - start_f, time.time() - t0,
            diag.get("frames_inferred", 0),
            diag.get("heatmap_empty", 0),
            diag.get("tier1_hough", 0),
            diag.get("tier3_argmax", 0),
            diag.get("none_returned", 0),
            diag.get("delta_fallback_hits", 0),
        )
        logger.info(
            "  results:  total_dets=%d (in-zone=%d)  bounces=%d (in-zone=%d)",
            total_dets, dets_in_zone, total_bounces, bounces_in_zone,
        )

        # Keep only bounces in the TARGET service box zone
        for r in projected:
            if not r["is_bounce"]:
                continue
            if not _in_target_zone(r["court_x"], r["court_y"], box):
                continue
            r["window_serve_ts"] = ts
            all_rows.append(r)

    n_total = sum(1 for r in all_rows)
    logger.info("")
    logger.info("=== SUMMARY ===")
    logger.info("  total bounces kept in service-box zones: %d (across %d serves)",
                n_total, len(sa_serves))

    if args.dry_run:
        logger.info("dry-run: not writing to DB")
        return 0

    with engine.begin() as conn:
        _init_roi_schema(conn)
        _replace_rows(conn, args.task, args.source_tag)
        n = _insert_rows(conn, args.task, args.source_tag, all_rows)
        logger.info("wrote %d rows (source=%s)", n, args.source_tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
