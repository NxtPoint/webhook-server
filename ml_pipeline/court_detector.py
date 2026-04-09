"""
CourtDetector — CNN for 14 tennis court keypoints + homography.
Uses the yastrebksv/TennisCourtDetector architecture (TrackNet-style encoder-decoder).
Runs every COURT_DETECTION_INTERVAL frames; returns cached result between runs.
Falls back to Hough line detection if confidence is below threshold.
"""

import logging
import numpy as np
import cv2
import torch
import torch.nn as nn
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

from ml_pipeline.config import (
    COURT_DETECTOR_WEIGHTS,
    COURT_NUM_KEYPOINTS,
    COURT_DETECTION_INTERVAL,
    COURT_CONFIDENCE_THRESHOLD,
    COURT_REFERENCE_KEYPOINTS,
    COURT_LENGTH_M,
    COURT_WIDTH_SINGLES_M,
    TRACKNET_INPUT_WIDTH,
    TRACKNET_INPUT_HEIGHT,
    HOUGH_RHO,
    HOUGH_THETA_DIVISOR,
    HOUGH_THRESHOLD,
    HOUGH_MIN_LINE_LENGTH,
    HOUGH_MAX_LINE_GAP,
)


# ── Court keypoint CNN (same ConvBlock arch as TrackNet, different I/O) ─────

class _ConvBlock(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_size=3, pad=1, stride=1, bias=True):
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, kernel_size, stride=stride, padding=pad, bias=bias),
            nn.ReLU(),
            nn.BatchNorm2d(out_ch),
        )

    def forward(self, x):
        return self.block(x)


class CourtKeypointNet(nn.Module):
    """Same architecture as BallTrackerNet but with 3-channel input (single frame)
    and 15 output channels (14 keypoints + 1 court center)."""

    def __init__(self, in_channels=3, out_channels=15):
        super().__init__()
        self.out_channels = out_channels
        self.conv1 = _ConvBlock(in_channels, 64)
        self.conv2 = _ConvBlock(64, 64)
        self.pool1 = nn.MaxPool2d(2, 2)
        self.conv3 = _ConvBlock(64, 128)
        self.conv4 = _ConvBlock(128, 128)
        self.pool2 = nn.MaxPool2d(2, 2)
        self.conv5 = _ConvBlock(128, 256)
        self.conv6 = _ConvBlock(256, 256)
        self.conv7 = _ConvBlock(256, 256)
        self.pool3 = nn.MaxPool2d(2, 2)
        self.conv8 = _ConvBlock(256, 512)
        self.conv9 = _ConvBlock(512, 512)
        self.conv10 = _ConvBlock(512, 512)
        self.ups1 = nn.Upsample(scale_factor=2)
        self.conv11 = _ConvBlock(512, 256)
        self.conv12 = _ConvBlock(256, 256)
        self.conv13 = _ConvBlock(256, 256)
        self.ups2 = nn.Upsample(scale_factor=2)
        self.conv14 = _ConvBlock(256, 128)
        self.conv15 = _ConvBlock(128, 128)
        self.ups3 = nn.Upsample(scale_factor=2)
        self.conv16 = _ConvBlock(128, 64)
        self.conv17 = _ConvBlock(64, 64)
        self.conv18 = _ConvBlock(64, out_channels)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, x, testing=False):
        b = x.size(0)
        x = self.conv1(x); x = self.conv2(x); x = self.pool1(x)
        x = self.conv3(x); x = self.conv4(x); x = self.pool2(x)
        x = self.conv5(x); x = self.conv6(x); x = self.conv7(x); x = self.pool3(x)
        x = self.conv8(x); x = self.conv9(x); x = self.conv10(x)
        x = self.ups1(x); x = self.conv11(x); x = self.conv12(x); x = self.conv13(x)
        x = self.ups2(x); x = self.conv14(x); x = self.conv15(x)
        x = self.ups3(x); x = self.conv16(x); x = self.conv17(x); x = self.conv18(x)
        out = x.reshape(b, self.out_channels, -1)
        if testing:
            out = self.softmax(out)
        return out


# ── Data class ──────────────────────────────────────────────────────────────

@dataclass
class CourtDetection:
    keypoints: np.ndarray             # (14, 2) pixel coordinates
    homography: Optional[np.ndarray]  # 3x3 matrix, pixel→court ref
    confidence: float                 # 0-1, fraction of keypoints detected
    used_fallback: bool


# ── CourtDetector ───────────────────────────────────────────────────────────

class CourtDetector:
    def __init__(self, weights_path: str = COURT_DETECTOR_WEIGHTS, device: str = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = self._load_model(weights_path)
        self.ref_keypoints = np.array(COURT_REFERENCE_KEYPOINTS, dtype=np.float32)
        self._last_detection: Optional[CourtDetection] = None
        self._detect_interval: int = COURT_DETECTION_INTERVAL
        self._last_frame_idx: int = -COURT_DETECTION_INTERVAL

    def _load_model(self, weights_path: str) -> CourtKeypointNet:
        model = CourtKeypointNet(in_channels=3, out_channels=15)
        state = torch.load(weights_path, map_location=self.device, weights_only=True)
        model.load_state_dict(state)
        model.to(self.device)
        model.eval()
        return model

    def detect(self, frame: np.ndarray, frame_idx: int = 0) -> CourtDetection:
        """Detect court keypoints. Uses cached result if within detection interval."""
        if (frame_idx - self._last_frame_idx) < self._detect_interval and self._last_detection is not None:
            return self._last_detection

        detection = self._detect_cnn(frame)
        if detection.confidence < COURT_CONFIDENCE_THRESHOLD:
            fallback = self._detect_hough(frame)
            if fallback is not None:
                detection = fallback

        self._last_detection = detection
        self._last_frame_idx = frame_idx
        return detection

    def _detect_cnn(self, frame: np.ndarray) -> CourtDetection:
        h, w = frame.shape[:2]
        resized = cv2.resize(frame, (TRACKNET_INPUT_WIDTH, TRACKNET_INPUT_HEIGHT))
        tensor = torch.from_numpy(
            resized.astype(np.float32) / 255.0
        ).permute(2, 0, 1).unsqueeze(0).to(self.device)

        with torch.no_grad():
            output = self.model(tensor, testing=True)

        # Output: (1, 15, H*W) — 15 heatmaps. Extract peak (x,y) from each.
        heatmaps = output.squeeze(0).cpu().numpy()  # (15, H*W)
        heatmaps = heatmaps.reshape(15, TRACKNET_INPUT_HEIGHT, TRACKNET_INPUT_WIDTH)

        scale_x = w / TRACKNET_INPUT_WIDTH
        scale_y = h / TRACKNET_INPUT_HEIGHT

        keypoints = np.zeros((COURT_NUM_KEYPOINTS, 2), dtype=np.float32)
        detected_count = 0
        for i in range(COURT_NUM_KEYPOINTS):  # first 14 channels
            hm = heatmaps[i]
            peak_val = hm.max()
            if peak_val > 0.01:  # minimal threshold for peak existence
                peak_idx = np.unravel_index(hm.argmax(), hm.shape)
                keypoints[i] = [peak_idx[1] * scale_x, peak_idx[0] * scale_y]
                detected_count += 1
            else:
                keypoints[i] = [-1, -1]

        confidence = detected_count / COURT_NUM_KEYPOINTS

        # Filter to valid keypoints for homography
        valid_mask = keypoints[:, 0] >= 0
        homography = None
        if valid_mask.sum() >= 4 and confidence >= COURT_CONFIDENCE_THRESHOLD:
            homography = self._compute_homography(keypoints, valid_mask)

        return CourtDetection(
            keypoints=keypoints,
            homography=homography,
            confidence=confidence,
            used_fallback=False,
        )

    def _compute_homography(self, detected_kps: np.ndarray, valid_mask: np.ndarray = None) -> Optional[np.ndarray]:
        if valid_mask is None:
            valid_mask = detected_kps[:, 0] >= 0
        n_valid = int(valid_mask.sum())
        if n_valid < 4:
            logger.warning("_compute_homography: only %d valid keypoints (need 4)", n_valid)
            return None
        src = detected_kps[valid_mask].astype(np.float32)
        dst = self.ref_keypoints[valid_mask[:len(self.ref_keypoints)]].astype(np.float32)
        n = min(len(src), len(dst))
        if n < 4:
            logger.warning("_compute_homography: src/dst mismatch — src=%d dst=%d", len(src), len(dst))
            return None
        H, mask = cv2.findHomography(src[:n], dst[:n], cv2.RANSAC, 5.0)
        if H is None:
            logger.warning("_compute_homography: findHomography returned None with %d points", n)
        else:
            logger.info("_compute_homography: success with %d points, H diag=[%.2f, %.2f, %.2f]",
                         n, H[0, 0], H[1, 1], H[2, 2])
        return H

    def _detect_hough(self, frame: np.ndarray) -> Optional[CourtDetection]:
        """Fallback: use Hough line detection to find court lines."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        lines = cv2.HoughLinesP(
            edges, HOUGH_RHO, np.pi / HOUGH_THETA_DIVISOR,
            HOUGH_THRESHOLD, minLineLength=HOUGH_MIN_LINE_LENGTH, maxLineGap=HOUGH_MAX_LINE_GAP,
        )
        if lines is None or len(lines) < 4:
            return None

        h_lines, v_lines = [], []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            angle = abs(np.arctan2(y2 - y1, x2 - x1) * 180 / np.pi)
            if angle < 30 or angle > 150:
                h_lines.append(line[0])
            elif 60 < angle < 120:
                v_lines.append(line[0])

        if len(h_lines) < 2 or len(v_lines) < 2:
            return None

        h_lines.sort(key=lambda l: (l[1] + l[3]) / 2)
        v_lines.sort(key=lambda l: (l[0] + l[2]) / 2)

        corners = []
        for hl in [h_lines[0], h_lines[-1]]:
            for vl in [v_lines[0], v_lines[-1]]:
                pt = self._line_intersection(hl, vl)
                if pt is not None:
                    corners.append(pt)

        if len(corners) < 4:
            return None

        keypoints = np.zeros((COURT_NUM_KEYPOINTS, 2), dtype=np.float32)
        keypoints[0] = corners[0]
        keypoints[1] = corners[1]
        keypoints[2] = corners[2]
        keypoints[3] = corners[3]
        for i in range(4, COURT_NUM_KEYPOINTS):
            t = (i - 4) / max(COURT_NUM_KEYPOINTS - 5, 1)
            keypoints[i] = (1 - t) * keypoints[0] + t * keypoints[3]

        homography = self._compute_homography(keypoints)
        return CourtDetection(
            keypoints=keypoints,
            homography=homography,
            confidence=0.3,
            used_fallback=True,
        )

    @staticmethod
    def _line_intersection(line1, line2):
        x1, y1, x2, y2 = line1
        x3, y3, x4, y4 = line2
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-10:
            return None
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        px = x1 + t * (x2 - x1)
        py = y1 + t * (y2 - y1)
        return np.array([px, py], dtype=np.float32)

    _coord_log_count = 0

    def to_court_coords(self, pixel_x: float, pixel_y: float) -> Optional[tuple]:
        """Convert pixel coordinates to real-world court coordinates (metres)."""
        if self._last_detection is None or self._last_detection.homography is None:
            if self._coord_log_count < 3:
                logger.warning(
                    "to_court_coords: returning None — detection=%s, homography=%s",
                    self._last_detection is not None,
                    self._last_detection.homography is not None if self._last_detection else "N/A",
                )
                self._coord_log_count += 1
            return None
        H = self._last_detection.homography
        pt = np.array([pixel_x, pixel_y, 1.0])
        court_pt = H @ pt
        if abs(court_pt[2]) < 1e-10:
            return None
        court_pt = court_pt[:2] / court_pt[2]

        ref = self.ref_keypoints
        ref_w = ref[1][0] - ref[0][0]
        ref_h = ref[2][1] - ref[0][1]
        if ref_w == 0 or ref_h == 0:
            return None

        mx = (court_pt[0] - ref[0][0]) / ref_w * COURT_WIDTH_SINGLES_M
        my = (court_pt[1] - ref[0][1]) / ref_h * COURT_LENGTH_M
        return (float(mx), float(my))

    def get_court_bbox_pixels(self) -> Optional[tuple]:
        """Return (x_min, y_min, x_max, y_max) bounding box of detected court."""
        if self._last_detection is None:
            return None
        kps = self._last_detection.keypoints
        valid = kps[kps[:, 0] >= 0]
        if len(valid) == 0:
            return None
        return (
            float(valid[:, 0].min()),
            float(valid[:, 1].min()),
            float(valid[:, 0].max()),
            float(valid[:, 1].max()),
        )
