"""
ml_pipeline/training/tracknet_dataset.py — PyTorch Dataset for TrackNet V2 fine-tuning.

Each sample is a sliding window of 3 consecutive BGR frames stacked into a
(9, H, W) float32 tensor, normalised to [0, 1].  The label is a 2-D Gaussian
heatmap centred on the ball position (sigma=2.5 px) at the same resolution as
the model input (640×360).  Frames where the ball is not visible produce an
all-zero heatmap.

Label JSON format (produced by export_labels.py):
    {
        "labels": [
            {"frame_idx": 42, "x": 320.5, "y": 180.3},
            ...
        ]
    }

x, y must be in pixel coordinates relative to the 640×360 model input space.
If your source labels are in a different resolution, rescale before saving.

Example:
    from ml_pipeline.training.tracknet_dataset import TrackNetDataset
    ds = TrackNetDataset("./frames", "./labels.json")
    sample, heatmap = ds[0]
    # sample: torch.Tensor (9, 360, 640)  float32  [0, 1]
    # heatmap: torch.Tensor (360, 640)    float32  [0, 1]
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import torch
from torch.utils.data import Dataset

logger = logging.getLogger(__name__)

# Default model input size — must match TrackNet V2 training resolution
_DEFAULT_INPUT_W = 640
_DEFAULT_INPUT_H = 360
_DEFAULT_SIGMA = 2.5       # Gaussian sigma in pixels (model-input space)
_SEQUENCE_LENGTH = 3       # TrackNet V2: 3 consecutive frames


def _make_gaussian_heatmap(
    x: float,
    y: float,
    width: int,
    height: int,
    sigma: float,
) -> np.ndarray:
    """Return a 2-D Gaussian heatmap as float32 array (H, W) with peak at (x, y).

    Values are normalised so the peak equals 1.0.  If (x, y) is outside the
    frame, the heatmap is all zeros.
    """
    if x < 0 or x >= width or y < 0 or y >= height:
        return np.zeros((height, width), dtype=np.float32)

    xs = np.arange(width, dtype=np.float32)
    ys = np.arange(height, dtype=np.float32)
    grid_x, grid_y = np.meshgrid(xs, ys)

    heatmap = np.exp(
        -((grid_x - x) ** 2 + (grid_y - y) ** 2) / (2 * sigma ** 2)
    )
    return heatmap.astype(np.float32)


def _load_labels(labels_json: str) -> Dict[int, Tuple[float, float]]:
    """Load labels JSON and return a frame_idx -> (x, y) dict.

    Accepts either the full dict produced by export_labels.py (with a top-level
    "labels" key) or a plain list of label objects.

    Only entries with non-None x and y are included.
    """
    data = json.loads(Path(labels_json).read_text())
    if isinstance(data, dict):
        raw = data.get("labels", [])
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError(f"Unexpected labels JSON format in {labels_json}")

    by_frame: Dict[int, Tuple[float, float]] = {}
    for entry in raw:
        frame_idx = entry.get("frame_idx")
        x = entry.get("x")
        y = entry.get("y")
        if frame_idx is None or x is None or y is None:
            continue
        by_frame[int(frame_idx)] = (float(x), float(y))

    logger.info("Loaded %d ball labels from %s", len(by_frame), labels_json)
    return by_frame


def _frame_path(frames_dir: str, frame_idx: int) -> str:
    """Return the path for frame_{frame_idx:06d}.jpg."""
    return os.path.join(frames_dir, f"frame_{frame_idx:06d}.jpg")


class TrackNetDataset(Dataset):
    """PyTorch Dataset for TrackNet V2 fine-tuning.

    Iterates over sequences of `sequence_length` consecutive frames extracted
    from a single video.  The label (Gaussian heatmap or zeros) corresponds to
    the LAST (most recent) frame in the window — matching the TrackNet V2
    convention where the model predicts the ball position in frame t given
    frames [t-2, t-1, t].

    Args:
        frames_dir:
            Directory containing frame JPEG files named frame_000000.jpg …
        labels_json:
            Path to JSON file with ball positions.  Format: see module docstring.
        sequence_length:
            Number of consecutive frames per sample (default 3 for V2).
        input_size:
            (width, height) to resize frames to (default (640, 360)).
        sigma:
            Gaussian sigma in pixels for the heatmap label (default 2.5).
        skip_no_label_middle:
            If True (default), skip sequences where the MIDDLE frame has no
            ball label.  This avoids training on sequences where the ball
            trajectory is completely absent during the window.  The last-frame
            label is always used regardless.
    """

    def __init__(
        self,
        frames_dir: str,
        labels_json: str,
        sequence_length: int = _SEQUENCE_LENGTH,
        input_size: Tuple[int, int] = (_DEFAULT_INPUT_W, _DEFAULT_INPUT_H),
        sigma: float = _DEFAULT_SIGMA,
        skip_no_label_middle: bool = True,
    ):
        self.frames_dir = frames_dir
        self.labels_json = labels_json
        self.sequence_length = sequence_length
        self.input_w, self.input_h = input_size
        self.sigma = sigma
        self.skip_no_label_middle = skip_no_label_middle

        # Load label lookup: frame_idx -> (x, y)
        self._labels = _load_labels(labels_json)

        # Discover available frame files and sort by index
        self._frame_indices = self._discover_frames()

        # Build valid sample windows
        self._samples = self._build_samples()

        logger.info(
            "TrackNetDataset: frames_dir=%s  frames=%d  labels=%d  samples=%d",
            frames_dir, len(self._frame_indices), len(self._labels), len(self._samples),
        )

    def _discover_frames(self) -> List[int]:
        """Scan frames_dir and return sorted list of available frame indices."""
        frames_dir = Path(self.frames_dir)
        if not frames_dir.is_dir():
            raise FileNotFoundError(f"frames_dir not found: {self.frames_dir}")

        indices = []
        for f in frames_dir.iterdir():
            name = f.name
            if not name.startswith("frame_") or not name.endswith(".jpg"):
                continue
            stem = name[len("frame_"):-len(".jpg")]
            try:
                indices.append(int(stem))
            except ValueError:
                continue

        indices.sort()
        if not indices:
            raise ValueError(f"No frame_*.jpg files found in {self.frames_dir}")
        return indices

    def _build_samples(self) -> List[List[int]]:
        """Build list of valid sequences (each is a list of frame indices).

        A sequence is valid when:
        - All `sequence_length` frame files exist
        - The indices are consecutive (no frame gaps within the window)
        - If skip_no_label_middle: the middle frame has a ball label
        """
        # Fast lookup set
        available = set(self._frame_indices)

        samples = []
        n = self.sequence_length
        mid = n // 2  # index of middle frame within window

        for i in range(len(self._frame_indices) - n + 1):
            window = self._frame_indices[i: i + n]

            # Require strictly consecutive frame indices (no video gaps)
            if window[-1] - window[0] != n - 1:
                continue

            # All frames must exist on disk
            if not all(idx in available for idx in window):
                continue

            # Optionally skip if middle frame has no ball label
            if self.skip_no_label_middle and window[mid] not in self._labels:
                continue

            samples.append(window)

        return samples

    def __len__(self) -> int:
        return len(self._samples)

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """Return (frames_tensor, heatmap_tensor) for sample idx.

        frames_tensor: float32 (9, H, W) — 3 frames × 3 BGR channels, /255
        heatmap_tensor: float32 (H, W)   — Gaussian at ball pos, or zeros
        """
        window = self._samples[idx]

        # Load and resize all frames in the window
        frames_chw = []
        for frame_idx in window:
            path = _frame_path(self.frames_dir, frame_idx)
            frame = cv2.imread(path)
            if frame is None:
                raise FileNotFoundError(f"Cannot read frame file: {path}")
            # Resize to model input size (W, H)
            frame = cv2.resize(frame, (self.input_w, self.input_h))
            # (H, W, 3) uint8 → float32 [0, 1], then (3, H, W)
            arr = frame.astype(np.float32) / 255.0
            frames_chw.append(torch.from_numpy(arr).permute(2, 0, 1))

        # Stack along channel dim: (9, H, W) for 3-frame V2 input
        frames_tensor = torch.cat(frames_chw, dim=0)

        # Label: Gaussian heatmap centred on ball in the LAST frame of window
        last_frame_idx = window[-1]
        if last_frame_idx in self._labels:
            x, y = self._labels[last_frame_idx]
            heatmap_np = _make_gaussian_heatmap(x, y, self.input_w, self.input_h, self.sigma)
        else:
            # Ball not visible in this frame — all-zero heatmap
            heatmap_np = np.zeros((self.input_h, self.input_w), dtype=np.float32)

        heatmap_tensor = torch.from_numpy(heatmap_np)

        return frames_tensor, heatmap_tensor

    def label_stats(self) -> Dict[str, int]:
        """Return a summary of how many samples have / lack ball labels."""
        with_ball = sum(
            1 for w in self._samples if w[-1] in self._labels
        )
        without_ball = len(self._samples) - with_ball
        return {"total": len(self._samples), "with_ball": with_ball, "without_ball": without_ball}
