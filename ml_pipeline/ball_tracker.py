"""
BallTracker — TrackNet V2 based ball detection with bounce/speed analysis.
Sliding window of 3 frames → heatmap → (x,y). Linear interpolation for small gaps.
"""

import logging
import numpy as np
import cv2
import torch
import torch.nn as nn
from dataclasses import dataclass, field
from typing import Optional, List

logger = logging.getLogger(__name__)

from ml_pipeline.config import (
    TRACKNET_WEIGHTS,
    TRACKNET_INPUT_WIDTH,
    TRACKNET_INPUT_HEIGHT,
    TRACKNET_NUM_INPUT_FRAMES,
    TRACKNET_OUTPUT_CHANNELS,
    TRACKNET_HEATMAP_THRESHOLD,
    TRACKNET_HOUGH_DP,
    TRACKNET_HOUGH_MIN_DIST,
    TRACKNET_HOUGH_PARAM1,
    TRACKNET_HOUGH_PARAM2,
    TRACKNET_HOUGH_MIN_RADIUS,
    TRACKNET_HOUGH_MAX_RADIUS,
    BALL_MAX_INTERPOLATION_GAP,
    BALL_MAX_DIST_BETWEEN_FRAMES,
    BALL_MAX_DIST_GAP,
    BOUNCE_VELOCITY_WINDOW,
    BOUNCE_MIN_DIRECTION_CHANGE,
    COURT_LENGTH_M,
    COURT_WIDTH_SINGLES_M,
    COURT_WIDTH_DOUBLES_M,
    FRAME_SAMPLE_FPS,
)


# ── TrackNet V2 Architecture (from yastrebksv/TrackNet) ────────────────────

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


class BallTrackerNet(nn.Module):
    def __init__(self, out_channels=TRACKNET_OUTPUT_CHANNELS):
        super().__init__()
        self.out_channels = out_channels
        # Encoder
        self.conv1 = _ConvBlock(9, 64)
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
        # Decoder
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


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class BallDetection:
    frame_idx: int
    x: float          # pixel x in original frame
    y: float          # pixel y in original frame
    court_x: Optional[float] = None  # metres
    court_y: Optional[float] = None  # metres
    speed_kmh: Optional[float] = None
    is_bounce: bool = False
    is_in: Optional[bool] = None     # None = unknown


# ── BallTracker ─────────────────────────────────────────────────────────────

class BallTracker:
    def __init__(self, weights_path: str = TRACKNET_WEIGHTS, device: str = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = self._load_model(weights_path)
        self.scale_x = 1.0
        self.scale_y = 1.0
        self._frame_buffer: list = []  # last 3 frames (resized)
        self.detections: List[BallDetection] = []

    def _load_model(self, weights_path: str) -> BallTrackerNet:
        model = BallTrackerNet()
        state = torch.load(weights_path, map_location=self.device, weights_only=True)
        model.load_state_dict(state)
        model.to(self.device)
        model.eval()
        # Enable FP16 on CUDA for ~1.5x inference speedup
        self._use_fp16 = ("cuda" in str(self.device))
        if self._use_fp16:
            model = model.half()
        return model

    def detect_frame(self, frame: np.ndarray, frame_idx: int) -> Optional[BallDetection]:
        """Feed one frame. Returns detection once 3-frame window is filled."""
        h, w = frame.shape[:2]
        self.scale_x = w / TRACKNET_INPUT_WIDTH
        self.scale_y = h / TRACKNET_INPUT_HEIGHT

        resized = cv2.resize(frame, (TRACKNET_INPUT_WIDTH, TRACKNET_INPUT_HEIGHT))
        self._frame_buffer.append(resized)
        if len(self._frame_buffer) > TRACKNET_NUM_INPUT_FRAMES:
            self._frame_buffer.pop(0)
        if len(self._frame_buffer) < TRACKNET_NUM_INPUT_FRAMES:
            return None

        # Stack 3 frames → 9 channels
        stacked = np.concatenate(self._frame_buffer, axis=2)  # (H, W, 9)
        tensor = torch.from_numpy(
            stacked.astype(np.float32) / 255.0
        ).permute(2, 0, 1).unsqueeze(0).to(self.device)
        if self._use_fp16:
            tensor = tensor.half()

        with torch.no_grad():
            output = self.model(tensor, testing=True)
        heatmap = output.argmax(dim=1).squeeze().cpu().numpy()

        x, y = self._postprocess_heatmap(heatmap)
        if x is None:
            return None

        det = BallDetection(
            frame_idx=frame_idx,
            x=x * self.scale_x,
            y=y * self.scale_y,
        )
        self.detections.append(det)
        return det

    def _postprocess_heatmap(self, feature_map: np.ndarray):
        """Convert heatmap to (x, y) via Hough circle detection."""
        fm = (feature_map * 255).astype(np.uint8)
        fm = fm.reshape((TRACKNET_INPUT_HEIGHT, TRACKNET_INPUT_WIDTH))
        _, binary = cv2.threshold(fm, TRACKNET_HEATMAP_THRESHOLD, 255, cv2.THRESH_BINARY)
        circles = cv2.HoughCircles(
            binary, cv2.HOUGH_GRADIENT,
            dp=TRACKNET_HOUGH_DP,
            minDist=TRACKNET_HOUGH_MIN_DIST,
            param1=TRACKNET_HOUGH_PARAM1,
            param2=TRACKNET_HOUGH_PARAM2,
            minRadius=TRACKNET_HOUGH_MIN_RADIUS,
            maxRadius=TRACKNET_HOUGH_MAX_RADIUS,
        )
        if circles is not None and len(circles) == 1:
            return float(circles[0][0][0]), float(circles[0][0][1])
        return None, None

    def interpolate_gaps(self):
        """Fill missing detections with linear interpolation for gaps <= BALL_MAX_INTERPOLATION_GAP."""
        if len(self.detections) < 2:
            return
        # Build frame→detection map
        by_frame = {d.frame_idx: d for d in self.detections}
        frames = sorted(by_frame.keys())

        interpolated = []
        for i in range(len(frames) - 1):
            f_start = frames[i]
            f_end = frames[i + 1]
            gap = f_end - f_start - 1
            if 0 < gap <= BALL_MAX_INTERPOLATION_GAP:
                d1 = by_frame[f_start]
                d2 = by_frame[f_end]
                dist = np.hypot(d2.x - d1.x, d2.y - d1.y)
                if dist > BALL_MAX_DIST_GAP:
                    continue
                for g in range(1, gap + 1):
                    t = g / (gap + 1)
                    interpolated.append(BallDetection(
                        frame_idx=f_start + g,
                        x=d1.x + t * (d2.x - d1.x),
                        y=d1.y + t * (d2.y - d1.y),
                    ))

        self.detections.extend(interpolated)
        self.detections.sort(key=lambda d: d.frame_idx)
        # Remove outlier jumps
        self._filter_outliers()

    def _filter_outliers(self):
        """Remove detections where ball jumps > BALL_MAX_DIST_BETWEEN_FRAMES pixels."""
        if len(self.detections) < 2:
            return
        filtered = [self.detections[0]]
        for d in self.detections[1:]:
            prev = filtered[-1]
            dist = np.hypot(d.x - prev.x, d.y - prev.y)
            if dist <= BALL_MAX_DIST_BETWEEN_FRAMES:
                filtered.append(d)
        self.detections = filtered

    def detect_bounces(self, court_detector=None):
        """Detect bounces via velocity reversal in y-coordinate. Optionally map to court coords.

        A valid bounce requires:
          1. Sign change in y-velocity (direction flip)
          2. Minimum velocity magnitude on both sides (reject gentle rolls/noise)
          3. Minimum spacing from the previous bounce (reject double-counting)
        """
        if len(self.detections) < BOUNCE_VELOCITY_WINDOW * 2:
            return

        # Compute rolling y-velocity
        ys = np.array([d.y for d in self.detections])
        vel = np.convolve(np.diff(ys), np.ones(BOUNCE_VELOCITY_WINDOW) / BOUNCE_VELOCITY_WINDOW, mode="valid")

        # Minimum magnitude for a real bounce (px/frame)
        MIN_VEL_MAG = 2.0
        # Minimum frame spacing between bounces (frames)
        MIN_BOUNCE_SPACING = 8

        last_bounce_idx = -MIN_BOUNCE_SPACING  # allow first bounce
        bounce_count = 0

        for i in range(len(vel) - 1):
            sign_flip = (vel[i] > 0 and vel[i + 1] < 0) or (vel[i] < 0 and vel[i + 1] > 0)
            if not sign_flip:
                continue
            # Require minimum magnitude on both sides — rejects slow rolls
            if abs(vel[i]) < MIN_VEL_MAG or abs(vel[i + 1]) < MIN_VEL_MAG:
                continue

            det_idx = i + BOUNCE_VELOCITY_WINDOW
            if det_idx >= len(self.detections):
                continue

            # Minimum spacing — rejects double-counting on the same bounce
            if det_idx - last_bounce_idx < MIN_BOUNCE_SPACING:
                continue
            last_bounce_idx = det_idx

            self.detections[det_idx].is_bounce = True
            bounce_count += 1

            # In/out detection via court boundary (doubles court, matches homography)
            if court_detector is not None:
                coords = court_detector.to_court_coords(
                    self.detections[det_idx].x, self.detections[det_idx].y
                )
                if coords is not None:
                    cx, cy = coords
                    self.detections[det_idx].court_x = cx
                    self.detections[det_idx].court_y = cy
                    self.detections[det_idx].is_in = (
                        0 <= cx <= COURT_WIDTH_DOUBLES_M and
                        0 <= cy <= COURT_LENGTH_M
                    )
        logger.info("detect_bounces: found %d bounces (after validation)", bounce_count)

    def compute_speeds(self, court_detector=None, fps: float = None):
        """Compute ball speed in km/h using court-coordinate distances between frames."""
        if court_detector is None or len(self.detections) < 2:
            return
        sample_fps = fps or FRAME_SAMPLE_FPS
        none_count = 0
        ok_count = 0
        for i in range(1, len(self.detections)):
            d_prev = self.detections[i - 1]
            d_curr = self.detections[i]
            c_prev = court_detector.to_court_coords(d_prev.x, d_prev.y)
            c_curr = court_detector.to_court_coords(d_curr.x, d_curr.y)
            if c_prev is None or c_curr is None:
                none_count += 1
                continue
            ok_count += 1
            dist_m = np.hypot(c_curr[0] - c_prev[0], c_curr[1] - c_prev[1])
            dt_sec = (d_curr.frame_idx - d_prev.frame_idx) / sample_fps
            if dt_sec > 0:
                speed_ms = dist_m / dt_sec
                d_curr.speed_kmh = speed_ms * 3.6
                d_curr.court_x = c_curr[0]
                d_curr.court_y = c_curr[1]
        if none_count > 0:
            logger.warning(
                "compute_speeds: %d/%d pairs had None court coords (homography=%s)",
                none_count, none_count + ok_count,
                court_detector._last_detection.homography is not None
                if court_detector._last_detection else "no_detection",
            )

    def reset(self):
        self._frame_buffer.clear()
        self.detections.clear()
