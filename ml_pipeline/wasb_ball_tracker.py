"""WASB-SBDT ball detector — HRNet backbone drop-in replacement for TrackNet V2.

WASB (Widely Applicable Strong Baseline) is the BMVC 2023 tennis ball
detector that outperforms TrackNet V2 specifically on small/fast balls
in broadcast-style footage. Architecture: HRNet with 4 parallel multi-res
branches — keeps high-resolution features at full stride, no downsample→
upsample information loss that hurts sub-pixel ball detection.

Source: https://github.com/nttcom/WASB-SBDT (MIT license)
Paper:  https://arxiv.org/abs/2311.05237
Weights: wasb_tennis_best.pth.tar in ml_pipeline/models/ (6.1 MB)

Input contract (matches BallTracker.detect_frame):
  - one BGR frame per call (any HxW — wrapper resizes to 512×288 internally)
  - frame_idx (int)

Output contract (matches BallTracker — DROP-IN COMPATIBLE):
  - detect_frame returns Optional[BallDetection] (was: Optional[dict] pre-2026-05-21
    refactor; archived diag tools in ml_pipeline/diag/_archive/ depend on the
    old dict format — bench tools work with both formats via _normalise_detection)
  - self.detections: List[BallDetection], same shape as BallTracker.detections
  - Post-processing methods (interpolate_gaps, _filter_outliers, detect_bounces,
    compute_speeds, assign_peak_flight_speeds, log_diagnostics, reset) match
    BallTracker semantics. They operate on self.detections — tracker-agnostic.

The pipeline.TennisAnalysisPipeline picks between BallTracker and WASBBallTracker
via the BALL_TRACKER env var. Default is `tracknet_v2` in code; set
`BALL_TRACKER=wasb` on Render's main API service to flip production.

WASB benchmarked materially better than TrackNetV2 on the 880dff02 SA point 6
coverage gap (2/9 vs 0/9 strokes recovered) per
ml_pipeline/diag/bench_ball_baseline.json — see commit `7100792`.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
import torch

from ml_pipeline.ball_tracker import BallDetection
from ml_pipeline.config import (
    BALL_MAX_INTERPOLATION_GAP,
    BALL_MAX_DIST_BETWEEN_FRAMES,
    BALL_MAX_DIST_GAP,
    BALL_FILTER_REANCHOR_RUN,
    BALL_BATCH_SIZE,
    BOUNCE_VELOCITY_WINDOW,
    COURT_LENGTH_M,
    COURT_WIDTH_DOUBLES_M,
    FRAME_SAMPLE_FPS,
)
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
    """HRNet ball detector with BallTracker-compatible interface.

    Sliding 3-frame window, HRNet sigmoid heatmap, peak in original-frame
    pixel coords. Detections below score_threshold (default 0.5) return None
    — no fallback fires, no motion-noise. This is the key difference from
    TrackNetV2's permissive 4-tier strategy.
    """

    def __init__(
        self,
        weights_path: Optional[str] = None,
        device: Optional[str] = None,
        score_threshold: float = 0.5,
        batch_size: Optional[int] = None,
    ):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.score_threshold = score_threshold
        # GPU batching (Lever #2). batch_size>1 accumulates that many
        # sliding-window inputs and runs ONE forward pass. Default from
        # BALL_BATCH_SIZE env (1 = per-frame, current behaviour).
        self._batch_size = max(1, int(batch_size if batch_size is not None else BALL_BATCH_SIZE))

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

        self._buffer: list = []                       # last 3 (H, W, 3) BGR frames resized to 512×288
        self._frame_orig_shape: Optional[Tuple[int, int]] = None
        # Pending sliding-window inputs awaiting a batched forward pass.
        # Each entry is (window_array (H,W,9) float32/255, frame_idx). Flushed
        # when it reaches _batch_size, and by flush() at end-of-video.
        self._pending: list = []

        # BallTracker-compatible: list of BallDetection in original frame coords.
        self.detections: List[BallDetection] = []
        # WASB-specific diagnostic: parallel list of peak heatmap scores per
        # detection, for log_diagnostics. Cleared/reset alongside detections.
        self._scores: List[float] = []
        # Counter set used by reset(); kept for log_diagnostics symmetry with
        # BallTracker (which has a richer per-tier _diag dict).
        self._diag = {
            "frames_inferred": 0,
            "below_threshold": 0,
            "detected": 0,
        }

        logger.info(
            "WASBBallTracker loaded: weights=%s device=%s fp16=%s threshold=%.2f",
            wp, self.device, self._fp16, self.score_threshold,
        )

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_frame(
        self, frame: np.ndarray, frame_idx: int,
    ) -> Optional[BallDetection]:
        """Feed one BGR frame (any HxW). Returns the BallDetection for this
        frame when batch_size==1 (per-frame, original behaviour). When
        batch_size>1 the forward pass is deferred: this returns None and the
        detections are produced in arrears by the batched flush and read from
        self.detections (the pipeline ignores the return value). flush() drains
        the final partial batch — the pipeline calls it before post-processing.
        """
        if self._frame_orig_shape is None:
            self._frame_orig_shape = frame.shape[:2]

        resized = cv2.resize(frame, (WASB_INPUT_W, WASB_INPUT_H))
        self._buffer.append(resized)
        if len(self._buffer) > WASB_FRAMES_IN:
            self._buffer.pop(0)
        if len(self._buffer) < WASB_FRAMES_IN:
            return None

        # Snapshot the current sliding window (H, W, 9) and queue it for the
        # next batched forward pass.
        arr = np.concatenate(self._buffer, axis=2).astype(np.float32) / 255.0
        self._pending.append((arr, frame_idx))
        if len(self._pending) >= self._batch_size:
            made = self._flush_pending()
            if self._batch_size == 1:           # preserve the per-frame return contract
                return made[-1] if made else None
        return None

    def _flush_pending(self) -> List[BallDetection]:
        """Run ONE forward pass over all pending sliding windows, append the
        detections (in frame order) to self.detections, return those created.

        Equivalence: stacking B windows into (B, 9, H, W) is per-element
        identical to B separate (1, 9, H, W) passes — conv is batch-independent
        and BatchNorm runs in eval (running-stats) mode — so detections match
        the per-frame path bit-for-bit on CPU (within fp-noise on GPU). Same
        threshold / argmax / scaling / diag accounting as the original.
        """
        if not self._pending:
            return []

        arrs = [a for a, _ in self._pending]
        idxs = [fi for _, fi in self._pending]
        self._pending = []

        # (B, 9, H, W) — one forward pass for the whole batch.
        batch = np.stack([a.transpose(2, 0, 1) for a in arrs], axis=0)
        ten = torch.from_numpy(batch).to(self.device)
        if self._fp16:
            ten = ten.half()

        with torch.no_grad():
            y_out = self.model(ten)   # dict {scale: (B, frames_out, H, W)}
        heatmaps = torch.sigmoid(y_out[0]).float().cpu().numpy()   # (B, frames_out, H, W)

        orig_h, orig_w = self._frame_orig_shape
        scale_x = orig_w / WASB_INPUT_W
        scale_y = orig_h / WASB_INPUT_H

        made: List[BallDetection] = []
        for b, frame_idx in enumerate(idxs):
            self._diag["frames_inferred"] += 1
            hm = heatmaps[b][-1]      # most-recent frame's channel
            peak_val = float(hm.max())
            if peak_val < self.score_threshold:
                self._diag["below_threshold"] += 1
                continue
            peak_y_m, peak_x_m = np.unravel_index(int(hm.argmax()), hm.shape)
            det = BallDetection(
                frame_idx=frame_idx,
                x=float(peak_x_m * scale_x),
                y=float(peak_y_m * scale_y),
            )
            self.detections.append(det)
            self._scores.append(peak_val)
            self._diag["detected"] += 1
            made.append(det)
        return made

    def flush(self) -> None:
        """Drain the final partial batch (idempotent). The pipeline calls this
        after the last detect_frame, before post-processing."""
        self._flush_pending()

    # ------------------------------------------------------------------
    # Post-processing (copied verbatim from BallTracker — tracker-agnostic;
    # they operate purely on self.detections and config constants).
    # ------------------------------------------------------------------

    def interpolate_gaps(self):
        """Fill missing detections with linear interpolation for gaps ≤ BALL_MAX_INTERPOLATION_GAP."""
        if len(self.detections) < 2:
            return
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
        self._filter_outliers()

    def _filter_outliers(self):
        """Remove pixel-jump outliers but re-anchor on a coherent post-gap cluster.

        See BallTracker._filter_outliers in ml_pipeline/ball_tracker.py for the
        algorithm rationale. Identical implementation here so both trackers
        emit the same shape into ml_analysis.ball_detections.
        """
        if len(self.detections) < 2:
            return
        filtered = [self.detections[0]]
        pending: list[BallDetection] = []
        for d in self.detections[1:]:
            anchor = filtered[-1]
            if np.hypot(d.x - anchor.x, d.y - anchor.y) <= BALL_MAX_DIST_BETWEEN_FRAMES:
                pending = []
                filtered.append(d)
                continue
            if pending and np.hypot(d.x - pending[-1].x, d.y - pending[-1].y) <= BALL_MAX_DIST_BETWEEN_FRAMES:
                pending.append(d)
            else:
                pending = [d]
            if len(pending) >= BALL_FILTER_REANCHOR_RUN:
                filtered.extend(pending)
                pending = []
        self.detections = filtered

    def detect_bounces(self, court_detector=None):
        """Detect bounces via velocity reversal in y-coordinate. Optionally map to court coords.

        Mirrors BallTracker.detect_bounces — see that method for the
        validation criteria (sign-flip + magnitude + spacing).
        """
        if len(self.detections) < BOUNCE_VELOCITY_WINDOW * 2:
            return

        ys = np.array([d.y for d in self.detections])
        vel = np.convolve(
            np.diff(ys), np.ones(BOUNCE_VELOCITY_WINDOW) / BOUNCE_VELOCITY_WINDOW,
            mode="valid",
        )

        MIN_VEL_MAG = 2.0
        MIN_BOUNCE_SPACING = 8
        last_bounce_idx = -MIN_BOUNCE_SPACING
        bounce_count = 0

        for i in range(len(vel) - 1):
            sign_flip = (vel[i] > 0 and vel[i + 1] < 0) or (vel[i] < 0 and vel[i + 1] > 0)
            if not sign_flip:
                continue
            if abs(vel[i]) < MIN_VEL_MAG or abs(vel[i + 1]) < MIN_VEL_MAG:
                continue

            det_idx = i + BOUNCE_VELOCITY_WINDOW
            if det_idx >= len(self.detections):
                continue
            if det_idx - last_bounce_idx < MIN_BOUNCE_SPACING:
                continue
            last_bounce_idx = det_idx

            self.detections[det_idx].is_bounce = True
            bounce_count += 1

            if court_detector is not None:
                coords = court_detector.to_court_coords(
                    self.detections[det_idx].x, self.detections[det_idx].y,
                )
                if coords is not None:
                    cx, cy = coords
                    self.detections[det_idx].court_x = cx
                    self.detections[det_idx].court_y = cy
                    self.detections[det_idx].is_in = (
                        0 <= cx <= COURT_WIDTH_DOUBLES_M and
                        0 <= cy <= COURT_LENGTH_M
                    )
        logger.info("detect_bounces (wasb): found %d bounces (after validation)", bounce_count)

    def compute_speeds(self, court_detector=None, fps: float = None):
        """Compute ball speed in km/h using court-coordinate distances between frames.

        Same semantics as BallTracker.compute_speeds.
        """
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
                if speed_kmh <= 250:
                    d_curr.speed_kmh = speed_kmh
                d_curr.court_x = c_curr[0]
                d_curr.court_y = c_curr[1]
        if none_count > 0:
            logger.warning(
                "compute_speeds (wasb): %d/%d pairs had None court coords",
                none_count, none_count + ok_count,
            )

    def assign_peak_flight_speeds(self, window_frames: int = 15):
        """Overwrite each bounce's speed_kmh with the p75 of pairwise speeds
        in the preceding window. Identical semantics to BallTracker — see
        ball_tracker.py for the full rationale."""
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
                speeds.sort()
                k = max(0, min(len(speeds) - 1, int(len(speeds) * 0.75)))
                det.speed_kmh = speeds[k]
                n_updated += 1
        logger.info(
            "assign_peak_flight_speeds (wasb): set p75 on %d/%d bounces (window=%d)",
            n_updated, sum(1 for d in self.detections if d.is_bounce), window_frames,
        )

    def log_diagnostics(self):
        """WASB-specific diagnostics — score distribution + detection/threshold counts.

        Counterpart to BallTracker.log_diagnostics, which prints a per-tier
        breakdown of the 4-tier strategy. WASB has one tier (sigmoid heatmap
        peak ≥ threshold) so the breakdown is simpler: how many frames had
        no detection (below threshold), how many did, and what the score
        distribution looked like.
        """
        d = self._diag
        total = d["frames_inferred"]
        if total == 0:
            logger.info("WASBBallTracker diagnostics: no frames inferred")
            return

        def pct(n):
            return 100.0 * n / total

        logger.info("=== WASBBallTracker diagnostics ===")
        logger.info("frames_inferred:       %d", total)
        logger.info("below_threshold:       %d (%.1f%%)", d["below_threshold"], pct(d["below_threshold"]))
        logger.info("detected:              %d (%.1f%%)", d["detected"], pct(d["detected"]))
        if self._scores:
            arr = np.array(self._scores)
            logger.info(
                "score (peak heatmap):  min=%.3f p25=%.3f median=%.3f p75=%.3f max=%.3f  threshold=%.2f",
                float(arr.min()), float(np.percentile(arr, 25)),
                float(np.median(arr)), float(np.percentile(arr, 75)),
                float(arr.max()), self.score_threshold,
            )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self):
        self._buffer.clear()
        self._pending.clear()
        self.detections.clear()
        self._scores.clear()
        self._frame_orig_shape = None
        for k in self._diag:
            self._diag[k] = 0
