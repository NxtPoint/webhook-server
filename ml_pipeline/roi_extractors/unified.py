"""Unified ROI pass — decode the video ONCE and fan each decoded frame out to
both the far-player pose extractor and the service-box bounce extractor.

Why this exists
---------------
The two post-pipeline ROI passes (pose.extract_far_pose and
bounces.extract_far_bounces) each used to open their own VideoCapture and walk
the video independently — the pose pass decoded the whole video a second time,
and the bounce pass re-opened + CAP_PROP_POS_FRAMES-seeked once PER bounce
window (thrashing the decoder on long matches). That meant the video was
decoded ~3× end to end and was a primary reason long matches raced the 6h Batch
timeout (docs/_investigation/t5_pipeline_speed.md, Lever #1).

This driver decodes the video ONE more time after the main pipeline (so the
whole job decodes the video twice total: the main per-frame loop + this single
ROI sweep) and dispatches each frame to whichever ROI consumer wants it. It
cannot be folded into the main loop because both ROI passes depend on the
*final* bounce list (pose's rally gate + the bounce windows) which only exists
after pipeline.process() + _postprocess complete.

ZERO accuracy risk: the per-frame cores (FarPoseProcessor / RoiBounceProcessor)
run the same models on the same frames and emit the same rows as the standalone
extractors — only the decode scheduling changed. Per-consumer failures are
isolated: if one processor raises mid-sweep it is dropped (writes nothing,
matching the old all-or-nothing per-pass try/except in __main__) while the other
continues.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional, Tuple

import cv2

from ml_pipeline.roi_extractors.pose import FarPoseProcessor
from ml_pipeline.roi_extractors.bounces import RoiBounceProcessor

logger = logging.getLogger("roi_unified")


def run_unified_roi(
    video_path: str,
    job_id: str,
    engine,
    *,
    fps: float = 25.0,
    court_detector=None,
    bounces: Optional[List] = None,
    pose_sample_every: int = 2,
    bounce_window_s: float = 2.5,
    bounce_cluster_gap_s: float = 0.5,
    bounce_anchor_zone_filter: bool = False,
    bounce_anchor_bounce_only: bool = True,
) -> Tuple[int, int]:
    """Decode the video once, drive both ROI extractors, return (n_pose, n_bounce).

    Args mirror the kwargs __main__ passed to the two standalone extractors so
    the production behaviour is identical. court_detector must be the calibrated
    pipeline.court_detector; bounces is result.ball_detections.
    """
    if not os.path.exists(video_path):
        logger.warning("roi_unified: video not found: %s; skipping", video_path)
        return (0, 0)

    t_start = time.time()

    # Read the first frame for shape (both processors project their ROI off it).
    cap = cv2.VideoCapture(video_path)
    ok, first = cap.read()
    if not ok:
        cap.release()
        logger.warning("roi_unified: cannot read first frame; skipping")
        return (0, 0)
    frame_shape = first.shape

    # Build + prepare each processor. A prepare failure (e.g. ROI can't project,
    # no bounce windows) just disables that consumer; the other still runs.
    pose: Optional[FarPoseProcessor] = None
    try:
        p = FarPoseProcessor(
            job_id, engine,
            fps=fps, sample_every=pose_sample_every,
            court_detector=court_detector, bounces=bounces,
        )
        if p.prepare(frame_shape):
            pose = p
    except Exception as e:
        logger.warning("roi_unified: pose prepare failed (non-fatal): %s", e)
        pose = None

    bounce: Optional[RoiBounceProcessor] = None
    try:
        b = RoiBounceProcessor(
            job_id, engine,
            court_detector=court_detector, bounces=bounces, fps=fps,
            window_s=bounce_window_s, cluster_gap_s=bounce_cluster_gap_s,
            anchor_zone_filter=bounce_anchor_zone_filter,
            anchor_bounce_only=bounce_anchor_bounce_only,
        )
        if b.windows and b.prepare(frame_shape):
            bounce = b
    except Exception as e:
        logger.warning("roi_unified: bounce prepare failed (non-fatal): %s", e)
        bounce = None

    if pose is None and bounce is None:
        cap.release()
        logger.info("roi_unified: nothing to do (both processors disabled)")
        return (0, 0)

    # Sweep extent: pose needs the whole video; bounce only up to its last
    # window end. If only bounce is active we can stop early.
    sweep_to = None
    if pose is None and bounce is not None:
        sweep_to = bounce.last_frame_needed()

    # Single sequential decode from frame 0 (we already consumed frame 0 above).
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    pose_failed = False
    bounce_failed = False
    idx = 0
    while True:
        if sweep_to is not None and idx >= sweep_to:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if pose is not None and not pose_failed:
            try:
                pose.feed(frame, idx)
            except Exception as e:
                logger.error(
                    "roi_unified: pose.feed raised at frame %d (dropping pose "
                    "pass, no rows written): %s", idx, e,
                )
                pose_failed = True
        if bounce is not None and not bounce_failed:
            try:
                bounce.feed(frame, idx)
            except Exception as e:
                logger.error(
                    "roi_unified: bounce.feed raised at frame %d (dropping "
                    "bounce pass, no rows written): %s", idx, e,
                )
                bounce_failed = True
        idx += 1
    cap.release()

    # Finalize each surviving consumer independently.
    n_pose = 0
    if pose is not None and not pose_failed:
        try:
            n_pose = pose.finalize(scan_seconds=time.time() - t_start)
        except Exception as e:
            logger.error("roi_unified: pose.finalize raised (non-fatal): %s", e)

    n_bounce = 0
    if bounce is not None and not bounce_failed:
        try:
            n_bounce = bounce.finalize()
        except Exception as e:
            logger.error("roi_unified: bounce.finalize raised (non-fatal): %s", e)

    logger.info(
        "roi_unified: single-decode sweep of %d frames in %.1fs "
        "(pose=%d rows, bounce=%d rows)",
        idx, time.time() - t_start, n_pose, n_bounce,
    )
    return (n_pose, n_bounce)
