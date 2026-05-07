"""Prototype A/B probe: TrackNet full-frame vs ROI-crop for service-box bounces.

Problem: prod TrackNet runs at 640x360 on the full 1920x1080 frame. At that
scale a ball in the far service box (court_y in [5.48, 11.88], far from
camera) is ~1-2 px and often below TrackNet's detection threshold. The
hypothesis: cropping to a tight rectangle covering both service boxes and
upsampling that crop to 640x360 gives the ball ~3-6 px effective size,
which TrackNet can detect.

This script runs BOTH pipelines side-by-side on a single window of the
local test video and reports detection / bounce counts + per-half breakdowns.

    python -m ml_pipeline.diag.roi_ball_probe \\
        --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \\
        --start-frame 750 --end-frame 1500

No DB access, no prod dependencies beyond ml_pipeline.*.
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("roi_ball_probe")


# ---------------------------------------------------------------------------
# Court geometry (matches ml_pipeline.config SPORT_CONFIG_SINGLES)
# ---------------------------------------------------------------------------
COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
FAR_SERVICE_LINE_M = HALF_Y - SERVICE_LINE_FROM_NET_M   # 5.485
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M  # 18.285


def _calibrate_court(video_path: str, n_frames: int = 300):
    """Run CourtDetector on the first `n_frames` to get a locked homography."""
    from ml_pipeline.court_detector import CourtDetector

    detector = CourtDetector()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")

    logger.info("court_calibration: reading %d frames sequentially", n_frames)
    for idx in range(n_frames + 1):  # +1 so the lock fires
        ok, frame = cap.read()
        if not ok:
            break
        detector.detect(frame, idx)

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
    """Project the service-box rectangle from court metres to pixel space
    and return (x0, y0, x1, y1) with padding.

    The "service box rectangle" covers both service boxes: court_x in
    [-1, DOUBLES_WIDTH+1], court_y in [FAR_SERVICE_LINE_M-1.5, NEAR_SERVICE_LINE_M+1.5].
    """
    # Four corners of the extended service-box rectangle
    corners_m = [
        (-1.0, FAR_SERVICE_LINE_M - 1.5),
        (COURT_WIDTH_DOUBLES_M + 1.0, FAR_SERVICE_LINE_M - 1.5),
        (COURT_WIDTH_DOUBLES_M + 1.0, NEAR_SERVICE_LINE_M + 1.5),
        (-1.0, NEAR_SERVICE_LINE_M + 1.5),
    ]

    # Prefer the lens-calibration projection if available; fall back to homography
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj_calib

    pixel_corners = []
    calib = detector._calibration
    for (mx, my) in corners_m:
        p = None
        if calib is not None:
            p = proj_calib(mx, my, calib)
        if p is None:
            # Homography fallback: invert court→pixel via solving H^{-1}
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
        "service_box_pixel_roi: pixel_corners=%s -> crop=(%d,%d) to (%d,%d), size=%dx%d",
        [(int(x), int(y)) for x, y in pixel_corners], x0, y0, x1, y1, x1 - x0, y1 - y0,
    )
    return (x0, y0, x1, y1)


def _run_tracker(frames_iter, frame_start_idx: int, label: str):
    """Run BallTracker on the given frame iterator. Returns (tracker, timing)."""
    from ml_pipeline.ball_tracker import BallTracker

    tracker = BallTracker()
    t0 = time.time()
    n = 0
    for idx, frame in frames_iter:
        tracker.detect_frame(frame, idx)
        n += 1
        if n % 100 == 0:
            logger.info("  %s: %d frames processed (%.1fs)", label, n, time.time() - t0)
    elapsed = time.time() - t0
    logger.info(
        "  %s: DONE %d frames in %.1fs (%.1f fps)",
        label, n, elapsed, n / max(elapsed, 1e-6),
    )
    tracker.interpolate_gaps()
    tracker.detect_bounces()  # no court_detector here; we'll project manually after
    return tracker, elapsed


def _full_frame_iterator(cap, start: int, end: int):
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    for i in range(start, end):
        ok, frame = cap.read()
        if not ok:
            break
        yield i, frame


def _roi_iterator(cap, start: int, end: int, roi):
    x0, y0, x1, y1 = roi
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    for i in range(start, end):
        ok, frame = cap.read()
        if not ok:
            break
        crop = frame[y0:y1, x0:x1]
        # TrackNet's detect_frame will resize to 640x360 itself.
        # We just feed the crop; the model sees the ball at larger effective size.
        yield i, crop


def _project_detections(detections, scale_x: float, scale_y: float,
                        offset_x: float, offset_y: float, detector):
    """Map tracker pixel detections back to full-frame pixel then to court coords.

    tracker.detections are in ORIGINAL INPUT pixels (tracker does
    x*self.scale_x internally), which for the cropped iterator means
    `crop` pixels. We convert crop -> full-frame pixels by adding the
    offset, then project to court metres.
    """
    projected = []
    for d in detections:
        full_x = d.x * scale_x + offset_x
        full_y = d.y * scale_y + offset_y
        coords = detector.to_court_coords(full_x, full_y, strict=False)
        court_x, court_y = (coords if coords is not None else (None, None))
        projected.append({
            "frame_idx": d.frame_idx,
            "full_px": (full_x, full_y),
            "court": (court_x, court_y),
            "is_bounce": d.is_bounce,
        })
    return projected


def _classify_service_box(court_y: Optional[float]) -> str:
    if court_y is None:
        return "no_coords"
    if FAR_SERVICE_LINE_M <= court_y <= HALF_Y:
        return "far_service_box"
    if HALF_Y < court_y <= NEAR_SERVICE_LINE_M:
        return "near_service_box"
    if court_y < FAR_SERVICE_LINE_M:
        return "behind_far_service_line"
    if court_y > NEAR_SERVICE_LINE_M:
        return "behind_near_service_line"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="ml_pipeline/test_videos/match_90ad59a8.mp4.mp4")
    ap.add_argument("--start-frame", type=int, default=750)
    ap.add_argument("--end-frame", type=int, default=1500)
    ap.add_argument("--out-image", default="ml_pipeline/diag/roi_ball_probe_overlay.png",
                    help="Where to save a visualization frame with the ROI overlay")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        logger.error("video not found: %s", args.video)
        return 2

    # --- Step 1: Calibrate court from the first 300 frames
    t0 = time.time()
    detector = _calibrate_court(args.video, n_frames=300)
    logger.info("court calibration took %.1fs", time.time() - t0)

    # --- Step 2: Compute service-box pixel ROI
    cap = cv2.VideoCapture(args.video)
    ok, first_frame = cap.read()
    if not ok:
        raise RuntimeError("cannot read any frame")
    roi = _service_box_pixel_roi(detector, first_frame.shape)
    x0, y0, x1, y1 = roi

    # --- Step 3: Save an overlay frame for visual sanity
    overlay = first_frame.copy()
    cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 255, 255), 3)
    # Project service-box lines for more detail
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj
    calib = detector._calibration
    if calib is not None:
        for y_m, color in [(FAR_SERVICE_LINE_M, (0, 255, 0)),
                           (HALF_Y, (0, 0, 255)),
                           (NEAR_SERVICE_LINE_M, (0, 255, 0))]:
            p_left = proj(0.0, y_m, calib)
            p_right = proj(COURT_WIDTH_DOUBLES_M, y_m, calib)
            if p_left and p_right:
                cv2.line(overlay,
                         (int(p_left[0]), int(p_left[1])),
                         (int(p_right[0]), int(p_right[1])),
                         color, 2)
    cv2.imwrite(args.out_image, overlay)
    logger.info("wrote visualization to %s", args.out_image)

    # --- Step 4: Run full-frame TrackNet
    logger.info("=== FULL-FRAME pass ===")
    tracker_full, t_full = _run_tracker(
        _full_frame_iterator(cap, args.start_frame, args.end_frame),
        args.start_frame, "full",
    )

    # --- Step 5: Run ROI-cropped TrackNet (fresh video handle)
    cap.release()
    cap = cv2.VideoCapture(args.video)
    logger.info("=== ROI-CROP pass: crop=(%d,%d) size=%dx%d upscale to 640x360 ===",
                x0, y0, x1 - x0, y1 - y0)
    tracker_roi, t_roi = _run_tracker(
        _roi_iterator(cap, args.start_frame, args.end_frame, roi),
        args.start_frame, "roi",
    )
    cap.release()

    # --- Step 6: Project detections back to court coords
    full_projected = _project_detections(
        tracker_full.detections, scale_x=1.0, scale_y=1.0,
        offset_x=0.0, offset_y=0.0, detector=detector,
    )
    # For ROI tracker: the tracker scale_x/scale_y were computed for the
    # crop's original resolution (crop_w, crop_h), so tracker.detections
    # already hold crop-pixel coords. Add (x0, y0) to get full-frame.
    roi_projected = _project_detections(
        tracker_roi.detections, scale_x=1.0, scale_y=1.0,
        offset_x=float(x0), offset_y=float(y0), detector=detector,
    )

    # --- Step 7: Summarize
    def _summ(label, projected):
        n = len(projected)
        n_bounces = sum(1 for p in projected if p["is_bounce"])
        n_coords = sum(1 for p in projected if p["court"][0] is not None)
        zones = {}
        bounce_zones = {}
        for p in projected:
            z = _classify_service_box(p["court"][1])
            zones[z] = zones.get(z, 0) + 1
            if p["is_bounce"]:
                bounce_zones[z] = bounce_zones.get(z, 0) + 1
        logger.info("=== %s summary ===", label)
        logger.info("  detections=%d with_coords=%d bounces=%d", n, n_coords, n_bounces)
        logger.info("  detections by zone:")
        for z, c in sorted(zones.items(), key=lambda x: -x[1]):
            logger.info("    %s: %d", z, c)
        logger.info("  bounces by zone:")
        for z, c in sorted(bounce_zones.items(), key=lambda x: -x[1]):
            logger.info("    %s: %d", z, c)
        return n, n_bounces, bounce_zones

    n_full, b_full, bz_full = _summ("FULL", full_projected)
    n_roi, b_roi, bz_roi = _summ("ROI", roi_projected)

    logger.info("")
    logger.info("=== HEAD-TO-HEAD ===")
    logger.info("  full: %d detections, %d bounces, %.1f fps", n_full, b_full,
                (args.end_frame - args.start_frame) / max(t_full, 1e-6))
    logger.info("  roi:  %d detections, %d bounces, %.1f fps", n_roi, b_roi,
                (args.end_frame - args.start_frame) / max(t_roi, 1e-6))

    svcbox_full = bz_full.get("far_service_box", 0) + bz_full.get("near_service_box", 0)
    svcbox_roi = bz_roi.get("far_service_box", 0) + bz_roi.get("near_service_box", 0)
    logger.info("  service-box bounces:  full=%d  roi=%d",
                svcbox_full, svcbox_roi)

    if svcbox_roi > svcbox_full * 1.3:
        logger.info("  VERDICT: ROI finds %dx more service-box bounces than full-frame",
                    svcbox_roi // max(svcbox_full, 1))
    elif svcbox_roi >= svcbox_full:
        logger.info("  VERDICT: ROI about the same as full-frame")
    else:
        logger.info("  VERDICT: ROI LOSES — full-frame finds more service-box bounces")

    return 0


if __name__ == "__main__":
    sys.exit(main())
