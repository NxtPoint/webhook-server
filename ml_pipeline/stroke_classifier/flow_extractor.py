"""
Optical flow extraction around hit events for stroke classification.

Given a sequence of video frames and a list of hit events (frame_idx + bbox),
extracts Farneback dense optical flow on the player bbox crop for a window
of ±FLOW_WINDOW frames around each hit. Produces a tensor of shape
(2*FLOW_WINDOW, H, W, 2) per hit — the motion fingerprint of the swing.

The flow tensor is resized to a canonical (CROP_H, CROP_W) regardless of
the original bbox size, so the classifier sees a uniform input shape.
"""

import cv2
import numpy as np
from typing import List, Optional, Tuple
from dataclasses import dataclass

# --- Configuration ---
FLOW_WINDOW = 5          # ±5 frames around the hit = 10 flow pairs
CROP_H = 64              # Canonical crop height (resized)
CROP_W = 48              # Canonical crop width (resized, portrait orientation)
FLOW_PYRAMID_SCALE = 0.5
FLOW_LEVELS = 3
FLOW_WINSIZE = 15
FLOW_ITERATIONS = 3
FLOW_POLY_N = 5
FLOW_POLY_SIGMA = 1.2
BBOX_PAD_RATIO = 0.15    # Expand bbox by 15% on each side for context


@dataclass
class HitEvent:
    """A single hit event to classify."""
    frame_idx: int
    player_id: int
    bbox: Tuple[float, float, float, float]  # (x1, y1, x2, y2) in pixels
    # Optional ground truth for training
    stroke_label: Optional[str] = None


@dataclass
class FlowFeature:
    """Extracted flow tensor for one hit event."""
    hit: HitEvent
    flow_tensor: np.ndarray   # shape: (n_pairs, CROP_H, CROP_W, 2)
    magnitude_hist: np.ndarray  # 10-bin histogram of flow magnitudes (summary)
    dominant_angle: float       # dominant flow direction in radians


def _pad_bbox(bbox: Tuple[float, float, float, float],
              frame_h: int, frame_w: int) -> Tuple[int, int, int, int]:
    """Expand bbox by BBOX_PAD_RATIO and clamp to frame bounds."""
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    px, py = w * BBOX_PAD_RATIO, h * BBOX_PAD_RATIO
    x1 = max(0, int(x1 - px))
    y1 = max(0, int(y1 - py))
    x2 = min(frame_w, int(x2 + px))
    y2 = min(frame_h, int(y2 + py))
    return x1, y1, x2, y2


def _crop_and_resize(frame: np.ndarray,
                     bbox: Tuple[int, int, int, int]) -> np.ndarray:
    """Crop bbox from frame and resize to canonical (CROP_H, CROP_W)."""
    x1, y1, x2, y2 = bbox
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return np.zeros((CROP_H, CROP_W), dtype=np.uint8)
    return cv2.resize(crop, (CROP_W, CROP_H), interpolation=cv2.INTER_LINEAR)


def extract_flow_features(
    frames: dict,
    hits: List[HitEvent],
) -> List[FlowFeature]:
    """Extract optical flow features for a list of hit events.

    Args:
        frames: dict mapping frame_idx -> numpy BGR frame.
                Only frames within ±FLOW_WINDOW of each hit need to be present.
        hits: list of HitEvent with frame_idx and bbox.

    Returns:
        List of FlowFeature, one per hit (skips hits with insufficient frames).
    """
    results = []
    for hit in hits:
        flow_feature = _extract_single(frames, hit)
        if flow_feature is not None:
            results.append(flow_feature)
    return results


def _extract_single(
    frames: dict,
    hit: HitEvent,
) -> Optional[FlowFeature]:
    """Extract flow for a single hit event."""
    start = hit.frame_idx - FLOW_WINDOW
    end = hit.frame_idx + FLOW_WINDOW

    # Collect available frames in the window
    available = sorted(idx for idx in range(start, end + 1) if idx in frames)
    if len(available) < 4:  # need at least 4 frames for 3 flow pairs
        return None

    frame_h, frame_w = frames[available[0]].shape[:2]
    padded_bbox = _pad_bbox(hit.bbox, frame_h, frame_w)

    # Compute optical flow between consecutive frame pairs
    flow_pairs = []
    for i in range(len(available) - 1):
        f1 = frames[available[i]]
        f2 = frames[available[i + 1]]

        # Crop and resize to canonical size
        gray1 = _crop_and_resize(
            cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY), padded_bbox
        )
        gray2 = _crop_and_resize(
            cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY), padded_bbox
        )

        flow = cv2.calcOpticalFlowFarneback(
            gray1, gray2, None,
            pyr_scale=FLOW_PYRAMID_SCALE,
            levels=FLOW_LEVELS,
            winsize=FLOW_WINSIZE,
            iterations=FLOW_ITERATIONS,
            poly_n=FLOW_POLY_N,
            poly_sigma=FLOW_POLY_SIGMA,
            flags=0,
        )
        flow_pairs.append(flow)

    if not flow_pairs:
        return None

    # Stack into tensor: (n_pairs, CROP_H, CROP_W, 2)
    flow_tensor = np.stack(flow_pairs, axis=0)

    # Summary statistics for simple classifiers / debugging
    magnitudes = np.sqrt(flow_tensor[..., 0] ** 2 + flow_tensor[..., 1] ** 2)
    mag_hist, _ = np.histogram(magnitudes.ravel(), bins=10, range=(0, 20))
    mag_hist = mag_hist.astype(np.float32)
    total = mag_hist.sum()
    if total > 0:
        mag_hist /= total

    # Dominant angle: weighted average direction of strongest flows
    angles = np.arctan2(flow_tensor[..., 1], flow_tensor[..., 0])
    weights = magnitudes.ravel()
    if weights.sum() > 0:
        # Circular mean
        sin_sum = np.sum(np.sin(angles.ravel()) * weights)
        cos_sum = np.sum(np.cos(angles.ravel()) * weights)
        dominant_angle = float(np.arctan2(sin_sum, cos_sum))
    else:
        dominant_angle = 0.0

    return FlowFeature(
        hit=hit,
        flow_tensor=flow_tensor,
        magnitude_hist=mag_hist,
        dominant_angle=dominant_angle,
    )


def flow_to_input_tensor(flow_feature: FlowFeature) -> np.ndarray:
    """Convert FlowFeature to a fixed-size input tensor for the CNN.

    Pads or truncates to exactly 2*FLOW_WINDOW flow pairs.
    Returns shape: (2*FLOW_WINDOW, CROP_H, CROP_W, 2), dtype float32.
    """
    target_pairs = 2 * FLOW_WINDOW
    tensor = flow_feature.flow_tensor
    n = tensor.shape[0]

    if n >= target_pairs:
        # Take the center window
        start = (n - target_pairs) // 2
        return tensor[start:start + target_pairs].astype(np.float32)

    # Pad with zeros (symmetric)
    pad_total = target_pairs - n
    pad_before = pad_total // 2
    pad_after = pad_total - pad_before
    padded = np.pad(
        tensor,
        ((pad_before, pad_after), (0, 0), (0, 0), (0, 0)),
        mode='constant',
        constant_values=0,
    )
    return padded.astype(np.float32)
