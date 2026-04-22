"""WASB-SBDT ball detector — HRNet backbone drop-in replacement for TrackNet V2.

WASB (Widely Applicable Strong Baseline) is the BMVC 2023 tennis ball
detector that outperforms TrackNet V2 specifically on small/fast balls
in broadcast-style footage. Architecture: HRNet with 4 parallel multi-res
branches — keeps high-resolution features at full stride, no downsample→
upsample information loss that hurts sub-pixel ball detection.

Source: https://github.com/nttcom/WASB-SBDT (MIT license)
Paper:  https://arxiv.org/abs/2311.05237
Weights: wasb_tennis_best.pth.tar in ml_pipeline/models/ (6.1 MB)

Input contract (same as TrackNet V2):
  - 3 consecutive BGR frames (native tennis video, no resize)
  - this module resizes each to 512×288 internally
Output:
  - (x, y) ball pixel coords in the INPUT frame's native coordinate
    system (the wrapper rescales the model's 512×288 heatmap peak back)
  - None if no ball detected above score_threshold

Intended usage: bolt-on comparison harness against TrackNet V2. NOT yet
wired into production serve_detector — we validate first, then integrate.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np
import torch

from ml_pipeline.wasb_hrnet import HRNet

logger = logging.getLogger(__name__)

# Model input dims (matches WASB training config — wasb.yaml)
WASB_INPUT_W = 512
WASB_INPUT_H = 288
WASB_FRAMES_IN = 3
WASB_FRAMES_OUT = 3

_MODELS_DIR = Path(__file__).parent / "models"
_DEFAULT_WEIGHTS = _MODELS_DIR / "wasb_tennis_best.pth.tar"


def _default_wasb_cfg() -> dict:
    """Matches src/configs/model/wasb.yaml from the WASB repo."""
    return {
        "frames_in": WASB_FRAMES_IN,
        "frames_out": WASB_FRAMES_OUT,
        "inp_height": WASB_INPUT_H,
        "inp_width": WASB_INPUT_W,
        "out_height": WASB_INPUT_H,
        "out_width": WASB_INPUT_W,
        "rgb_diff": False,
        "out_scales": [0],
        "MODEL": {
            "EXTRA": {
                "FINAL_CONV_KERNEL": 1,
                "PRETRAINED_LAYERS": ["*"],
                "STEM": {"INPLANES": 64, "STRIDES": [1, 1]},
                "STAGE1": {
                    "NUM_MODULES": 1,
                    "NUM_BRANCHES": 1,
                    "BLOCK": "BOTTLENECK",
                    "NUM_BLOCKS": [1],
                    "NUM_CHANNELS": [32],
                    "FUSE_METHOD": "SUM",
                },
                "STAGE2": {
                    "NUM_MODULES": 1,
                    "NUM_BRANCHES": 2,
                    "BLOCK": "BASIC",
                    "NUM_BLOCKS": [2, 2],
                    "NUM_CHANNELS": [16, 32],
                    "FUSE_METHOD": "SUM",
                },
                "STAGE3": {
                    "NUM_MODULES": 1,
                    "NUM_BRANCHES": 3,
                    "BLOCK": "BASIC",
                    "NUM_BLOCKS": [2, 2, 2],
                    "NUM_CHANNELS": [16, 32, 64],
                    "FUSE_METHOD": "SUM",
                },
                "STAGE4": {
                    "NUM_MODULES": 1,
                    "NUM_BRANCHES": 4,
                    "BLOCK": "BASIC",
                    "NUM_BLOCKS": [2, 2, 2, 2],
                    "NUM_CHANNELS": [16, 32, 64, 128],
                    "FUSE_METHOD": "SUM",
                },
                "DECONV": {
                    "NUM_DECONVS": 0,
                    "KERNEL_SIZE": [],
                    "NUM_BASIC_BLOCKS": 2,
                },
            },
            "INIT_WEIGHTS": True,
        },
    }


class WASBBallTracker:
    """Minimal sliding-3-frame ball detector using WASB HRNet."""

    def __init__(
        self,
        weights_path: Optional[str] = None,
        device: Optional[str] = None,
        score_threshold: float = 0.5,
    ):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.score_threshold = score_threshold

        wp = weights_path or str(_DEFAULT_WEIGHTS)
        if not os.path.exists(wp):
            raise FileNotFoundError(f"WASB weights not found: {wp}")

        self.model = HRNet(_default_wasb_cfg())
        ckpt = torch.load(wp, map_location=self.device, weights_only=True)
        state = ckpt.get("model_state_dict", ckpt)
        missing, unexpected = self.model.load_state_dict(state, strict=False)
        if missing:
            logger.warning("WASB: missing keys (%d): %s", len(missing), missing[:5])
        if unexpected:
            logger.warning("WASB: unexpected keys (%d): %s", len(unexpected), unexpected[:5])
        self.model.to(self.device).eval()
        self._fp16 = "cuda" in str(self.device)
        if self._fp16:
            self.model = self.model.half()

        self._buffer: list = []  # last 3 (H, W, 3) BGR frames resized to 512×288
        self._frame_orig_shape: Optional[Tuple[int, int]] = None

        self.detections: list = []  # list of (frame_idx, x, y, score) in ORIGINAL pixel coords
        logger.info("WASBBallTracker loaded: weights=%s device=%s fp16=%s",
                    wp, self.device, self._fp16)

    def detect_frame(self, frame: np.ndarray, frame_idx: int) -> Optional[dict]:
        """Feed one BGR frame (any HxW). Returns detection dict when 3-frame
        window is filled, else None.
        """
        if self._frame_orig_shape is None:
            self._frame_orig_shape = frame.shape[:2]

        resized = cv2.resize(frame, (WASB_INPUT_W, WASB_INPUT_H))
        self._buffer.append(resized)
        if len(self._buffer) > WASB_FRAMES_IN:
            self._buffer.pop(0)
        if len(self._buffer) < WASB_FRAMES_IN:
            return None

        # Build input tensor (1, 9, 288, 512)
        arr = np.concatenate(self._buffer, axis=2).astype(np.float32) / 255.0
        ten = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(self.device)
        if self._fp16:
            ten = ten.half()

        with torch.no_grad():
            y_out = self.model(ten)  # dict {scale: (1, frames_out, H, W)}
        # Take scale 0 (full-res heatmap output)
        heatmap = torch.sigmoid(y_out[0]).float().cpu().numpy()[0]  # (frames_out, H, W)
        # Use last frame's heatmap (frame t — the most recent)
        hm = heatmap[-1]

        peak_val = float(hm.max())
        if peak_val < self.score_threshold:
            return None

        # Peak pixel in model coords
        peak_y_m, peak_x_m = np.unravel_index(int(hm.argmax()), hm.shape)

        # Rescale to original frame coordinates
        orig_h, orig_w = self._frame_orig_shape
        scale_x = orig_w / WASB_INPUT_W
        scale_y = orig_h / WASB_INPUT_H
        x = float(peak_x_m * scale_x)
        y = float(peak_y_m * scale_y)

        det = {
            "frame_idx": frame_idx,
            "x": x, "y": y,
            "score": peak_val,
            "model_x": int(peak_x_m),
            "model_y": int(peak_y_m),
        }
        self.detections.append(det)
        return det

    def reset(self):
        self._buffer.clear()
        self.detections.clear()
        self._frame_orig_shape = None
