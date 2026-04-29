"""Production far-player pose extractor — runs in AWS Batch after the
main TennisAnalysisPipeline. Scans the whole video for far-baseline
person detections and runs ViTPose-Base to produce high-quality pose
keypoints. Writes to ml_analysis.player_detections_roi where the
serve_detector's merge logic picks them up.

Differs from ml_pipeline/diag/extract_vitpose_far.py:
  - No SA reference needed (scans entire video, not per-serve windows)
  - Not a CLI — called as a function from _run_batch
  - Writes directly to DB instead of deferring to a label JSON
  - Samples every Nth frame (default N=2) to keep total runtime bounded
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

import cv2
import numpy as np
from sqlalchemy import text as sql_text

logger = logging.getLogger("roi_pose")

COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97

# Far-baseline ROI in court metric space (where the far player stands).
# -8 m lets the ROI catch raised arms above head. +5 m covers the
# half-court side. See diag/extract_vitpose_far.py for the rationale.
FAR_ROI_Y_LO = -8.0
FAR_ROI_Y_HI = 5.0
FAR_ROI_X_PAD = 1.5

BBOX_EXPAND_W = 1.5
BBOX_EXPAND_H = 5.0

VITPOSE_REPO = "usyd-community/vitpose-plus-base"

DEFAULT_DET_CONF = 0.15
DEFAULT_WRIST_CONF = 0.3
DEFAULT_SHOULDER_CONF = 0.3


def _init_schema(conn):
    """Ensure ml_analysis.player_detections_roi exists (idempotent)."""
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


def _project(mx, my, detector):
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj
    calib = detector._calibration
    if calib is not None:
        p = proj(mx, my, calib)
        if p is not None:
            return p
    best = detector._locked_detection or detector._best_detection
    if best is not None and best.homography is not None:
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
    pxs = []
    for mx, my in corners_m:
        p = _project(mx, my, detector)
        if p is None:
            return None
        pxs.append(p)
    xs = [p[0] for p in pxs]
    ys = [p[1] for p in pxs]
    h, w = frame_shape[:2]
    return (max(0, int(min(xs) - pad_px)),
            max(0, int(min(ys) - pad_px)),
            min(w, int(max(xs) + pad_px)),
            min(h, int(max(ys) + pad_px)))


def _expand_bbox(bbox, sw, sh, fw, fh, extend_down=4.0):
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2
    w = (x2 - x1) * sw
    h_orig = y2 - y1
    h_ext = h_orig * sh
    up_frac = 0.5 / (0.5 + extend_down)
    new_top = y1 - (h_ext - h_orig) * up_frac
    new_bot = y2 + (h_ext - h_orig) * (1.0 - up_frac)
    return (max(0, int(cx - w / 2)),
            max(0, int(new_top)),
            min(fw - 1, int(cx + w / 2)),
            min(fh - 1, int(new_bot)))


def extract_far_pose(
    video_path: str,
    job_id: str,
    engine,
    *,
    fps: float = 25.0,
    sample_every: int = 2,
    det_conf: float = DEFAULT_DET_CONF,
    source_tag: str = "far_vitpose",
    vitpose_repo: str = VITPOSE_REPO,
    calib_frames: int = 300,
    court_detector=None,
    bounces: Optional[List] = None,
    frame_from: Optional[int] = None,
    frame_to: Optional[int] = None,
    replace: bool = True,
) -> int:
    """Run far-baseline ViTPose extraction across the entire video.

    Writes rows to ml_analysis.player_detections_roi for every sampled
    frame that YOLO detects a person in the far-baseline ROI and
    ViTPose returns usable keypoints.

    Args:
        video_path: local filesystem path to the video (Batch has it).
        job_id: the ml_analysis.video_analysis_jobs.job_id (used as FK).
        engine: SQLAlchemy engine (DATABASE_URL).
        fps: video fps (default 25, used for timestamp calc).
        sample_every: process 1 of every N frames. 2 = 12.5 fps effective.
        det_conf: YOLO person-detection confidence threshold.
        source_tag: ml_analysis.player_detections_roi.source value.
        vitpose_repo: HuggingFace repo for the ViTPose model weights.
        calib_frames: frames used by CourtDetector to lock the homography.
        court_detector: an already-calibrated CourtDetector instance. If
            None, a new one is built from the first calib_frames of the
            video. Pass the pipeline's court detector when available to
            save ~10-20 s of re-calibration.
        bounces: in-memory list of BallDetection-like objects with
            frame_idx + is_bounce attributes (typically result.ball_detections
            from the just-finished pipeline.process()). When supplied, frames
            whose ts falls inside an IN_RALLY window are skipped at the source
            — real serves only happen between rallies, so this drops mid-rally
            trophy-pose noise that the downstream pose-first detector can't
            disambiguate. When None, every sampled frame is processed.
        frame_from / frame_to: optional inclusive frame range to limit the
            scan. Default None on both = whole video (production behaviour).
            Used by diag tooling (replay_roi_pose) to test specific windows
            without reprocessing the whole video.
        replace: when True (default), DELETEs prior rows for (job_id, source)
            before inserting — production idempotency. Set False for diag /
            additive scans where multiple frame-range runs share a source_tag.

    Returns:
        Number of rows written.
    """
    if not os.path.exists(video_path):
        logger.warning("roi_pose: video not found: %s; skipping", video_path)
        return 0

    t_start = time.time()

    # 1. Court calibration — reuse pipeline's detector if provided.
    if court_detector is None:
        from ml_pipeline.court_detector import CourtDetector
        court_detector = CourtDetector()
        cap = cv2.VideoCapture(video_path)
        try:
            for i in range(calib_frames + 1):
                ok, f = cap.read()
                if not ok:
                    break
                court_detector.detect(f, i)
        finally:
            cap.release()
        if (court_detector._locked_detection is None
                and court_detector._best_detection is None):
            logger.warning("roi_pose: court calibration failed; skipping")
            return 0

    # 2. Compute far-baseline ROI in pixel coords
    cap = cv2.VideoCapture(video_path)
    ok, first = cap.read()
    if not ok:
        cap.release()
        logger.warning("roi_pose: cannot read first frame; skipping")
        return 0
    H_FRAME, W_FRAME = first.shape[:2]
    roi = _compute_far_roi_pixel(court_detector, first.shape)
    if roi is None:
        cap.release()
        logger.warning("roi_pose: cannot project ROI corners; skipping")
        return 0
    x0, y0, x1, y1 = roi
    logger.info(
        "roi_pose: far ROI pixel (%d,%d)-(%d,%d) size=%dx%d",
        x0, y0, x1, y1, x1 - x0, y1 - y0,
    )

    # 3. Load detectors
    from ultralytics import YOLO
    from ml_pipeline.config import YOLO_WEIGHTS
    det_model = YOLO(YOLO_WEIGHTS)
    logger.info("roi_pose: YOLO loaded")

    import torch
    from transformers import VitPoseForPoseEstimation, VitPoseImageProcessor
    vit_model = VitPoseForPoseEstimation.from_pretrained(vitpose_repo)
    vit_proc = VitPoseImageProcessor.from_pretrained(vitpose_repo)
    vit_model.eval()
    if torch.cuda.is_available():
        vit_model = vit_model.to("cuda")
        logger.info("roi_pose: ViTPose on cuda")
    else:
        logger.info("roi_pose: ViTPose on cpu")
    coco_idx = torch.tensor([0])

    # 3b. Build rally state machine from in-memory bounces. Real serves only
    # happen between rallies; mid-rally trophy poses (overheads, lobs, stretch
    # volleys) are pose-locally indistinguishable from real serves at the
    # baseline, so we skip them at the source. Bronze ml_analysis.ball_detections
    # is empty at this stage (Render ingests bronze later from the JSON export),
    # which is why the in-memory list is the right input — see handover_t5.md
    # NEXT SESSION block.
    rally = None
    rally_in_rally_state = None
    rally_gate_broken = False
    if bounces:
        try:
            from ml_pipeline.serve_detector.rally_state import (
                RallyStateMachine, RallyState,
            )
            bounce_ts = [
                d.frame_idx / fps
                for d in bounces
                if getattr(d, "is_bounce", False)
            ]
            rally = RallyStateMachine(bounce_ts=bounce_ts)
            rally_in_rally_state = RallyState.IN_RALLY
            logger.info(
                "roi_pose: rally gate active, %d bounces (of %d ball detections)",
                len(bounce_ts), len(bounces),
            )
        except Exception as e:
            # Caller asked for the gate (passed bounces) but we couldn't build
            # it. This is a packaging / dependency bug — fall through and
            # process every frame so the run still succeeds, but flag it loud
            # in logs so the next CloudWatch grep catches it instead of
            # silently regressing to baseline.
            logger.error(
                "roi_pose: BUG — rally gate requested but failed to build (%s). "
                "Falling back to UNGATED full-video scan. Fix me before relying on results.",
                e,
            )
            rally = None
            rally_in_rally_state = None
            rally_gate_broken = True
    else:
        logger.info("roi_pose: no bounces supplied; processing all sampled frames (no rally gate)")

    # 4. Scan frames, run detection + pose, collect rows
    total_frames_probed = 0
    total_in_rally_skipped = 0
    total_dets = 0
    total_usable = 0
    rows_to_write = []

    # Seek to frame_from if set — saves walking through 10s of thousands of
    # frames just to discard them. CAP_PROP_POS_FRAMES seek can be slow on
    # h264 keyframe boundaries but works for our 100-1000-frame diag windows.
    start_frame = frame_from if frame_from is not None else 0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    idx = start_frame
    while True:
        if frame_to is not None and idx > frame_to:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if idx % sample_every != 0:
            idx += 1
            continue
        if rally is not None:
            ts = idx / fps
            if rally.state_at(ts) == rally_in_rally_state:
                total_in_rally_skipped += 1
                idx += 1
                continue
        total_frames_probed += 1

        roi_crop = frame[y0:y1, x0:x1]
        if roi_crop.size == 0:
            idx += 1
            continue

        det_res = det_model.predict(
            roi_crop, conf=det_conf, imgsz=1280, classes=[0], verbose=False,
        )
        if not det_res or det_res[0].boxes is None or len(det_res[0].boxes) == 0:
            idx += 1
            continue

        boxes = det_res[0].boxes.xyxy.cpu().numpy()
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        big = int(np.argmax(areas))
        bx1, by1, bx2, by2 = [float(v) for v in boxes[big]]
        fbx1 = bx1 + x0
        fby1 = by1 + y0
        fbx2 = bx2 + x0
        fby2 = by2 + y0
        total_dets += 1

        ebx1, eby1, ebx2, eby2 = _expand_bbox(
            (fbx1, fby1, fbx2, fby2),
            BBOX_EXPAND_W, BBOX_EXPAND_H, W_FRAME, H_FRAME,
        )
        bbox_w = ebx2 - ebx1
        bbox_h = eby2 - eby1
        if bbox_w <= 0 or bbox_h <= 0:
            idx += 1
            continue
        pose_input = frame[eby1:eby2, ebx1:ebx2]
        if pose_input.size == 0:
            idx += 1
            continue
        rgb = cv2.cvtColor(pose_input, cv2.COLOR_BGR2RGB)
        vit_inputs = vit_proc(
            images=[rgb],
            boxes=[[[0, 0, bbox_w, bbox_h]]],
            return_tensors="pt",
        )
        if torch.cuda.is_available():
            vit_inputs = {k: v.to("cuda") for k, v in vit_inputs.items()}
        with torch.no_grad():
            vit_out = vit_model(
                pixel_values=vit_inputs["pixel_values"],
                dataset_index=coco_idx.to(vit_model.device),
            )
        results = vit_proc.post_process_pose_estimation(
            vit_out, boxes=[[[0, 0, bbox_w, bbox_h]]],
        )
        if not results or not results[0]:
            idx += 1
            continue
        pkp = results[0][0]["keypoints"].cpu().numpy()
        psc = results[0][0]["scores"].cpu().numpy()
        kp_full = np.column_stack([
            pkp[:, 0] + ebx1,
            pkp[:, 1] + eby1,
            psc,
        ])
        wrist_conf = float(max(kp_full[9, 2], kp_full[10, 2]))
        shoulder_conf = float(max(kp_full[5, 2], kp_full[6, 2]))
        has_usable = (
            (wrist_conf > DEFAULT_WRIST_CONF and shoulder_conf > DEFAULT_SHOULDER_CONF)
            or wrist_conf > 0.5
        )
        if not has_usable:
            idx += 1
            continue
        total_usable += 1

        feet_x = (fbx1 + fbx2) / 2
        feet_y = fby2
        # Project feet to real court coords. The diag-tool predecessor
        # (diag/extract_vitpose_far.py) hardcoded court_y=0.0 because
        # it was bounded to ±2.5s windows around SA-GT serves where the
        # player WAS at the baseline by definition. The production
        # extractor scans the full video, so we MUST keep the real
        # projected court_y — without it, downstream serve_detector
        # can't tell a baseline trophy pose (real serve setup) apart
        # from a mid-court trophy pose (rally overhead/forehand). Skip
        # the row entirely when projection fails (strict=False already
        # gives ±5m slack for far-baseline calibration noise).
        court = court_detector.to_court_coords(feet_x, feet_y, strict=False)
        if court is None:
            idx += 1
            continue
        cx, cy = float(court[0]), float(court[1])

        import json as _json
        kp_json = [
            [float(kp_full[j, 0]), float(kp_full[j, 1]), float(kp_full[j, 2])]
            for j in range(kp_full.shape[0])
        ]
        rows_to_write.append({
            "job_id": job_id, "frame_idx": idx, "player_id": 1,
            "bbox_x1": fbx1, "bbox_y1": fby1,
            "bbox_x2": fbx2, "bbox_y2": fby2,
            "center_x": feet_x, "center_y": feet_y,
            "court_x": cx, "court_y": cy,
            "keypoints": _json.dumps(kp_json),
            "source": source_tag,
        })
        idx += 1

    cap.release()
    dt_scan = time.time() - t_start
    gate_tag = " [RALLY GATE BROKEN — UNGATED RESULTS]" if rally_gate_broken else ""
    logger.info(
        "roi_pose: scanned %d sampled frames (every %d), skipped %d IN_RALLY frames, "
        "%d detections, %d usable poses in %.1fs%s",
        total_frames_probed, sample_every, total_in_rally_skipped,
        total_dets, total_usable, dt_scan, gate_tag,
    )

    if not rows_to_write:
        logger.info("roi_pose: nothing to write")
        return 0

    # 5. Write to DB (replace previous source_tag rows for idempotency, unless
    # caller asked for additive insert — used by diag tooling that runs
    # multiple non-overlapping frame ranges with the same source tag).
    with engine.begin() as conn:
        _init_schema(conn)
        if replace:
            n_del = conn.execute(sql_text("""
                DELETE FROM ml_analysis.player_detections_roi
                WHERE job_id = :tid AND source = :src
            """), {"tid": job_id, "src": source_tag}).rowcount
            if n_del:
                logger.info("roi_pose: deleted %d prior rows (source=%s)", n_del, source_tag)
        conn.execute(sql_text("""
            INSERT INTO ml_analysis.player_detections_roi
              (job_id, frame_idx, player_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
               center_x, center_y, court_x, court_y, keypoints, source)
            VALUES
              (:job_id, :frame_idx, :player_id, :bbox_x1, :bbox_y1, :bbox_x2, :bbox_y2,
               :center_x, :center_y, :court_x, :court_y, CAST(:keypoints AS JSONB), :source)
        """), rows_to_write)
    logger.info("roi_pose: wrote %d rows (source=%s)", len(rows_to_write), source_tag)
    return len(rows_to_write)
