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

logger = logging.getLogger("roi_bounces")


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
# ROI ball tracking
# ---------------------------------------------------------------------------

def _run_roi_window(
    video_path: str,
    start_frame: int,
    end_frame: int,
    roi: Tuple[int, int, int, int],
) -> list:
    """Run a fresh BallTracker on the ROI crop for frames [start, end).

    Returns BallDetection list in CROP-PIXEL coords (caller projects)."""
    import cv2
    from ml_pipeline.ball_tracker import BallTracker

    x0, y0, x1, y1 = roi
    tracker = BallTracker()

    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    try:
        for idx in range(start_frame, end_frame):
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            tracker.detect_frame(crop, idx)
    finally:
        cap.release()

    tracker.interpolate_gaps()
    tracker.detect_bounces()
    return tracker.detections


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
# Public entry point
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
    t_start = time.time()

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

    # 1. Select anchors per configured strategy (see _select_anchors docstring)
    anchors = _select_anchors(
        bounces, fps,
        zone_filter=anchor_zone_filter,
        bounce_only=anchor_bounce_only,
    )
    if not anchors:
        logger.info(
            "roi_bounces: 0 anchors (zone_filter=%s, bounce_only=%s, from %d input); "
            "skipping",
            anchor_zone_filter, anchor_bounce_only, len(bounces),
        )
        return (0, []) if return_rows else 0

    # 2. Cluster anchors temporally
    centroids = _cluster_anchors(anchors, fps, cluster_gap_s)

    # 3. Build merged frame windows
    windows = _windows_from_centroids(centroids, fps, window_s)
    if max_windows is not None and max_windows >= 0:
        windows = windows[:max_windows]

    logger.info(
        "roi_bounces: %d bounces → %d anchors → %d clusters → %d merged windows",
        len(bounces), len(anchors), len(centroids), len(windows),
    )

    # 4. Compute service-box pixel ROI (one rectangle for all windows)
    import cv2
    cap = cv2.VideoCapture(video_path)
    ok, first = cap.read()
    cap.release()
    if not ok:
        logger.warning("roi_bounces: cannot read first frame; skipping")
        return (0, []) if return_rows else 0

    pixel_roi = _service_box_pixel_roi(court_detector, first.shape)
    if pixel_roi is None:
        logger.warning(
            "roi_bounces: cannot project service-box corners to pixels — "
            "court_detector likely uncalibrated; skipping"
        )
        return (0, []) if return_rows else 0
    x0, y0, x1, y1 = pixel_roi
    logger.info(
        "roi_bounces: service-box pixel ROI (%d,%d)-(%d,%d) size=%dx%d",
        x0, y0, x1, y1, x1 - x0, y1 - y0,
    )

    # 5. Run BallTracker on each window, project, filter to service-box zone
    all_rows: list = []
    for i, (s, e, c) in enumerate(windows):
        t0 = time.time()
        dets = _run_roi_window(video_path, s, e, pixel_roi)
        projected = _project_dets_to_court(dets, pixel_roi, court_detector)
        # Stamp the cluster centroid timestamp on each row for traceability
        window_ts = c / fps
        kept = []
        for r in projected:
            if not _in_service_box_zone(r["court_x"], r["court_y"]):
                continue
            r["window_serve_ts"] = window_ts
            kept.append(r)
        n_bounces = sum(1 for r in kept if r["is_bounce"])
        logger.info(
            "roi_bounces: [%d/%d] frames [%d,%d) center=%d ts=%.2fs "
            "-> %d dets in zone, %d bounces (%.1fs)",
            i + 1, len(windows), s, e, c, window_ts,
            len(kept), n_bounces, time.time() - t0,
        )
        all_rows.extend(kept)

    n_bounces_total = sum(1 for r in all_rows if r["is_bounce"])
    logger.info(
        "roi_bounces: total %d rows (%d bounces) across %d windows in %.1fs",
        len(all_rows), n_bounces_total, len(windows), time.time() - t_start,
    )

    # 6. Persist
    if engine is None:
        logger.info("roi_bounces: engine=None — skipping DB write")
    else:
        _persist_rows(engine, job_id, source_tag, all_rows, replace)

    if return_rows:
        return len(all_rows), all_rows
    return len(all_rows)
