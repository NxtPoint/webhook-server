"""Visual debug for a missed FAR serve — render annotated frames.

Around an SA ground-truth serve timestamp, run YOLO on the far-
baseline ROI for a sequence of frames and render each frame with:
  - ROI box (cyan)
  - Every YOLO person detection bbox (green, with confidence label)
  - ViTPose keypoints for the biggest body (red dots + lines)
  - Court-coordinate projection of the biggest body's feet (text)

Saves a contact-sheet image so we can actually see whether the
detected body is the server, a line judge, a ball kid, or some
other object the ROI is picking up.

Output: <output-dir>/<ts>_<N_frames>.jpg — contact sheet.

Usage:
    python -m ml_pipeline.diag.visualize_far_serve \\
        --task d1fed568-b285-4117-bcef-c6039d52fc37 \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --ts 386.60 --n-frames 9 --fps 25.0 \\
        --output-dir ml_pipeline/training/visual_debug
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import cv2
import numpy as np


logger = logging.getLogger("visualize_far_serve")


COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
FAR_ROI_Y_LO = -8.0
FAR_ROI_Y_HI = 5.0
FAR_ROI_X_PAD = 1.5
BBOX_EXPAND_W = 1.5
BBOX_EXPAND_H = 5.0
VITPOSE_REPO = "usyd-community/vitpose-plus-small"


def _calibrate_court(video_path, n_frames=300):
    from ml_pipeline.court_detector import CourtDetector
    det = CourtDetector()
    cap = cv2.VideoCapture(video_path)
    try:
        for i in range(n_frames + 1):
            ok, f = cap.read()
            if not ok:
                break
            det.detect(f, i)
    finally:
        cap.release()
    if det._locked_detection is None and det._best_detection is None:
        raise RuntimeError("court_calibration failed")
    return det


def _project(mx, my, det):
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj
    if det._calibration is not None:
        p = proj(mx, my, det._calibration)
        if p:
            return p
    best = det._locked_detection or det._best_detection
    if best and best.homography is not None:
        H_inv = np.linalg.inv(best.homography)
        pt = H_inv @ np.array([mx, my, 1.0])
        if pt[2] != 0:
            return float(pt[0] / pt[2]), float(pt[1] / pt[2])
    return None


def _compute_far_roi_pixel(detector, frame_shape, pad_px=20):
    corners_m = [
        (-FAR_ROI_X_PAD, FAR_ROI_Y_LO),
        (COURT_WIDTH_DOUBLES_M + FAR_ROI_X_PAD, FAR_ROI_Y_LO),
        (COURT_WIDTH_DOUBLES_M + FAR_ROI_X_PAD, FAR_ROI_Y_HI),
        (-FAR_ROI_X_PAD, FAR_ROI_Y_HI),
    ]
    pxs = [_project(mx, my, detector) for mx, my in corners_m]
    if any(p is None for p in pxs):
        raise RuntimeError("cannot project ROI corners")
    xs = [p[0] for p in pxs]
    ys = [p[1] for p in pxs]
    h, w = frame_shape[:2]
    return (max(0, int(min(xs) - pad_px)),
            max(0, int(min(ys) - pad_px)),
            min(w, int(max(xs) + pad_px)),
            min(h, int(max(ys) + pad_px)))


def _expand_bbox(bbox, sw, sh, fw, fh, extend_down=4.0):
    x1, y1, x2, y2 = bbox
    w = x2 - x1
    h = y2 - y1
    cx = (x1 + x2) / 2
    w_new = w * sw
    h_new = h * sh
    up_frac = 0.5 / (0.5 + extend_down)
    new_top = y1 - (h_new - h) * up_frac
    new_bot = y2 + (h_new - h) * (1 - up_frac)
    return (max(0, int(cx - w_new / 2)),
            max(0, int(new_top)),
            min(fw - 1, int(cx + w_new / 2)),
            min(fh - 1, int(new_bot)))


# COCO skeleton for drawing: pairs of keypoint indices
_COCO_SKELETON = [
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),      # arms + shoulders
    (5, 11), (6, 12), (11, 12),                    # torso
    (11, 13), (13, 15), (12, 14), (14, 16),        # legs
    (0, 1), (0, 2), (1, 3), (2, 4),                # face
]


def _draw_person(frame, bbox, keypoints=None, label="", color=(0, 255, 0)):
    x1, y1, x2, y2 = [int(v) for v in bbox]
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    if label:
        cv2.putText(frame, label, (x1, max(y1 - 5, 15)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    if keypoints is not None:
        # keypoints is (17, 3) array
        for i, (kx, ky, kc) in enumerate(keypoints):
            if kc < 0.2:
                continue
            cv2.circle(frame, (int(kx), int(ky)), 3, (0, 0, 255), -1)
        for a, b in _COCO_SKELETON:
            ka = keypoints[a]
            kb = keypoints[b]
            if ka[2] < 0.2 or kb[2] < 0.2:
                continue
            cv2.line(frame, (int(ka[0]), int(ka[1])),
                     (int(kb[0]), int(kb[1])), (0, 0, 255), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--ts", type=float, required=True,
                    help="SA ground-truth serve ts (seconds)")
    ap.add_argument("--n-frames", type=int, default=9,
                    help="Number of frames to render (spaced evenly across window)")
    ap.add_argument("--window-s", type=float, default=2.0)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--det-conf", type=float, default=0.15)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(args.output_dir, exist_ok=True)

    # Calibrate court + pin ROI
    detector = _calibrate_court(args.video)
    cap = cv2.VideoCapture(args.video)
    ok, first = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError("cannot read first frame")
    H, W = first.shape[:2]
    roi = _compute_far_roi_pixel(detector, first.shape)
    x0, y0, x1, y1 = roi
    logger.info("Far ROI: (%d,%d)-(%d,%d) size=%dx%d", x0, y0, x1, y1, x1 - x0, y1 - y0)

    # Load YOLO + ViTPose
    from ultralytics import YOLO
    from ml_pipeline.config import YOLO_WEIGHTS
    import torch
    from transformers import VitPoseForPoseEstimation, VitPoseImageProcessor
    det_model = YOLO(YOLO_WEIGHTS)
    logger.info("loaded YOLO")
    vit_model = VitPoseForPoseEstimation.from_pretrained(VITPOSE_REPO)
    vit_proc = VitPoseImageProcessor.from_pretrained(VITPOSE_REPO)
    vit_model.eval()
    coco_idx = torch.tensor([0])
    logger.info("loaded ViTPose")

    # Choose frames to render: evenly spaced across [ts - window, ts + window]
    center_frame = int(round(args.ts * args.fps))
    window_frames = int(round(args.window_s * args.fps))
    start_f = center_frame - window_frames
    end_f = center_frame + window_frames
    step = max(1, (end_f - start_f) // args.n_frames)
    target_frames = list(range(start_f, end_f + 1, step))[:args.n_frames]
    logger.info("rendering %d frames around ts=%.2f (center_frame=%d): %s",
                len(target_frames), args.ts, center_frame, target_frames)

    # Process each target frame: render YOLO bboxes + ViTPose on biggest body
    cap = cv2.VideoCapture(args.video)
    tile_shape = None
    tiles = []
    for tf in target_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, tf)
        ok, frame = cap.read()
        if not ok:
            logger.warning("could not read frame %d", tf)
            continue

        annotated = frame.copy()
        # Draw ROI
        cv2.rectangle(annotated, (x0, y0), (x1, y1), (255, 255, 0), 2)
        cv2.putText(annotated, f"ROI court_y=[-8, 5] x_pad=1.5",
                    (x0, y0 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (255, 255, 0), 1, cv2.LINE_AA)

        roi_crop = frame[y0:y1, x0:x1]
        det_res = det_model.predict(
            roi_crop, conf=args.det_conf, imgsz=1280, classes=[0], verbose=False,
        )
        if not det_res or det_res[0].boxes is None or len(det_res[0].boxes) == 0:
            cv2.putText(annotated, f"frame {tf} ts={tf/args.fps:.2f}  NO DETECTIONS",
                        (10, H - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                        (0, 0, 255), 2, cv2.LINE_AA)
            tiles.append(annotated)
            continue

        boxes = det_res[0].boxes.xyxy.cpu().numpy()
        confs = det_res[0].boxes.conf.cpu().numpy()
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        biggest = int(np.argmax(areas))

        n_dets = len(boxes)
        for bi, (b, c) in enumerate(zip(boxes, confs)):
            bx1, by1, bx2, by2 = b
            # Shift to full frame
            fbx1 = bx1 + x0
            fby1 = by1 + y0
            fbx2 = bx2 + x0
            fby2 = by2 + y0
            # Project feet to court
            feet_x = (fbx1 + fbx2) / 2
            feet_y = fby2
            court = detector.to_court_coords(feet_x, feet_y, strict=False)
            court_str = f"cx={court[0]:.1f},cy={court[1]:.1f}" if court else "cxy=?"
            is_biggest = (bi == biggest)
            color = (0, 255, 0) if is_biggest else (128, 128, 0)
            label = f"#{bi} c={c:.2f} {court_str}" + (" [BIG]" if is_biggest else "")

            keypoints = None
            if is_biggest:
                # Run ViTPose on the biggest body only (matches extractor behaviour)
                ebx1, eby1, ebx2, eby2 = _expand_bbox(
                    (fbx1, fby1, fbx2, fby2),
                    BBOX_EXPAND_W, BBOX_EXPAND_H, W, H)
                bw = ebx2 - ebx1
                bh = eby2 - eby1
                if bw > 0 and bh > 0:
                    pose_input = frame[eby1:eby2, ebx1:ebx2]
                    if pose_input.size > 0:
                        rgb = cv2.cvtColor(pose_input, cv2.COLOR_BGR2RGB)
                        vit_inputs = vit_proc(
                            images=[rgb],
                            boxes=[[[0, 0, bw, bh]]],
                            return_tensors="pt",
                        )
                        with torch.no_grad():
                            vit_out = vit_model(
                                pixel_values=vit_inputs["pixel_values"],
                                dataset_index=coco_idx,
                            )
                        results = vit_proc.post_process_pose_estimation(
                            vit_out, boxes=[[[0, 0, bw, bh]]],
                        )
                        if results and results[0]:
                            pkp = results[0][0]["keypoints"].cpu().numpy()
                            psc = results[0][0]["scores"].cpu().numpy()
                            keypoints = np.column_stack([
                                pkp[:, 0] + ebx1,
                                pkp[:, 1] + eby1,
                                psc,
                            ])
            _draw_person(annotated, (fbx1, fby1, fbx2, fby2),
                         keypoints=keypoints, label=label, color=color)

        cv2.putText(annotated, f"frame {tf} ts={tf/args.fps:.2f}s  {n_dets} detections",
                    (10, H - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (255, 255, 255), 2, cv2.LINE_AA)
        tiles.append(annotated)

    cap.release()

    # Build contact sheet — crop each to a region around the ROI to keep readable
    if not tiles:
        logger.error("no frames rendered")
        return 1

    # Crop each tile tightly to the ROI + small pad so pose details are legible
    pad_h = 120  # extra headroom above ROI (for raised-arm frames)
    pad_s = 60   # sides/bottom pad
    crop_x1 = max(0, x0 - pad_s)
    crop_y1 = max(0, y0 - pad_h)
    crop_x2 = min(W, x1 + pad_s)
    crop_y2 = min(H, y1 + pad_s)
    cropped = [t[crop_y1:crop_y2, crop_x1:crop_x2] for t in tiles]

    # Build grid: 2 per row for larger tiles (was 3)
    per_row = 2
    rows = []
    for i in range(0, len(cropped), per_row):
        row = cropped[i:i + per_row]
        # Pad last row to per_row elements
        while len(row) < per_row:
            row.append(np.zeros_like(cropped[0]))
        rows.append(np.hstack(row))
    sheet = np.vstack(rows)

    out_name = f"ts{args.ts:.2f}_n{len(tiles)}.jpg"
    out_path = Path(args.output_dir) / out_name
    cv2.imwrite(str(out_path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    logger.info("wrote contact sheet: %s (size %dx%d)",
                out_path, sheet.shape[1], sheet.shape[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
