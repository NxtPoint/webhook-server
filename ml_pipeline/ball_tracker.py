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

import os

from ml_pipeline.config import (
    TRACKNET_WEIGHTS,
    TRACKNET_V3_WEIGHTS,
    TRACKNET_V3_NUM_INPUT_FRAMES,
    TRACKNET_V3_IN_CHANNELS,
    TRACKNET_V3_BACKGROUND_WARMUP_FRAMES,
    TRACKNET_INPUT_WIDTH,
    TRACKNET_INPUT_HEIGHT,
    TRACKNET_NUM_INPUT_FRAMES,
    TRACKNET_OUTPUT_CHANNELS,
    TRACKNET_HEATMAP_THRESHOLD,
    TRACKNET_BGR2RGB,
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
from ml_pipeline.tracknet_v3 import TrackNetV3, BackgroundEstimator


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
    def __init__(self, in_channels=9, out_channels=TRACKNET_OUTPUT_CHANNELS):
        super().__init__()
        self.out_channels = out_channels
        # Encoder — in_channels = num_frames * 3 (9 for V2, 15 for V3)
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
    def __init__(self, weights_path: str = None, device: str = None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")

        # ── TrackNet version selection ──────────────────────────────────────
        # V3 is automatically preferred when its weights file exists and no
        # explicit weights_path override is supplied. V2 remains the default
        # when only tracknet_v2.pt is present.
        #
        # V3 differences (qaz812345/TrackNetV3):
        #   - 8-frame sliding window (vs 3 in V2)
        #   - Background median image prepended → 27 input channels (3+8×3)
        #   - U-Net with skip connections (V2 has none)
        #   - Sigmoid output — one heatmap per frame (vs softmax argmax in V2)
        if weights_path is None:
            if os.path.exists(TRACKNET_V3_WEIGHTS):
                self._use_v3 = True
                weights_path = TRACKNET_V3_WEIGHTS
                self._num_input_frames = TRACKNET_V3_NUM_INPUT_FRAMES    # 8
                self._in_channels = TRACKNET_V3_IN_CHANNELS              # 27
                logger.info(
                    "TrackNet V3 weights found — loading V3 architecture "
                    "(%d-frame + background, %d channels, U-Net with skips): %s",
                    self._num_input_frames, self._in_channels, weights_path,
                )
            else:
                self._use_v3 = False
                weights_path = TRACKNET_WEIGHTS
                self._num_input_frames = TRACKNET_NUM_INPUT_FRAMES       # 3
                self._in_channels = TRACKNET_NUM_INPUT_FRAMES * 3        # 9
                logger.info("Using TrackNet V2 (3-frame context): %s", weights_path)
        else:
            # Explicit override — detect from channel count
            self._use_v3 = (weights_path == TRACKNET_V3_WEIGHTS) or (
                TRACKNET_V3_IN_CHANNELS == 27 and "v3" in weights_path.lower()
            )
            if self._use_v3:
                self._num_input_frames = TRACKNET_V3_NUM_INPUT_FRAMES
                self._in_channels = TRACKNET_V3_IN_CHANNELS
            else:
                self._num_input_frames = TRACKNET_NUM_INPUT_FRAMES
                self._in_channels = TRACKNET_NUM_INPUT_FRAMES * 3

        self.model = self._load_model(weights_path)
        self.scale_x = 1.0
        self.scale_y = 1.0
        self._frame_buffer: list = []  # last N BGR frames (resized to model input dims)
        self._prev_gray: Optional[np.ndarray] = None  # for frame-delta ball fallback

        # V3-specific: background estimator and cached tensor
        self._bg_estimator: Optional[BackgroundEstimator] = None
        self._bg_tensor: Optional[torch.Tensor] = None   # (1, 3, H, W) on device
        if self._use_v3:
            self._bg_estimator = BackgroundEstimator(
                warmup_frames=TRACKNET_V3_BACKGROUND_WARMUP_FRAMES,
                target_w=TRACKNET_INPUT_WIDTH,
                target_h=TRACKNET_INPUT_HEIGHT,
            )

        self.detections: List[BallDetection] = []
        # Diagnostics — counters only, no behavior change. Used to diagnose
        # the 7% detection rate on T5 runs. Reported via log_diagnostics().
        self._diag = {
            "frames_inferred": 0,
            "heatmap_empty": 0,            # fm.max() below threshold
            "mask_nonzero_sum": 0,         # total pixels above threshold (all frames)
            "tier1_hough": 0,
            "tier2_cc": 0,
            "tier2_cc_rejected_size": 0,   # CC fired but area outside [2,200]
            "tier3_argmax": 0,
            "none_returned": 0,
            "fm_max_hist": [0] * 8,        # 8 buckets: 0-31, 32-63, ..., 224-255
            "fm_raw_max_hist": [0] * 8,    # same, but for raw argmax output BEFORE the *255
        }

    def _load_model(self, weights_path: str):
        """Load the appropriate model class based on which TrackNet version is active.

        V2: BallTrackerNet (encoder-decoder, no skip connections, 9 channels)
        V3: TrackNetV3    (U-Net with skip connections, 27 channels, sigmoid output)
        """
        if self._use_v3:
            model = TrackNetV3(
                in_dim=self._in_channels,           # 27
                out_dim=TRACKNET_V3_NUM_INPUT_FRAMES,  # 8 — one heatmap per frame
            )
        else:
            model = BallTrackerNet(in_channels=self._in_channels)

        state = torch.load(weights_path, map_location=self.device, weights_only=True)
        model.load_state_dict(state)
        model.to(self.device)
        model.eval()
        # Enable FP16 on CUDA for ~1.5x inference speedup
        self._use_fp16 = ("cuda" in str(self.device))
        if self._use_fp16:
            model = model.half()
        logger.info(
            "BallTracker: loaded %s from %s  device=%s fp16=%s",
            "TrackNetV3" if self._use_v3 else "BallTrackerNet (V2)",
            weights_path, self.device, self._use_fp16,
        )
        return model

    def detect_frame(self, frame: np.ndarray, frame_idx: int) -> Optional[BallDetection]:
        """Feed one BGR frame. Returns a BallDetection once the sliding window is full.

        V2 path: 3-frame window → 9 channels → softmax argmax heatmap.
        V3 path: 8-frame window + background median → 27 channels → sigmoid heatmap
                 (last frame's channel is used for detection).

        For V3, background estimation runs automatically during the warmup period.
        If the background is not ready when the window is first filled, we force-
        compute from however many frames have been collected so detection can start.
        """
        h, w = frame.shape[:2]
        self.scale_x = w / TRACKNET_INPUT_WIDTH
        self.scale_y = h / TRACKNET_INPUT_HEIGHT

        # ── Resize + colour conversion ───────────────────────────────────────
        resized = cv2.resize(frame, (TRACKNET_INPUT_WIDTH, TRACKNET_INPUT_HEIGHT))
        if TRACKNET_BGR2RGB:
            resized = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        # ── V3: feed raw BGR frame into background estimator ─────────────────
        if self._use_v3 and self._bg_estimator is not None and not self._bg_estimator.ready:
            self._bg_estimator.update(frame)

        self._frame_buffer.append(resized)
        if len(self._frame_buffer) > self._num_input_frames:
            self._frame_buffer.pop(0)
        if len(self._frame_buffer) < self._num_input_frames:
            return None

        # ── Build model input tensor ─────────────────────────────────────────
        if self._use_v3:
            x, y = self._detect_frame_v3()
        else:
            x, y = self._detect_frame_v2()

        if x is None:
            # Model produced no output — try frame-delta Hough fallback.
            # On 63.5% of V2 frames TrackNet produces nothing. Frame differencing
            # detects ANY moving circular object (the ball) regardless of size.
            x, y = self._detect_ball_frame_delta(frame)
            if x is not None:
                self._diag["delta_fallback_hits"] = self._diag.get("delta_fallback_hits", 0) + 1
            else:
                return None

        det = BallDetection(
            frame_idx=frame_idx,
            x=x * self.scale_x,
            y=y * self.scale_y,
        )
        self.detections.append(det)
        return det

    def _detect_frame_v2(self):
        """Run V2 inference on the current 3-frame buffer. Returns (x, y) or (None, None)."""
        # Stack 3 frames → (H, W, 9)
        stacked = np.concatenate(self._frame_buffer, axis=2)
        tensor = torch.from_numpy(
            stacked.astype(np.float32) / 255.0
        ).permute(2, 0, 1).unsqueeze(0).to(self.device)
        if self._use_fp16:
            tensor = tensor.half()

        with torch.no_grad():
            output = self.model(tensor, testing=True)
        heatmap = output.argmax(dim=1).squeeze().cpu().numpy()
        return self._postprocess_heatmap(heatmap)

    def _detect_frame_v3(self):
        """Run V3 inference on the current 8-frame buffer + background. Returns (x, y) or (None, None).

        Input layout (27 channels):
          [0:3]    background median  (3 ch, normalised)
          [3:27]   8 frames × 3 ch   (24 ch, normalised)

        Output: (N=1, 8, H, W) sigmoid heatmaps.  We use channel index -1 (last
        frame) to match V2's convention of detecting the most recent frame.
        """
        # Ensure background is ready
        if not self._bg_estimator.ready:
            self._bg_estimator.force_compute()
        if self._bg_tensor is None:
            self._bg_tensor = self._bg_estimator.as_tensor(self.device, self._use_fp16)

        # Build frame tensor (8, 3, H, W) → (1, 24, H, W) then cat with bg
        frames_chw = []
        for f in self._frame_buffer:
            # f is (H, W, 3) uint8 RGB (or BGR if TRACKNET_BGR2RGB is False)
            arr = f.astype(np.float32) / 255.0
            frames_chw.append(torch.from_numpy(arr).permute(2, 0, 1))   # (3, H, W)

        frames_tensor = torch.stack(frames_chw, dim=0)                  # (8, 3, H, W)
        frames_tensor = frames_tensor.view(1, -1,
                                           TRACKNET_INPUT_HEIGHT,
                                           TRACKNET_INPUT_WIDTH)         # (1, 24, H, W)
        frames_tensor = frames_tensor.to(self.device)
        if self._use_fp16:
            frames_tensor = frames_tensor.half()

        # Prepend background: (1, 3, H, W) + (1, 24, H, W) → (1, 27, H, W)
        tensor = torch.cat([self._bg_tensor, frames_tensor], dim=1)

        with torch.no_grad():
            output = self.model(tensor)                                  # (1, 8, H, W)

        # Use the last channel — heatmap for the most recent frame
        heatmap_f32 = output[0, -1].cpu().float().numpy()               # (H, W) in [0, 1]

        # Convert to uint8 in [0, 255] for _postprocess_heatmap (threshold at 127)
        heatmap_u8 = (heatmap_f32 * 255.0).clip(0, 255).astype(np.uint8)
        return self._postprocess_heatmap(heatmap_u8)

    def _postprocess_heatmap(self, feature_map: np.ndarray):
        """Convert heatmap to (x, y) via Hough circle detection.

        Three-tier strategy:
        1. Try Hough circles — if any found, use the strongest one
        2. Fallback: largest connected component centroid in the binary mask
        3. Final fallback: argmax of the heatmap

        Previous version REQUIRED exactly 1 circle, dropping frames where
        Hough found 2+ candidates — that was throwing away ~30-40% of valid
        ball detections. The new version uses ANY signal we can find.
        """
        self._diag["frames_inferred"] += 1

        # Record raw argmax-class-index max BEFORE the *255 transform, so we
        # can tell whether the model is producing signal at all, independent
        # of any postprocess arithmetic.
        raw_max = int(feature_map.max())
        self._diag["fm_raw_max_hist"][min(raw_max // 32, 7)] += 1

        # feature_map is the argmax class index in [0, 255] (int64 from torch).
        # Previous code did (feature_map * 255).astype(np.uint8) which caused
        # modular wrap: class 255 (strongest ball signal) → uint8 1, class 1
        # (weakest) → uint8 255. This inverted the heatmap. Diagnostic run
        # 5672962e confirmed: 5584 frames with raw_max in [128-255] had their
        # ball signal destroyed by the wrap while noise from class 1 became hot.
        fm = feature_map.astype(np.uint8)
        fm = fm.reshape((TRACKNET_INPUT_HEIGHT, TRACKNET_INPUT_WIDTH))

        fm_max_after = int(fm.max())
        self._diag["fm_max_hist"][min(fm_max_after // 32, 7)] += 1
        if fm_max_after < TRACKNET_HEATMAP_THRESHOLD:
            self._diag["heatmap_empty"] += 1

        _, binary = cv2.threshold(fm, TRACKNET_HEATMAP_THRESHOLD, 255, cv2.THRESH_BINARY)
        self._diag["mask_nonzero_sum"] += int((binary > 0).sum())

        # Tier 1: Hough circles (use strongest if any found)
        circles = cv2.HoughCircles(
            binary, cv2.HOUGH_GRADIENT,
            dp=TRACKNET_HOUGH_DP,
            minDist=TRACKNET_HOUGH_MIN_DIST,
            param1=TRACKNET_HOUGH_PARAM1,
            param2=TRACKNET_HOUGH_PARAM2,
            minRadius=TRACKNET_HOUGH_MIN_RADIUS,
            maxRadius=TRACKNET_HOUGH_MAX_RADIUS,
        )
        if circles is not None and len(circles) > 0 and len(circles[0]) > 0:
            # circles[0] is sorted by accumulator strength descending — first is best
            self._diag["tier1_hough"] += 1
            return float(circles[0][0][0]), float(circles[0][0][1])

        # Tier 2: Connected component centroid (largest blob in the binary mask)
        # This catches cases where the ball is in the heatmap but doesn't form
        # a clean circle (motion blur, partial occlusion, edge of frame).
        try:
            n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
                binary, connectivity=8,
            )
            if n_labels > 1:  # 0 is background
                # Find largest component (excluding background at index 0)
                largest_idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                area = stats[largest_idx, cv2.CC_STAT_AREA]
                # Sanity check: ball blob should be small but not tiny
                if 2 <= area <= 200:
                    cx, cy = centroids[largest_idx]
                    self._diag["tier2_cc"] += 1
                    return float(cx), float(cy)
                else:
                    self._diag["tier2_cc_rejected_size"] += 1
        except Exception:
            pass

        # Tier 3: Heatmap argmax (any signal at all)
        # Only used when binary mask has no significant blobs
        if fm.max() > TRACKNET_HEATMAP_THRESHOLD:
            flat_idx = int(fm.argmax())
            cy, cx = divmod(flat_idx, TRACKNET_INPUT_WIDTH)
            self._diag["tier3_argmax"] += 1
            return float(cx), float(cy)

        self._diag["none_returned"] += 1
        return None, None

    def log_diagnostics(self):
        """Dump cumulative ball-detection diagnostics. Call once post-inference.

        Read this to diagnose the 7% detection rate. What to look for:
        - If `heatmap_empty` is high (>80%), the model is producing nothing —
          likely an input issue (BGR/RGB, resolution, normalization).
        - If `fm_raw_max_hist` is bottom-heavy (most frames in bucket 0-31)
          but `fm_max_hist` is distributed, the `*255` transform is mangling
          signal (modular wrap).
        - If `tier1_hough` >> `tier2_cc` + `tier3_argmax`, Hough is carrying
          the load — good.
        - If `tier2_cc_rejected_size` >> `tier2_cc`, CC is finding big blobs
          (noise, not balls) — a sign of postprocess problems.
        """
        d = self._diag
        total = d["frames_inferred"]
        if total == 0:
            logger.info("BallTracker diagnostics: no frames inferred")
            return

        def pct(n):
            return 100.0 * n / total

        logger.info("=== BallTracker diagnostics ===")
        logger.info("frames_inferred: %d", total)
        logger.info("heatmap_empty (fm_max < threshold): %d (%.1f%%)",
                    d["heatmap_empty"], pct(d["heatmap_empty"]))
        logger.info("avg mask nonzero pixels per frame: %.1f",
                    d["mask_nonzero_sum"] / total)
        logger.info("tier1_hough:         %d (%.1f%%)", d["tier1_hough"], pct(d["tier1_hough"]))
        logger.info("tier2_cc:            %d (%.1f%%)", d["tier2_cc"], pct(d["tier2_cc"]))
        logger.info("tier2_cc_rejected:   %d (%.1f%%)", d["tier2_cc_rejected_size"], pct(d["tier2_cc_rejected_size"]))
        logger.info("tier3_argmax:        %d (%.1f%%)", d["tier3_argmax"], pct(d["tier3_argmax"]))
        logger.info("none_returned:       %d (%.1f%%)", d["none_returned"], pct(d["none_returned"]))
        logger.info("delta_fallback_hits: %d (%.1f%%)", d.get("delta_fallback_hits", 0),
                    pct(d.get("delta_fallback_hits", 0)))
        logger.info("fm_raw_max histogram (argmax class index, PRE *255):")
        for i, c in enumerate(d["fm_raw_max_hist"]):
            lo, hi = i * 32, (i + 1) * 32 - 1
            logger.info("  [%3d-%3d]: %6d (%5.1f%%)", lo, hi, c, pct(c))
        logger.info("fm_max histogram (uint8 value, POST *255):")
        for i, c in enumerate(d["fm_max_hist"]):
            lo, hi = i * 32, (i + 1) * 32 - 1
            logger.info("  [%3d-%3d]: %6d (%5.1f%%)", lo, hi, c, pct(c))

    def _detect_ball_frame_delta(self, frame: np.ndarray):
        """Fallback ball detection via frame differencing + Hough circles.

        When TrackNet produces no output (63.5% of frames), this method
        detects the ball from the difference between consecutive frames.
        The ball is the primary small moving circular object on court.

        Works well for fixed indoor cameras where the background is static.
        The frame delta eliminates background, leaving only moving objects.
        Hough circles then finds the ball-sized circle in the delta.

        Returns (x, y) in TrackNet input coordinates (640×360), or (None, None).
        """
        resized = cv2.resize(frame, (TRACKNET_INPUT_WIDTH, TRACKNET_INPUT_HEIGHT))
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)

        if self._prev_gray is None:
            self._prev_gray = gray
            return None, None

        # Frame difference — highlights moving objects
        delta = cv2.absdiff(gray, self._prev_gray)
        self._prev_gray = gray

        # Threshold the delta to get a binary mask of motion
        _, motion_mask = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)

        # Blur to merge nearby motion pixels into blobs
        motion_mask = cv2.GaussianBlur(motion_mask, (5, 5), 0)
        _, motion_mask = cv2.threshold(motion_mask, 15, 255, cv2.THRESH_BINARY)

        # Find circles in the motion mask — ball is small and round
        circles = cv2.HoughCircles(
            motion_mask, cv2.HOUGH_GRADIENT,
            dp=1, minDist=30,
            param1=50, param2=5,
            minRadius=2, maxRadius=15,
        )

        if circles is not None and len(circles) > 0 and len(circles[0]) > 0:
            # Take the strongest circle
            x, y, r = circles[0][0]
            return float(x), float(y)

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

        # Minimum magnitude for a real bounce (px/frame). 2.0 = ignore slow
        # rolls/noise. Lowered to 1.0 broke detection (more false positives
        # disrupted velocity smoothing).
        MIN_VEL_MAG = 2.0
        # Minimum frame spacing between bounces — rejects double-counting on
        # the same impact event.
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
                speed_kmh = speed_ms * 3.6
                # Clamp impossible speeds — TrackNet position glitches can
                # produce 800+ km/h. Fastest recorded serve is ~263 km/h;
                # 250 km/h is a generous ceiling for any ball movement.
                if speed_kmh <= 250:
                    d_curr.speed_kmh = speed_kmh
                d_curr.court_x = c_curr[0]
                d_curr.court_y = c_curr[1]
        if none_count > 0:
            logger.warning(
                "compute_speeds: %d/%d pairs had None court coords (homography=%s)",
                none_count, none_count + ok_count,
                court_detector._last_detection.homography is not None
                if court_detector._last_detection else "no_detection",
            )

    def assign_peak_flight_speeds(self, window_frames: int = 15):
        """Overwrite each bounce's ``speed_kmh`` with the peak pairwise speed
        observed in the preceding ``window_frames`` frames.

        Motivation: per-frame pairwise speed at the bounce itself tends to
        under-report the true shot velocity because the ball has already
        decelerated and is about to bounce. SportAI reports "ball speed at
        hit" — the velocity during flight between the player's strike and
        the bounce. A peak over the preceding window approximates that
        semantic from the same data.

        Call after ``compute_speeds`` has populated pairwise speeds on all
        detections. Non-bounce detections are left unchanged.
        """
        n_updated = 0
        for bi, det in enumerate(self.detections):
            if not det.is_bounce:
                continue
            low_frame = det.frame_idx - window_frames
            speeds = []
            for j in range(bi - 1, -1, -1):
                d = self.detections[j]
                if d.frame_idx < low_frame:
                    break
                if d.speed_kmh is not None and d.speed_kmh > 0:
                    speeds.append(d.speed_kmh)
            if speeds:
                det.speed_kmh = max(speeds)
                n_updated += 1
        logger.info(
            "assign_peak_flight_speeds: set peak-flight speed on %d/%d bounces (window=%d frames)",
            n_updated,
            sum(1 for d in self.detections if d.is_bounce),
            window_frames,
        )

    def reset(self):
        self._frame_buffer.clear()
        self.detections.clear()
        self._prev_gray = None
        for k in self._diag:
            if isinstance(self._diag[k], list):
                self._diag[k] = [0] * len(self._diag[k])
            else:
                self._diag[k] = 0
        # V3: reset background estimator so a new video gets a fresh median
        if self._use_v3 and self._bg_estimator is not None:
            self._bg_tensor = None
            self._bg_estimator = BackgroundEstimator(
                warmup_frames=TRACKNET_V3_BACKGROUND_WARMUP_FRAMES,
                target_w=TRACKNET_INPUT_WIDTH,
                target_h=TRACKNET_INPUT_HEIGHT,
            )
