"""Batch-side serve-candidate inference -- anchors -> features -> MLP scores.

Mirrors the bounce stage shape (`bounce_detector.detector.detect_bounces_offline`):
pure in-memory inputs, returns scored candidate events; the __main__ stage
persists them to ml_analysis.serve_candidates.

FEATURE PARITY IS LOAD-BEARING: inputs here must be built exactly the way
`dataset.load_task_arrays` built them at training time --
  - bounce_ts from the LEGACY is_bounce flags on ball_rows (training tasks
    predate the CNN bounce currency; do NOT swap in CNN events without
    retraining),
  - roi rows as {ts, kp(17x[x,y,conf]), bbox_h} from player_detections_roi
    shapes,
  - far/near pose ts from player_detections frame_idx / sampled fps.

Persists RAW scored anchors above SCORE_FLOOR (no NMS): the Render
consumer applies the operating threshold + NMS so tuning needs no Batch
rerun.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from ml_pipeline.serve_model.candidates import bounce_anchors, pose_anchors, merge_anchors
from ml_pipeline.serve_model.features import featurize
from ml_pipeline.serve_model.model import load, score, DEFAULT_THRESHOLD

logger = logging.getLogger(__name__)

MODEL_VERSION = "serve_model_v1"
SCORE_FLOOR = 0.2  # persist anchors scoring above this; threshold lives Render-side


@dataclass
class ServeCandidate:
    ts: float
    frame_idx: int
    score: float
    anchor_source: str               # 'bounce' | 'pose'
    bounce_court_x: Optional[float]
    bounce_court_y: Optional[float]
    model_version: str
    train_threshold: Optional[float]


def detect_serve_candidates_offline(
    *,
    task_id: str,
    fps: float,
    ball_rows: Sequence[dict],
    roi_rows_raw: Sequence[tuple],   # (frame_idx, keypoints, bbox_y1, bbox_y2)
    far_pose_frames: Sequence[int],
    near_pose_frames: Sequence[int],
    weights_path: str,
) -> List[ServeCandidate]:
    """Score serve-candidate anchors for one task. Returns [] when the
    weights file is absent (image built before the model trained) or no
    anchors generate -- callers treat [] as 'stage produced nothing'."""
    if not os.path.exists(weights_path):
        logger.warning("serve_model: weights not found at %s -- skipping", weights_path)
        return []

    # Arrays exactly as dataset.load_task_arrays builds them.
    roi_ts = sorted(fi / fps for fi, _kp, _y1, _y2 in roi_rows_raw)
    roi_rows = []
    for fi, kp, y1, y2 in roi_rows_raw:
        if isinstance(kp, str):
            kp = json.loads(kp)
        bbox_h = (y2 - y1) if (y1 is not None and y2 is not None) else None
        roi_rows.append({"ts": fi / fps, "kp": kp, "bbox_h": bbox_h})
    far_ts = sorted(fi / fps for fi in far_pose_frames)
    near_ts = sorted(fi / fps for fi in near_pose_frames)
    with_y = [(b["frame_idx"] / fps, b["y"]) for b in ball_rows if b.get("y") is not None]
    ball_t = np.array([t for t, _ in with_y], dtype=np.float64)
    ball_y = np.array([y for _, y in with_y], dtype=np.float64)
    bounce_ts = sorted(b["frame_idx"] / fps for b in ball_rows if b.get("is_bounce"))

    anchors = merge_anchors(bounce_anchors(list(ball_rows), fps), pose_anchors(roi_ts))
    if not anchors:
        logger.info("serve_model: no candidate anchors for %s", task_id[:8])
        return []

    X = np.stack([
        featurize(a, bounce_ts, roi_ts, far_ts, near_ts, ball_t, ball_y,
                  roi_rows=roi_rows)
        for a in anchors
    ])

    model, meta = load(weights_path)
    train_thr = float(meta.get("threshold", DEFAULT_THRESHOLD))
    scores = score(model, X)

    out = [
        ServeCandidate(
            ts=float(a.ts),
            frame_idx=int(round(a.ts * fps)),
            score=float(s),
            anchor_source=a.source,
            bounce_court_x=a.bounce_court_x,
            bounce_court_y=a.bounce_court_y,
            model_version=MODEL_VERSION,
            train_threshold=train_thr,
        )
        for a, s in zip(anchors, scores) if s >= SCORE_FLOOR
    ]
    logger.info(
        "serve_model: %s -> %d anchors scored, %d above floor %.2f "
        "(train_thr %.2f, heldout_f1 %s)",
        task_id[:8], len(anchors), len(out), SCORE_FLOOR,
        train_thr, meta.get("heldout_f1"),
    )
    return out
