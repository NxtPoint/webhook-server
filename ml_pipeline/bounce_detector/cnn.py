"""1D temporal CNN for tennis ground-bounce detection.

Architecture per ADR-01 §"Build spec v1":
  - 3 conv blocks: kernel 5, channels 32 -> 64 -> 64
  - Dropout 0.3 between blocks
  - Sigmoid head -> per-frame P(bounce) over the input window
  - 14 input channels, 41-frame window (~±20 frames @ 30 fps)
  - CPU-runnable; same envelope as serve_detector

Blueprint: TTNet (CVPR-W 2020) + Precise Event Spotting (ECCV 2022).
Lightweight temporal CNN beats heavier transformers when data budget
< 1000 labels and CPU latency matters — which is exactly v1's situation
(488 corpus labels).

# STOPGAP-untrained-stage1: This file ships in v0 with RANDOM weights.
# load_weights() handles the weights-absent case gracefully so the
# detector plumbing is end-to-end runnable without trained weights.
# Training (and the lock-in of v1 weights) is the next-session job.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Architectural constants — kept module-level so feature_extractor + bench
# can import them without instantiating the model.
N_CHANNELS = 14
WINDOW_FRAMES = 41          # ±20 frames around candidate (centre frame at idx 20)
CENTRE_IDX = WINDOW_FRAMES // 2
CONV_KERNEL = 5
CONV_CHANNELS = (32, 64, 64)
DROPOUT_P = 0.3


# Torch is heavy. Lazy-import so `from ml_pipeline.bounce_detector import ...`
# doesn't pull torch into services that only need the schema/db helpers.
def _torch():
    import torch  # noqa
    return torch


def _nn():
    import torch.nn as nn
    return nn


def build_model():
    """Construct the temporal CNN per ADR-01 spec. Returns a torch nn.Module.

    Input shape:  (batch, N_CHANNELS=14, WINDOW_FRAMES=41)
    Output shape: (batch, 1) — sigmoid P(bounce at centre frame)

    Pad='same' (kernel 5 -> padding 2) preserves the temporal dim through
    the conv stack; a final adaptive-pool + linear collapses to one
    per-window prediction at the centre frame, matching the PES task
    formulation in Hong et al. ECCV 2022.
    """
    nn = _nn()

    class BounceCNN(nn.Module):
        def __init__(self):
            super().__init__()
            ch_in = N_CHANNELS
            layers = []
            for ch_out in CONV_CHANNELS:
                layers.extend([
                    nn.Conv1d(ch_in, ch_out, kernel_size=CONV_KERNEL,
                              padding=CONV_KERNEL // 2),
                    nn.BatchNorm1d(ch_out),
                    nn.ReLU(inplace=True),
                    nn.Dropout(p=DROPOUT_P),
                ])
                ch_in = ch_out
            self.conv = nn.Sequential(*layers)
            # Collapse the temporal dim to a single per-window logit.
            self.pool = nn.AdaptiveAvgPool1d(1)
            self.head = nn.Linear(CONV_CHANNELS[-1], 1)
            self.sigmoid = nn.Sigmoid()

        def forward(self, x):
            # x: (B, C, T)
            h = self.conv(x)              # (B, 64, T)
            h = self.pool(h).squeeze(-1)  # (B, 64)
            logit = self.head(h)          # (B, 1)
            return self.sigmoid(logit)

    return BounceCNN()


class BounceCNNWrapper:
    """Thin wrapper around the torch nn.Module that handles weight loading
    + inference + the untrained-stage1 stopgap.

    Use this class — not the bare nn.Module — from the detector. Keeps the
    detector's import surface narrow (no torch import in the detector hot
    path until needed) and makes the "weights absent" path explicit.
    """

    def __init__(self):
        self._model = None
        self._weights_loaded = False

    def _ensure_model(self):
        if self._model is None:
            self._model = build_model()
            self._model.eval()

    def load_weights(self, weights_path: Optional[Path]) -> bool:
        """Load weights from disk. Returns True on success, False if the
        file is missing or unreadable. STOPGAP-untrained-stage1: a False
        return is the expected v0 path — random weights are kept, the
        scoring layer still runs, downstream gets all-zeros (sigmoid of
        random init averages ~0.5 but the bench expects no thresholding
        of those values anyway in v0; once trained, threshold is 0.55
        per ADR §"Threshold defaults").
        """
        self._ensure_model()
        if weights_path is None:
            logger.warning(
                "bounce_detector: STOPGAP-untrained-stage1 — no weights path "
                "supplied; model running with random init. Outputs are NOT "
                "calibrated; v0 bench is plumbing-only."
            )
            self._weights_loaded = False
            return False
        wp = Path(weights_path)
        if not wp.exists():
            logger.warning(
                "bounce_detector: STOPGAP-untrained-stage1 — weights file "
                "%s missing; model running with random init.", wp,
            )
            self._weights_loaded = False
            return False
        try:
            torch = _torch()
            state = torch.load(wp, map_location="cpu")
            # Accept either a bare state_dict OR the trainer's wrapped
            # format {"state_dict": ..., "meta": ...} — matches the
            # swing-type training pattern.
            sd = state.get("state_dict", state) if isinstance(state, dict) else state
            self._model.load_state_dict(sd)
            self._weights_loaded = True
            meta = state.get("meta") if isinstance(state, dict) else None
            if meta:
                logger.info(
                    "bounce_detector: loaded weights from %s "
                    "(best_epoch=%s best_%s=%.4f n_train=%s n_val=%s)",
                    wp, meta.get("best_epoch"),
                    meta.get("best_metric_name", "metric"),
                    meta.get("best_metric_value", float("nan")),
                    meta.get("n_train"), meta.get("n_val"),
                )
            else:
                logger.info("bounce_detector: loaded weights from %s", wp)
            return True
        except Exception:
            logger.exception("bounce_detector: failed to load weights from %s; "
                             "falling back to random init", wp)
            self._weights_loaded = False
            return False

    @property
    def weights_loaded(self) -> bool:
        return self._weights_loaded

    def score(self, features) -> float:
        """Score one (N_CHANNELS, WINDOW_FRAMES) feature window.

        Returns P(bounce) in [0, 1]. With random weights (v0 stage-1) the
        value is uncalibrated noise — the caller MUST treat it as zero
        for v0 bench purposes. We don't hardcode the 0.0 return here so
        we can still smoke-test the torch forward pass.
        """
        import numpy as np
        torch = _torch()
        self._ensure_model()
        arr = np.ascontiguousarray(features, dtype=np.float32)
        if arr.shape != (N_CHANNELS, WINDOW_FRAMES):
            raise ValueError(
                f"bounce_detector.cnn.score expected shape "
                f"({N_CHANNELS},{WINDOW_FRAMES}); got {arr.shape}"
            )
        with torch.no_grad():
            tensor = torch.from_numpy(arr).unsqueeze(0)  # (1, C, T)
            p = self._model(tensor).item()
        return float(p)

    def score_batch(self, features_batch) -> list:
        """Score a batch of windows. Returns a list of floats.

        features_batch: array of shape (B, N_CHANNELS, WINDOW_FRAMES).
        """
        import numpy as np
        torch = _torch()
        self._ensure_model()
        arr = np.ascontiguousarray(features_batch, dtype=np.float32)
        if arr.ndim != 3 or arr.shape[1:] != (N_CHANNELS, WINDOW_FRAMES):
            raise ValueError(
                f"bounce_detector.cnn.score_batch expected shape "
                f"(B,{N_CHANNELS},{WINDOW_FRAMES}); got {arr.shape}"
            )
        with torch.no_grad():
            tensor = torch.from_numpy(arr)
            p = self._model(tensor).squeeze(-1).cpu().numpy().tolist()
        return [float(x) for x in p]
