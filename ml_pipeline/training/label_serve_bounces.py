"""Export strong SA-GT serve-bounce labels for TrackNet fine-tuning.

For each SA-annotated serve in silver.point_detail, we know the BOUNCE
location (normalized court coords) and the HIT time. Convert bounce
position to pixels via court homography. Approximate bounce frame as
hit_frame + 12 (≈ 0.5 s flight at 25 fps) with a ±5 frame search range
to allow for slow/fast serves.

Output JSON schema:
{
  "task_id": "<t5 task>",
  "sportai_task_id": "<sa task>",
  "video_fps": 25.0,
  "frame_height": 1080, "frame_width": 1920,
  "labels": [
    {
      "serve_ts": 378.08,
      "role": "FAR",
      "hit_frame": 9452,
      "bounce_frame_est": 9464,         # approximate, used as label frame
      "bounce_frame_search": [9459, 9469],  # range where the true bounce sits
      "pixel_x": 1132.5, "pixel_y": 438.2,   # projected from SA normalized coords
      "court_x": 4.31, "court_y": 15.7,
      "source": "sportai_bounce_norm"
    },
    ...
  ]
}

Usage:
    python -m ml_pipeline.training.label_serve_bounces \\
        --task 8a5e0b5e-58a5-4236-a491-0fb7b3a25088 \\
        --sportai 2c1ad953-b65b-41b4-9999-975964ff92e1 \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --output ml_pipeline/training/labels/8a5e0b5e_serve_bounces.json

The (T5 task, SA task, video) triple is enough to produce labels
without requiring the T5 ingest to be fully complete — we only need
the video to run court calibration, and silver.point_detail from SA.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from sqlalchemy import create_engine, text as sql_text

logger = logging.getLogger("label_serve_bounces")


COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
SERVE_FLIGHT_FRAMES_EST = 12   # ≈ 0.5 s at 25 fps
SERVE_FLIGHT_SEARCH = 5         # ±5 frame range around the estimate


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


def _get_sa_serves(conn, sportai_tid: str) -> list:
    """Pull per-serve metadata including BOUNCE court position."""
    rows = conn.execute(sql_text("""
        SELECT
            ball_hit_s                          AS hit_s,
            CASE
                WHEN ball_hit_location_y > 22 THEN 'NEAR'
                WHEN ball_hit_location_y < 2  THEN 'FAR'
                ELSE '?'
            END                                 AS role,
            ball_bounce_x_norm                  AS bx_norm,
            ball_bounce_y_norm                  AS by_norm,
            court_x                             AS bounce_court_x,
            court_y                             AS bounce_court_y,
            serve_side_d                        AS side
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND serve_d = TRUE
          AND ball_hit_s IS NOT NULL
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
    return detector


def _project_court_to_pixel(mx, my, detector) -> Optional[tuple]:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True,
                    help="T5 task_id the labels belong to (for metadata)")
    ap.add_argument("--sportai", required=True,
                    help="SA task_id to pull ground-truth bounces from")
    ap.add_argument("--video", required=True,
                    help="Local video path for court calibration")
    ap.add_argument("--output", required=True,
                    help="Output JSON path")
    ap.add_argument("--fps", type=float, default=25.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not os.path.exists(args.video):
        raise RuntimeError(f"video not found: {args.video}")

    # --- Pull SA serves ---
    engine = _get_engine()
    with engine.connect() as conn:
        serves = _get_sa_serves(conn, args.sportai)
    if not serves:
        raise RuntimeError(f"no SA serves for task {args.sportai}")
    logger.info("pulled %d SA serves from %s", len(serves), args.sportai[:8])

    # --- Calibrate court ---
    detector = _calibrate_court(args.video)
    import cv2
    cap = cv2.VideoCapture(args.video)
    ok, first = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("cannot read first frame")
    H, W = first.shape[:2]
    logger.info("calibration locked; video=%dx%d", W, H)

    # --- Project per-serve bounce ---
    labels = []
    n_ok = 0
    n_missing_bounce = 0
    n_oob = 0
    for s in serves:
        hit_s = float(s["hit_s"])
        hit_frame = int(round(hit_s * args.fps))
        bounce_frame_est = hit_frame + SERVE_FLIGHT_FRAMES_EST

        # Prefer absolute court_x/y (metres) if populated; otherwise
        # derive from normalized coords. For SA, typically both are set.
        mx, my = s.get("bounce_court_x"), s.get("bounce_court_y")
        if mx is None or my is None:
            bx_norm = s.get("bx_norm")
            by_norm = s.get("by_norm")
            if bx_norm is None or by_norm is None:
                n_missing_bounce += 1
                continue
            mx = float(bx_norm) * COURT_WIDTH_DOUBLES_M
            my = float(by_norm) * COURT_LENGTH_M
        else:
            mx = float(mx)
            my = float(my)

        p = _project_court_to_pixel(mx, my, detector)
        if p is None:
            n_missing_bounce += 1
            continue
        px, py = p
        if not (0 <= px < W and 0 <= py < H):
            n_oob += 1
            continue

        labels.append({
            "serve_ts": hit_s,
            "role": s["role"],
            "side": s.get("side"),
            "hit_frame": hit_frame,
            "bounce_frame_est": bounce_frame_est,
            "bounce_frame_search": [bounce_frame_est - SERVE_FLIGHT_SEARCH,
                                    bounce_frame_est + SERVE_FLIGHT_SEARCH],
            "pixel_x": round(px, 2),
            "pixel_y": round(py, 2),
            "court_x": round(mx, 3),
            "court_y": round(my, 3),
            "source": "sportai_bounce_norm",
        })
        n_ok += 1

    out = {
        "task_id": args.task,
        "sportai_task_id": args.sportai,
        "video_fps": args.fps,
        "frame_height": H,
        "frame_width": W,
        "serve_flight_frames_est": SERVE_FLIGHT_FRAMES_EST,
        "serve_flight_search": SERVE_FLIGHT_SEARCH,
        "label_count": len(labels),
        "labels": labels,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))

    logger.info("kept %d labels, missing_bounce=%d out_of_bounds=%d",
                n_ok, n_missing_bounce, n_oob)
    logger.info("role breakdown: NEAR=%d FAR=%d other=%d",
                sum(1 for l in labels if l["role"] == "NEAR"),
                sum(1 for l in labels if l["role"] == "FAR"),
                sum(1 for l in labels if l["role"] not in ("NEAR", "FAR")))
    logger.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
