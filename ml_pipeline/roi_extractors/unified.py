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
from ml_pipeline.roi_extractors.far_ball import FarBallProcessor

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
    cnn_bounce_ts: Optional[List[float]] = None,
    cnn_bounce_events: Optional[List[dict]] = None,
    far_ball_window_s: float = 1.5,
    far_ball_cluster_gap_s: float = 0.5,
) -> Tuple[int, int, int]:
    """Decode the video once, drive the ROI extractors, return
    (n_pose, n_bounce, n_far_ball).

    Args mirror the kwargs __main__ passed to the standalone extractors so the
    production behaviour is identical. court_detector must be the calibrated
    pipeline.court_detector; bounces is result.ball_detections (also the
    far-ball anchor source — far-court presence frames).

    The far-ball consumer (env ROI_FAR_BALL_ENABLED, default on) re-detects the
    far ball on a high-res far-court crop → sharper far trajectory that lifts
    far-bounce candidate recall (40%->80% offline) and far-hit emission. Writes
    source='roi_far_ball'; readers dedup via ml_pipeline.ball_merge.
    """
    if not os.path.exists(video_path):
        logger.warning("roi_unified: video not found: %s; skipping", video_path)
        return (0, 0, 0)

    t_start = time.time()

    # Read the first frame for shape (both processors project their ROI off it).
    cap = cv2.VideoCapture(video_path)
    ok, first = cap.read()
    if not ok:
        cap.release()
        logger.warning("roi_unified: cannot read first frame; skipping")
        return (0, 0, 0)
    frame_shape = first.shape

    # Build + prepare each processor. A prepare failure (e.g. ROI can't project,
    # no bounce windows) just disables that consumer; the other still runs.
    pose: Optional[FarPoseProcessor] = None
    try:
        p = FarPoseProcessor(
            job_id, engine,
            fps=fps, sample_every=pose_sample_every,
            court_detector=court_detector, bounces=bounces,
            cnn_bounce_ts=cnn_bounce_ts,
            cnn_bounce_events=cnn_bounce_events,
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

    far_ball: Optional[FarBallProcessor] = None
    if os.environ.get("ROI_FAR_BALL_ENABLED", "1") != "0":
        try:
            fb = FarBallProcessor(
                job_id, engine,
                court_detector=court_detector, detections=bounces, fps=fps,
                window_s=far_ball_window_s, cluster_gap_s=far_ball_cluster_gap_s,
            )
            if fb.windows and fb.prepare(frame_shape):
                far_ball = fb
        except Exception as e:
            logger.warning("roi_unified: far_ball prepare failed (non-fatal): %s", e)
            far_ball = None

    if pose is None and bounce is None and far_ball is None:
        cap.release()
        logger.info("roi_unified: nothing to do (all processors disabled)")
        return (0, 0, 0)

    # Sweep extent: pose needs the whole video; bounce + far_ball each only up
    # to their last window end. If pose is inactive we can stop at the latest
    # window any active windowed consumer needs.
    sweep_to = None
    if pose is None:
        ends = []
        if bounce is not None:
            ends.append(bounce.last_frame_needed())
        if far_ball is not None:
            ends.append(far_ball.last_frame_needed())
        sweep_to = max(ends) if ends else 0

    # Single sequential decode, SAMPLED to the bronze frame rate.
    #
    # The ROI passes must index frames in the SAME sampled space as the main
    # pipeline / bronze (the caller passes fps = the pipeline's FRAME_SAMPLE_FPS,
    # e.g. 25). The old code walked every SOURCE frame and emitted source-frame
    # indices, so on a 60fps match far-pose rows landed in 60fps space (idx up to
    # ~172k) while bronze player_detections are 25fps (~72k) — misaligning the
    # bronze_export merge AND wasting a 2.4x over-decode. Here we sample the
    # source down to target fps and emit a target-fps-aligned out_idx, and we
    # grab()-skip (no decode) the unsampled frames — the big sweep speedup.
    source_fps = cap.get(cv2.CAP_PROP_FPS) or float(fps) or 25.0
    target_fps = float(fps) if fps else source_fps
    stride = (source_fps / target_fps) if (target_fps and target_fps < source_fps) else 1.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    pose_failed = False
    bounce_failed = False
    far_ball_failed = False
    src_idx = 0
    out_idx = 0
    next_sample_at = 0.0
    while True:
        if sweep_to is not None and out_idx >= sweep_to:
            break
        if not cap.grab():           # advance decoder; cheap (no full decode)
            break
        if src_idx >= next_sample_at:
            ok, frame = cap.retrieve()   # decode ONLY the sampled frames
            if not ok:
                break
            if pose is not None and not pose_failed:
                try:
                    pose.feed(frame, out_idx)
                except Exception as e:
                    logger.error(
                        "roi_unified: pose.feed raised at frame %d (dropping pose "
                        "pass, no rows written): %s", out_idx, e,
                    )
                    pose_failed = True
            if bounce is not None and not bounce_failed:
                try:
                    bounce.feed(frame, out_idx)
                except Exception as e:
                    logger.error(
                        "roi_unified: bounce.feed raised at frame %d (dropping "
                        "bounce pass, no rows written): %s", out_idx, e,
                    )
                    bounce_failed = True
            if far_ball is not None and not far_ball_failed:
                try:
                    far_ball.feed(frame, out_idx)
                except Exception as e:
                    logger.error(
                        "roi_unified: far_ball.feed raised at frame %d (dropping "
                        "far_ball pass, no rows written): %s", out_idx, e,
                    )
                    far_ball_failed = True
            next_sample_at += stride
            out_idx += 1
        src_idx += 1
    cap.release()
    logger.info(
        "roi_unified: decoded %d sampled frames of %d source (stride=%.2f, "
        "source_fps=%.1f target_fps=%.1f)",
        out_idx, src_idx, stride, source_fps, target_fps,
    )

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

    n_far_ball = 0
    if far_ball is not None and not far_ball_failed:
        try:
            n_far_ball = far_ball.finalize()
        except Exception as e:
            logger.error("roi_unified: far_ball.finalize raised (non-fatal): %s", e)

    logger.info(
        "roi_unified: single-decode sweep of %d sampled frames in %.1fs "
        "(pose=%d rows, bounce=%d rows, far_ball=%d rows)",
        out_idx, time.time() - t_start, n_pose, n_bounce, n_far_ball,
    )
    return (n_pose, n_bounce, n_far_ball)
