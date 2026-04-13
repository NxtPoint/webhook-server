"""
TrackNetV3 model architecture and background estimator.

Source: https://github.com/qaz812345/TrackNetV3 (MIT licence)
Paper: "TrackNetV3: Enhancing ShuttleCock Tracking with Augmentations and
        Trajectory Rectification" — MMAsia 2023.

Key differences from TrackNet V2 (BallTrackerNet in ball_tracker.py):
  - 8 input frames instead of 3
  - Background median image prepended → 27 total input channels (3 + 8×3)
  - U-Net architecture WITH skip connections (V2 has none)
  - Sigmoid output (not softmax) — one heatmap per frame
  - Separate InpaintNet rectification module (trajectory repair)

This file is self-contained — V2 code in ball_tracker.py is unchanged.

Usage in BallTracker:
  - When ml_pipeline/models/tracknet_v3.pt exists, BallTracker loads TrackNetV3
    instead of BallTrackerNet.
  - BackgroundEstimator computes the median image from early frames.
  - BallTracker.detect_frame() builds the 8-frame buffer and prepends the
    background channel before feeding the model.

Input shape:  (1, 27, H, W)  — background (3ch) + 8 frames × 3ch, normalised /255
Output shape: (1,  8, H, W)  — one sigmoid heatmap per input frame
The heatmap for the LAST frame in the sequence is used for detection,
matching the V2 convention (detect the current frame).
"""

import logging
import numpy as np
import cv2
import torch
import torch.nn as nn
from typing import Optional, List

logger = logging.getLogger(__name__)


# ── Building blocks (faithful port of qaz812345/TrackNetV3/model.py) ─────────

class _Conv2DBlock(nn.Module):
    """Conv2D + BN + ReLU.  Mirrors TrackNetV3's Conv2DBlock."""

    def __init__(self, in_dim: int, out_dim: int):
        super().__init__()
        # padding='same' keeps spatial dims constant — equivalent to pad=1 for kernel=3
        self.conv = nn.Conv2d(in_dim, out_dim, kernel_size=3, padding=1, bias=False)
        self.bn = nn.BatchNorm2d(out_dim)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.relu(self.bn(self.conv(x)))


class _Double2DConv(nn.Module):
    """Two consecutive Conv2DBlocks (encoder building block)."""

    def __init__(self, in_dim: int, out_dim: int):
        super().__init__()
        self.block = nn.Sequential(
            _Conv2DBlock(in_dim, out_dim),
            _Conv2DBlock(out_dim, out_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.block(x)


class _Triple2DConv(nn.Module):
    """Three consecutive Conv2DBlocks (deeper encoder / bottleneck block)."""

    def __init__(self, in_dim: int, out_dim: int):
        super().__init__()
        self.block = nn.Sequential(
            _Conv2DBlock(in_dim, out_dim),
            _Conv2DBlock(out_dim, out_dim),
            _Conv2DBlock(out_dim, out_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.block(x)


class TrackNetV3(nn.Module):
    """TrackNetV3 — U-Net with skip connections.

    Architecture from qaz812345/TrackNetV3/model.py::TrackNet.

    Args:
        in_dim:  Number of input channels.  27 for bg_mode='concat' (3 bg + 8×3 frames).
        out_dim: Number of output channels.  Equal to seq_len (8) — one heatmap per frame.

    Forward:
        x: (N, in_dim, H, W) float tensor, values in [0, 1].

    Returns:
        (N, out_dim, H, W) sigmoid heatmap — values in [0, 1].
        The last channel (index out_dim-1) corresponds to the most recent frame.
    """

    def __init__(self, in_dim: int = 27, out_dim: int = 8):
        super().__init__()
        # Encoder
        self.down_block_1 = _Double2DConv(in_dim, 64)        # skip → up_block_3
        self.down_block_2 = _Double2DConv(64, 128)            # skip → up_block_2
        self.down_block_3 = _Triple2DConv(128, 256)           # skip → up_block_1
        self.bottleneck = _Triple2DConv(256, 512)
        # Decoder — skip channels concatenated before each up-block
        self.up_block_1 = _Triple2DConv(512 + 256, 256)       # cat bottleneck + skip3
        self.up_block_2 = _Double2DConv(256 + 128, 128)       # cat up1   + skip2
        self.up_block_3 = _Double2DConv(128 + 64, 64)         # cat up2   + skip1
        # Final 1×1 predictor
        self.predictor = nn.Conv2d(64, out_dim, kernel_size=1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Encoder
        x1 = self.down_block_1(x)                                     # (N,  64, H,   W)
        x2 = self.down_block_2(nn.functional.max_pool2d(x1, 2))       # (N, 128, H/2, W/2)
        x3 = self.down_block_3(nn.functional.max_pool2d(x2, 2))       # (N, 256, H/4, W/4)
        xb = self.bottleneck(nn.functional.max_pool2d(x3, 2))         # (N, 512, H/8, W/8)
        # Decoder with skip connections
        x = torch.cat([nn.functional.interpolate(xb, scale_factor=2, mode='nearest'), x3], dim=1)
        x = self.up_block_1(x)                                         # (N, 256, H/4, W/4)
        x = torch.cat([nn.functional.interpolate(x, scale_factor=2, mode='nearest'), x2], dim=1)
        x = self.up_block_2(x)                                         # (N, 128, H/2, W/2)
        x = torch.cat([nn.functional.interpolate(x, scale_factor=2, mode='nearest'), x1], dim=1)
        x = self.up_block_3(x)                                         # (N,  64, H,   W)
        return self.sigmoid(self.predictor(x))                         # (N, out_dim, H, W)


# ── Background estimator ──────────────────────────────────────────────────────

class BackgroundEstimator:
    """Estimates a per-pixel median image from the first N frames of a video.

    The median background is computed once during warm-up and then held fixed
    for the remainder of inference, matching the TrackNetV3 dataset behaviour
    (Video_IterableDataset.__gen_median__).

    Args:
        warmup_frames: How many frames to collect before computing the median.
                       More frames → more stable estimate; 200 is a good default
                       for a fixed indoor court camera.
        target_w, target_h: Model input resolution (640×360 to match V2 constants).
    """

    def __init__(self, warmup_frames: int = 200, target_w: int = 640, target_h: int = 360):
        self._warmup_frames = warmup_frames
        self._target_w = target_w
        self._target_h = target_h
        self._buffer: List[np.ndarray] = []   # RGB uint8 frames at model resolution
        self.median: Optional[np.ndarray] = None   # (3, H, W) float32 in [0, 1]
        self._ready = False

    @property
    def ready(self) -> bool:
        """True once the median image has been computed."""
        return self._ready

    def update(self, frame_bgr: np.ndarray) -> bool:
        """Feed one BGR frame.  Returns True the first time the median is ready."""
        if self._ready:
            return False

        # Resize to model input resolution and convert to RGB
        resized = cv2.resize(frame_bgr, (self._target_w, self._target_h))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        self._buffer.append(rgb)

        if len(self._buffer) >= self._warmup_frames:
            self._compute()
            return True
        return False

    def _compute(self):
        arr = np.stack(self._buffer, axis=0).astype(np.float32)   # (N, H, W, 3)
        median_hwc = np.median(arr, axis=0)                        # (H, W, 3)
        # Convert to (3, H, W) and normalise to [0, 1]
        self.median = np.moveaxis(median_hwc, -1, 0) / 255.0      # (3, H, W)
        self._ready = True
        self._buffer.clear()
        logger.info(
            "BackgroundEstimator: median computed from %d frames  shape=%s",
            self._warmup_frames, self.median.shape,
        )

    def force_compute(self):
        """Compute the median immediately from whatever frames have been collected.

        Call this if the video is shorter than warmup_frames so the V3 tracker
        can still function (with a less stable background estimate).
        """
        if self._ready:
            return
        if not self._buffer:
            logger.warning("BackgroundEstimator.force_compute: no frames collected; background will be zeros")
            self.median = np.zeros((3, self._target_h, self._target_w), dtype=np.float32)
        else:
            logger.warning(
                "BackgroundEstimator.force_compute: only %d/%d frames available",
                len(self._buffer), self._warmup_frames,
            )
            self._compute()
        self._ready = True

    def as_tensor(self, device: str, use_fp16: bool = False) -> torch.Tensor:
        """Return median as a (1, 3, H, W) tensor ready to prepend to the frame stack."""
        assert self._ready, "Background not yet ready — call update() or force_compute() first"
        t = torch.from_numpy(self.median).unsqueeze(0)   # (1, 3, H, W)
        t = t.to(device)
        if use_fp16:
            t = t.half()
        return t
