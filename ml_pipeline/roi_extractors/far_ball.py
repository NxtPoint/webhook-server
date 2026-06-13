"""Far-court ROI ball re-detection.

The production global ball tracker (WASB) localizes the FAR ball too coarsely:
at full-frame downscale the distant ball is ~1.6 px (sub-pixel), so its
trajectory jitters and the hit model cannot tell a far HIT (sharp velocity
reversal) from a far BOUNCE — both render as a few-px wobble (fork probe
2026-06-12: far-hit vs far-bounce feature-indistinguishable). Re-detecting the
far ball on a high-resolution far-court CROP sharpens it dramatically (local
A/B: trajectory residual 298 px -> 45 px, 6.7x cleaner, same TrackNet model).

Same hybrid pattern as roi_extractors/bounces.py — WASB owns the global frame,
TrackNet re-detects a projected ROI crop — but here:
  * anchored on far-court ball PRESENCE (court_y < HALF_Y), not bounces, so the
    whole far rally trajectory is re-detected (the hit model needs the arc, not
    just bounce moments);
  * keeps every far-half detection (not just is_bounce);
  * writes ml_analysis.ball_detections with source='roi_far_ball'.

Failure-tolerant and ADDITIVE: any exception at the call site is logged and the
job continues (silver/trim/notify must not be blocked by a re-detection miss).
Local validation: pass an explicit pixel_roi to skip court projection.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional, Tuple

from sqlalchemy import text as sql_text

# Reuse the proven projection / clustering / persistence helpers.
from ml_pipeline.roi_extractors.bounces import (
    _project_metres,
    _cluster_anchors,
    _windows_from_centroids,
    _project_dets_to_court,
    COURT_WIDTH_DOUBLES_M,
    HALF_Y,
)

logger = logging.getLogger("roi_far_ball")

# Far-court rectangle in metres: full doubles width + margin, from a little
# behind the far baseline (court_y=0; behind-baseline far players reach
# negative court_y) up to just past the net (HALF_Y=11.885).
FAR_X_MARGIN = 1.0
FAR_Y_BEHIND_BASELINE = -2.0
FAR_Y_TO_NET = HALF_Y + 0.5

SOURCE_TAG = "roi_far_ball"


# ---------------------------------------------------------------------------
# Far-court pixel ROI
# ---------------------------------------------------------------------------

def far_court_pixel_roi(detector, frame_shape, pad_px: int = 30):
    """Project the far-court rectangle (metres) to a pixel crop box.

    Returns (x0, y0, x1, y1) clamped to the frame, or None if projection
    fails (uncalibrated court)."""
    corners_m = [
        (-1.0, FAR_Y_BEHIND_BASELINE),
        (COURT_WIDTH_DOUBLES_M + 1.0, FAR_Y_BEHIND_BASELINE),
        (COURT_WIDTH_DOUBLES_M + 1.0, FAR_Y_TO_NET),
        (-1.0, FAR_Y_TO_NET),
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
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    return (x0, y0, x1, y1)


def _in_far_half(cy) -> bool:
    return cy is not None and FAR_Y_BEHIND_BASELINE <= cy < HALF_Y


def _select_far_anchors(detections) -> List[int]:
    """Frame indices where the in-memory ball sits in the far half — the
    stretches worth re-detecting at high res."""
    return sorted(
        int(d.frame_idx) for d in detections
        if _in_far_half(getattr(d, "court_y", None))
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist_far_rows(engine, job_id: str, rows: list, replace: bool) -> int:
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(sql_text(
            "ALTER TABLE ml_analysis.ball_detections "
            "ADD COLUMN IF NOT EXISTS source TEXT"))
        if replace:
            n_del = conn.execute(sql_text(
                "DELETE FROM ml_analysis.ball_detections "
                "WHERE job_id = :tid AND source = :src"),
                {"tid": job_id, "src": SOURCE_TAG}).rowcount
            if n_del:
                logger.info("roi_far_ball: deleted %d prior rows", n_del)
        conn.execute(sql_text("""
            INSERT INTO ml_analysis.ball_detections
                (job_id, frame_idx, x, y, court_x, court_y, is_bounce, source)
            VALUES
                (:job_id, :frame_idx, :x, :y, :court_x, :court_y,
                 :is_bounce, :source)
        """), [
            {"job_id": job_id, "frame_idx": r["frame_idx"], "x": r["x"],
             "y": r["y"], "court_x": r["court_x"], "court_y": r["court_y"],
             "is_bounce": r["is_bounce"], "source": SOURCE_TAG}
            for r in rows
        ])
    return len(rows)


# ---------------------------------------------------------------------------
# Processor — single-sweep window state machine (eager TrackNet)
# ---------------------------------------------------------------------------

class FarBallProcessor:
    """Re-detect the far ball on a projected high-res crop across far-court
    windows. Eager per-frame TrackNet (correctness-first; the batched-forward
    perf path from bounces.py can be ported once the hit-gate win is proven)."""

    def __init__(self, job_id, engine, *, court_detector=None,
                 detections=None, fps=25.0, window_s=1.5, cluster_gap_s=0.5,
                 pixel_roi=None, source_tag=SOURCE_TAG, replace=True,
                 max_windows=None):
        self.job_id = job_id
        self.engine = engine
        self.court_detector = court_detector
        self.fps = fps or 25.0
        self.source_tag = source_tag
        self.replace = replace
        self._explicit_roi = pixel_roi

        self.windows: List[Tuple[int, int, int]] = []
        if detections:
            anchors = _select_far_anchors(detections)
            if anchors:
                centroids = _cluster_anchors(anchors, self.fps, cluster_gap_s)
                windows = _windows_from_centroids(centroids, self.fps, window_s)
                if max_windows is not None and max_windows >= 0:
                    windows = windows[:max_windows]
                self.windows = windows
                logger.info(
                    "roi_far_ball: %d far detections -> %d anchors -> %d "
                    "clusters -> %d windows",
                    len(detections), len(anchors), len(centroids), len(windows))
            else:
                logger.info("roi_far_ball: 0 far-half anchors; skipping")

        self._ready = False
        self.pixel_roi = None
        self.x0 = self.y0 = self.x1 = self.y1 = 0
        self._shared_model = None
        self.all_rows: list = []
        self._t_start = None
        self._wptr = 0
        self._active_tracker = None
        self._active_t0 = None
        self._done = 0

    def prepare(self, frame_shape) -> bool:
        self._t_start = time.time()
        if not self.windows:
            return False
        roi = self._explicit_roi
        if roi is None:
            if self.court_detector is None:
                logger.warning("roi_far_ball: no court_detector and no explicit "
                               "ROI; skipping")
                return False
            roi = far_court_pixel_roi(self.court_detector, frame_shape)
        if roi is None:
            logger.warning("roi_far_ball: far-court ROI projection failed "
                           "(uncalibrated court); skipping")
            return False
        self.pixel_roi = roi
        self.x0, self.y0, self.x1, self.y1 = roi
        logger.info("roi_far_ball: far ROI (%d,%d)-(%d,%d) size=%dx%d",
                    self.x0, self.y0, self.x1, self.y1,
                    self.x1 - self.x0, self.y1 - self.y0)
        from ml_pipeline.ball_tracker import BallTracker
        self._shared_model = BallTracker().model
        self._ready = True
        return True

    def first_frame_needed(self) -> int:
        return self.windows[0][0] if self.windows else 0

    def last_frame_needed(self) -> int:
        return self.windows[-1][1] if self.windows else 0

    def feed(self, frame, idx: int):
        if not self._ready:
            return
        while self._wptr < len(self.windows) and idx >= self.windows[self._wptr][1]:
            self._close_active_window()
            self._wptr += 1
        if self._wptr >= len(self.windows):
            return
        s, _e, _c = self.windows[self._wptr]
        if idx < s:
            return
        crop = frame[self.y0:self.y1, self.x0:self.x1]
        if crop.size == 0:
            return
        if self._active_tracker is None:
            from ml_pipeline.ball_tracker import BallTracker
            self._active_tracker = BallTracker(model=self._shared_model)
            self._active_t0 = time.time()
        self._active_tracker.detect_frame(crop, idx)

    def _close_active_window(self):
        if self._active_tracker is None:
            return
        tracker = self._active_tracker
        tracker.interpolate_gaps()
        detector = self.court_detector if self.court_detector is not None \
            else _NullProjector()
        projected = _project_dets_to_court(tracker.detections, self.pixel_roi,
                                           detector)
        if self.court_detector is None:
            # Local validation: no calibration -> keep all crop detections
            # (pixel x/y is all candidates.py needs).
            kept = projected
        else:
            # Production: keep only far-half detections (safety against the
            # crop's edges projecting outside the far court).
            kept = [r for r in projected if _in_far_half(r["court_y"])]
        self.all_rows.extend(kept)
        self._done += 1
        self._active_tracker = None
        self._active_t0 = None

    def finalize(self) -> int:
        if self._active_tracker is not None:
            self._close_active_window()
        dt = (time.time() - self._t_start) if self._t_start else 0.0
        logger.info("roi_far_ball: %d rows across %d windows in %.1fs",
                    len(self.all_rows), len(self.windows), dt)
        if self.engine is not None:
            _persist_far_rows(self.engine, self.job_id, self.all_rows, self.replace)
        return len(self.all_rows)


class _NullProjector:
    """Stand-in court_detector for local validation without calibration —
    to_court_coords returns None so rows carry pixel x/y only."""
    def to_court_coords(self, x, y, strict=False):
        return None


# ---------------------------------------------------------------------------
# Driver (standalone, owns its decode) — local + Batch
# ---------------------------------------------------------------------------

def extract_far_ball(video_path, job_id, engine, *, court_detector=None,
                     detections=None, fps=25.0, window_s=1.5,
                     cluster_gap_s=0.5, pixel_roi=None, max_windows=None,
                     replace=True, return_rows=False):
    """Re-detect the far ball on a high-res far-court crop around far-court
    presence anchors. Pass pixel_roi to skip court projection (local test)."""
    import cv2

    if not os.path.exists(video_path):
        logger.warning("roi_far_ball: video not found: %s", video_path)
        return (0, []) if return_rows else 0
    if not detections:
        logger.info("roi_far_ball: no detections to anchor on")
        return (0, []) if return_rows else 0

    proc = FarBallProcessor(
        job_id, engine, court_detector=court_detector, detections=detections,
        fps=fps, window_s=window_s, cluster_gap_s=cluster_gap_s,
        pixel_roi=pixel_roi, max_windows=max_windows, replace=replace)
    if not proc.windows:
        return (0, []) if return_rows else 0

    cap = cv2.VideoCapture(video_path)
    ok, first = cap.read()
    cap.release()
    if not ok or not proc.prepare(first.shape):
        return (0, []) if return_rows else 0

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, proc.first_frame_needed())
    try:
        idx = proc.first_frame_needed()
        end = proc.last_frame_needed()
        while idx < end:
            ok, frame = cap.read()
            if not ok:
                break
            proc.feed(frame, idx)
            idx += 1
    finally:
        cap.release()

    count = proc.finalize()
    return (count, proc.all_rows) if return_rows else count
