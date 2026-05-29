"""Production ROI bounce extractor — runs in AWS Batch after the main
TennisAnalysisPipeline. Targets the service-box areas at high resolution
to recover bounces the full-frame TrackNet pass misses because the ball
is 1-2 px at 640×360 global scale.

Architectural shape mirrors extract_far_pose (roi_extractors/pose.py):
  - Called from ml_pipeline/__main__.py inside a non-fatal try/except
  - Reuses the pipeline's calibrated court_detector
  - Takes the in-memory result.ball_detections as the anchor source
  - **Writes directly to ml_analysis.ball_detections (the canonical
    bronze table) with source='roi_prod'** so silver_t5 and
    serve_detector see the rows through their existing single-table
    loaders. Original Phase 5a build wrote to a parallel
    ball_detections_roi table — that was an architectural mistake
    (forced every downstream consumer to merge two tables); fixed in
    Option A on 2026-05-21. See feedback_t5_single_canonical_bronze.

Anchor logic (option c from the stub docstring + phase5a_kickoff.md):
  1. Filter result.ball_detections to the service-box zone in court metres
  2. Cluster consecutive anchors within cluster_gap_s into single moments
  3. Build ±window_s frame windows around each cluster centroid
  4. Merge overlapping windows so we don't run TrackNet twice on the same frames
  5. For each merged window: run a fresh BallTracker on a tight pixel crop
     covering both service boxes; project back to court metres
  6. Keep only detections inside the service-box zone; persist with source tag

Decode model: a single sequential sweep feeds frames to the active window's
fresh BallTracker as the decode reaches them (RoiBounceProcessor). The earlier
implementation re-opened the VideoCapture and CAP_PROP_POS_FRAMES-seeked PER
WINDOW — on a long match with many bounce clusters that thrashed the decoder
and was a primary reason long matches raced the 6h Batch timeout. The sweep
reads each frame at most once. A fresh BallTracker is still constructed per
window (per-window state stays clean) and the TrackNet model is loaded ONCE and
shared (the original "Bug 2" fix), so outputs are identical — only the decode
scheduling changed. unified.py drives the same processor off the shared pose
decode so the whole video is decoded ONCE for both ROI passes.

Failure-tolerant: any exception inside the call site is logged and the
job continues. This module is ADDITIVE — silver/trim/notify must not be
blocked by a bounce-extraction failure.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional, Tuple

import numpy as np
from sqlalchemy import text as sql_text

from ml_pipeline.config import ROI_BOUNCE_BATCH

logger = logging.getLogger("roi_bounces")


# ---------------------------------------------------------------------------
# TASK 2 — batched TrackNet forward across ROI bounce windows
# ---------------------------------------------------------------------------
#
# The eager path (ROI_BOUNCE_BATCH==1, default) feeds each window's crops into a
# fresh BallTracker one frame at a time; TrackNet runs at batch=1 per frame. The
# batched path (ROI_BOUNCE_BATCH>1) defers the GPU forward: it collects a
# window's crops, builds the per-frame TrackNet input tensors EXACTLY as
# BallTracker._detect_frame_v2 would, runs them through the shared model in
# batches of ROI_BOUNCE_BATCH, then replays the per-frame postprocess via a
# fresh BallTracker whose model is swapped for a replay shim that serves the
# precomputed forward output for that frame. Because the replay runs the real
# detect_frame → _detect_frame_v2 → _postprocess_heatmap path (and the
# sequential frame-delta Hough fallback when TrackNet emits no signal), the
# per-window BallDetection rows — and therefore interpolate_gaps / detect_bounces
# / projection / zone-filter outputs — are identical to the eager path on CPU
# and within fp-noise on GPU (conv is batch-element-independent, BatchNorm is
# eval/running-stats, the heatmap postprocess is per-element).
#
# Scope: the batched forward is implemented for TrackNet V2 (the production ROI
# ball model — tracknet_v3.pt is absent in prod). If a window's shared model is
# V3 (8-frame + background, 27-channel), the processor falls back to the eager
# per-frame path for that window (logged once) — still correct, just unbatched.


class _ReplayModel:
    """Stand-in for BallTracker.model that serves precomputed forward outputs.

    Constructed with the batched model output as a list of per-frame tensors
    (each shaped exactly like a single-frame BallTrackerNet forward:
    (1, out_channels, H*W)). __call__ pops them in invocation order, ignoring
    the input tensor BallTracker rebuilds — the rebuilt tensor is the same one
    we already ran through the real model in batch, so the served output is
    identical to what an eager self.model(tensor, testing=True) call returns.
    """

    def __init__(self, outputs: list):
        self._outputs = outputs
        self._i = 0

    def __call__(self, tensor, testing=False):  # noqa: D401 — mimics nn.Module
        out = self._outputs[self._i]
        self._i += 1
        return out

    def exhausted(self) -> bool:
        return self._i >= len(self._outputs)


# Court geometry — kept in sync with extract_roi_bounces.py (diag tool)
COURT_LENGTH_M = 23.77
COURT_WIDTH_DOUBLES_M = 10.97
HALF_Y = COURT_LENGTH_M / 2.0
SERVICE_LINE_FROM_NET_M = 6.40
FAR_SERVICE_LINE_M = HALF_Y - SERVICE_LINE_FROM_NET_M   # 5.485
NEAR_SERVICE_LINE_M = HALF_Y + SERVICE_LINE_FROM_NET_M  # 18.285

# Service-box zone — generous ±1.5 m margin in both axes
SB_X_MARGIN = 1.5
SB_Y_MARGIN = 1.5


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _init_schema(conn) -> None:
    """Ensure ml_analysis.ball_detections has the `source` column (idempotent).

    Phase 5a writes ROI rows directly into the canonical bronze table
    `ml_analysis.ball_detections` (with source='roi_prod') so silver and
    serve_detector see them through the same loader they already use. The
    `source` column is the only schema addition — distinguishes main-pass
    rows (NULL or 'main') from ROI-pass rows ('roi_prod' for production,
    'roi_far' for the diag tool). This avoids the parallel-bronze-table
    architecture that the original Phase 5a build accidentally created
    (see .claude/session_2026-05-21_phase5a_stage2.md and
    feedback_t5_single_canonical_bronze for the rationale)."""
    conn.execute(sql_text(
        "ALTER TABLE ml_analysis.ball_detections "
        "ADD COLUMN IF NOT EXISTS source TEXT"
    ))


# ---------------------------------------------------------------------------
# Court projection
# ---------------------------------------------------------------------------

def _project_metres(mx, my, detector):
    """Court (metres) → pixel. Calibration first, homography fallback."""
    from ml_pipeline.camera_calibration import project_metres_to_pixel as proj
    calib = detector._calibration
    if calib is not None:
        p = proj(mx, my, calib)
        if p is not None:
            return p
    best = (detector._locked_detection
            or detector._best_validated_detection
            or detector._best_detection)
    if best is not None and best.homography is not None:
        H_inv = np.linalg.inv(best.homography)
        pt = H_inv @ np.array([mx, my, 1.0])
        if pt[2] != 0:
            return float(pt[0] / pt[2]), float(pt[1] / pt[2])
    return None


def _service_box_pixel_roi(detector, frame_shape, pad_px: int = 40):
    """Project the service-box rectangle from metres to pixels.

    Covers court_x in [-1, DOUBLES_WIDTH+1], court_y in
    [FAR_SERVICE_LINE_M-1.5, NEAR_SERVICE_LINE_M+1.5] — i.e. both
    service boxes plus a small margin. Identical to the diag tool's
    rectangle so output rows are comparable."""
    corners_m = [
        (-1.0, FAR_SERVICE_LINE_M - 1.5),
        (COURT_WIDTH_DOUBLES_M + 1.0, FAR_SERVICE_LINE_M - 1.5),
        (COURT_WIDTH_DOUBLES_M + 1.0, NEAR_SERVICE_LINE_M + 1.5),
        (-1.0, NEAR_SERVICE_LINE_M + 1.5),
    ]
    pxs = []
    for mx, my in corners_m:
        p = _project_metres(mx, my, detector)
        if p is None:
            return None
        pxs.append(p)
    xs = [p[0] for p in pxs]
    ys = [p[1] for p in pxs]
    h, w = frame_shape[:2]
    x0 = max(0, int(min(xs) - pad_px))
    y0 = max(0, int(min(ys) - pad_px))
    x1 = min(w, int(max(xs) + pad_px))
    y1 = min(h, int(max(ys) + pad_px))
    return (x0, y0, x1, y1)


def _in_service_box_zone(cx, cy) -> bool:
    if cx is None or cy is None:
        return False
    if not (-SB_X_MARGIN <= cx <= COURT_WIDTH_DOUBLES_M + SB_X_MARGIN):
        return False
    return FAR_SERVICE_LINE_M - SB_Y_MARGIN <= cy <= NEAR_SERVICE_LINE_M + SB_Y_MARGIN


# ---------------------------------------------------------------------------
# Anchor selection — cluster in-memory bounces in service-box zone
# ---------------------------------------------------------------------------

def _select_anchors(
    bounces,
    fps: float,
    *,
    zone_filter: bool = False,
    bounce_only: bool = True,
) -> List[int]:
    """Return frame indices of in-memory ball detections to anchor on.

    Two orthogonal filters control which detections are used as anchors:

    * `zone_filter=True` keeps only detections whose court coords fall inside
      the service-box zone. False keeps all detections regardless of position.
    * `bounce_only=True` keeps only detections marked `is_bounce`. False keeps
      every detection (every frame TrackNet succeeded on).

    Defaults: `zone_filter=False, bounce_only=True`. Rationale — pre-flight
    diagnostic on the 880dff02 fixture (2026-05-21) showed the original
    `zone=T, bounce=F` default covered only 1/24 SA serves (4%) because bronze
    detections are concentrated in 4 distinct 10s buckets that don't align
    with serve times. `zone=F, bounce=T` covered 6/24 (25%) — best of 4
    strategies tested. Bronze bounces are temporally sparse so they cluster
    into many small windows; the zone filter on anchors removed useful
    triggers at baseline / mid-court positions. Stage 2 measurement on
    880dff02 will validate the choice."""
    if not bounces:
        return []
    anchors = []
    for d in bounces:
        if bounce_only and not getattr(d, "is_bounce", False):
            continue
        if zone_filter:
            cx = getattr(d, "court_x", None)
            cy = getattr(d, "court_y", None)
            if not _in_service_box_zone(cx, cy):
                continue
        anchors.append(int(d.frame_idx))
    return sorted(anchors)


def _cluster_anchors(anchors: List[int], fps: float, gap_s: float) -> List[int]:
    """Group anchors within gap_s of each other into single centroids."""
    if not anchors:
        return []
    gap_frames = max(1, int(round(gap_s * fps)))
    clusters: List[List[int]] = [[anchors[0]]]
    for f in anchors[1:]:
        if f - clusters[-1][-1] <= gap_frames:
            clusters[-1].append(f)
        else:
            clusters.append([f])
    return [int(round(sum(c) / len(c))) for c in clusters]


def _windows_from_centroids(
    centroids: List[int],
    fps: float,
    window_s: float,
) -> List[Tuple[int, int, int]]:
    """Build (start, end, center) frame windows and merge overlapping ones.

    Returns list of (start_inclusive, end_exclusive, anchor_centroid_used_for_logging).
    Window half-width in frames is round(window_s * fps)."""
    if not centroids:
        return []
    half = max(1, int(round(window_s * fps)))
    sorted_c = sorted(centroids)
    merged: List[Tuple[int, int, int]] = []
    for c in sorted_c:
        s = max(0, c - half)
        e = c + half + 1
        if merged and s <= merged[-1][1]:
            ps, pe, pc = merged[-1]
            merged[-1] = (ps, max(pe, e), pc)
        else:
            merged.append((s, e, c))
    return merged


# ---------------------------------------------------------------------------
# Court projection of crop-pixel detections
# ---------------------------------------------------------------------------

def _project_dets_to_court(dets, roi, detector):
    """Map crop-pixel detections to full-frame pixel + court metres."""
    x0, y0, _x1, _y1 = roi
    out = []
    for d in dets:
        full_x = d.x + float(x0)
        full_y = d.y + float(y0)
        court = detector.to_court_coords(full_x, full_y, strict=False)
        if court is None:
            cx = cy = None
        else:
            cx, cy = court
        out.append({
            "frame_idx": d.frame_idx,
            "x": full_x,
            "y": full_y,
            "court_x": cx,
            "court_y": cy,
            "is_bounce": bool(getattr(d, "is_bounce", False)),
        })
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist_rows(
    engine,
    job_id: str,
    source_tag: str,
    rows: list,
    replace: bool,
) -> int:
    if not rows:
        return 0
    with engine.begin() as conn:
        _init_schema(conn)
        if replace:
            n_del = conn.execute(sql_text("""
                DELETE FROM ml_analysis.ball_detections
                WHERE job_id = :tid AND source = :src
            """), {"tid": job_id, "src": source_tag}).rowcount
            if n_del:
                logger.info("roi_bounces: deleted %d prior rows (source=%s)",
                            n_del, source_tag)
        conn.execute(sql_text("""
            INSERT INTO ml_analysis.ball_detections
                (job_id, frame_idx, x, y, court_x, court_y,
                 is_bounce, source)
            VALUES
                (:job_id, :frame_idx, :x, :y, :court_x, :court_y,
                 :is_bounce, :source)
        """), [
            {
                "job_id": job_id,
                "frame_idx": r["frame_idx"],
                "x": r["x"],
                "y": r["y"],
                "court_x": r["court_x"],
                "court_y": r["court_y"],
                "is_bounce": r["is_bounce"],
                "source": source_tag,
            }
            for r in rows
        ])
    return len(rows)


# ---------------------------------------------------------------------------
# Per-frame processor — single-sweep window state machine
# ---------------------------------------------------------------------------

class RoiBounceProcessor:
    """Per-frame ROI-bounce core driven by a single sequential decode.

    Lifecycle:
        proc = RoiBounceProcessor(job_id, engine, court_detector=..., bounces=...)
        if not proc.windows:            # 0 anchors → nothing to do
            return 0
        if not proc.prepare(frame_shape):   # projects service-box ROI, loads model
            return 0
        for idx, frame in decode(video):     # caller owns the decode (idx ascending)
            proc.feed(frame, idx)
        n = proc.finalize()                  # closes last window, writes rows

    `windows` are merged, non-overlapping and sorted, so at most one window is
    active at any frame. Each window gets its own fresh BallTracker fed the same
    contiguous frames the per-window seek loop fed — so the rows are identical;
    only the decode scheduling differs.
    """

    def __init__(
        self,
        job_id: str,
        engine,
        *,
        court_detector=None,
        bounces: Optional[List] = None,
        fps: float = 25.0,
        window_s: float = 2.5,
        cluster_gap_s: float = 0.5,
        anchor_zone_filter: bool = False,
        anchor_bounce_only: bool = True,
        max_windows: Optional[int] = None,
        source_tag: str = "roi_prod",
        replace: bool = True,
    ):
        self.job_id = job_id
        self.engine = engine
        self.court_detector = court_detector
        self.fps = fps or 25.0
        self.source_tag = source_tag
        self.replace = replace

        # Build anchors → clusters → windows up front (no video needed).
        self.windows: List[Tuple[int, int, int]] = []
        if bounces and court_detector is not None:
            anchors = _select_anchors(
                bounces, self.fps,
                zone_filter=anchor_zone_filter,
                bounce_only=anchor_bounce_only,
            )
            if not anchors:
                logger.info(
                    "roi_bounces: 0 anchors (zone_filter=%s, bounce_only=%s, "
                    "from %d input); skipping",
                    anchor_zone_filter, anchor_bounce_only, len(bounces),
                )
            else:
                centroids = _cluster_anchors(anchors, self.fps, cluster_gap_s)
                windows = _windows_from_centroids(centroids, self.fps, window_s)
                if max_windows is not None and max_windows >= 0:
                    windows = windows[:max_windows]
                self.windows = windows
                logger.info(
                    "roi_bounces: %d bounces → %d anchors → %d clusters → "
                    "%d merged windows",
                    len(bounces), len(anchors), len(centroids), len(windows),
                )

        self._ready = False
        self.pixel_roi: Optional[Tuple[int, int, int, int]] = None
        self.x0 = self.y0 = self.x1 = self.y1 = 0
        self._shared_model = None
        self.all_rows: list = []
        self._t_start = None
        # window state machine
        self._wptr = 0
        self._active_tracker = None
        self._active_t0 = None
        self._windows_done = 0

        # TASK 2: batched-forward mode. >1 defers the per-frame TrackNet GPU
        # forward and runs it in batches of this size across each window's
        # frames. 1 = eager per-frame (today's exact behaviour). Resolved at
        # prepare() against the actual model version (V3 falls back to eager).
        self._bounce_batch = max(1, int(ROI_BOUNCE_BATCH))
        self._batched_mode = False           # set in prepare() once model known
        self._active_crops: list = []         # [(idx, crop_ndarray), ...] for the active window
        self._use_v3_model = False

    # -- setup ---------------------------------------------------------------

    def prepare(self, frame_shape) -> bool:
        """Project the service-box ROI and load the shared TrackNet model.

        Returns False (and logs) when there are no windows, the ROI can't be
        projected, or there's no court_detector — caller then skips bounces."""
        self._t_start = time.time()
        if not self.windows:
            return False
        if self.court_detector is None:
            logger.warning(
                "roi_bounces: no court_detector supplied; cannot project ROI — "
                "skipping"
            )
            return False

        pixel_roi = _service_box_pixel_roi(self.court_detector, frame_shape)
        if pixel_roi is None:
            logger.warning(
                "roi_bounces: cannot project service-box corners to pixels — "
                "court_detector likely uncalibrated; skipping"
            )
            return False
        self.pixel_roi = pixel_roi
        self.x0, self.y0, self.x1, self.y1 = pixel_roi
        logger.info(
            "roi_bounces: service-box pixel ROI (%d,%d)-(%d,%d) size=%dx%d",
            self.x0, self.y0, self.x1, self.y1,
            self.x1 - self.x0, self.y1 - self.y0,
        )

        # Min-ROI guard (45×40 degeneracy, 2026-05-28): a degenerate court
        # calibration collapses the service-box ROI to a tiny box. Bail before
        # loading the model / scanning. Thresholds are looser than the far-pose
        # ROI because the service box is a smaller legit region, but still far
        # above the ~45×40 degenerate box. Defence-in-depth behind Fix G.
        fh, fw = frame_shape[:2]
        roi_w, roi_h = self.x1 - self.x0, self.y1 - self.y0
        if (roi_w < fw * 0.04 or roi_h < fh * 0.03 or roi_w * roi_h < fw * fh * 0.005):
            logger.error(
                "roi_bounces: service-box ROI degenerate (%dx%d = %.4f%% of frame) "
                "— court calibration likely degenerate; skipping bounces to avoid a "
                "wasted scan", roi_w, roi_h, 100.0 * roi_w * roi_h / max(1, fw * fh))
            return False

        # Load the TrackNet model ONCE and share it across windows (Bug 2 fix:
        # constructing a fresh BallTracker per window reloaded the weights every
        # time, ~7x slowdown that timed out long matches at the 6h Batch limit).
        from ml_pipeline.ball_tracker import BallTracker
        probe = BallTracker()
        self._shared_model = probe.model
        # Capture inference config from the probe so the batched-forward path
        # builds tensors identically to BallTracker._detect_frame_v2 (device,
        # fp16, version). Read-only at inference, so sharing is safe.
        self._model_device = probe.device
        self._model_fp16 = probe._use_fp16
        self._use_v3_model = probe._use_v3
        self._num_input_frames = probe._num_input_frames

        # TASK 2: enable batched forward only for V2 (the prod ROI ball model).
        # V3 (8-frame + background, 27ch) keeps the eager per-frame path —
        # correct, just unbatched. Logged once so the deploy log shows which
        # path ran.
        self._batched_mode = (self._bounce_batch > 1) and (not self._use_v3_model)
        if self._bounce_batch > 1:
            if self._batched_mode:
                logger.info(
                    "roi_bounces: ROI_BOUNCE_BATCH=%d → batched TrackNet-V2 "
                    "forward ENABLED across window frames", self._bounce_batch,
                )
            else:
                logger.info(
                    "roi_bounces: ROI_BOUNCE_BATCH=%d set but model is V3 — "
                    "falling back to eager per-frame forward (batched path is "
                    "V2-only)", self._bounce_batch,
                )

        self._ready = True
        return True

    def first_frame_needed(self) -> int:
        return self.windows[0][0] if self.windows else 0

    def last_frame_needed(self) -> int:
        """Exclusive end frame of the last window — the standalone driver
        stops the sweep here; the unified driver may sweep past it for pose."""
        return self.windows[-1][1] if self.windows else 0

    # -- per-frame -----------------------------------------------------------

    def feed(self, frame, idx: int):
        """Accumulate one decoded frame into the active window's tracker.

        Closes windows whose exclusive end has been reached as the decode
        advances. Frames outside every window (gaps between merged windows) are
        ignored."""
        if not self._ready:
            return
        # Close any windows that ended at or before this frame.
        while self._wptr < len(self.windows) and idx >= self.windows[self._wptr][1]:
            self._close_active_window()
            self._wptr += 1
        if self._wptr >= len(self.windows):
            return
        s, e, _c = self.windows[self._wptr]
        if idx < s:
            return  # gap before the next window
        crop = frame[self.y0:self.y1, self.x0:self.x1]
        if crop.size == 0:
            return

        if self._batched_mode:
            # TASK 2 batched path: collect the window's crops; the TrackNet
            # forward + per-frame postprocess run together at window close so
            # the forward can be batched. crop.copy() because `frame` is a
            # decoder-owned buffer reused on the next read — without the copy
            # the stored crop would alias whatever frame is decoded next.
            if self._active_t0 is None:
                self._active_t0 = time.time()
            self._active_crops.append((idx, crop.copy()))
            return

        # Eager path (default) — byte-identical to pre-TASK-2 behaviour.
        if self._active_tracker is None:
            from ml_pipeline.ball_tracker import BallTracker
            self._active_tracker = BallTracker(model=self._shared_model)
            self._active_t0 = time.time()
        self._active_tracker.detect_frame(crop, idx)

    def _close_active_window(self):
        """Finalize the active window: interpolate, detect bounces, project,
        filter to the service-box zone, collect rows."""
        # Batched path: turn the collected crops into a populated tracker by
        # running the batched forward + per-frame replay. Sets _active_tracker.
        if self._batched_mode:
            if not self._active_crops:
                return
            self._active_tracker = self._run_window_batched(self._active_crops)
            self._active_crops = []

        if self._active_tracker is None:
            return
        s, e, c = self.windows[self._wptr]
        tracker = self._active_tracker
        tracker.interpolate_gaps()
        tracker.detect_bounces()
        projected = _project_dets_to_court(
            tracker.detections, self.pixel_roi, self.court_detector,
        )
        window_ts = c / self.fps
        kept = []
        for r in projected:
            if not _in_service_box_zone(r["court_x"], r["court_y"]):
                continue
            r["window_serve_ts"] = window_ts
            kept.append(r)
        n_bounces = sum(1 for r in kept if r["is_bounce"])
        self._windows_done += 1
        logger.info(
            "roi_bounces: [%d/%d] frames [%d,%d) center=%d ts=%.2fs "
            "-> %d dets in zone, %d bounces (%.1fs)",
            self._windows_done, len(self.windows), s, e, c, window_ts,
            len(kept), n_bounces,
            time.time() - self._active_t0 if self._active_t0 else 0.0,
        )
        self.all_rows.extend(kept)
        self._active_tracker = None
        self._active_t0 = None

    # -- TASK 2 batched forward ---------------------------------------------

    def _build_v2_input(self, frame_buffer):
        """Build the TrackNet-V2 model input tensor from a 3-frame buffer.

        Byte-for-byte the same construction as BallTracker._detect_frame_v2:
        concat the 3 resized BGR frames on the channel axis → (H, W, 9),
        normalise /255, permute to (9, H, W), add batch dim → (1, 9, H, W),
        cast to fp16 on cuda. Returns a torch tensor on the model's device.
        """
        import torch
        stacked = np.concatenate(frame_buffer, axis=2)             # (H, W, 9)
        tensor = torch.from_numpy(
            stacked.astype(np.float32) / 255.0
        ).permute(2, 0, 1).unsqueeze(0).to(self._model_device)
        if self._model_fp16:
            tensor = tensor.half()
        return tensor

    def _run_window_batched(self, crops):
        """Replay a window's crops with the TrackNet forward run in batches.

        Phase 1 — replicate BallTracker's resize + 3-frame sliding window to
        enumerate every model-input tensor the eager path would have produced,
        in frame order (the model is called once per frame ONCE the buffer is
        full; the first num_input_frames-1 frames produce no call). Phase 2 —
        run those tensors through the shared model in batches of
        self._bounce_batch and slice the output per call. Phase 3 — replay
        detect_frame on a fresh BallTracker whose model is swapped for a
        _ReplayModel serving the precomputed slices in order; the real
        _detect_frame_v2 postprocess + sequential frame-delta Hough fallback
        run unchanged, so the resulting detections are identical to the eager
        path (within fp-noise on GPU).

        Returns a populated BallTracker (detections filled, model restored).
        """
        import cv2 as _cv2
        import torch
        from ml_pipeline.ball_tracker import BallTracker
        from ml_pipeline.config import (
            TRACKNET_INPUT_WIDTH, TRACKNET_INPUT_HEIGHT, TRACKNET_BGR2RGB,
        )

        n = self._num_input_frames  # 3 for V2

        # Phase 1: enumerate per-call input tensors using the SAME resize +
        # buffer logic BallTracker.detect_frame uses. A model call happens for
        # frame k once the buffer holds n frames, i.e. for crops[n-1:].
        resized_buffer: list = []
        call_tensors: list = []
        for _idx, crop in crops:
            resized = _cv2.resize(crop, (TRACKNET_INPUT_WIDTH, TRACKNET_INPUT_HEIGHT))
            if TRACKNET_BGR2RGB:
                resized = _cv2.cvtColor(resized, _cv2.COLOR_BGR2RGB)
            resized_buffer.append(resized)
            if len(resized_buffer) > n:
                resized_buffer.pop(0)
            if len(resized_buffer) < n:
                continue
            call_tensors.append(self._build_v2_input(resized_buffer))

        # Phase 2: batched forward. Concatenate up to self._bounce_batch
        # single-frame tensors (each (1, 9, H, W)) into one (B, 9, H, W) batch,
        # run ONE forward, and split the output back into per-call (1, C, H*W)
        # slices. testing=True matches _detect_frame_v2's softmax path.
        per_call_outputs: list = []
        if call_tensors:
            with torch.no_grad():
                for i in range(0, len(call_tensors), self._bounce_batch):
                    chunk = call_tensors[i:i + self._bounce_batch]
                    batched = torch.cat(chunk, dim=0)             # (B, 9, H, W)
                    out = self._shared_model(batched, testing=True)  # (B, C, H*W)
                    # Slice per call, keeping the batch dim so the served tensor
                    # is shaped exactly like a single-frame forward output.
                    for b in range(out.shape[0]):
                        per_call_outputs.append(out[b:b + 1])

        # Phase 3: replay detect_frame with the precomputed outputs. Swap the
        # tracker's model for a _ReplayModel; everything else (buffer warmup,
        # postprocess, frame-delta fallback, scaling) runs unchanged.
        tracker = BallTracker(model=self._shared_model)
        tracker.model = _ReplayModel(per_call_outputs)
        for _idx, crop in crops:
            tracker.detect_frame(crop, _idx)
        # Restore the shared model reference (defensive; the tracker is about to
        # be consumed and discarded, but keep it in a sane state).
        tracker.model = self._shared_model
        return tracker

    # -- teardown ------------------------------------------------------------

    def finalize(self) -> int:
        """Close any open window, persist, return row count."""
        # Eager mode leaves a live _active_tracker; batched mode leaves
        # collected crops. Either means the last window never hit its
        # exclusive-end close in feed() — flush it here.
        if self._active_tracker is not None or self._active_crops:
            self._close_active_window()

        n_bounces_total = sum(1 for r in self.all_rows if r["is_bounce"])
        dt = (time.time() - self._t_start) if self._t_start else 0.0
        logger.info(
            "roi_bounces: total %d rows (%d bounces) across %d windows in %.1fs",
            len(self.all_rows), n_bounces_total, len(self.windows), dt,
        )

        if self.engine is None:
            logger.info("roi_bounces: engine=None — skipping DB write")
        else:
            _persist_rows(
                self.engine, self.job_id, self.source_tag,
                self.all_rows, self.replace,
            )
        return len(self.all_rows)


# ---------------------------------------------------------------------------
# Public entry point — standalone (owns its own single-sweep decode)
# ---------------------------------------------------------------------------

def extract_far_bounces(
    video_path: str,
    job_id: str,
    engine,
    *,
    court_detector=None,
    bounces: Optional[List] = None,
    fps: float = 25.0,
    window_s: float = 2.5,
    cluster_gap_s: float = 0.5,
    anchor_zone_filter: bool = False,
    anchor_bounce_only: bool = True,
    max_windows: Optional[int] = None,
    source_tag: str = "roi_prod",
    replace: bool = True,
    return_rows: bool = False,
):
    """Run service-box-targeted TrackNet around in-memory bounce anchors.

    Standalone driver: seeks once to the first window and sweeps sequentially
    to the last window end, feeding the active window's tracker. For the
    production path where pose also needs to decode, prefer
    roi_extractors.unified which decodes the whole video ONCE for both passes.

    Args:
        video_path: local filesystem path to the video (Batch has it).
        job_id: ml_analysis.video_analysis_jobs.job_id (used as FK).
        engine: SQLAlchemy engine. Pass None to skip the DB write (Stage 1
            local validation).
        court_detector: already-calibrated CourtDetector (typically
            pipeline.court_detector after pipeline.process()). Required —
            we don't re-calibrate here because it's already done upstream
            and re-running adds 10-20s for no gain.
        bounces: in-memory result.ball_detections list. The anchor source.
            Falsy → returns 0 (logged).
        fps: video fps. Used to convert cluster_gap_s and window_s to frames.
        window_s: half-window in seconds around each cluster centroid.
        cluster_gap_s: anchors within this many seconds collapse to one
            cluster centroid before window construction.
        anchor_zone_filter: when True, restrict anchors to detections inside
            the service-box zone. Default False (see _select_anchors docstring
            for the diagnostic table that drove the default).
        anchor_bounce_only: when True, anchor only on detections with
            is_bounce=True. Default True.
        max_windows: cap on the number of ROI windows to run (test/diag
            knob — None = unlimited).
        source_tag: ml_analysis.ball_detections.source value. Use a
            distinct tag ('roi_prod' by default) from the diag tool's
            'roi_far' and from main-pass rows (source='main' or NULL).
        replace: when True (default), DELETE prior rows for
            (job_id, source_tag) before inserting — production idempotency.
        return_rows: when True, returns (count, rows) for Stage 1 inspection.

    Returns:
        Number of rows written, or (count, rows) when return_rows=True.
    """
    import cv2

    if not os.path.exists(video_path):
        logger.warning("roi_bounces: video not found: %s; skipping", video_path)
        return (0, []) if return_rows else 0

    if not bounces:
        logger.info("roi_bounces: no bounces supplied; nothing to anchor on")
        return (0, []) if return_rows else 0

    if court_detector is None:
        logger.warning(
            "roi_bounces: no court_detector supplied; cannot project ROI — skipping"
        )
        return (0, []) if return_rows else 0

    proc = RoiBounceProcessor(
        job_id, engine,
        court_detector=court_detector, bounces=bounces, fps=fps,
        window_s=window_s, cluster_gap_s=cluster_gap_s,
        anchor_zone_filter=anchor_zone_filter,
        anchor_bounce_only=anchor_bounce_only,
        max_windows=max_windows, source_tag=source_tag, replace=replace,
    )
    if not proc.windows:
        return (0, []) if return_rows else 0

    # Read the first frame for shape (used to clamp the ROI rectangle).
    cap = cv2.VideoCapture(video_path)
    ok, first = cap.read()
    cap.release()
    if not ok:
        logger.warning("roi_bounces: cannot read first frame; skipping")
        return (0, []) if return_rows else 0

    if not proc.prepare(first.shape):
        return (0, []) if return_rows else 0

    # Single sweep over the spanned range [first_window_start, last_window_end).
    start = proc.first_frame_needed()
    end = proc.last_frame_needed()
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    try:
        idx = start
        while idx < end:
            ok, frame = cap.read()
            if not ok:
                break
            proc.feed(frame, idx)
            idx += 1
    finally:
        cap.release()

    count = proc.finalize()
    if return_rows:
        return count, proc.all_rows
    return count
