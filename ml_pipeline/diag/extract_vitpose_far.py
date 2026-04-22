"""Far-player pose via YOLOv8m-det → ViTPose++ cascade (Pose2Trajectory 2024 recipe).

Research survey (agent, 2026-04-22): for small-person pose in tennis
broadcast video, the state-of-art is a 2-stage cascade:

  Stage 1 — detector:  YOLOv8m-det (or any clean person detector)
    Works on small bodies (~40 px). We already have this; the existing
    player_tracker's _run_yolo_far_baseline path fires reliably.

  Stage 2 — top-down pose: ViTPose++ (usyd-community/vitpose-plus-small)
    Takes a TIGHT BBOX CROP resized to 256x192. The vision-transformer
    backbone handles blur/small-limb conditions better than YOLO's
    pose head, which struggles when the trained-scale assumption breaks.
    Pose2Trajectory 2024 (arXiv:2411.04501) validated this exact recipe
    on broadcast tennis including the far/blurry player.

Why YOLOv8x-pose at native resolution FAILED (prior attempt):
  - YOLO's pose head operates on the same feature map as detection
  - When the input body is 10-14 px tall (our ROI mis-shape), pose
    keypoints can only localise face (nose/eyes) — wrist and shoulder
    have zero confidence because they're below-bbox or sub-pixel.

This cascade instead:
  1. Detects bbox (YOLOv8m-det).
  2. Expands bbox 1.25x, extracts tight crop, resizes to 256x192.
  3. Runs ViTPose. The pose model sees a normalised, human-proportioned
     input at its trained scale.
  4. Un-warps keypoints back to full-frame coords.

Writes to ml_analysis.player_detections_roi (player_id=1, source=
'far_vitpose'). serve_detector._load_pose_rows merges this with bronze
bronze, so the existing pose-first far-player gate picks it up.

Usage:
    python -m ml_pipeline.diag.extract_vitpose_far \\
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

logger = logging.getLogger("extract_vitpose_far")

COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
DEFAULT_SPORTAI_REF = "1515aff7-1ec7-472d-8dba-8fff9f939ff1"

# Far-baseline ROI in court metric space (where to look for the far player)
# NOTE: during trophy pose, the player's raised arm extends well above
# their head. With FAR_ROI_Y_LO=-3 (original), the ROI pixel top sat at
# ~y=215 and YOLO systematically LOST the player in frames 9447-9487
# (the serve-motion window on task d1fed568 ts=378.08). Widening to
# -8 gives ~60 extra pixels of headroom so the raised arm stays in the
# crop that YOLOv8m-det sees.
FAR_ROI_Y_LO = -8.0
FAR_ROI_Y_HI = 5.0
FAR_ROI_X_PAD = 1.5

BBOX_EXPAND_W = 1.5   # uniform 50% width expansion (centered)
BBOX_EXPAND_H = 5.0   # 5x height expansion, biased downward (see _expand_bbox)
                      # 10 px head bbox → 50 px full-body bbox; catches feet.

VITPOSE_REPO = "usyd-community/vitpose-plus-small"


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
                    ELSE '?' END AS role,
               serve_side_d AS side
        FROM silver.point_detail
        WHERE task_id = CAST(:tid AS uuid)
          AND model = 'sportai'
          AND serve_d = TRUE
          AND ball_hit_s IS NOT NULL
        ORDER BY ball_hit_s
    """), {"tid": sportai_tid}).mappings().all()
    return [dict(r) for r in rows]


def _expected_server_half(role, side):
    """Return (court_x_min, court_x_max) for where the SERVER stands given
    role ('NEAR'/'FAR') and serve_side_d ('deuce'/'ad').

    The visual-debug pass revealed two figures at the far baseline during
    many serves (one server + one non-server walking / standing at the
    baseline). Biggest-bbox YOLO sometimes locks onto the wrong one,
    producing 'dead-static keypoints' for 5 of 11 FAR serves on
    d1fed568. SA's serve_side_d disambiguates which half of the baseline
    the server stands on.

    Geometry:
      - Court center at court_x = 5.485 m (half of 10.97 m doubles width).
      - FAR player faces the camera, so their LEFT = camera's RIGHT.
      - FAR DEUCE = server stands in their OWN deuce box (their right
        side) = camera's LEFT half = court_x < 5.485.
      - FAR AD    = server stands in their OWN ad box (their left
        side)   = camera's RIGHT half = court_x > 5.485.
      - NEAR is the mirror (near player faces away from camera):
        NEAR DEUCE = camera's RIGHT half; NEAR AD = camera's LEFT half.
      - 1 m overlap around the centre mark so a server a few cm into the
        wrong half (unusual but not impossible) isn't rejected.
    """
    if not side:
        return (-10.0, 20.0)  # permissive: accept anywhere
    side = str(side).lower()
    if role == "FAR":
        if side == "deuce":
            return (-2.0, 6.5)   # camera's LEFT half + 1m slop
        if side == "ad":
            return (4.5, 13.0)   # camera's RIGHT half + 1m slop
    elif role == "NEAR":
        if side == "deuce":
            return (4.5, 13.0)   # mirror
        if side == "ad":
            return (-2.0, 6.5)
    return (-10.0, 20.0)


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


def _compute_far_roi_pixel(detector, frame_shape, pad_px=20):
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
            raise RuntimeError(f"cannot project ({mx},{my})")
        pxs.append(p)
    xs = [p[0] for p in pxs]; ys = [p[1] for p in pxs]
    h, w = frame_shape[:2]
    return (max(0, int(min(xs) - pad_px)),
            max(0, int(min(ys) - pad_px)),
            min(w, int(max(xs) + pad_px)),
            min(h, int(max(ys) + pad_px)))


def _expand_bbox(bbox, scale_w, scale_h, frame_w, frame_h, extend_down_factor=4.0):
    """Expand bbox asymmetrically:
       - width: scale_w from center (uniform left/right)
       - height: scale_h from TOP (extend downward more than up)

    The YOLOv8m-det bbox on small far players often captures only the
    head/upper torso (10-14 px tall). Feet and knees are BELOW that
    bbox, so ViTPose fabricates those keypoints instead of seeing them.
    Aggressive downward extension keeps the FULL body in frame for the
    pose model, giving wrist/ankle real visual evidence to lock onto.
    """
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2
    w = (x2 - x1) * scale_w
    h_orig = y2 - y1
    h_extended = h_orig * scale_h
    # Keep the TOP (head) location mostly unchanged; extend mainly DOWN
    up_fraction = 0.5 / (0.5 + extend_down_factor)
    new_top = y1 - (h_extended - h_orig) * up_fraction
    new_bottom = y2 + (h_extended - h_orig) * (1.0 - up_fraction)
    return (max(0, int(cx - w / 2)),
            max(0, int(new_top)),
            min(frame_w - 1, int(cx + w / 2)),
            min(frame_h - 1, int(new_bottom)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--sportai", default=DEFAULT_SPORTAI_REF)
    ap.add_argument("--window-s", type=float, default=1.5)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--only-role", choices=["NEAR", "FAR"], default="FAR")
    ap.add_argument("--max-serves", type=int, default=None)
    ap.add_argument("--det-conf", type=float, default=0.15)
    ap.add_argument("--source-tag", default="far_vitpose")
    ap.add_argument("--verbose-kp", action="store_true")
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
        logger.error("no serves"); return 1
    logger.info("Processing %d %s serves", len(serves), args.only_role or "all")

    detector = _calibrate_court(args.video)
    cap = cv2.VideoCapture(args.video)
    ok, first = cap.read()
    cap.release()
    if not ok: raise RuntimeError("cannot read first frame")
    H_FRAME, W_FRAME = first.shape[:2]

    roi = _compute_far_roi_pixel(detector, first.shape)
    x0, y0, x1, y1 = roi
    logger.info("Far ROI: (%d,%d)-(%d,%d) size=%dx%d", x0, y0, x1, y1, x1-x0, y1-y0)

    # Stage 1: person detector (YOLOv8m-det)
    from ultralytics import YOLO
    from ml_pipeline.config import YOLO_WEIGHTS
    logger.info("Stage 1: loading YOLO detector: %s", YOLO_WEIGHTS)
    det_model = YOLO(YOLO_WEIGHTS)

    # Stage 2: ViTPose++ small
    import torch
    from transformers import VitPoseForPoseEstimation, VitPoseImageProcessor
    logger.info("Stage 2: loading ViTPose++ small from HuggingFace...")
    vit_model = VitPoseForPoseEstimation.from_pretrained(VITPOSE_REPO)
    vit_proc = VitPoseImageProcessor.from_pretrained(VITPOSE_REPO)
    vit_model.eval()
    coco_idx = torch.tensor([0])  # COCO expert head
    logger.info("Both stages loaded.")

    window_frames = int(round(args.window_s * args.fps))
    rows_to_write = []
    per_serve_summary = []

    for i, s in enumerate(serves):
        ts = float(s["ts"])
        role = s["role"]
        side = s.get("side")
        cx_min, cx_max = _expected_server_half(role, side)
        center_frame = int(round(ts * args.fps))
        start_f = max(0, center_frame - window_frames)
        end_f = center_frame + window_frames
        logger.info("[%d/%d] ts=%.2f role=%s side=%s frames [%d,%d) "
                    "server_cx_range=[%.1f,%.1f]",
                    i+1, len(serves), ts, role, side, start_f, end_f,
                    cx_min, cx_max)

        cap = cv2.VideoCapture(args.video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_f)
        n_frames = 0
        n_dets = 0
        n_pose_usable = 0
        n_in_baseline = 0
        t0 = time.time()
        try:
            for idx in range(start_f, end_f):
                ok, frame = cap.read()
                if not ok: break
                n_frames += 1
                # Stage 1: detect person in far ROI
                roi_crop = frame[y0:y1, x0:x1]
                if roi_crop.size == 0: continue
                det_res = det_model.predict(
                    roi_crop, conf=args.det_conf, imgsz=1280,
                    classes=[0], verbose=False,
                )
                if not det_res or det_res[0].boxes is None or len(det_res[0].boxes) == 0:
                    continue
                # Side-prior bbox selection: filter YOLO detections to ones
                # whose feet project into the expected-server half of the
                # court, THEN pick biggest remaining. The far baseline often
                # has TWO figures (server + non-server walking / standing)
                # and biggest-bbox alone locks onto the wrong one, producing
                # dead-static keypoints. Visual debug at 386.60 confirmed
                # the pattern on d1fed568. The side filter uses SA's
                # serve_side_d as ground truth for which half the server
                # occupies.
                boxes = det_res[0].boxes.xyxy.cpu().numpy()
                # Compute feet court_x for each candidate bbox
                in_side = []
                for bi, b in enumerate(boxes):
                    bbx1, bby1, bbx2, bby2 = [float(v) for v in b]
                    fbx1_c = bbx1 + x0; fby1_c = bby1 + y0
                    fbx2_c = bbx2 + x0; fby2_c = bby2 + y0
                    feet_x_c = (fbx1_c + fbx2_c) / 2
                    feet_y_c = fby2_c
                    court_c = detector.to_court_coords(feet_x_c, feet_y_c, strict=False)
                    cx_c = court_c[0] if court_c else None
                    if cx_c is None:
                        # No projection — keep as permissive (server side
                        # unknown). Happens at frame edges / top of ROI.
                        in_side.append(bi)
                        continue
                    if cx_min <= cx_c <= cx_max:
                        in_side.append(bi)
                if not in_side:
                    # No detection on the expected side — skip this frame.
                    # Better to drop a frame than write pose from the wrong
                    # body (which was the source of the 5 NO_MATCH serves).
                    continue
                # Among the side-filtered candidates, pick biggest (area)
                areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
                big = max(in_side, key=lambda bi: areas[bi])
                bx1, by1, bx2, by2 = [float(v) for v in boxes[big]]
                fbx1 = bx1 + x0; fby1 = by1 + y0
                fbx2 = bx2 + x0; fby2 = by2 + y0
                n_dets += 1

                # Stage 2: expand bbox asymmetrically (mainly downward)
                # + feed tight crop to ViTPose
                ebx1, eby1, ebx2, eby2 = _expand_bbox(
                    (fbx1, fby1, fbx2, fby2),
                    BBOX_EXPAND_W, BBOX_EXPAND_H,
                    W_FRAME, H_FRAME)
                bbox_w = ebx2 - ebx1; bbox_h = eby2 - eby1
                if bbox_w <= 0 or bbox_h <= 0:
                    continue
                pose_input = frame[eby1:eby2, ebx1:ebx2]
                if pose_input.size == 0:
                    continue

                # ViTPose expects RGB
                rgb = cv2.cvtColor(pose_input, cv2.COLOR_BGR2RGB)
                # boxes relative to the pose_input — [0, 0, w, h]
                vit_inputs = vit_proc(
                    images=[rgb],
                    boxes=[[[0, 0, bbox_w, bbox_h]]],
                    return_tensors="pt",
                )
                with torch.no_grad():
                    vit_out = vit_model(
                        pixel_values=vit_inputs["pixel_values"],
                        dataset_index=coco_idx,
                    )
                results = vit_proc.post_process_pose_estimation(
                    vit_out, boxes=[[[0, 0, bbox_w, bbox_h]]],
                )
                if not results or not results[0]:
                    continue
                person_kp = results[0][0]["keypoints"].cpu().numpy()  # (17, 2)
                person_sc = results[0][0]["scores"].cpu().numpy()     # (17,)
                # Shift kp from pose_input-local to full-frame
                kp_full = np.column_stack([
                    person_kp[:, 0] + ebx1,
                    person_kp[:, 1] + eby1,
                    person_sc,
                ])
                wrist_conf = float(max(kp_full[9, 2], kp_full[10, 2]))
                shoulder_conf = float(max(kp_full[5, 2], kp_full[6, 2]))
                kp_max = float(kp_full[:, 2].max())
                # Keep rows where EITHER (wrist AND shoulder both > 0.3) OR
                # (wrist > 0.5 alone). Trophy pose occludes the shoulder
                # behind the raised arm so shoulder conf drops to ~0.12
                # precisely during the serve motion. Without the OR branch,
                # our filter REJECTS the key trophy-pose frames — the
                # opposite of what we need. pose_signal.score_pose_frame
                # already handles single-shoulder-available gracefully.
                has_usable = (
                    (wrist_conf > 0.3 and shoulder_conf > 0.3)
                    or wrist_conf > 0.5
                )
                if has_usable:
                    n_pose_usable += 1

                # Feet court projection. NOTE: court calibration has material
                # residual errors at top-of-frame (far baseline projects
                # ~10 m behind the true baseline on our test video). Since
                # the ROI IS the far-baseline metric region by construction,
                # we override court_y=0.0 for ROI-sourced rows so the
                # downstream baseline-zone filter in _detect_pose_based_serves
                # doesn't drop these poses. The real projected court coords
                # are logged for diagnostic visibility.
                feet_x = (fbx1 + fbx2) / 2
                feet_y = fby2
                court = detector.to_court_coords(feet_x, feet_y, strict=False)
                proj_cx = court[0] if court else None
                proj_cy = court[1] if court else None
                # Synthetic court coords: x from projection (reliable — lateral
                # position within ROI works), y=0.0 (ROI geometry guarantees
                # far baseline)
                cx = proj_cx
                cy = 0.0
                n_in_baseline += 1  # all ROI rows are in baseline by construction

                if args.verbose_kp:
                    cx_s = f"{cx:.2f}" if cx is not None else "None"
                    cy_s = f"{cy:.2f}" if cy is not None else "None"
                    logger.info(
                        "  frame %d: bbox %.0fx%.0f  kp_max=%.2f  "
                        "wrist=%.2f shoulder=%.2f  court=(%s,%s) usable=%s",
                        idx, fbx2 - fbx1, fby2 - fby1,
                        kp_max, wrist_conf, shoulder_conf,
                        cx_s, cy_s, has_usable,
                    )

                kp_json = [
                    [float(kp_full[j, 0]), float(kp_full[j, 1]), float(kp_full[j, 2])]
                    for j in range(kp_full.shape[0])
                ]
                rows_to_write.append({
                    "job_id": args.task, "frame_idx": idx, "player_id": 1,
                    "bbox_x1": fbx1, "bbox_y1": fby1,
                    "bbox_x2": fbx2, "bbox_y2": fby2,
                    "center_x": feet_x, "center_y": feet_y,
                    "court_x": cx, "court_y": cy,
                    "keypoints": json.dumps(kp_json) if has_usable else None,
                    "source": args.source_tag,
                })
        finally:
            cap.release()
        dt = time.time() - t0
        logger.info("  [%d frames, %.1fs] det=%d usable_pose=%d in_baseline=%d",
                    n_frames, dt, n_dets, n_pose_usable, n_in_baseline)
        per_serve_summary.append((ts, role, n_frames, n_dets, n_pose_usable, n_in_baseline))

    logger.info("")
    logger.info("=== SUMMARY ===")
    logger.info("  %-8s %-5s %-6s %-5s %-9s %-9s", "ts", "role", "frames", "det", "pose_ok", "baseline")
    tf = td = tp = tb = 0
    for ts, role, nf, nd, np_, nb in per_serve_summary:
        logger.info("  %-8.2f %-5s %-6d %-5d %-9d %-9d", ts, role, nf, nd, np_, nb)
        tf += nf; td += nd; tp += np_; tb += nb
    logger.info("  TOTAL      frames=%d det=%d pose=%d baseline=%d", tf, td, tp, tb)

    if args.dry_run:
        logger.info("dry-run: not writing to DB")
        return 0
    if not rows_to_write:
        logger.info("nothing to write"); return 0

    # Only write rows where pose was usable (has keypoints JSON)
    usable_rows = [r for r in rows_to_write if r["keypoints"] is not None]
    logger.info("Writing %d rows with usable pose (of %d total detections)",
                len(usable_rows), len(rows_to_write))
    if not usable_rows:
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
        """), usable_rows)
    logger.info("wrote %d rows (source=%s)", len(usable_rows), args.source_tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
