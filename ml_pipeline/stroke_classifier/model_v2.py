"""ADR-02 v2 swing-type classifier — R(2+1)D-18 on optical flow.

Per ADR-02 §"Build spec v1" (docs/_investigation/adr_02_swing_type_classifier_plan.md):
  - Architecture: R(2+1)D-18 (torchvision.models.video.r2plus1d_18)
  - Input: 16-frame x 112x112 dense optical flow, 2-channel (dx, dy)
    -> reshaped from (N, 16, 112, 112, 2) -> (N, 2, 16, 112, 112) at dataloader time
  - Handedness: 1-bit feature concatenated to the penultimate FC layer
  - Output: 3 classes {forehand=0, backhand=1, overhead=2} + per-class confidences

Two adaptations from the stock torchvision implementation:
  1. The stem's first conv is 3-channel (RGB-pretrained). We replace it with a
     2-channel conv (flow dx, dy). Pretrained weights are NOT loaded -- we train
     from random init on our 368-hit (eventually ~2-3k) optical-flow corpus.
  2. The penultimate 512-dim global-avg-pool feature is concatenated with the
     1-bit handedness flag (1.0 = right, 0.0 = left) before the final FC.

STOPGAP semantics (mirrors bounce_detector/cnn.py): until trained weights exist
at MODEL_WEIGHTS_V2, the classifier returns no predictions -- caller of
predict_batch() should be ready for an empty list. Until then the silver
pose-keypoint inference STOPGAP in build_silver_match_t5._infer_swing_type_*
remains the live path; the new v2 inference call is wired but flagged as
"weights not present" and skipped cleanly.

Volume target before flipping STOPGAP off: ~2,000-3,000 labelled hit-events
(ADR-02 spec line 109). Today: 368 training-ready hits across 3 matches.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F


logger = logging.getLogger(__name__)

# Class order (matches Dataset label encoding -- keep in sync)
CLASSES = ("forehand", "backhand", "overhead")
NUM_CLASSES = len(CLASSES)
CLASS_TO_IDX = {c: i for i, c in enumerate(CLASSES)}

# Input tensor shape (B, C=2 flow channels, T=16 frames, H=112, W=112)
INPUT_FLOW_CHANNELS = 2
INPUT_T = 16
INPUT_H = 112
INPUT_W = 112

# Pre-FC feature dim from torchvision r2plus1d_18 (after global avg pool)
R2PLUS1D_FEATURE_DIM = 512

# Handedness bit added to the penultimate FC input
HANDEDNESS_DIM = 1

# Trained weights path (gitignored). STOPGAP: model returns no predictions
# until this file exists. Filename follows the `bounce_detector_v1.pt`
# convention -- v-suffix lets us bump architecture without overwriting.
MODEL_WEIGHTS_V2 = str(Path(__file__).resolve().parent.parent / "models" / "swing_classifier_v2.pt")


class SwingTypeR2plus1D(nn.Module):
    """R(2+1)D-18 swing-type classifier per ADR-02 v1 spec.

    Forward signature:
        model(flow, handedness) -> logits (B, NUM_CLASSES)
        flow:       (B, 2, 16, 112, 112) float32 in approx [-50, +50] (Farneback)
        handedness: (B, 1) float32, 1.0 = right, 0.0 = left
    """

    def __init__(self) -> None:
        super().__init__()
        # torchvision is in requirements (used elsewhere in ml_pipeline)
        from torchvision.models.video import r2plus1d_18

        backbone = r2plus1d_18(weights=None)

        # Swap the 3-channel RGB stem for a 2-channel optical-flow stem.
        # torchvision's stem is Sequential([Conv3d(3,45,...), BN, ReLU, Conv3d(...), ...]).
        # We only need to replace stem[0] -- preserve the rest.
        orig_stem0 = backbone.stem[0]
        new_stem0 = nn.Conv3d(
            in_channels=INPUT_FLOW_CHANNELS,
            out_channels=orig_stem0.out_channels,
            kernel_size=orig_stem0.kernel_size,
            stride=orig_stem0.stride,
            padding=orig_stem0.padding,
            bias=(orig_stem0.bias is not None),
        )
        backbone.stem[0] = new_stem0

        # Drop torchvision's final FC -- we'll attach our own after handedness concat.
        backbone.fc = nn.Identity()
        self.backbone = backbone

        # Penultimate head: concat(feature_512, handedness_1) -> 256 -> NUM_CLASSES
        self.fc1 = nn.Linear(R2PLUS1D_FEATURE_DIM + HANDEDNESS_DIM, 256)
        self.dropout = nn.Dropout(0.3)
        self.fc2 = nn.Linear(256, NUM_CLASSES)

    def forward(self, flow: torch.Tensor, handedness: torch.Tensor) -> torch.Tensor:
        feats = self.backbone(flow)  # (B, 512)
        x = torch.cat([feats, handedness], dim=1)  # (B, 513)
        x = F.relu(self.fc1(x))
        x = self.dropout(x)
        return self.fc2(x)  # (B, NUM_CLASSES) logits


class SwingTypeClassifierV2:
    """High-level wrapper -- mirror of bounce_detector.cnn.BounceCNN entry-point shape.

    Loads weights from MODEL_WEIGHTS_V2 if present; otherwise stays in STOPGAP
    mode (`available` is False, `predict_batch` returns []). Inference callers
    should check `available` before assuming anything.
    """

    def __init__(self, device: str = "cpu", weights_path: Optional[str] = None) -> None:
        self.device = device
        self.weights_path = weights_path or MODEL_WEIGHTS_V2
        self.model: Optional[SwingTypeR2plus1D] = None
        self._available = False
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.weights_path):
            logger.info(
                "swing_classifier_v2 STOPGAP — weights not present at %s; "
                "predict_batch will return [] (silver pose-keypoint STOPGAP remains live)",
                self.weights_path,
            )
            return
        try:
            self.model = SwingTypeR2plus1D()
            state = torch.load(self.weights_path, map_location=self.device, weights_only=True)
            # Allow either a raw state_dict or a {'state_dict': ..., 'meta': ...} sidecar
            if isinstance(state, dict) and "state_dict" in state:
                state = state["state_dict"]
            self.model.load_state_dict(state)
            self.model.to(self.device)
            self.model.eval()
            self._available = True
            logger.info("swing_classifier_v2 weights loaded from %s", self.weights_path)
        except Exception as e:
            logger.warning("swing_classifier_v2 failed to load weights: %s", e)
            self.model = None
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @torch.no_grad()
    def predict_batch(
        self,
        flows: torch.Tensor,
        handedness: torch.Tensor,
    ) -> list[tuple[str, float]]:
        """Predict per-hit class + confidence.

        Args:
            flows:      (B, 2, 16, 112, 112) float32
            handedness: (B, 1) float32 -- 1.0 right, 0.0 left

        Returns:
            List of (class_name, confidence) tuples of length B.
            Empty list if model unavailable (STOPGAP).
        """
        if not self._available:
            return []
        flows = flows.to(self.device)
        handedness = handedness.to(self.device)
        logits = self.model(flows, handedness)
        probs = F.softmax(logits, dim=1)
        confs, idxs = probs.max(dim=1)
        return [(CLASSES[int(i)], float(c)) for i, c in zip(idxs.tolist(), confs.tolist())]
