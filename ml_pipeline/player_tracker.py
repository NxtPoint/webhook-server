"""
PlayerTracker — YOLOv8-pose person detection + court filtering + IoU tracking.
Assigns consistent player_id (0 = near-side, 1 = far-side) across frames.
Extracts 17 COCO body keypoints per player for stroke classification.
"""

import logging
import os
import numpy as np
from ultralytics import YOLO
from dataclasses import dataclass, field
from typing import Optional, List, Dict

import cv2

from ml_pipeline.config import (
    YOLO_WEIGHTS,
    YOLO_POSE_WEIGHTS,
    YOLO_POSE_WEIGHTS_FALLBACK,
    YOLO_CONFIDENCE,
    YOLO_COURT_CROP_CONFIDENCE,
    YOLO_IMGSZ,
    YOLO_COURT_CROP_INFERENCE,
    YOLO_COURT_CROP_MARGIN_PX,
    YOLO_PERSON_CLASS_ID,
    PLAYER_IOU_THRESHOLD,
    PLAYER_COURT_MARGIN_PX,
    PLAYER_OUTSIDE_COURT_MARGIN_PX,
    PLAYER_DETECTION_INTERVAL,
    DEBUG_FRAME_INTERVAL,
    MOG2_MIN_MOTION_RATIO,
    MOG2_MOTION_SCORE_WEIGHT,
    SAHI_ENABLED,
    SAHI_SLICE_HEIGHT,
    SAHI_SLICE_WIDTH,
    SAHI_OVERLAP_RATIO,
    SAHI_CONFIDENCE,
    SAHI_POSTPROCESS_TYPE,
    SAHI_POSTPROCESS_MATCH_THRESHOLD,
)

# SAHI — lazy import to avoid startup cost when disabled
_sahi_detection_model = None

DEBUG_FRAMES_DIR = "/tmp/debug_frames"

logger = logging.getLogger(__name__)

# COCO keypoint indices (17 keypoints)
KP_NOSE = 0
KP_LEFT_EYE = 1; KP_RIGHT_EYE = 2
KP_LEFT_EAR = 3; KP_RIGHT_EAR = 4
KP_LEFT_SHOULDER = 5; KP_RIGHT_SHOULDER = 6
KP_LEFT_ELBOW = 7; KP_RIGHT_ELBOW = 8
KP_LEFT_WRIST = 9; KP_RIGHT_WRIST = 10
KP_LEFT_HIP = 11; KP_RIGHT_HIP = 12
KP_LEFT_KNEE = 13; KP_RIGHT_KNEE = 14
KP_LEFT_ANKLE = 15; KP_RIGHT_ANKLE = 16


@dataclass
class PlayerDetection:
    frame_idx: int
    player_id: int          # 0 = near-side player, 1 = far-side player
    bbox: tuple             # (x1, y1, x2, y2) pixel coordinates
    center: tuple           # (cx, cy) pixel center
    court_x: Optional[float] = None  # metres
    court_y: Optional[float] = None  # metres
    keypoints: Optional[np.ndarray] = field(default=None, repr=False)
    # keypoints: (17, 3) array — x, y, confidence per COCO keypoint
    stroke_class: Optional[str] = None  # optical flow classification for far player


class PlayerTracker:
    def __init__(self, weights_path: str = None, device: str = None):
        self.device = device or ("cuda:0" if __import__("torch").cuda.is_available() else "cpu")
        # Prefer the larger YOLOv8x-pose model, then fall back to yolov8m-pose,
        # then to plain yolov8m (detection-only).
        if weights_path is None:
            if os.path.exists(YOLO_POSE_WEIGHTS):
                weights_path = YOLO_POSE_WEIGHTS
                self.has_pose = True
                logger.info("Using YOLO pose model (preferred): %s", weights_path)
            elif os.path.exists(YOLO_POSE_WEIGHTS_FALLBACK):
                weights_path = YOLO_POSE_WEIGHTS_FALLBACK
                self.has_pose = True
                logger.info("Using YOLO pose model (fallback): %s", weights_path)
            else:
                weights_path = YOLO_WEIGHTS
                self.has_pose = False
                logger.info("No pose model found, using detection-only: %s", weights_path)
        else:
            self.has_pose = "pose" in weights_path
        self.model = YOLO(weights_path)
        # Detection-only model for far-baseline Pass 3. YOLOv8-pose suppresses
        # small detections (~30-40px) because keypoints can't be resolved at
        # that size. The detection-only model reliably detects people down to
        # ~20-30px. We only need bbox (not keypoints) for far-player assignment.
        self._det_model = None
        if os.path.exists(YOLO_WEIGHTS):
            self._det_model = YOLO(YOLO_WEIGHTS)
            logger.info("Loaded detection-only model for far-baseline pass: %s", YOLO_WEIGHTS)

        # SAHI tiled inference model — lazy init on first use.
        # Uses the detection-only model (yolov8m) for systematic small-object
        # detection via overlapping tiles. Replaces manual 3-pass when enabled.
        self._sahi_model = None
        if SAHI_ENABLED and os.path.exists(YOLO_WEIGHTS):
            try:
                from sahi import AutoDetectionModel
                self._sahi_model = AutoDetectionModel.from_pretrained(
                    model_type="yolov8",
                    model_path=YOLO_WEIGHTS,
                    confidence_threshold=SAHI_CONFIDENCE,
                    device=self.device,
                )
                logger.info("SAHI tiled inference enabled with %s", YOLO_WEIGHTS)
            except ImportError:
                logger.warning("SAHI not installed (pip install sahi), falling back to 3-pass")
            except Exception as e:
                logger.warning("SAHI init failed: %s, falling back to 3-pass", e)
        self._prev_players: Dict[int, tuple] = {}  # player_id → bbox from prev frame
        self.detections: List[PlayerDetection] = []
        self._last_result: List[PlayerDetection] = []
        self._detect_interval: int = PLAYER_DETECTION_INTERVAL
        self._last_detect_frame: int = -PLAYER_DETECTION_INTERVAL

        # Debug frame upload context — set by __main__.py for live S3 streaming
        self._debug_s3_client = None
        self._debug_s3_bucket = None
        self._debug_job_id = None

        # Diagnostics — counters only, reported via log_diagnostics(). Used to
        # diagnose the ball-boy / bench-sitter mis-mapping where a non-player
        # near the net gets locked into pid=1 because YOLO occasionally misses
        # the real far player. See commit log for detailed analysis.
        self._diag = {
            "frames_yolo_ran": 0,
            "candidates_total": 0,
            "candidates_hist": [0] * 7,          # buckets: 0, 1, 2, 3, 4, 5, 6+
            "choose2_kept_2": 0,
            "choose2_kept_1_span_fail": 0,       # 2+ cands, span too small → dropped top
            "choose2_kept_1_single": 0,          # only 1 candidate to begin with
            "choose2_kept_0": 0,
            "choose2_dropped_middle": 0,         # 3+ cands, dropped middle ones
        }

    def set_debug_upload_context(self, s3_client, s3_bucket: str, job_id: str) -> None:
        """Enable live debug frame upload to S3 (called from __main__.py)."""
        self._debug_s3_client = s3_client
        self._debug_s3_bucket = s3_bucket
        self._debug_job_id = job_id

    def detect_frame(
        self,
        frame: np.ndarray,
        frame_idx: int,
        court_bbox: Optional[tuple] = None,
        motion_mask: Optional[np.ndarray] = None,
        court_corners: Optional[list] = None,
    ) -> List[PlayerDetection]:
        """Detect players. Runs YOLO every N frames, reuses last result otherwise.

        Args:
            motion_mask: MOG2 foreground mask (same size as frame). 255 = foreground
                (moving), 0 = background (static). Used in _choose_two_players to
                prefer moving candidates over stationary ones in the far half.
            court_corners: 4 baseline corner pixel coords [(x,y),...] from court
                detector. Used for three-tier court-geometry scoring.
        """
        if (frame_idx - self._last_detect_frame) < self._detect_interval and self._last_result:
            # Reuse last detection with updated frame_idx
            reused = []
            for d in self._last_result:
                reused.append(PlayerDetection(
                    frame_idx=frame_idx, player_id=d.player_id,
                    bbox=d.bbox, center=d.center, keypoints=d.keypoints,
                ))
            self.detections.extend(reused)
            return reused
        self._last_detect_frame = frame_idx

        # ── Detection strategy: SAHI (systematic) or manual 3-pass (legacy) ──
        if SAHI_ENABLED and self._sahi_model is not None:
            # SAHI: systematic tiled inference that automatically handles
            # small distant objects. Replaces the manual 3-pass approach.
            sahi_boxes, sahi_kps = self._run_sahi(frame)
            # Also run full-frame YOLO for near player with pose keypoints
            full_boxes_list, full_kps_list = self._run_yolo(frame)
            all_boxes = full_boxes_list + sahi_boxes
            all_kps = full_kps_list + sahi_kps
        else:
            # ── Pass 1: Full-frame YOLO ──
            full_boxes_list, full_kps_list = self._run_yolo(frame)

            # ── Pass 2: Court-cropped + upscaled YOLO ──
            crop_boxes_list, crop_kps_list = [], []
            if YOLO_COURT_CROP_INFERENCE and court_bbox is not None:
                try:
                    crop_boxes_list, crop_kps_list = self._run_yolo_court_crop(frame, court_bbox)
                except Exception as e:
                    logger.warning("court-crop YOLO pass failed: %s", e)

            # ── Pass 3: Far-baseline dedicated crop ──
            far_boxes_list, far_kps_list = [], []
            try:
                far_boxes_list, far_kps_list = self._run_yolo_far_baseline(frame)
            except Exception as e:
                logger.warning("far-baseline YOLO pass failed: %s", e)

            all_boxes = full_boxes_list + crop_boxes_list + far_boxes_list
            all_kps = full_kps_list + crop_kps_list + far_kps_list

        # ── Deduplicate via IoU ──
        deduped_boxes, deduped_kps = self._dedupe_iou(all_boxes, all_kps, iou_thresh=0.5)
        n_yolo_boxes = len(deduped_boxes)
        # Log dedup details every 150 frames to diagnose far-player loss
        if frame_idx % 150 == 0:
            logger.info(
                "dedup_detail frame=%d: full=%d crop=%d far=%d → deduped=%d",
                frame_idx, len(full_boxes_list), len(crop_boxes_list),
                len(far_boxes_list), len(deduped_boxes),
            )
            for bi, (bx1, by1, bx2, by2) in enumerate(deduped_boxes):
                cy = (by1 + by2) / 2
                logger.info("  deduped[%d] box=(%.0f,%.0f,%.0f,%.0f) cy=%.0f",
                           bi, bx1, by1, bx2, by2, cy)

        # ── Court area filter ──
        # Reject detections far from the court (ball persons, spectators, umpires).
        # Only applies when court_bbox is reliable (we now use _last_good_detection).
        candidates = []
        candidate_kps = []
        n_filtered_out = 0
        skip_court_filter = (court_bbox is None) or (PLAYER_OUTSIDE_COURT_MARGIN_PX >= 1000)
        for bi, (x1, y1, x2, y2) in enumerate(deduped_boxes):
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            if not skip_court_filter:
                cb_x1, cb_y1, cb_x2, cb_y2 = court_bbox
                margin = PLAYER_OUTSIDE_COURT_MARGIN_PX
                if not (cb_x1 - margin <= cx <= cb_x2 + margin and
                        cb_y1 - margin <= cy <= cb_y2 + margin):
                    n_filtered_out += 1
                    continue
            candidates.append((float(x1), float(y1), float(x2), float(y2)))
            candidate_kps.append(deduped_kps[bi])

        # Diagnostic logging — every 30 frames
        if frame_idx % 30 == 0:
            logger.info(
                "player_tracker frame=%d full=%d crop=%d deduped=%d filtered_out=%d kept=%d",
                frame_idx, len(full_boxes_list), len(crop_boxes_list),
                n_yolo_boxes, n_filtered_out, len(candidates),
            )

        # Diagnostic accounting — one bucket per frame, by candidate count.
        self._diag["frames_yolo_ran"] += 1
        self._diag["candidates_total"] += len(candidates)
        self._diag["candidates_hist"][min(len(candidates), 6)] += 1

        if not candidates:
            self._diag["choose2_kept_0"] += 1
            return []

        # Always run _choose_two_players — previously gated on len>2, but the
        # 2-candidate case is exactly the bench-sitter mis-mapping failure
        # mode: when YOLO misses the real far player, {real_near, bench_sitter}
        # pass straight through to _assign_ids and bench_sitter gets locked
        # into pid=1. _choose_two_players now enforces a y-span check to
        # reject this case.
        candidates, candidate_kps = self._choose_two_players(
            candidates, candidate_kps, court_bbox, frame.shape[:2],
            motion_mask=motion_mask, court_corners=court_corners,
        )

        # Debug frame export AFTER _choose_two_players so the image shows
        # the true final kept set (bench sitter rejected, real players kept).
        # Previously drawn before the span check — misleadingly showed
        # rejected candidates as KEPT.
        # EARLY-RUN BIAS: dense sampling in first 600 frames so user can
        # verify mid-job and cancel bad runs without waiting full 35min.
        should_save = False
        if frame_idx > 0:
            if frame_idx <= 600 and frame_idx % 50 == 0:
                should_save = True
            elif DEBUG_FRAME_INTERVAL > 0 and frame_idx % DEBUG_FRAME_INTERVAL == 0:
                should_save = True

        if should_save:
            try:
                self._save_debug_frame_v2(
                    frame, frame_idx, deduped_boxes, candidates,
                )
            except Exception as e:
                logger.warning("debug frame save failed: %s", e)

        # Assign player_id via IoU matching with previous frame
        frame_detections = self._assign_ids(candidates, frame_idx, candidate_kps)
        self.detections.extend(frame_detections)
        self._last_result = frame_detections
        return frame_detections

    def _run_yolo(self, frame: np.ndarray, conf: float = None):
        """Run YOLO on a full frame. Returns (boxes_list, kps_list).

        boxes_list: list of (x1, y1, x2, y2) tuples in frame coordinates
        kps_list: list of (17, 3) numpy arrays or None per detection
        """
        confidence = conf or YOLO_CONFIDENCE
        if self.has_pose:
            results = self.model.predict(
                frame, conf=confidence, imgsz=YOLO_IMGSZ, verbose=False,
            )
        else:
            results = self.model.predict(
                frame, conf=confidence, imgsz=YOLO_IMGSZ,
                classes=[YOLO_PERSON_CLASS_ID], verbose=False,
            )
        boxes = results[0].boxes if results else []
        kps_data = results[0].keypoints if (results and self.has_pose) else None

        out_boxes = []
        out_kps = []
        for bi, box in enumerate(boxes):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            out_boxes.append((float(x1), float(y1), float(x2), float(y2)))
            if kps_data is not None and bi < len(kps_data.data):
                out_kps.append(kps_data.data[bi].cpu().numpy())
            else:
                out_kps.append(None)
        return out_boxes, out_kps

    def _run_yolo_court_crop(self, frame: np.ndarray, court_bbox: tuple):
        """Run YOLO on the court-cropped region. Returns (boxes_list, kps_list)
        with coordinates translated back to the FULL frame.

        Cropping focuses YOLO on the court area and effectively upscales
        distant players (since YOLO resizes the smaller crop to imgsz=1280
        instead of the full 1920x1080 frame).

        IMPORTANT: y1 is clamped to at most 10% of frame height. The court
        keypoint model often misses far-baseline keypoints (too small at
        camera distance), causing court_bbox to only cover the near court.
        Without this clamp, the crop excludes the far baseline entirely —
        the far player is never seen by the crop pass. The 10% floor
        ensures the far baseline area is always included.
        """
        cb_x1, cb_y1, cb_x2, cb_y2 = court_bbox
        h, w = frame.shape[:2]
        margin = YOLO_COURT_CROP_MARGIN_PX
        x1 = max(0, int(cb_x1 - margin))
        # Clamp y1 to at most 10% from top — guarantees far baseline is in crop
        y1_from_bbox = max(0, int(cb_y1 - margin))
        y1 = min(y1_from_bbox, int(h * 0.10))
        x2 = min(w, int(cb_x2 + margin))
        y2 = min(h, int(cb_y2 + margin))

        if x2 <= x1 or y2 <= y1:
            return [], []

        cropped = frame[y1:y2, x1:x2]
        if cropped.size == 0:
            return [], []

        crop_boxes, crop_kps = self._run_yolo(cropped, conf=YOLO_COURT_CROP_CONFIDENCE)

        # Translate crop coords → full frame coords
        out_boxes = []
        out_kps = []
        for (cx1, cy1, cx2, cy2), kp in zip(crop_boxes, crop_kps):
            out_boxes.append((cx1 + x1, cy1 + y1, cx2 + x1, cy2 + y1))
            if kp is not None:
                kp_shifted = kp.copy()
                kp_shifted[:, 0] += x1
                kp_shifted[:, 1] += y1
                out_kps.append(kp_shifted)
            else:
                out_kps.append(None)
        return out_boxes, out_kps

    def _run_yolo_far_baseline(self, frame: np.ndarray):
        """Run DETECTION-ONLY YOLO on a tight crop of the far-baseline area.

        KEY INSIGHT (from research): YOLOv8-pose SUPPRESSES small detections
        (~30-40px) because the pose NMS requires resolvable keypoints. The
        far player is 30-40px — well above the detection-only floor (~20px)
        but below the pose floor (~60-80px). Using yolov8m (detection-only)
        instead of yolov8x-pose for this pass is the fix.

        Crop: top 28% height × central 70% width, conf=0.15.
        Confidence raised from 0.05 to 0.15 because the detection-only model
        produces higher-confidence detections on small people (no keypoint
        suppression). This reduces false positives from the aggressive 0.05.
        """
        if self._det_model is None:
            return [], []

        h, w = frame.shape[:2]
        y1 = 0
        y2 = int(h * 0.28)
        x1 = int(w * 0.15)
        x2 = int(w * 0.85)

        if y2 <= y1 or x2 <= x1:
            return [], []

        cropped = frame[y1:y2, x1:x2]
        if cropped.size == 0:
            return [], []

        # Use detection-only model (yolov8m) — NOT the pose model
        results = self._det_model.predict(
            cropped, conf=0.15, imgsz=YOLO_IMGSZ,
            classes=[YOLO_PERSON_CLASS_ID], verbose=False,
        )
        boxes = results[0].boxes if results else []
        crop_boxes = []
        crop_kps = []
        for box in boxes:
            bx1, by1, bx2, by2 = box.xyxy[0].cpu().numpy()
            crop_boxes.append((float(bx1), float(by1), float(bx2), float(by2)))
            crop_kps.append(None)  # no keypoints from detection-only model
        # Log every detection with coordinates — need to see whether the
        # far player is detected but lost in dedup, or genuinely not seen.
        for bi, (bx1, by1, bx2, by2) in enumerate(crop_boxes):
            bw, bh = bx2 - bx1, by2 - by1
            logger.info(
                "far_baseline_pass: det[%d] crop_box=(%.0f,%.0f,%.0f,%.0f) "
                "size=%.0fx%.0f frame_box=(%.0f,%.0f,%.0f,%.0f)",
                bi, bx1, by1, bx2, by2, bw, bh,
                bx1 + x1, by1 + y1, bx2 + x1, by2 + y1,
            )
        logger.info(
            "far_baseline_pass: crop=%dx%d found=%d conf=0.05",
            x2 - x1, y2, len(crop_boxes),
        )

        # Translate crop coords → full frame coords
        out_boxes = []
        out_kps = []
        for (cx1, cy1, cx2, cy2), kp in zip(crop_boxes, crop_kps):
            out_boxes.append((cx1 + x1, cy1 + y1, cx2 + x1, cy2 + y1))
            if kp is not None:
                kp_shifted = kp.copy()
                kp_shifted[:, 0] += x1
                kp_shifted[:, 1] += y1
                out_kps.append(kp_shifted)
            else:
                out_kps.append(None)
        return out_boxes, out_kps

    def _run_sahi(self, frame: np.ndarray):
        """Run SAHI tiled inference for systematic small-object person detection.

        SAHI slices the frame into overlapping 416×416 tiles, runs YOLO on each
        tile independently, then merges results via NMS. This gives the far
        player (~30-40px in 1080p) much higher resolution within its tile than
        full-frame inference provides.

        Returns (boxes_list, kps_list) in full-frame coordinates.
        """
        if self._sahi_model is None:
            return [], []

        try:
            from sahi.predict import get_sliced_prediction

            result = get_sliced_prediction(
                frame,
                self._sahi_model,
                slice_height=SAHI_SLICE_HEIGHT,
                slice_width=SAHI_SLICE_WIDTH,
                overlap_height_ratio=SAHI_OVERLAP_RATIO,
                overlap_width_ratio=SAHI_OVERLAP_RATIO,
                postprocess_type=SAHI_POSTPROCESS_TYPE,
                postprocess_match_threshold=SAHI_POSTPROCESS_MATCH_THRESHOLD,
                verbose=0,
            )

            boxes = []
            kps = []
            for pred in result.object_prediction_list:
                # Filter to person class only (COCO class 0)
                if pred.category.id != YOLO_PERSON_CLASS_ID:
                    continue
                bbox = pred.bbox
                x1, y1, x2, y2 = bbox.minx, bbox.miny, bbox.maxx, bbox.maxy
                boxes.append((float(x1), float(y1), float(x2), float(y2)))
                kps.append(None)  # SAHI uses detection-only, no keypoints

            logger.debug("sahi_pass: found %d persons in %d tiles",
                        len(boxes), len(result.object_prediction_list))
            return boxes, kps

        except Exception as e:
            logger.warning("SAHI inference failed: %s", e)
            return [], []

    @staticmethod
    def _compute_motion_ratio(box: tuple, motion_mask: np.ndarray) -> float:
        """Compute fraction of bbox pixels that are foreground (moving) in the MOG2 mask.

        Returns 0.0-1.0. A moving player typically scores 0.05-0.15;
        a seated spectator scores 0.00-0.01.
        """
        mask_h, mask_w = motion_mask.shape[:2]
        x1 = max(0, int(box[0]))
        y1 = max(0, int(box[1]))
        x2 = min(mask_w, int(box[2]))
        y2 = min(mask_h, int(box[3]))
        if x2 <= x1 or y2 <= y1:
            return 0.0
        roi = motion_mask[y1:y2, x1:x2]
        total_pixels = roi.size
        if total_pixels == 0:
            return 0.0
        # MOG2 mask: 255 = foreground, 0 = background
        fg_pixels = int((roi > 127).sum())
        return fg_pixels / total_pixels

    def _dedupe_iou(self, boxes_list, kps_list, iou_thresh: float = 0.5):
        """Remove overlapping boxes via greedy IoU deduplication.

        When the full-frame and crop passes both detect the same player, we
        get duplicates. Keeps the FIRST occurrence, drops subsequent boxes
        with IoU > iou_thresh.
        """
        if not boxes_list:
            return [], []
        kept_boxes = []
        kept_kps = []
        for box, kp in zip(boxes_list, kps_list):
            duplicate = False
            for existing in kept_boxes:
                if self._compute_iou(box, existing) > iou_thresh:
                    duplicate = True
                    break
            if not duplicate:
                kept_boxes.append(box)
                kept_kps.append(kp)
        return kept_boxes, kept_kps

    def _save_debug_frame_v2(self, frame, frame_idx: int, all_boxes, kept_boxes) -> None:
        """Save a frame with YOLO bboxes drawn on it. Uploads to S3 immediately
        if upload context is set, so the user can inspect mid-run.

        Draws ALL detections (red = filtered, green = kept).
        """
        os.makedirs(DEBUG_FRAMES_DIR, exist_ok=True)
        img = frame.copy()
        kept_set = set(
            (round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1))
            for b in kept_boxes
        )
        for box in all_boxes:
            x1, y1, x2, y2 = [int(v) for v in box]
            key = (round(box[0], 1), round(box[1], 1), round(box[2], 1), round(box[3], 1))
            color = (0, 255, 0) if key in kept_set else (0, 0, 255)
            label = "KEPT" if key in kept_set else "FILTER"
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)
            cv2.putText(
                img, label, (x1, y1 - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2,
            )
        header = (f"frame={frame_idx} all={len(all_boxes)} kept={len(kept_boxes)} "
                  f"crop_inf={YOLO_COURT_CROP_INFERENCE} imgsz={YOLO_IMGSZ}")
        cv2.putText(
            img, header, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2,
        )
        fname = f"frame_{frame_idx:06d}_n{len(kept_boxes)}.jpg"
        out_path = os.path.join(DEBUG_FRAMES_DIR, fname)
        cv2.imwrite(out_path, img)
        logger.info("debug frame saved: %s (kept=%d/%d)",
                     out_path, len(kept_boxes), len(all_boxes))

        # Upload directly to S3 if context is set (LIVE upload, no waiting
        # for post-processing). User can inspect mid-run and cancel bad jobs.
        if (self._debug_s3_client is not None and
                self._debug_s3_bucket and self._debug_job_id):
            try:
                s3_key = f"debug/{self._debug_job_id}/{fname}"
                self._debug_s3_client.upload_file(
                    out_path, self._debug_s3_bucket, s3_key,
                    ExtraArgs={"ContentType": "image/jpeg"},
                )
                logger.info("debug frame uploaded: s3://%s/%s",
                             self._debug_s3_bucket, s3_key)
            except Exception as e:
                logger.warning("debug frame S3 upload failed: %s", e)

    def _save_debug_frame(self, frame, frame_idx: int, boxes) -> None:
        """Save a frame with YOLO bboxes drawn on it for visual debugging.

        Output: /tmp/debug_frames/frame_{idx:06d}_n{count}.jpg
        Uploaded to S3 by __main__.py post-processing.
        """
        os.makedirs(DEBUG_FRAMES_DIR, exist_ok=True)
        img = frame.copy()
        n_boxes = len(boxes)
        for box in boxes:
            try:
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                conf = float(box.conf[0].cpu().numpy())
                # Color: green if conf >= 0.5, yellow otherwise
                color = (0, 255, 0) if conf >= 0.5 else (0, 255, 255)
                cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), color, 3)
                cv2.putText(
                    img, f"{conf:.2f}", (int(x1), int(y1) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2,
                )
            except Exception:
                continue
        # Header banner
        header = f"frame={frame_idx} yolo_boxes={n_boxes} conf>={YOLO_CONFIDENCE} imgsz={YOLO_IMGSZ}"
        cv2.putText(
            img, header, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2,
        )
        out_path = os.path.join(
            DEBUG_FRAMES_DIR, f"frame_{frame_idx:06d}_n{n_boxes}.jpg"
        )
        cv2.imwrite(out_path, img)
        logger.info("debug frame saved: %s (n_boxes=%d)", out_path, n_boxes)

    def _choose_two_players(self, candidates: list, candidate_kps: list,
                            court_bbox, frame_shape,
                            motion_mask: Optional[np.ndarray] = None,
                            court_corners: Optional[list] = None) -> tuple:
        """Select up to 2 players using three-tier court-geometry scoring.

        THREE-TIER PRIORITY (per half — near and far scored independently):
          Tier 1 (3000): INSIDE the court quadrilateral — strongest signal
          Tier 2 (2000): Behind the baseline, within sideline extensions —
                         take closest to baseline
          Tier 3 (1000): Near the sideline corridor (baseline-to-net) —
                         covers players running wide

        Within each tier, MOG2 motion adds +500 bonus (moving > stationary).
        Final tiebreaker: proximity to court center.

        Falls back to centering + motion heuristic when court corners unavailable.

        One candidate selected from each half (far/near) of the frame.
        """
        MIN_Y_SEPARATION_RATIO = 0.35

        if len(candidates) == 0:
            self._diag["choose2_kept_0"] += 1
            return [], []

        if len(candidates) == 1:
            self._diag["choose2_kept_1_single"] += 1
            return candidates, candidate_kps

        frame_h = frame_shape[0]
        frame_w = frame_shape[1] if len(frame_shape) > 1 else 1920
        min_span_px = MIN_Y_SEPARATION_RATIO * frame_h
        midline_y = frame_h / 2

        # Build court polygon and geometry if corners are available
        court_poly = None       # cv2 polygon for point-in-court test
        far_baseline_y = None   # pixel y of far baseline (top of court)
        near_baseline_y = None  # pixel y of near baseline (bottom of court)
        left_sideline_x = None  # left boundary
        right_sideline_x = None # right boundary
        court_center_x = frame_w / 2  # fallback

        if court_corners is not None and len(court_corners) == 4:
            # corners: [far_left, far_right, near_left, near_right]
            fl, fr, nl, nr = court_corners
            court_poly = np.array([fl, fr, nr, nl], dtype=np.float32)
            far_baseline_y = (fl[1] + fr[1]) / 2
            near_baseline_y = (nl[1] + nr[1]) / 2
            left_sideline_x = min(fl[0], nl[0])
            right_sideline_x = max(fr[0], nr[0])
            court_center_x = (left_sideline_x + right_sideline_x) / 2
            # Extend sidelines by 15% for "near sideline" corridor (wide runs)
            sideline_margin = (right_sideline_x - left_sideline_x) * 0.15
            # Extend baselines by 20% of court depth for "behind baseline" zone
            court_depth = abs(near_baseline_y - far_baseline_y)
            baseline_margin = court_depth * 0.20

        scored = []
        for box, kps in zip(candidates, candidate_kps):
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            y2 = box[3]  # bottom of bbox = feet
            half = "far" if cy < midline_y else "near"

            # Compute motion ratio from MOG2 foreground mask
            motion_ratio = 0.0
            if motion_mask is not None:
                motion_ratio = self._compute_motion_ratio(box, motion_mask)
            motion_bonus = 500 if motion_ratio >= MOG2_MIN_MOTION_RATIO else 0

            # ── Court-geometry scoring (when court corners available) ────
            if court_poly is not None:
                # Point-in-polygon test using feet position (cx, y2)
                in_court = cv2.pointPolygonTest(court_poly, (cx, y2), False) >= 0

                # Behind baseline check: within sideline extensions, behind baseline
                in_sideline_band = (left_sideline_x - sideline_margin <= cx
                                    <= right_sideline_x + sideline_margin)

                if half == "far":
                    behind_baseline = (y2 < far_baseline_y and
                                       y2 >= far_baseline_y - baseline_margin and
                                       in_sideline_band)
                    near_sideline = (not in_court and not behind_baseline and
                                     in_sideline_band and
                                     far_baseline_y <= cy <= near_baseline_y)
                else:
                    behind_baseline = (y2 > near_baseline_y and
                                       y2 <= near_baseline_y + baseline_margin and
                                       in_sideline_band)
                    near_sideline = (not in_court and not behind_baseline and
                                     in_sideline_band and
                                     far_baseline_y <= cy <= near_baseline_y)

                if in_court:
                    tier = 3000
                elif behind_baseline:
                    tier = 2000
                elif near_sideline:
                    tier = 1000
                else:
                    tier = 0  # off-court, off-sideline — spectator/bench

                # Tiebreaker 1: Bbox area — the actual player is CLOSER to the
                # camera than a spectator behind them, so their bbox is BIGGER.
                # Normalise to 0-200 range (a 50×100px box scores ~100).
                bbox_w = box[2] - box[0]
                bbox_h = box[3] - box[1]
                bbox_area = bbox_w * bbox_h
                bbox_score = min(200, bbox_area / 25.0)  # 5000px² → 200

                # Tiebreaker 2: Proximity to court CENTER LINE (midpoint of
                # sidelines). Players stand near the center line; spectators
                # sit on the sidelines. Normalise to 0-100.
                court_width = right_sideline_x - left_sideline_x
                if court_width > 0:
                    center_dist = abs(cx - court_center_x) / (court_width / 2)
                    proximity = max(0, 1.0 - center_dist) * 100
                else:
                    proximity = 0

                score = tier + motion_bonus + bbox_score + proximity

            else:
                # ── Fallback: centering + motion (no court geometry) ────
                x_offset = abs(cx - court_center_x) / frame_w
                centering = max(0.0, 1.0 - x_offset / 0.30) * 500
                score = motion_bonus + centering + y2

            scored.append((score, cy, half, box, kps, motion_ratio))

        # Pick best (highest score) from each half
        far_candidates = [s for s in scored if s[2] == "far"]
        near_candidates = [s for s in scored if s[2] == "near"]
        far_candidates.sort(key=lambda s: s[0], reverse=True)
        near_candidates.sort(key=lambda s: s[0], reverse=True)

        best_far = far_candidates[0] if far_candidates else None
        best_near = near_candidates[0] if near_candidates else None

        if best_far:
            logger.debug(
                "_choose_two_players: best_far cy=%.0f feet_y=%.0f score=%.0f motion=%.3f",
                best_far[1], best_far[3][3], best_far[0], best_far[5],
            )
            # Log all far candidates with motion ratios every 150 frames
            if len(far_candidates) > 1:
                self._diag["far_multi_candidate_frames"] = self._diag.get("far_multi_candidate_frames", 0) + 1
                # Log top 3 far candidates for debugging
                for i, fc in enumerate(far_candidates[:3]):
                    logger.debug(
                        "  far_cand[%d] cy=%.0f y2=%.0f motion=%.3f score=%.0f",
                        i, fc[1], fc[3][3], fc[5], fc[0],
                    )
        if best_near:
            logger.debug(
                "_choose_two_players: best_near cy=%.0f score=%.0f",
                best_near[1], best_near[0],
            )

        chosen = []
        if best_far:
            chosen.append(best_far)
        if best_near:
            chosen.append(best_near)

        if len(chosen) == 2:
            span = abs(chosen[0][1] - chosen[1][1])
            if span >= min_span_px:
                if len(candidates) > 2:
                    self._diag["choose2_dropped_middle"] += 1
                self._diag["choose2_kept_2"] += 1
                logger.debug(
                    "_choose_two_players: kept 2, far_cy=%.1f near_cy=%.1f "
                    "span=%.1f midline=%.1f far_motion=%.3f (from %d)",
                    best_far[1], best_near[1], span, midline_y,
                    best_far[5], len(candidates),
                )
                return [c[3] for c in chosen], [c[4] for c in chosen]
            else:
                self._diag["choose2_kept_1_span_fail"] += 1
                logger.debug(
                    "_choose_two_players: span=%.1f < min=%.1f, dropping far "
                    "(far_cy=%.1f near_cy=%.1f midline=%.1f, from %d cands)",
                    span, min_span_px, best_far[1], best_near[1],
                    midline_y, len(candidates),
                )
                return [best_near[3]], [best_near[4]]

        # Only one half has candidates
        pick = chosen[0] if chosen else None
        if pick:
            self._diag["choose2_kept_1_single"] += 1
            return [pick[3]], [pick[4]]

        self._diag["choose2_kept_0"] += 1
        return [], []

    def _assign_ids(self, bboxes: list, frame_idx: int,
                    kps_list: list = None) -> List[PlayerDetection]:
        """Assign player_id 0/1 consistently across frames using IoU."""
        if kps_list is None:
            kps_list = [None] * len(bboxes)

        if not self._prev_players:
            # First detection: assign by vertical position (higher y = near-side = player 0)
            paired = list(zip(bboxes, kps_list))
            paired.sort(key=lambda p: (p[0][1] + p[0][3]) / 2, reverse=True)
            results = []
            for i, (bbox, kps) in enumerate(paired[:2]):
                pid = i
                cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                det = PlayerDetection(
                    frame_idx=frame_idx, player_id=pid,
                    bbox=bbox, center=(cx, cy), keypoints=kps,
                )
                results.append(det)
                self._prev_players[pid] = bbox
            return results

        # Match via IoU
        assignments = {}
        used_pids = set()
        used_bboxes = set()

        # Greedy matching: best IoU first
        pairs = []
        for pid, prev_bbox in self._prev_players.items():
            for bi, bbox in enumerate(bboxes):
                iou = self._compute_iou(prev_bbox, bbox)
                pairs.append((iou, pid, bi))
        pairs.sort(reverse=True)

        for iou, pid, bi in pairs:
            if pid in used_pids or bi in used_bboxes:
                continue
            if iou >= PLAYER_IOU_THRESHOLD:
                assignments[bi] = pid
                used_pids.add(pid)
                used_bboxes.add(bi)

        # Assign unmatched bboxes to remaining player IDs
        available_pids = [p for p in range(2) if p not in used_pids]
        for bi in range(len(bboxes)):
            if bi not in used_bboxes and available_pids:
                assignments[bi] = available_pids.pop(0)

        results = []
        new_prev = {}
        for bi, pid in assignments.items():
            bbox = bboxes[bi]
            cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            det = PlayerDetection(
                frame_idx=frame_idx, player_id=pid,
                bbox=bbox, center=(cx, cy), keypoints=kps_list[bi],
            )
            results.append(det)
            new_prev[pid] = bbox

        self._prev_players = new_prev
        return results

    @staticmethod
    def _compute_iou(box1: tuple, box2: tuple) -> float:
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2])
        y2 = min(box1[3], box2[3])
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
        area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
        union = area1 + area2 - inter
        return inter / union if union > 0 else 0.0

    def map_to_court(self, court_detector):
        """Map all player detections to court coordinates."""
        for det in self.detections:
            coords = court_detector.to_court_coords(det.center[0], det.center[1])
            if coords is not None:
                det.court_x, det.court_y = coords

    def log_diagnostics(self):
        """Dump cumulative player-detection diagnostics. Call once post-inference.

        What to look for:
        - candidates_hist: if the '2' bucket is huge and '3+' is small, YOLO
          is missing the far player often — which is exactly when the
          bench-sitter mis-mapping triggers.
        - choose2_kept_1_span_fail: how many frames the new y-span check
          rejected a bench-sitter-like scenario. If > 0 on a run that
          previously had ball-boy mis-mapping, the fix is firing.
        - choose2_kept_2 should dominate; kept_0 should be near zero.
        - choose2_dropped_middle shows how often 3+ candidates were culled.
        """
        d = self._diag
        total = d["frames_yolo_ran"]
        if total == 0:
            logger.info("PlayerTracker diagnostics: no frames inferred")
            return

        def pct(n):
            return 100.0 * n / total

        logger.info("=== PlayerTracker diagnostics ===")
        logger.info("frames_yolo_ran: %d", total)
        logger.info("avg candidates/frame: %.2f", d["candidates_total"] / total)
        logger.info("candidates_hist:")
        for i, c in enumerate(d["candidates_hist"]):
            label = str(i) if i < 6 else "6+"
            logger.info("  %s: %6d (%5.1f%%)", label, c, pct(c))
        logger.info("_choose_two_players outcomes:")
        logger.info("  kept_2:                %6d (%5.1f%%)", d["choose2_kept_2"], pct(d["choose2_kept_2"]))
        logger.info("  kept_1_span_fail:      %6d (%5.1f%%)", d["choose2_kept_1_span_fail"], pct(d["choose2_kept_1_span_fail"]))
        logger.info("  kept_1_single_cand:    %6d (%5.1f%%)", d["choose2_kept_1_single"], pct(d["choose2_kept_1_single"]))
        logger.info("  kept_0:                %6d (%5.1f%%)", d["choose2_kept_0"], pct(d["choose2_kept_0"]))
        logger.info("  dropped_middle (3+):   %6d (%5.1f%%)", d["choose2_dropped_middle"], pct(d["choose2_dropped_middle"]))
        logger.info("  far_multi_cand_frames: %6d", d.get("far_multi_candidate_frames", 0))

    def reset(self):
        self._prev_players.clear()
        self.detections.clear()
        for k in self._diag:
            if isinstance(self._diag[k], list):
                self._diag[k] = [0] * len(self._diag[k])
            else:
                self._diag[k] = 0
