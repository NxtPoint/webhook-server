"""Serve model v1 — small MLP scorer over anchor features.

Same scale as the bounce CNN (~150KB): trains in minutes on CPU, tiny
enough to bundle in the Batch image via the wholesale models/ COPY layer.
Torch is lazy-imported so candidate/feature code stays importable on
Render (which has no torch).
"""
from __future__ import annotations

import logging

import numpy as np

from ml_pipeline.serve_model.features import N_FEATURES

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.5
MIN_SERVE_GAP_S = 5.0  # same NMS gap as serve_detector


def build_mlp():
    import torch.nn as nn
    return nn.Sequential(
        nn.Linear(N_FEATURES, 64), nn.ReLU(), nn.Dropout(0.2),
        nn.Linear(64, 32), nn.ReLU(), nn.Dropout(0.2),
        nn.Linear(32, 1),
    )


def score(model, X: np.ndarray) -> np.ndarray:
    import torch
    model.eval()
    with torch.no_grad():
        logits = model(torch.from_numpy(X.astype(np.float32)))
        return torch.sigmoid(logits).squeeze(-1).numpy()


def nms(anchor_ts, scores, threshold: float = DEFAULT_THRESHOLD,
        gap_s: float = MIN_SERVE_GAP_S):
    """Greedy score-ordered NMS: highest-scoring anchors win, later anchors
    within gap_s of a kept one are suppressed. Returns kept indices sorted
    by ts. (Score-ordered, not time-ordered — a time-ordered greedy pass
    freezes on a bad early anchor; feedback_greedy_chain_rejection.)"""
    order = np.argsort(-np.asarray(scores))
    kept: list[int] = []
    for i in order:
        if scores[i] < threshold:
            break
        if any(abs(anchor_ts[i] - anchor_ts[j]) < gap_s for j in kept):
            continue
        kept.append(int(i))
    return sorted(kept, key=lambda i: anchor_ts[i])


def save(model, path: str, meta: dict):
    import torch
    torch.save({"state_dict": model.state_dict(), "meta": meta}, path)
    logger.info("serve_model: saved %s (%s)", path, meta)


def load(path: str):
    import torch
    ckpt = torch.load(path, map_location="cpu", weights_only=False)
    model = build_mlp()
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    return model, ckpt.get("meta", {})
