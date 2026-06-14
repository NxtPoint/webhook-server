"""Production inference for the ADR-02 v2 swing-type classifier.

Runs INSIDE the Batch pipeline (bronze stage). For each detected hit it builds
the same optical-flow input the v2 model was trained on, classifies the swing
type, and writes the answer to PlayerDetection.stroke_class — a BRONZE fact
(persisted to ml_analysis.player_detections.stroke_class by db_writer). Silver
Pass 1 then projects stroke_class -> silver.swing_type verbatim.

This module is the inference twin of training/build_swing_type_dataset.py:
the crop + Farneback flow extraction MUST match the training distribution
exactly, so the ROI math, window geometry and flow params are ported verbatim
from that builder. It is deliberately self-contained (no import of training/,
which is not shipped in the Batch image) and lives under stroke_classifier/,
which the Dockerfile already COPYs.

Frame-space (the bug that ate 62% of training hits — feedback_t5_two_frame_spaces):
  - det.frame_idx is the pipeline's SAMPLED index (target_fps, ~25fps).
  - the optical-flow window is read from the SOURCE-fps video, so the sampled
    index is converted to a source frame via frame_interval = source_fps/target_fps.
  Training read the SOURCE-fps trimmed copy and seeked by the raw SA source
  hit_frame; here we recreate the same source-fps window from the sampled index.

Vocabulary normalisation: the model emits forehand/backhand/overhead; bronze
stores the canonical silver label set (fh/bh/overhead) so silver projects it
verbatim. north_star defines bronze as the "normalised answers" layer, so
canonicalising the label set at the bronze write is a bronze concern.

Handedness: training used the right-handed default (1.0) for every player
(DEFAULT_HANDEDNESS in stroke_classifier/dataset.py; no overrides were passed),
so inference matches that distribution with handedness=1.0 for all hits.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import cv2
import numpy as np
import torch

logger = logging.getLogger(__name__)

# --- Geometry / window constants — PORTED VERBATIM from
#     training/build_swing_type_dataset.py. Changing either side without the
#     other re-introduces a train/inference distribution gap. ---
WINDOW_PRE = 10        # frames before the hit
WINDOW_TOTAL = 16      # 16-frame temporal window (hit sits at index 10)
ROI_SCALE = 1.5
ROI_SIZE = 112

# Model class name -> canonical bronze/silver swing_type vocabulary.
# Old StrokeClassifier emitted silver-vocab directly; v2 emits the long names.
# ADR-02 revision 2026-06-14: `other` is now a real model output (SA-labelled
# non-groundstroke / junk-hit) — projected verbatim so silver can carry it
# instead of a heuristic guess.
_VOCAB_MAP = {"forehand": "fh", "backhand": "bh", "overhead": "overhead",
              "other": "other"}


def _bbox_to_roi(bbox_x1: float, bbox_y1: float, bbox_x2: float, bbox_y2: float,
                 frame_w: int, frame_h: int,
                 scale: float = ROI_SCALE) -> tuple[int, int, int, int]:
    """Square ROI expanded by `scale`, clipped to frame bounds. (x, y, w, h).

    Bbox is in the video's own coord system. At inference the pipeline processes
    the SOURCE video and the bbox is source-res, so — unlike training, which
    read a 720p trimmed copy of a 1080p run — there is NO resolution rescale.
    """
    cx = (bbox_x1 + bbox_x2) / 2.0
    cy = (bbox_y1 + bbox_y2) / 2.0
    half = max(bbox_x2 - bbox_x1, bbox_y2 - bbox_y1) * scale / 2.0
    x = int(round(cx - half))
    y = int(round(cy - half))
    side = int(round(half * 2))
    if x < 0:
        x = 0
    if y < 0:
        y = 0
    if x + side > frame_w:
        x = max(0, frame_w - side)
    if y + side > frame_h:
        y = max(0, frame_h - side)
    side = min(side, frame_w - x, frame_h - y)
    return x, y, side, side


def _compute_flow_window(crops: list[np.ndarray]) -> np.ndarray:
    """Farneback dense flow over a sequence of BGR crops -> (T, H, W, 2) float32.
    First frame's flow is zero-padded. Params match the training builder."""
    T = len(crops)
    if T == 0:
        return np.zeros((0, ROI_SIZE, ROI_SIZE, 2), dtype=np.float32)
    H, W = crops[0].shape[:2]
    flows = np.zeros((T, H, W, 2), dtype=np.float32)
    prev_gray = cv2.cvtColor(crops[0], cv2.COLOR_BGR2GRAY)
    for t in range(1, T):
        curr_gray = cv2.cvtColor(crops[t], cv2.COLOR_BGR2GRAY)
        flow = cv2.calcOpticalFlowFarneback(
            prev_gray, curr_gray, None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
        )
        flows[t] = flow.astype(np.float32)
        prev_gray = curr_gray
    return flows


def _nearest_det(frames: list[int], dets: list, target: int, window: int):
    """Nearest detection to `target` frame within ±window. frames is sorted."""
    if not frames:
        return None
    import bisect
    pos = bisect.bisect_left(frames, target)
    best = None
    best_d = window + 1
    for j in (pos - 1, pos, pos + 1):
        if 0 <= j < len(frames):
            d = abs(frames[j] - target)
            if d < best_d:
                best_d = d
                best = dets[j]
    return best if best_d <= window else None


def classify_strokes_v2(
    result,
    *,
    target_fps: int,
    device: str,
    min_conf: float = 0.5,
    micro_batch: int = 16,
) -> int:
    """Classify swing type for hitter-candidate detections around each bounce.

    For every bounce we snap back HIT_BEFORE_BOUNCE (~0.32s) to the estimated
    contact frame and classify the nearest detection of EACH player_id within a
    tolerance window — over-classifying the non-hitter is harmless because silver
    only reads stroke_class off the resolved hitter (and silver carries a wider
    windowed fallback so exact-frame alignment isn't required). Sets
    det.stroke_class in the canonical fh/bh/overhead vocabulary. Returns the
    number of detections classified.

    STOPGAP-safe: if the v2 weights are absent the classifier reports
    unavailable and this returns 0, leaving silver's pose/position heuristic as
    the live fallback.
    """
    from ml_pipeline.stroke_classifier.model_v2 import SwingTypeClassifierV2

    classifier = SwingTypeClassifierV2(device=device)
    if not classifier.available:
        logger.info("swing_classifier_v2 weights not present — skipping (silver heuristic stays live)")
        return 0

    bounces = [d for d in result.ball_detections if d.is_bounce]
    if not bounces:
        logger.info("swing_classifier_v2: no bounces — nothing to classify")
        return 0

    video_path = result.video_path
    if not video_path or not os.path.exists(video_path):
        logger.warning("swing_classifier_v2: video not accessible (%s) — skipping", video_path)
        return 0

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.warning("swing_classifier_v2: cannot open video %s — skipping", video_path)
        return 0
    try:
        source_fps = cap.get(cv2.CAP_PROP_FPS) or float(target_fps)
        n_src_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        # Sampled (det.frame_idx) -> source frame. Mirrors VideoPreprocessor:
        # it yields one sampled frame per `frame_interval` source frames.
        frame_interval = (source_fps / target_fps) if target_fps < source_fps else 1.0

        # Per-player sorted detection index for nearest-frame lookup.
        from collections import defaultdict
        by_pid_frames: dict = defaultdict(list)
        by_pid_dets: dict = defaultdict(list)
        for pd in sorted(result.player_detections, key=lambda d: d.frame_idx):
            by_pid_frames[pd.player_id].append(pd.frame_idx)
            by_pid_dets[pd.player_id].append(pd)

        hit_offset = max(1, int(round(target_fps * 0.32)))   # silver HIT_BEFORE_BOUNCE
        match_window = max(1, int(round(target_fps * 0.60)))  # ±0.6s tolerance

        # Collect the unique detections to classify (dedupe — a det can be the
        # nearest hitter-candidate for several bounces).
        to_classify: list = []
        seen: set = set()
        for b in bounces:
            hit_est = max(0, b.frame_idx - hit_offset)
            for pid in by_pid_frames:
                det = _nearest_det(by_pid_frames[pid], by_pid_dets[pid], hit_est, match_window)
                if det is None:
                    continue
                key = (det.player_id, det.frame_idx)
                if key in seen:
                    continue
                seen.add(key)
                to_classify.append(det)

        if not to_classify:
            logger.info("swing_classifier_v2: no hitter-candidate detections near bounces")
            return 0

        logger.info(
            "swing_classifier_v2: %d bounces -> %d unique hitter-candidate dets "
            "(source_fps=%.1f target_fps=%d interval=%.2f)",
            len(bounces), len(to_classify), source_fps, target_fps, frame_interval,
        )

        classified = 0
        batch_dets: list = []
        batch_flows: list = []

        def _flush():
            nonlocal classified, batch_dets, batch_flows
            if not batch_flows:
                return
            arr = np.stack(batch_flows, axis=0)                     # (B,16,112,112,2)
            flows = torch.from_numpy(arr).permute(0, 4, 1, 2, 3).contiguous()  # (B,2,16,112,112)
            hand = torch.ones((flows.shape[0], 1), dtype=torch.float32)
            preds = classifier.predict_batch(flows, hand)
            for det, (cls_name, conf) in zip(batch_dets, preds):
                if conf >= min_conf and cls_name in _VOCAB_MAP:
                    det.stroke_class = _VOCAB_MAP[cls_name]
                    classified += 1
            batch_dets = []
            batch_flows = []

        for det in to_classify:
            src_center = int(round(det.frame_idx * frame_interval))
            start = src_center - WINDOW_PRE
            if start < 0 or start + WINDOW_TOTAL > n_src_frames:
                continue
            bx1, by1, bx2, by2 = det.bbox
            if (bx2 - bx1) < 4 or (by2 - by1) < 4:
                continue
            roi_x, roi_y, roi_w, roi_h = _bbox_to_roi(bx1, by1, bx2, by2, video_w, video_h)
            if roi_w < 4 or roi_h < 4:
                continue

            cap.set(cv2.CAP_PROP_POS_FRAMES, start)
            crops = []
            ok = True
            for _ in range(WINDOW_TOTAL):
                ret, fr = cap.read()
                if not ret:
                    ok = False
                    break
                crop = fr[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
                if crop.size == 0:
                    crop = np.zeros((roi_h, roi_w, 3), dtype=np.uint8)
                crop = cv2.resize(crop, (ROI_SIZE, ROI_SIZE), interpolation=cv2.INTER_AREA)
                crops.append(crop)
            if not ok or len(crops) < WINDOW_TOTAL:
                continue

            batch_flows.append(_compute_flow_window(crops))
            batch_dets.append(det)
            if len(batch_flows) >= micro_batch:
                _flush()
        _flush()

        logger.info(
            "swing_classifier_v2: classified %d/%d candidate dets (min_conf=%.2f)",
            classified, len(to_classify), min_conf,
        )
        return classified
    finally:
        cap.release()
