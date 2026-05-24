"""T5 stroke detector — pose-first stroke (non-serve) detection.

Generalises the serve_detector pattern to all rally strokes. Same
architectural shape: pose signal → events table → silver consumer.

Architecture (per 2026-05-24 strategy):
  - velocity_signal: per-frame wrist-velocity computation (left+right, both
    players), smoothed and peak-detected.
  - detector: orchestrator that turns peaks into StrokeEvent records, with
    three refinements over the probe (`diag/ball_hit_pose.py`):
      1. Peak-to-contact offset (+4 frames): velocity peak fires on the
         backswing-to-contact transition, contact happens 4-6 frames later.
      2. min_gap_frames 15→25: suppresses backswing + forward-swing +
         follow-through firing as 3 separate peaks on the same stroke.
      3. Deceleration check: a real swing's velocity falls fast post-peak.
         A walking/picking-up motion plateaus. Reject peaks that don't
         drop ≥50% within 3 frames.
  - schema: ml_analysis.stroke_events DDL (idempotent).

Stroke detection runs AFTER bronze ingest, BEFORE silver build, alongside
serve detection. Emits `ml_analysis.stroke_events` rows; downstream silver
builders consume these for stroke counting / classification.

Entry point for production:
    from ml_pipeline.stroke_detector import detect_strokes_for_task
    detect_strokes_for_task(conn, task_id)

Entry point for offline validation (no DB, no side effects):
    from ml_pipeline.stroke_detector.detector import detect_strokes_offline
"""
from ml_pipeline.stroke_detector.models import StrokeEvent
from ml_pipeline.stroke_detector.detector import (
    detect_strokes_for_task,
    detect_strokes_offline,
)
from ml_pipeline.stroke_detector.schema import init_stroke_events_schema

__all__ = [
    "StrokeEvent",
    "detect_strokes_for_task",
    "detect_strokes_offline",
    "init_stroke_events_schema",
]
