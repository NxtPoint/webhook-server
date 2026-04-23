"""Far-baseline WASB bounce extractor — PRODUCTION STUB.

The diag version (ml_pipeline/diag/extract_wasb_bounces.py) needs a
SportAI reference task to identify serve-time windows around which to
scan for bounces. In production (fresh user upload, no SA counterpart)
we don't have that signal.

A proper production extractor would need to either:
  (a) Scan the entire video with WASB HRNet (expensive — ~15000 frames)
  (b) Use pose-event timestamps from the SERVE_DETECTOR as anchor
      points, but the serve_detector runs POST-Batch on Render, so the
      extractor would need to become a 3rd pipeline stage after
      Render-side ingest
  (c) Use bronze ball_detections to identify near-service-box candidate
      bounce moments and scan only those windows

For now this module is a stub: it logs a notice and returns 0 so the
Batch flow stays green. The far-player pose extractor (pose.py) is
the dominant driver of far-detection gains; the bounce-path
contribution was empirically 1 of 9 strict MATCHes on d1fed568.

Follow-up: implement proper SA-less variant in a subsequent session
once the pose pipeline is proven stable in Batch.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("roi_bounces")


def extract_far_bounces(
    video_path: str,
    job_id: str,
    engine,
    **kwargs,
) -> int:
    """STUB — production WASB bounces not yet implemented.

    Returns 0 (no rows written). See module docstring for rationale.
    """
    logger.info(
        "roi_bounces: STUB — production WASB bounces not implemented yet; "
        "skipping for job_id=%s",
        job_id,
    )
    return 0
