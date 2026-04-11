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
    YOLO_IMGSZ,
    YOLO_COURT_CROP_INFERENCE,
    YOLO_COURT_CROP_MARGIN_PX,
    YOLO_PERSON_CLASS_ID,
    PLAYER_IOU_THRESHOLD,
    PLAYER_COURT_MARGIN_PX,
    PLAYER_OUTSIDE_COURT_MARGIN_PX,
    PLAYER_DETECTION_INTERVAL,
    DEBUG_FRAME_INTERVAL,
)

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
        self._prev_players: Dict[int, tuple] = {}  # player_id → bbox from prev frame
        self.detections: List[PlayerDetection] = []
        self._last_result: List[PlayerDetection] = []
        self._detect_interval: int = PLAYER_DETECTION_INTERVAL
        self._last_detect_frame: int = -PLAYER_DETECTION_INTERVAL

    def detect_frame(
        self,
        frame: np.ndarray,
        frame_idx: int,
        court_bbox: Optional[tuple] = None,
    ) -> List[PlayerDetection]:
        """Detect players. Runs YOLO every N frames, reuses last result otherwise."""
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

        # ── Pass 1: Full-frame YOLO ──
        # Catches close/foreground players easily
        full_boxes_list, full_kps_list = self._run_yolo(frame)

        # ── Pass 2: Court-cropped + upscaled YOLO ──
        # Catches DISTANT players by giving them more pixels.
        # We crop to the court region (with margin), then YOLO's internal
        # letterboxing upscales it to imgsz=1280, making the far player
        # 2-3x bigger pixel-wise than they are in the full frame.
        crop_boxes_list, crop_kps_list = [], []
        if YOLO_COURT_CROP_INFERENCE and court_bbox is not None:
            try:
                crop_boxes_list, crop_kps_list = self._run_yolo_court_crop(frame, court_bbox)
            except Exception as e:
                logger.warning("court-crop YOLO pass failed: %s", e)

        # ── Combine all detections (full + crop), deduplicate via IoU ──
        all_boxes = full_boxes_list + crop_boxes_list
        all_kps = full_kps_list + crop_kps_list
        deduped_boxes, deduped_kps = self._dedupe_iou(all_boxes, all_kps, iou_thresh=0.5)
        n_yolo_boxes = len(deduped_boxes)

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

        # Debug frame export — saves a sampled frame with YOLO bboxes drawn on it
        if DEBUG_FRAME_INTERVAL > 0 and frame_idx % DEBUG_FRAME_INTERVAL == 0:
            try:
                self._save_debug_frame_v2(
                    frame, frame_idx, deduped_boxes, candidates,
                )
            except Exception as e:
                logger.warning("debug frame save failed: %s", e)

        if not candidates:
            return []

        # Pick the 2 players closest to the court (by bbox area = closer player is bigger)
        if len(candidates) > 2:
            candidates, candidate_kps = self._choose_two_players(
                candidates, candidate_kps, court_bbox, frame.shape[:2],
            )

        # Assign player_id via IoU matching with previous frame
        frame_detections = self._assign_ids(candidates, frame_idx, candidate_kps)
        self.detections.extend(frame_detections)
        self._last_result = frame_detections
        return frame_detections

    def _run_yolo(self, frame: np.ndarray):
        """Run YOLO on a full frame. Returns (boxes_list, kps_list).

        boxes_list: list of (x1, y1, x2, y2) tuples in frame coordinates
        kps_list: list of (17, 3) numpy arrays or None per detection
        """
        if self.has_pose:
            results = self.model.predict(
                frame, conf=YOLO_CONFIDENCE, imgsz=YOLO_IMGSZ, verbose=False,
            )
        else:
            results = self.model.predict(
                frame, conf=YOLO_CONFIDENCE, imgsz=YOLO_IMGSZ,
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
        """
        cb_x1, cb_y1, cb_x2, cb_y2 = court_bbox
        h, w = frame.shape[:2]
        margin = YOLO_COURT_CROP_MARGIN_PX
        x1 = max(0, int(cb_x1 - margin))
        y1 = max(0, int(cb_y1 - margin))
        x2 = min(w, int(cb_x2 + margin))
        y2 = min(h, int(cb_y2 + margin))

        if x2 <= x1 or y2 <= y1:
            return [], []

        cropped = frame[y1:y2, x1:x2]
        if cropped.size == 0:
            return [], []

        crop_boxes, crop_kps = self._run_yolo(cropped)

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
        """Save a frame with YOLO bboxes drawn on it for visual debugging.

        Draws ALL detections (red = filtered out as outside court, green = kept).
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
        out_path = os.path.join(
            DEBUG_FRAMES_DIR, f"frame_{frame_idx:06d}_n{len(kept_boxes)}.jpg"
        )
        cv2.imwrite(out_path, img)
        logger.info("debug frame saved: %s (kept=%d/%d)",
                     out_path, len(kept_boxes), len(all_boxes))

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
                            court_bbox, frame_shape) -> tuple:
        """Select up to 2 players: one closest to top of frame, one closest to bottom.

        BASELINE-SEEKING strategy. Tennis camera angles always show the two
        players at opposite vertical extremes of the frame:
          - Far player → small pixel y (top of frame, far baseline)
          - Near player → large pixel y (bottom of frame, near baseline)

        Ball persons / spectators / umpires / scoreboards are typically in the
        MIDDLE horizontal band (around the net level). Picking the two y-extremes
        naturally excludes them.

        Edge case: if all candidates are clustered at similar y (no meaningful
        separation), fall back to picking by bbox area (closer/bigger = more
        likely to be a real player).
        """
        if len(candidates) <= 2:
            return candidates, candidate_kps

        # Tag each candidate with its bbox center y
        paired = []
        for box, kps in zip(candidates, candidate_kps):
            cy = (box[1] + box[3]) / 2
            paired.append((cy, box, kps))

        # Sort by cy (top of frame first)
        paired.sort(key=lambda p: p[0])

        # Need a meaningful y-spread between top and bottom candidates,
        # otherwise they're on the same side of the court.
        MIN_Y_SEPARATION_PX = 100
        top_cand = paired[0]
        bot_cand = paired[-1]

        if (bot_cand[0] - top_cand[0]) >= MIN_Y_SEPARATION_PX:
            # Pick the y-extremes (top + bottom of frame)
            chosen = [top_cand, bot_cand]
            logger.debug(
                "_choose_two_players: baseline-seek picked top_cy=%.1f bot_cy=%.1f from %d candidates",
                top_cand[0], bot_cand[0], len(candidates),
            )
        else:
            # All candidates clustered on the same side — fall back to area
            # (largest bboxes = closest to camera = most likely real players)
            paired_by_area = sorted(
                paired,
                key=lambda p: (p[1][2] - p[1][0]) * (p[1][3] - p[1][1]),
                reverse=True,
            )
            chosen = paired_by_area[:2]
            logger.debug(
                "_choose_two_players: y-cluster (separation=%.1f), fell back to bbox area",
                bot_cand[0] - top_cand[0],
            )

        return [c[1] for c in chosen], [c[2] for c in chosen]

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

    def reset(self):
        self._prev_players.clear()
        self.detections.clear()
