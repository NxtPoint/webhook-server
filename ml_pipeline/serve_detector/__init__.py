"""T5 serve detector — pose-first serve detection.

Architecture (per Apr 17 design):
  - pose_signal: per-frame serve-pose score (Silent Impact passive-arm rule)
  - ball_toss: optional ball-rising confirmation signal
  - rally_state: HMM-style state machine gating serves to pre-point state
  - detector: orchestrator that combines signals into ServeEvent records
  - schema: ml_analysis.serve_events DDL (idempotent)

Serve detection runs AFTER bronze ingest, BEFORE silver build. Emits
`ml_analysis.serve_events` rows; the silver builder reads those instead
of iterating over ball bounces.

Entry point for production:
    from ml_pipeline.serve_detector import detect_serves_for_task
    detect_serves_for_task(conn, task_id)

Entry point for offline validation (reading from a JSONL pose dump
instead of DB):
    from ml_pipeline.serve_detector.detector import detect_serves_offline
"""
from ml_pipeline.serve_detector.models import ServeEvent, SignalSource
from ml_pipeline.serve_detector.detector import detect_serves_for_task
from ml_pipeline.serve_detector.schema import init_serve_events_schema

__all__ = [
    "ServeEvent",
    "SignalSource",
    "detect_serves_for_task",
    "init_serve_events_schema",
]
