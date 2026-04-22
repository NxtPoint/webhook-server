"""WASB smoke test: does WASB HRNet find serve bounces TrackNet V2 misses?

Runs WASB HRNet (pretrained tennis weights) over 3-frame windows around
each SA-GT FAR serve on the local test video, reporting whether the
detector fires at the expected bounce frame and pixel.

v1/v2/v3 tests with TrackNet V2 all produced heatmap_empty on the
serve-bounce frame because motion blur at impact defeats the downsample
pipeline. Research agent (2026-04-22) recommended WASB as the drop-in
fix — HRNet backbone, full-resolution feature maps, published
state-of-art on tennis broadcast footage.

Usage:
    python -m ml_pipeline.diag.wasb_serve_probe \\
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
from typing import Optional

import cv2
import numpy as np
from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.wasb_ball_tracker import WASBBallTracker

logger = logging.getLogger("wasb_serve_probe")

COURT_LENGTH_M = 23.77
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
FAR_SERVICE_LINE_M = HALF_Y - SERVICE_LINE_FROM_NET_M
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M
COURT_WIDTH_DOUBLES_M = 10.97


def _normalize_db_url(url):
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql+psycopg2://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _get_engine():
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL"))
    if not url:
        raise RuntimeError("DATABASE_URL required")
    return create_engine(_normalize_db_url(url))


def _get_sa_serves(conn, sportai_tid):
    rows = conn.execute(sql_text("""
        SELECT ball_hit_s AS ts,
               CASE WHEN ball_hit_location_y > 22 THEN 'NEAR'
                    WHEN ball_hit_location_y < 2 THEN 'FAR'
                    ELSE '?' END AS role,
               court_x AS bcx, court_y AS bcy
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND serve_d = TRUE
          AND ball_hit_s IS NOT NULL
          AND court_x IS NOT NULL AND court_y IS NOT NULL
        ORDER BY ball_hit_s
    """), {"tid": sportai_tid}).mappings().all()
    return [dict(r) for r in rows]


def _calibrate_court(video_path, n_frames=300):
    from ml_pipeline.court_detector import CourtDetector
    d = CourtDetector()
    cap = cv2.VideoCapture(video_path)
    for i in range(n_frames + 1):
        ok, f = cap.read()
        if not ok: break
        d.detect(f, i)
    cap.release()
    return d


def _project_to_pixel(mx, my, det):
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj
    if det._calibration is not None:
        p = proj(mx, my, det._calibration)
        if p: return p
    best = det._locked_detection or det._best_detection
    if best and best.homography is not None:
        Hi = np.linalg.inv(best.homography)
        pt = Hi @ np.array([mx, my, 1.0])
        if pt[2] != 0:
            return float(pt[0] / pt[2]), float(pt[1] / pt[2])
    return None


def _in_zone(cx, cy, role):
    if cx is None or cy is None: return False
    if not (-1.0 <= cx <= COURT_WIDTH_DOUBLES_M + 1.0): return False
    if role == "FAR":
        return HALF_Y - 0.5 <= cy <= NEAR_SERVICE_LINE_M + 1.0
    return FAR_SERVICE_LINE_M - 1.0 <= cy <= HALF_Y + 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--sportai", default="1515aff7-1ec7-472d-8dba-8fff9f939ff1")
    ap.add_argument("--window-s", type=float, default=1.5)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--only-role", choices=["NEAR", "FAR"], default=None)
    ap.add_argument("--max-serves", type=int, default=None)
    ap.add_argument("--score-threshold", type=float, default=0.5)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not os.path.exists(args.video):
        raise RuntimeError(f"video not found: {args.video}")

    engine = _get_engine()
    with engine.connect() as conn:
        serves = _get_sa_serves(conn, args.sportai)
    if args.only_role:
        serves = [s for s in serves if s["role"] == args.only_role]
    if args.max_serves:
        serves = serves[:args.max_serves]
    if not serves:
        logger.error("no serves")
        return 1
    logger.info("Testing WASB on %d serves (NEAR=%d FAR=%d)",
                len(serves),
                sum(1 for s in serves if s["role"] == "NEAR"),
                sum(1 for s in serves if s["role"] == "FAR"))

    detector = _calibrate_court(args.video)

    # Init WASB tracker
    wasb = WASBBallTracker(score_threshold=args.score_threshold)

    window_frames = int(round(args.window_s * args.fps))
    total_dets_in_zone = 0
    serves_with_any_det = 0
    for i, s in enumerate(serves):
        ts = float(s["ts"])
        role = s["role"]
        mx, my = float(s["bcx"]), float(s["bcy"])
        ex_px = _project_to_pixel(mx, my, detector)
        if ex_px is None:
            logger.warning("[%d] projection failed for (%.2f,%.2f)", i+1, mx, my)
            continue
        ex_pxx, ex_pxy = ex_px

        center_frame = int(round(ts * args.fps))
        start_f = max(0, center_frame - window_frames)
        end_f = center_frame + window_frames
        logger.info("[%d/%d] ts=%.2f role=%s expected bounce court=(%.2f,%.2f) pixel=(%d,%d) frames [%d,%d)",
                    i+1, len(serves), ts, role, mx, my, int(ex_pxx), int(ex_pxy), start_f, end_f)

        wasb.reset()
        cap = cv2.VideoCapture(args.video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
        t0 = time.time()
        per_frame = []
        try:
            for idx in range(start_f, end_f):
                ok, frame = cap.read()
                if not ok: break
                det = wasb.detect_frame(frame, idx)
                if det:
                    # Project pixel → court
                    court = detector.to_court_coords(det["x"], det["y"], strict=False)
                    cx = court[0] if court else None
                    cy = court[1] if court else None
                    in_z = _in_zone(cx, cy, role)
                    per_frame.append({
                        "frame": idx, "x": det["x"], "y": det["y"],
                        "score": det["score"],
                        "court_x": cx, "court_y": cy,
                        "in_zone": in_z,
                    })
        finally:
            cap.release()
        dt = time.time() - t0

        n_det = len(per_frame)
        n_in_zone = sum(1 for d in per_frame if d["in_zone"])
        nearest = None
        if per_frame:
            # find detection closest to expected bounce frame & pixel
            expected_f = center_frame + 12  # ~0.5s flight
            for d in per_frame:
                dt_f = abs(d["frame"] - expected_f)
                dt_px = np.hypot(d["x"] - ex_pxx, d["y"] - ex_pxy)
                if nearest is None or dt_f < nearest[0]:
                    nearest = (dt_f, dt_px, d)
        logger.info("  [%d frames, %.1fs] WASB dets=%d in_zone=%d",
                    end_f - start_f, dt, n_det, n_in_zone)
        if nearest:
            dt_f, dt_px, d = nearest
            logger.info("  nearest-to-expected:  frame_delta=%d  pixel_dist=%.1f  score=%.2f  "
                        "court=(%s,%s)  in_zone=%s",
                        dt_f, dt_px, d["score"],
                        f"{d['court_x']:.2f}" if d['court_x'] else "None",
                        f"{d['court_y']:.2f}" if d['court_y'] else "None",
                        d["in_zone"])
        total_dets_in_zone += n_in_zone
        if n_det > 0:
            serves_with_any_det += 1

    logger.info("")
    logger.info("=== WASB SUMMARY ===")
    logger.info("  serves tested: %d", len(serves))
    logger.info("  serves with any WASB detection in window: %d", serves_with_any_det)
    logger.info("  total detections in target service-box zone: %d", total_dets_in_zone)
    if total_dets_in_zone > 0:
        logger.info("  SIGNAL: WASB finds balls in the service-box zone where TrackNet V2 missed.")
        logger.info("  Next: measure bounce frame accuracy, wire into serve_detector.")
    else:
        logger.info("  NO SIGNAL: WASB also misses. Motion blur is too severe; BlurBall retrain needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
