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

from ml_pipeline.config import (
    YOLO_WEIGHTS,
    YOLO_POSE_WEIGHTS,
    YOLO_CONFIDENCE,
    YOLO_PERSON_CLASS_ID,
    PLAYER_IOU_THRESHOLD,
    PLAYER_COURT_MARGIN_PX,
    PLAYER_DETECTION_INTERVAL,
)

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
        # Prefer pose model if available, fall back to detection-only
        if weights_path is None:
            if os.path.exists(YOLO_POSE_WEIGHTS):
                weights_path = YOLO_POSE_WEIGHTS
                self.has_pose = True
                logger.info("Using YOLOv8-pose model: %s", weights_path)
            else:
                weights_path = YOLO_WEIGHTS
                self.has_pose = False
                logger.info("Pose model not found, using detection-only: %s", weights_path)
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
        if self.has_pose:
            results = self.model.predict(frame, conf=YOLO_CONFIDENCE, verbose=False)
        else:
            results = self.model.predict(
                frame, conf=YOLO_CONFIDENCE, classes=[YOLO_PERSON_CLASS_ID],
                verbose=False,
            )
        boxes = results[0].boxes if results else []
        kps_data = results[0].keypoints if (results and self.has_pose) else None

        candidates = []
        candidate_kps = []
        for bi, box in enumerate(boxes):
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            # Filter: must be within court bbox + margin
            if court_bbox is not None:
                cb_x1, cb_y1, cb_x2, cb_y2 = court_bbox
                if not (cb_x1 - PLAYER_COURT_MARGIN_PX <= cx <= cb_x2 + PLAYER_COURT_MARGIN_PX and
                        cb_y1 - PLAYER_COURT_MARGIN_PX <= cy <= cb_y2 + PLAYER_COURT_MARGIN_PX):
                    continue
            candidates.append((float(x1), float(y1), float(x2), float(y2)))
            if kps_data is not None and bi < len(kps_data.data):
                candidate_kps.append(kps_data.data[bi].cpu().numpy())  # (17, 3)
            else:
                candidate_kps.append(None)

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

    def _choose_two_players(self, candidates: list, candidate_kps: list,
                            court_bbox, frame_shape) -> tuple:
        """Select 2 most likely players from candidates. Returns (bboxes, kps)."""
        # Build paired list so keypoints stay aligned with bboxes
        paired = list(zip(candidates, candidate_kps))
        if court_bbox is not None:
            court_cx = (court_bbox[0] + court_bbox[2]) / 2
            court_cy = (court_bbox[1] + court_bbox[3]) / 2
            paired.sort(key=lambda p: np.hypot(
                (p[0][0] + p[0][2]) / 2 - court_cx,
                (p[0][1] + p[0][3]) / 2 - court_cy,
            ))
        else:
            paired.sort(key=lambda p: (p[0][2] - p[0][0]) * (p[0][3] - p[0][1]), reverse=True)
        top2 = paired[:2]
        return [p[0] for p in top2], [p[1] for p in top2]

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
