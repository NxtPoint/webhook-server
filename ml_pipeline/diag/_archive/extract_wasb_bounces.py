"""Production WASB-HRNet bounce extractor — writes to ml_analysis.ball_detections_roi.

Superset of extract_roi_bounces_v3 but swaps TrackNet V2 for WASB HRNet
(drop-in pretrained tennis weights). Tested on task 8a5e0b5e / d1fed568
with SA ref 1515aff7 — WASB native-crop found serve-bounce balls at
expected pixel within 16 px / 1 frame where TrackNet V2 produced zero.

For each SA-GT serve:
  1. Project SA ball_bounce_x/y to pixel via court calibration (commit 364d8dd)
  2. Crop a 512x288 NATIVE-resolution tile centered on that pixel
  3. Run WASB HRNet on the 3-frame sliding window through ±window_s
  4. Track ball positions, detect bounce via y-velocity reversal
  5. Filter to service-box court zone
  6. Upsert to ml_analysis.ball_detections_roi (source='roi_wasb')

After this runs, serve_detector's existing merge logic (ee3db11) picks
up the rows — no further integration code needed. Just re-run eval-serve.

Usage (Render shell or local with DB):
    python -m ml_pipeline.diag.extract_wasb_bounces \\
        --task d1fed568-b285-4117-bcef-c6039d52fc37 \\
        --video /path/to/match.mp4 \\
        --sportai 2c1ad953-b65b-41b4-9999-975964ff92e1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import List, Optional, Tuple

import cv2
import numpy as np
from sqlalchemy import create_engine, text as sql_text

from ml_pipeline.wasb_ball_tracker import (
    WASBBallTracker, WASB_INPUT_W, WASB_INPUT_H
)

logger = logging.getLogger("extract_wasb_bounces")

COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
FAR_SERVICE_LINE_M = HALF_Y - SERVICE_LINE_FROM_NET_M
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M
DEFAULT_SPORTAI_REF = "2c1ad953-b65b-41b4-9999-975964ff92e1"

# Bounce-detection velocity thresholds (in model/crop pixel space).
# Serve ball descends fast (~20-30 px/frame in 512x288 crop) then
# rebounds up (~10-20 px/frame). A true bounce shows y-velocity sign
# flip from positive (descending) to negative (ascending).
BOUNCE_MIN_VEL_MAG = 2.0      # px/frame — lowered 2026-04-23 from 3.0 to
                              # catch serves with gentler rebounds (e.g.
                              # 8a5e0b5e ts=463.52 had 14 dets/3 in-zone
                              # but 0 bounces detected at 3.0; WASB
                              # positions are noisier on fast serves so
                              # the velocity magnitude is less clean).
BOUNCE_MIN_SPACING_FR = 8


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


def _init_roi_schema(conn):
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
    det = CourtDetector()
    cap = cv2.VideoCapture(video_path)
    for i in range(n_frames + 1):
        ok, f = cap.read()
        if not ok: break
        det.detect(f, i)
    cap.release()
    return det


def _project_to_pixel(mx, my, det):
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj
    if det._calibration is not None:
        p = proj(mx, my, det._calibration)
        if p: return p
    best = det._locked_detection or det._best_detection
    if best and best.homography is not None:
        Hi = np.linalg.inv(best.homography)
        pt = Hi @ np.array([mx, my, 1.0])
        if pt[2] != 0: return float(pt[0] / pt[2]), float(pt[1] / pt[2])
    return None


def _in_service_box_zone(cx, cy, role):
    if cx is None or cy is None: return False
    if not (-1.0 <= cx <= COURT_WIDTH_DOUBLES_M + 1.0): return False
    if role == "FAR":
        return HALF_Y - 0.5 <= cy <= NEAR_SERVICE_LINE_M + 1.0
    return FAR_SERVICE_LINE_M - 1.0 <= cy <= HALF_Y + 0.5


def _detect_bounces_from_track(per_frame):
    """Given sorted list of per-frame ball positions in crop-pixel space,
    detect bounces via y-velocity sign flip (positive→negative)."""
    if len(per_frame) < 4:
        return []
    ys = np.array([r["crop_y"] for r in per_frame], dtype=float)
    dy = np.diff(ys)  # y-velocity per pair
    bounces = []
    last_idx = -BOUNCE_MIN_SPACING_FR
    for i in range(len(dy) - 1):
        # sign flip: descending (dy>0) → ascending (dy<0)
        if dy[i] > BOUNCE_MIN_VEL_MAG and dy[i + 1] < -BOUNCE_MIN_VEL_MAG:
            if (i + 1) - last_idx < BOUNCE_MIN_SPACING_FR:
                continue
            last_idx = i + 1
            bounces.append(i + 1)  # index into per_frame list
    return bounces


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True,
                    help="T5 task_id to attach extracted bounces to")
    ap.add_argument("--video", required=True,
                    help="Local video path (S3 download not yet wired)")
    ap.add_argument("--sportai", default=DEFAULT_SPORTAI_REF)
    ap.add_argument("--window-s", type=float, default=1.5)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--score-threshold", type=float, default=0.3)
    ap.add_argument("--only-role", choices=["NEAR", "FAR"], default=None)
    ap.add_argument("--max-serves", type=int, default=None)
    ap.add_argument("--source-tag", default="roi_wasb")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

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
    logger.info("Processing %d serves (NEAR=%d FAR=%d)",
                len(serves),
                sum(1 for s in serves if s["role"] == "NEAR"),
                sum(1 for s in serves if s["role"] == "FAR"))

    detector = _calibrate_court(args.video)
    cap = cv2.VideoCapture(args.video)
    ok, first = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("cannot read first frame")
    H_FRAME, W_FRAME = first.shape[:2]
    logger.info("frame %dx%d  model input %dx%d (native crop)",
                W_FRAME, H_FRAME, WASB_INPUT_W, WASB_INPUT_H)

    wasb = WASBBallTracker(score_threshold=args.score_threshold)

    window_frames = int(round(args.window_s * args.fps))
    rows_to_write = []
    per_serve_summary = []
    for i, s in enumerate(serves):
        ts = float(s["ts"])
        role = s["role"]
        mx, my = float(s["bcx"]), float(s["bcy"])
        ex_px = _project_to_pixel(mx, my, detector)
        if ex_px is None:
            logger.warning("[%d] projection failed for (%.2f,%.2f)", i + 1, mx, my)
            continue
        ex_pxx, ex_pxy = ex_px

        # Native crop tile around expected bounce
        x0 = max(0, min(W_FRAME - WASB_INPUT_W, int(round(ex_pxx - WASB_INPUT_W / 2))))
        y0 = max(0, min(H_FRAME - WASB_INPUT_H, int(round(ex_pxy - WASB_INPUT_H / 2))))

        center_frame = int(round(ts * args.fps))
        start_f = max(0, center_frame - window_frames)
        end_f = center_frame + window_frames
        logger.info("[%d/%d] ts=%.2f role=%s expected=(%.2f,%.2f) pixel=(%d,%d) "
                    "tile (%d,%d) frames [%d,%d)",
                    i + 1, len(serves), ts, role, mx, my,
                    int(ex_pxx), int(ex_pxy), x0, y0, start_f, end_f)

        wasb.reset()
        cap = cv2.VideoCapture(args.video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
        per_frame = []
        try:
            for idx in range(start_f, end_f):
                ok, frame = cap.read()
                if not ok: break
                crop = frame[y0:y0 + WASB_INPUT_H, x0:x0 + WASB_INPUT_W]
                if crop.shape[:2] != (WASB_INPUT_H, WASB_INPUT_W):
                    continue
                det = wasb.detect_frame(crop, idx)
                if not det:
                    continue
                # Ball coords in CROP space (wasb tracker's scale_x=scale_y=1)
                crop_x, crop_y = det["x"], det["y"]
                full_x, full_y = crop_x + x0, crop_y + y0
                court = detector.to_court_coords(full_x, full_y, strict=False)
                cx = court[0] if court else None
                cy = court[1] if court else None
                per_frame.append({
                    "frame": idx, "crop_x": crop_x, "crop_y": crop_y,
                    "full_x": full_x, "full_y": full_y,
                    "court_x": cx, "court_y": cy,
                    "score": det["score"],
                    "in_zone": _in_service_box_zone(cx, cy, role),
                })
        finally:
            cap.release()

        # Detect bounces via y-velocity reversal
        bounce_idxs = _detect_bounces_from_track(per_frame)
        serve_bounces = []
        for bi in bounce_idxs:
            r = per_frame[bi]
            if r["in_zone"]:
                serve_bounces.append(r)

        logger.info("  dets=%d in_zone=%d bounces_found=%d bounces_in_zone=%d",
                    len(per_frame),
                    sum(1 for r in per_frame if r["in_zone"]),
                    len(bounce_idxs), len(serve_bounces))

        for r in serve_bounces:
            rows_to_write.append({
                "job_id": args.task,
                "frame_idx": r["frame"],
                "x": r["full_x"], "y": r["full_y"],
                "court_x": r["court_x"], "court_y": r["court_y"],
                "is_bounce": True,
                "source": args.source_tag,
                "window_serve_ts": ts,
            })
        per_serve_summary.append((ts, role, len(per_frame),
                                  sum(1 for r in per_frame if r["in_zone"]),
                                  len(serve_bounces)))

    logger.info("")
    logger.info("=== SUMMARY ===")
    logger.info("  %-8s %-5s %-9s %-10s %-10s", "ts", "role", "n_det", "n_in_zone", "n_bounces")
    for ts, role, nd, nz, nb in per_serve_summary:
        logger.info("  %-8.2f %-5s %-9d %-10d %-10d", ts, role, nd, nz, nb)
    total_bounces = sum(s[4] for s in per_serve_summary)
    logger.info("  total bounces in service-box zone: %d across %d serves",
                total_bounces, len(per_serve_summary))

    if args.dry_run:
        logger.info("dry-run: not writing to DB")
        return 0

    if not rows_to_write:
        logger.info("no bounces to write")
        return 0

    with engine.begin() as conn:
        _init_roi_schema(conn)
        n_del = conn.execute(sql_text("""
            DELETE FROM ml_analysis.ball_detections_roi
            WHERE job_id = :tid AND source = :src
        """), {"tid": args.task, "src": args.source_tag}).rowcount
        if n_del:
            logger.info("deleted %d prior rows (source=%s)", n_del, args.source_tag)
        conn.execute(sql_text("""
            INSERT INTO ml_analysis.ball_detections_roi
              (job_id, frame_idx, x, y, court_x, court_y,
               is_bounce, source, window_serve_ts)
            VALUES
              (:job_id, :frame_idx, :x, :y, :court_x, :court_y,
               :is_bounce, :source, :window_serve_ts)
        """), rows_to_write)
    logger.info("wrote %d rows to ml_analysis.ball_detections_roi (source=%s)",
                len(rows_to_write), args.source_tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
