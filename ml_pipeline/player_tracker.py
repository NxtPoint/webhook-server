"""
PlayerTracker — YOLOv8 person detection + court filtering + IoU tracking.
Assigns consistent player_id (0 = near-side, 1 = far-side) across frames.
"""

import numpy as np
from ultralytics import YOLO
from dataclasses import dataclass
from typing import Optional, List, Dict

from ml_pipeline.config import (
    YOLO_WEIGHTS,
    YOLO_CONFIDENCE,
    YOLO_PERSON_CLASS_ID,
    PLAYER_IOU_THRESHOLD,
    PLAYER_COURT_MARGIN_PX,
    PLAYER_DETECTION_INTERVAL,
)


@dataclass
class PlayerDetection:
    frame_idx: int
    player_id: int          # 0 = near-side player, 1 = far-side player
    bbox: tuple             # (x1, y1, x2, y2) pixel coordinates
    center: tuple           # (cx, cy) pixel center
    court_x: Optional[float] = None  # metres
    court_y: Optional[float] = None  # metres


class PlayerTracker:
    def __init__(self, weights_path: str = YOLO_WEIGHTS, device: str = None):
        self.device = device or ("cuda:0" if __import__("torch").cuda.is_available() else "cpu")
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
                    bbox=d.bbox, center=d.center,
                ))
            self.detections.extend(reused)
            return reused
        self._last_detect_frame = frame_idx
        results = self.model.predict(
            frame, conf=YOLO_CONFIDENCE, classes=[YOLO_PERSON_CLASS_ID],
            verbose=False,
        )
        boxes = results[0].boxes if results else []

        candidates = []
        for box in boxes:
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            # Filter: must be within court bbox + margin
            if court_bbox is not None:
                cb_x1, cb_y1, cb_x2, cb_y2 = court_bbox
                if not (cb_x1 - PLAYER_COURT_MARGIN_PX <= cx <= cb_x2 + PLAYER_COURT_MARGIN_PX and
                        cb_y1 - PLAYER_COURT_MARGIN_PX <= cy <= cb_y2 + PLAYER_COURT_MARGIN_PX):
                    continue
            candidates.append((float(x1), float(y1), float(x2), float(y2)))

        if not candidates:
            return []

        # Pick the 2 players closest to the court (by bbox area = closer player is bigger)
        if len(candidates) > 2:
            candidates = self._choose_two_players(candidates, court_bbox, frame.shape[:2])

        # Assign player_id via IoU matching with previous frame
        frame_detections = self._assign_ids(candidates, frame_idx)
        self.detections.extend(frame_detections)
        self._last_result = frame_detections
        return frame_detections

    def _choose_two_players(self, candidates: list, court_bbox, frame_shape) -> list:
        """Select 2 most likely players from candidates."""
        if court_bbox is not None:
            # Sort by distance from court center
            court_cx = (court_bbox[0] + court_bbox[2]) / 2
            court_cy = (court_bbox[1] + court_bbox[3]) / 2
            candidates.sort(key=lambda b: np.hypot(
                (b[0] + b[2]) / 2 - court_cx,
                (b[1] + b[3]) / 2 - court_cy,
            ))
        else:
            # Sort by bbox area (largest = closest to camera)
            candidates.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        return candidates[:2]

    def _assign_ids(self, bboxes: list, frame_idx: int) -> List[PlayerDetection]:
        """Assign player_id 0/1 consistently across frames using IoU."""
        if not self._prev_players:
            # First detection: assign by vertical position (higher y = near-side = player 0)
            bboxes.sort(key=lambda b: (b[1] + b[3]) / 2, reverse=True)
            results = []
            for i, bbox in enumerate(bboxes[:2]):
                pid = i
                cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                det = PlayerDetection(frame_idx=frame_idx, player_id=pid, bbox=bbox, center=(cx, cy))
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
            det = PlayerDetection(frame_idx=frame_idx, player_id=pid, bbox=bbox, center=(cx, cy))
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
