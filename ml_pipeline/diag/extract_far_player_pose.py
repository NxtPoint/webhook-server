"""Native-resolution YOLOv8x-pose on far-baseline crop — far player pose.

The architectural insight from the WASB ball breakthrough applies here:

  - TrackNet V2 at 640×360 full-frame was missing the 1-2 px serve-bounce
    ball. Native-resolution crop + WASB found it at expected pixel.
  - YOLOv8x-pose at 1280 full-frame misses far-player keypoints because
    the body is 30-40 px → pose-keypoint NMS drops it. A native-resolution
    crop around the far-baseline region keeps the player's pixel size
    while effectively zooming the model's attention.

With reliable far-player pose keypoints, the EXISTING pose-first serve
detector (the one that gets near-player 13/14) can run for pid=1 too —
no need for the bounce-first workaround. Same logic also unblocks
stroke classification (forehand / backhand / volley) on the far side.

This extractor:
  1. Calibrates the court on the video.
  2. For each frame in the target window, crops the far-baseline region
     (court_y in [-3, 5] m, full court width + margin, projected to
     pixels via the calibration).
  3. Runs YOLOv8x-pose on the crop (keypoints + bbox).
  4. Picks the biggest person bbox in the crop as the far player.
  5. Shifts coords back to full-frame and writes to
     ml_analysis.player_detections_roi (NEW table).

serve_detector._load_pose_rows will be modified (follow-up commit) to
merge rows from this table for pid=1, so the existing pose-first gate
picks them up automatically.

Usage:
    python -m ml_pipeline.diag.extract_far_player_pose \\
        --task d1fed568-b285-4117-bcef-c6039d52fc37 \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --sportai 1515aff7-1ec7-472d-8dba-8fff9f939ff1 \\
        --only-role FAR --max-serves 2
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import List, Optional, Tuple

import cv2
import numpy as np
from sqlalchemy import create_engine, text as sql_text

logger = logging.getLogger("extract_far_player_pose")

COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
DEFAULT_SPORTAI_REF = "1515aff7-1ec7-472d-8dba-8fff9f939ff1"

# Far-baseline ROI in court metric space (behind + in front of far baseline).
# Cover a wide band so serves with motion outside the strict baseline are
# still captured. court_y=0 is far baseline; negative is behind it.
FAR_ROI_Y_LO = -3.0
FAR_ROI_Y_HI = 5.0
FAR_ROI_X_PAD = 1.5   # metres outside singles lines

# YOLO imgsz. Default 1280 on YOLOv8x-pose. We'll keep that; the win comes
# from the crop (reduces scene complexity + effectively zooms the player).
YOLO_IMGSZ = 1280


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


def _init_roi_pose_schema(conn):
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS ml_analysis.player_detections_roi (
            id          BIGSERIAL PRIMARY KEY,
            job_id      TEXT NOT NULL,
            frame_idx   INTEGER NOT NULL,
            player_id   INTEGER NOT NULL,
            bbox_x1     DOUBLE PRECISION NOT NULL,
            bbox_y1     DOUBLE PRECISION NOT NULL,
            bbox_x2     DOUBLE PRECISION NOT NULL,
            bbox_y2     DOUBLE PRECISION NOT NULL,
            center_x    DOUBLE PRECISION NOT NULL,
            center_y    DOUBLE PRECISION NOT NULL,
            court_x     DOUBLE PRECISION,
            court_y     DOUBLE PRECISION,
            keypoints   JSONB,
            source      TEXT NOT NULL DEFAULT 'far_roi_pose',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS idx_player_detections_roi_job_player
            ON ml_analysis.player_detections_roi (job_id, player_id);
    """))


def _get_sa_serves(conn, sportai_tid):
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


def _project(mx, my, det):
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


def _compute_far_roi_pixel(detector, frame_shape, mode="wide",
                           tight_w=256, tight_h=192) -> Tuple[int, int, int, int]:
    """Compute pixel ROI for far-baseline region.

    mode='wide':  full court-width band covering court_y in [-3, 5] m.
                  Good for tracking player across lateral moves but produces
                  a flat aspect ratio (e.g. 704x103) that YOLO only zooms
                  ~1.8x → pose keypoints (wrist/shoulder) unresolved on
                  far player.

    mode='tight': square-ish crop centered on far-baseline center point
                  (court_x=COURT_WIDTH/2, court_y=0). Default tight_w x
                  tight_h is 256x192. YOLO scales this to 1280x1280 at
                  imgsz=1280 → ~5x effective zoom of the player. Far
                  player at ~40x25 source px → ~200x125 in YOLO input.
                  Pose keypoints resolve at that size.
                  Assumes the server is near the baseline center during
                  the serve — valid for the ±1.5s window around contact.
    """
    h, w = frame_shape[:2]

    if mode == "tight":
        p = _project(COURT_WIDTH_DOUBLES_M / 2, 0.0, detector)
        if p is None:
            raise RuntimeError("cannot project far-baseline center")
        cx, cy = p
        x0 = max(0, min(w - tight_w, int(round(cx - tight_w / 2))))
        y0 = max(0, min(h - tight_h, int(round(cy - tight_h / 2))))
        return (x0, y0, x0 + tight_w, y0 + tight_h)

    # Default: wide band
    corners_m = [
        (-FAR_ROI_X_PAD, FAR_ROI_Y_LO),
        (COURT_WIDTH_DOUBLES_M + FAR_ROI_X_PAD, FAR_ROI_Y_LO),
        (COURT_WIDTH_DOUBLES_M + FAR_ROI_X_PAD, FAR_ROI_Y_HI),
        (-FAR_ROI_X_PAD, FAR_ROI_Y_HI),
    ]
    pxs = []
    for mx, my in corners_m:
        p = _project(mx, my, detector)
        if p is None:
            raise RuntimeError(f"cannot project far-ROI corner ({mx},{my})")
        pxs.append(p)
    xs = [p[0] for p in pxs]; ys = [p[1] for p in pxs]
    x0 = max(0, int(min(xs) - 20))
    y0 = max(0, int(min(ys) - 20))
    x1 = min(w, int(max(xs) + 20))
    y1 = min(h, int(max(ys) + 20))
    return (x0, y0, x1, y1)


def _run_yolo_pose_on_crop(model, crop, imgsz=YOLO_IMGSZ, conf=0.25):
    """Run YOLOv8x-pose on the crop, return list of (bbox, keypoints) tuples.

    bbox: (x1, y1, x2, y2) in crop-pixel coords
    keypoints: (17, 3) ndarray — (x, y, conf) per COCO keypoint, in crop coords
    """
    # Ultralytics predict returns a list of Results
    results = model.predict(crop, conf=conf, imgsz=imgsz,
                            classes=[0],  # person
                            verbose=False)
    out = []
    if not results:
        return out
    r = results[0]
    if r.boxes is None or r.keypoints is None:
        return out
    boxes = r.boxes.xyxy.cpu().numpy()  # (N, 4)
    # keypoints.data: (N, 17, 3)  — x, y, conf
    kps = r.keypoints.data.cpu().numpy() if r.keypoints is not None else None
    for i in range(len(boxes)):
        bx1, by1, bx2, by2 = [float(v) for v in boxes[i]]
        kp = kps[i] if kps is not None and i < len(kps) else None
        out.append((bx1, by1, bx2, by2, kp))
    return out


def _pick_biggest_person(detections):
    """From a list of YOLO pose results, pick the biggest by bbox area —
    this is the far player (other people like crowd/umpire would be smaller
    inside our crop, since the crop is tight to the far-baseline zone)."""
    if not detections:
        return None
    return max(detections, key=lambda d: (d[2] - d[0]) * (d[3] - d[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--sportai", default=DEFAULT_SPORTAI_REF)
    ap.add_argument("--window-s", type=float, default=1.5,
                    help="half-window around each SA serve ts to process")
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--only-role", choices=["NEAR", "FAR"], default="FAR",
                    help="which SA role to build ROI pose data around "
                         "(default FAR; we only need ROI pose for pid=1)")
    ap.add_argument("--max-serves", type=int, default=None)
    ap.add_argument("--conf", type=float, default=0.25,
                    help="YOLO detection confidence threshold")
    ap.add_argument("--kp-conf-threshold", type=float, default=0.3,
                    help="min confidence on dominant wrist AND shoulder to count as 'usable pose'")
    ap.add_argument("--verbose-kp", action="store_true",
                    help="log keypoint confidences per detection")
    ap.add_argument("--source-tag", default="far_roi_pose")
    ap.add_argument("--dry-run", action="store_true")
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
    logger.info("Processing %d %s serves", len(serves), args.only_role or "all")

    detector = _calibrate_court(args.video)
    cap = cv2.VideoCapture(args.video)
    ok, first = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("cannot read first frame")

    roi = _compute_far_roi_pixel(detector, first.shape)
    x0, y0, x1, y1 = roi
    logger.info("Far ROI pixel: (%d,%d)-(%d,%d) size=%dx%d",
                x0, y0, x1, y1, x1 - x0, y1 - y0)

    # Load YOLOv8x-pose
    from ultralytics import YOLO
    from ml_pipeline.config import YOLO_POSE_WEIGHTS, YOLO_POSE_WEIGHTS_FALLBACK
    weights = YOLO_POSE_WEIGHTS if os.path.exists(YOLO_POSE_WEIGHTS) else YOLO_POSE_WEIGHTS_FALLBACK
    logger.info("Loading YOLO pose model: %s", weights)
    model = YOLO(weights)

    window_frames = int(round(args.window_s * args.fps))
    rows_to_write = []
    per_serve_summary = []

    for i, s in enumerate(serves):
        ts = float(s["ts"])
        role = s["role"]
        center_frame = int(round(ts * args.fps))
        start_f = max(0, center_frame - window_frames)
        end_f = center_frame + window_frames
        logger.info("[%d/%d] ts=%.2f role=%s frames [%d,%d)",
                    i + 1, len(serves), ts, role, start_f, end_f)

        cap = cv2.VideoCapture(args.video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
        n_frames = 0
        n_dets = 0
        n_pose = 0
        n_in_far_baseline = 0
        t0 = time.time()
        try:
            for idx in range(start_f, end_f):
                ok, frame = cap.read()
                if not ok: break
                n_frames += 1
                crop = frame[y0:y1, x0:x1]
                if crop.size == 0:
                    continue
                detections = _run_yolo_pose_on_crop(model, crop, conf=args.conf)
                if not detections:
                    continue
                best = _pick_biggest_person(detections)
                if best is None:
                    continue
                n_dets += 1
                bx1, by1, bx2, by2, kp = best
                # Shift to full-frame coords
                fbx1 = bx1 + x0; fby1 = by1 + y0
                fbx2 = bx2 + x0; fby2 = by2 + y0
                feet_x = (fbx1 + fbx2) / 2
                feet_y = fby2
                court = detector.to_court_coords(feet_x, feet_y, strict=False)
                cx = court[0] if court else None
                cy = court[1] if court else None

                # Only count "usable pose" if keypoints are present AND confident
                has_pose = False
                kp_json = None
                kp_max_conf = 0.0
                wrist_conf = 0.0
                shoulder_conf = 0.0
                if kp is not None:
                    # Shift keypoints to full-frame
                    kp_full = kp.copy()
                    kp_full[:, 0] += x0
                    kp_full[:, 1] += y0
                    kp_json = [
                        [float(kp_full[j, 0]), float(kp_full[j, 1]), float(kp_full[j, 2])]
                        for j in range(kp_full.shape[0])
                    ]
                    kp_max_conf = float(kp_full[:, 2].max()) if kp_full.size else 0.0
                    wrist_conf = max(float(kp_full[9, 2]), float(kp_full[10, 2]))
                    shoulder_conf = max(float(kp_full[5, 2]), float(kp_full[6, 2]))
                    if wrist_conf > args.kp_conf_threshold and shoulder_conf > args.kp_conf_threshold:
                        has_pose = True
                        n_pose += 1
                # Per-det diagnostic line — shows whether keypoints are close
                # to the threshold or genuinely noisy
                if args.verbose_kp:
                    logger.info("  frame %d: bbox %.0fx%.0f  kp_max=%.2f  "
                                "wrist=%.2f shoulder=%.2f  usable=%s",
                                idx, fbx2 - fbx1, fby2 - fby1,
                                kp_max_conf, wrist_conf, shoulder_conf, has_pose)

                if cy is not None and -3.5 <= cy <= 4.5:
                    n_in_far_baseline += 1

                rows_to_write.append({
                    "job_id": args.task,
                    "frame_idx": idx,
                    "player_id": 1,  # far player
                    "bbox_x1": fbx1, "bbox_y1": fby1,
                    "bbox_x2": fbx2, "bbox_y2": fby2,
                    "center_x": feet_x,
                    "center_y": feet_y,
                    "court_x": cx,
                    "court_y": cy,
                    "keypoints": json.dumps(kp_json) if kp_json else None,
                    "source": args.source_tag,
                })
        finally:
            cap.release()

        dt = time.time() - t0
        logger.info("  [%d frames, %.1fs] detections=%d usable_pose=%d in_baseline_zone=%d",
                    n_frames, dt, n_dets, n_pose, n_in_far_baseline)
        per_serve_summary.append((ts, role, n_frames, n_dets, n_pose, n_in_far_baseline))

    logger.info("")
    logger.info("=== SUMMARY ===")
    logger.info("  %-8s %-5s %-7s %-6s %-9s %-15s", "ts", "role", "frames", "dets", "pose_ok", "in_baseline")
    total_frames = 0; total_dets = 0; total_pose = 0; total_baseline = 0
    for ts, role, nf, nd, np_, nb in per_serve_summary:
        logger.info("  %-8.2f %-5s %-7d %-6d %-9d %-15d", ts, role, nf, nd, np_, nb)
        total_frames += nf; total_dets += nd; total_pose += np_; total_baseline += nb
    logger.info("  TOTAL %-6s frames=%d det=%d pose=%d baseline=%d",
                "", total_frames, total_dets, total_pose, total_baseline)

    if args.dry_run:
        logger.info("dry-run: not writing to DB")
        return 0

    if not rows_to_write:
        logger.info("nothing to write")
        return 0

    with engine.begin() as conn:
        _init_roi_pose_schema(conn)
        n_del = conn.execute(sql_text("""
            DELETE FROM ml_analysis.player_detections_roi
            WHERE job_id = :tid AND source = :src
        """), {"tid": args.task, "src": args.source_tag}).rowcount
        if n_del:
            logger.info("deleted %d prior rows (source=%s)", n_del, args.source_tag)
        conn.execute(sql_text("""
            INSERT INTO ml_analysis.player_detections_roi
              (job_id, frame_idx, player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
               center_x, center_y, court_x, court_y, keypoints, source)
            VALUES
              (:job_id, :frame_idx, :player_id, :bbox_x1, :bbox_y1, :bbox_x2, :bbox_y2,
               :center_x, :center_y, :court_x, :court_y, CAST(:keypoints AS JSONB), :source)
        """), rows_to_write)
    logger.info("wrote %d rows to ml_analysis.player_detections_roi (source=%s)",
                len(rows_to_write), args.source_tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
