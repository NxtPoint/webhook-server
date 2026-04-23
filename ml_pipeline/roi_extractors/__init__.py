"""ROI extractors — refine bronze player/ball detections for small-body
far-player regions via tighter-crop models.

Called from ml_pipeline/__main__.py after the main TennisAnalysisPipeline
finishes, during AWS Batch processing. Writes to ml_analysis tables:
    player_detections_roi (source='far_vitpose')   — via pose.extract_far_pose
    ball_detections_roi   (source='roi_wasb')      — via bounces.extract_far_bounces

These supplement bronze ml_analysis.player_detections / ball_detections
with higher-resolution pose/bounce signal for the far-baseline area
where bronze models (YOLOv8x-pose, TrackNet V2) under-resolve the 30-50 px
far player body / fast serve bounce respectively.

Failure-tolerant: each extractor logs errors and returns 0 on exception
rather than propagating — Batch job stays successful if ROI extraction
flakes out. Near-player detection + ball tracking already completed
upstream, so the job's primary output is preserved.
"""
from ml_pipeline.roi_extractors.pose import extract_far_pose  # noqa: F401
from ml_pipeline.roi_extractors.bounces import extract_far_bounces  # noqa: F401
