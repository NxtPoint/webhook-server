"""ROI bounce extraction v3 — NATIVE-RESOLUTION tiled inference (SAHI-style).

v1 and v2 both failed because TrackNet V2's fixed 640x360 input resolution
downsampled the source (1920x1080), compressing a 1-2 px serve-bounce ball
to sub-pixel. Agent research (2026-04-22) confirmed this is the specific
failure mode WASB-SBDT (HRNet backbone) and SAHI (tiled inference) target.

This v3 is the CHEAP VALIDATION: does TrackNet V2 see the serve-bounce
ball when we feed it a native-resolution 640x360 crop centered on the
expected bounce location, bypassing the downsample?

Strategy:
  1. For each SA GT serve, project SA's ball_bounce_x/y to pixel via our
     court calibration (commit 364d8dd — the fixed one).
  2. Crop a 640x360 tile centered on that expected bounce pixel,
     clamped to frame bounds.
  3. Run TrackNet V2 on the native-resolution crop (no resize — feed it
     a tensor at exactly the input size).
  4. Track over ±1.5s window, detect bounces, project bounce pixel back
     to court coords.
  5. Report: did we find a bounce in the expected service-box zone?

If v3 finds the ball when v1/v2 could not, approach A is rescued (just
needed native resolution). If v3 ALSO fails, motion blur is the root
cause and WASB-SBDT / BlurBall is required.

Usage:
    python -m ml_pipeline.diag.extract_roi_bounces_v3 \\
        --task d1fed568-b285-4117-bcef-c6039d52fc37 \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --sportai 1515aff7-1ec7-472d-8dba-8fff9f939ff1 \\
        --only-role FAR --max-serves 2
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import List, Optional, Tuple

import numpy as np
from sqlalchemy import create_engine, text as sql_text

logger = logging.getLogger("extract_roi_bounces_v3")

COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
FAR_SERVICE_LINE_M = HALF_Y - SERVICE_LINE_FROM_NET_M
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M

TILE_W = 640
TILE_H = 360

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


def _get_sa_serves_with_bounces(conn, sportai_tid: str) -> list:
    rows = conn.execute(sql_text("""
        SELECT ball_hit_s AS ts,
               CASE WHEN ball_hit_location_y > 22 THEN 'NEAR'
                    WHEN ball_hit_location_y < 2 THEN 'FAR'
                    ELSE '?' END AS role,
               court_x AS bounce_court_x,
               court_y AS bounce_court_y
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND serve_d = TRUE
          AND ball_hit_s IS NOT NULL
          AND court_x IS NOT NULL AND court_y IS NOT NULL
        ORDER BY ball_hit_s
    """), {"tid": sportai_tid}).mappings().all()
    return [dict(r) for r in rows]


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
    logger.info("court calibration: mode=%s",
                detector._calibration.mode if detector._calibration else "homography")
    return detector


def _project_court_to_pixel(mx, my, detector):
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


def _tile_around(center_x, center_y, frame_shape):
    """Native-resolution 640x360 tile centered on (cx, cy), clamped to frame.

    No downsampling: the crop dimensions match TrackNet V2's input.
    """
    h, w = frame_shape[:2]
    x0 = int(round(center_x - TILE_W / 2))
    y0 = int(round(center_y - TILE_H / 2))
    # Clamp to frame
    x0 = max(0, min(w - TILE_W, x0))
    y0 = max(0, min(h - TILE_H, y0))
    return (x0, y0, x0 + TILE_W, y0 + TILE_H)


def _run_tracker_native_crop(video_path, start_frame, end_frame, tile):
    """Run BallTracker feeding it NATIVE-RES 640x360 crops (no resize loss).

    The existing BallTracker.detect_frame calls cv2.resize anyway, so
    providing a 640x360 crop means scale_x = scale_y = 1.0 — ball retains
    native pixel size.
    """
    import cv2
    from ml_pipeline.ball_tracker import BallTracker

    x0, y0, x1, y1 = tile
    assert x1 - x0 == TILE_W and y1 - y0 == TILE_H, \
        f"tile must be exactly {TILE_W}x{TILE_H}, got {x1-x0}x{y1-y0}"

    tracker = BallTracker()
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    try:
        for idx in range(start_frame, end_frame):
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[y0:y1, x0:x1]
            if crop.shape[:2] != (TILE_H, TILE_W):
                continue
            tracker.detect_frame(crop, idx)
    finally:
        cap.release()
    tracker.interpolate_gaps()
    tracker.detect_bounces()
    return tracker.detections, dict(tracker._diag)


def _project_detections(dets, tile, detector):
    x0, y0, _x1, _y1 = tile
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
            "x": full_x, "y": full_y,
            "court_x": cx, "court_y": cy,
            "is_bounce": d.is_bounce,
        })
    return out


def _expected_bounce_zone(role: str):
    """For a given serving role, which service box should the bounce land in?"""
    if role == "NEAR":
        # Near player serves → bounces in FAR service box
        return FAR_SERVICE_LINE_M - 1.0, HALF_Y + 0.5
    if role == "FAR":
        # Far player serves → bounces in NEAR service box
        return HALF_Y - 0.5, NEAR_SERVICE_LINE_M + 1.0
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--sportai", default=DEFAULT_SPORTAI_REF)
    ap.add_argument("--window-s", type=float, default=1.5)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--only-role", choices=["NEAR", "FAR"], default=None)
    ap.add_argument("--max-serves", type=int, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not os.path.exists(args.video):
        raise RuntimeError(f"video not found: {args.video}")

    engine = _get_engine()
    with engine.connect() as conn:
        serves = _get_sa_serves_with_bounces(conn, args.sportai)
    if args.only_role:
        serves = [s for s in serves if s["role"] == args.only_role]
    if args.max_serves:
        serves = serves[:args.max_serves]
    if not serves:
        logger.error("no SA serves with bounce coords")
        return 1
    logger.info("processing %d serves (NEAR=%d FAR=%d)",
                len(serves),
                sum(1 for s in serves if s["role"] == "NEAR"),
                sum(1 for s in serves if s["role"] == "FAR"))

    detector = _calibrate_court(args.video)
    import cv2
    cap = cv2.VideoCapture(args.video)
    ok, first = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("cannot read first frame")

    window_frames = int(round(args.window_s * args.fps))
    hits_in_zone = 0
    bounces_in_zone = 0
    for i, s in enumerate(serves):
        ts = float(s["ts"])
        role = s["role"]
        mx = float(s["bounce_court_x"])
        my = float(s["bounce_court_y"])
        # Expected bounce pixel (from SA ground truth)
        px = _project_court_to_pixel(mx, my, detector)
        if px is None:
            logger.warning("[%d] ts=%.2f: projection failed", i+1, ts)
            continue
        ex_px, ex_py = px
        tile = _tile_around(ex_px, ex_py, first.shape)
        center_frame = int(round(ts * args.fps))
        start_f = max(0, center_frame - window_frames)
        end_f = center_frame + window_frames
        logger.info(
            "[%d/%d] ts=%.2f role=%s expected bounce court=(%.2f,%.2f) pixel=(%d,%d) "
            "-> tile (%d,%d)-(%d,%d) frames [%d,%d)",
            i+1, len(serves), ts, role, mx, my, int(ex_px), int(ex_py),
            *tile, start_f, end_f,
        )
        t0 = time.time()
        dets, diag = _run_tracker_native_crop(args.video, start_f, end_f, tile)
        projected = _project_detections(dets, tile, detector)
        z = _expected_bounce_zone(role)
        def _in_zone(r):
            if r["court_y"] is None: return False
            return z[0] <= r["court_y"] <= z[1] and -1.0 <= (r["court_x"] or -99) <= COURT_WIDTH_DOUBLES_M + 1.0
        in_zone = [r for r in projected if _in_zone(r)]
        bounce_in_zone = [r for r in in_zone if r["is_bounce"]]
        logger.info(
            "  [%d frames, %.1fs] tracker: frames_inferred=%d heatmap_empty=%d "
            "tier1=%d tier3=%d delta_hits=%d",
            end_f - start_f, time.time() - t0,
            diag.get("frames_inferred", 0),
            diag.get("heatmap_empty", 0),
            diag.get("tier1_hough", 0),
            diag.get("tier3_argmax", 0),
            diag.get("delta_fallback_hits", 0),
        )
        logger.info(
            "  results: total_dets=%d in_zone=%d  bounces_total=%d bounces_in_zone=%d",
            len(projected), len(in_zone),
            sum(1 for r in projected if r["is_bounce"]), len(bounce_in_zone),
        )
        hits_in_zone += len(in_zone)
        bounces_in_zone += len(bounce_in_zone)

    logger.info("")
    logger.info("=== SUMMARY (v3 native-resolution tiling) ===")
    logger.info("  total serves tested: %d", len(serves))
    logger.info("  total detections in target service-box zone: %d", hits_in_zone)
    logger.info("  total bounces in target service-box zone: %d", bounces_in_zone)
    if bounces_in_zone > 0:
        logger.info("  SIGNAL: native-resolution SAHI-style tiling FINDS bounces TrackNet V2 missed before.")
        logger.info("  Next: scale to all 25 serves, wire into serve_detector.")
    else:
        logger.info("  NO BOUNCES: native resolution didn't rescue TrackNet V2.")
        logger.info("  Implication: motion blur at bounce frame is the root cause.")
        logger.info("  Next: WASB-SBDT pretrained tennis weights OR BlurBall retrain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
