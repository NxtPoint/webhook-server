"""
Small CNN for classifying stroke type from optical flow tensors.

Input: (batch, 2*FLOW_WINDOW, CROP_H, CROP_W, 2) — dense optical flow
       around a hit event. Channels are (dx, dy) flow vectors.

Output: 5-class softmax — fh, bh, serve, volley, other

Architecture: Lightweight 3D-CNN (temporal convolutions over the flow sequence).
Designed to run on CPU in <5ms per hit — no GPU required at inference.
Total params: ~50K — trivial to train on 200+ labeled examples.
"""

import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional

from ml_pipeline.stroke_classifier.flow_extractor import FLOW_WINDOW, CROP_H, CROP_W

# Stroke classes — order matters (matches label encoding)
STROKE_CLASSES = ["fh", "bh", "serve", "volley", "other"]
NUM_CLASSES = len(STROKE_CLASSES)

# Model weights path
STROKE_MODEL_WEIGHTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "models", "stroke_classifier.pt",
)


class StrokeFlowCNN(nn.Module):
    """Lightweight 3D-CNN for optical flow stroke classification.

    Input shape: (B, 2, T, H, W) where:
      B = batch size
      2 = flow channels (dx, dy)
      T = 2*FLOW_WINDOW = 10 temporal frames
      H = CROP_H = 64
      W = CROP_W = 48

    Uses 3D convolutions to capture spatiotemporal swing patterns.
    """

    def __init__(self):
        super().__init__()
        T = 2 * FLOW_WINDOW  # 10

        # Block 1: temporal + spatial reduction
        self.conv1 = nn.Conv3d(2, 16, kernel_size=(3, 5, 5), stride=(1, 2, 2), padding=(1, 2, 2))
        self.bn1 = nn.BatchNorm3d(16)
        # After: (B, 16, 10, 32, 24)

        # Block 2: further reduction
        self.conv2 = nn.Conv3d(16, 32, kernel_size=(3, 3, 3), stride=(2, 2, 2), padding=(1, 1, 1))
        self.bn2 = nn.BatchNorm3d(32)
        # After: (B, 32, 5, 16, 12)

        # Block 3: collapse temporal
        self.conv3 = nn.Conv3d(32, 64, kernel_size=(3, 3, 3), stride=(2, 2, 2), padding=(1, 1, 1))
        self.bn3 = nn.BatchNorm3d(64)
        # After: (B, 64, 3, 8, 6)

        # Global average pool → (B, 64)
        self.gap = nn.AdaptiveAvgPool3d(1)

        # Classifier
        self.fc1 = nn.Linear(64, 32)
        self.dropout = nn.Dropout(0.3)
        self.fc2 = nn.Linear(32, NUM_CLASSES)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass. Input: (B, 2, T, H, W). Output: (B, NUM_CLASSES) logits."""
        x = F.relu(self.bn1(self.conv1(x)))
        x = F.relu(self.bn2(self.conv2(x)))
        x = F.relu(self.bn3(self.conv3(x)))
        x = self.gap(x).flatten(1)  # (B, 64)
        x = F.relu(self.fc1(x))
        x = self.dropout(x)
        return self.fc2(x)

    def predict(self, x: torch.Tensor) -> tuple:
        """Predict class and confidence.

        Args:
            x: (B, 2, T, H, W) tensor

        Returns:
            (class_names: list[str], confidences: list[float])
        """
        self.eval()
        with torch.no_grad():
            logits = self.forward(x)
            probs = F.softmax(logits, dim=1)
            confidences, indices = probs.max(dim=1)
            names = [STROKE_CLASSES[i] for i in indices.tolist()]
            return names, confidences.tolist()


class StrokeClassifier:
    """High-level wrapper for stroke classification.

    Handles model loading, input preprocessing, and graceful fallback
    when weights are unavailable.
    """

    def __init__(self, device: str = "cpu"):
        self.device = device
        self.model: Optional[StrokeFlowCNN] = None
        self._available = False
        self._load_model()

    def _load_model(self):
        """Load model weights if available."""
        if not os.path.exists(STROKE_MODEL_WEIGHTS):
            return
        try:
            self.model = StrokeFlowCNN()
            state = torch.load(STROKE_MODEL_WEIGHTS, map_location=self.device, weights_only=True)
            self.model.load_state_dict(state)
            self.model.to(self.device)
            self.model.eval()
            self._available = True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to load stroke classifier: {e}")
            self.model = None

    @property
    def available(self) -> bool:
        """Whether the classifier has loaded weights and can make predictions."""
        return self._available

    def classify(self, flow_tensor: "numpy.ndarray") -> tuple:
        """Classify a single flow tensor.

        Args:
            flow_tensor: shape (T, H, W, 2) from flow_extractor.flow_to_input_tensor()

        Returns:
            (stroke_class: str, confidence: float) or ("other", 0.0) if unavailable.
        """
        if not self._available:
            return "other", 0.0

        import numpy as np

        # Reshape: (T, H, W, 2) → (1, 2, T, H, W) for Conv3d
        t = torch.from_numpy(flow_tensor).float()
        t = t.permute(3, 0, 1, 2).unsqueeze(0)  # (1, 2, T, H, W)
        t = t.to(self.device)

        names, confs = self.model.predict(t)
        return names[0], confs[0]

    def classify_batch(self, flow_tensors: list) -> list:
        """Classify multiple flow tensors at once.

        Args:
            flow_tensors: list of (T, H, W, 2) arrays

        Returns:
            list of (stroke_class, confidence) tuples
        """
        if not self._available or not flow_tensors:
            return [("other", 0.0)] * len(flow_tensors)

        import numpy as np

        batch = []
        for ft in flow_tensors:
            t = torch.from_numpy(ft).float().permute(3, 0, 1, 2)
            batch.append(t)

        x = torch.stack(batch, dim=0).to(self.device)
        names, confs = self.model.predict(x)
        return list(zip(names, confs))
