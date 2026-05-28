"""T5 bounce detector — temporal CNN for ground-bounce events.

Architecture per ADR-01 (`docs/_investigation/adr_01_bounce_model_architecture.md`):
  - models       : BounceEvent dataclass + SignalSource enum
  - db           : ml_analysis.ball_bounces DDL (idempotent)
  - cnn          : 1D temporal CNN (3 blocks, 14ch x 41fr; STOPGAP-untrained-stage1)
  - feature_extractor: 14-channel x 41-frame window builder
  - pre_gates    : wrist-proximity / net-line / rally-state hard gates
  - detector     : orchestrator — pre_gates -> features -> CNN -> NMS -> persist

Bounce detection runs AFTER bronze ingest + serve_detector, BEFORE silver
build (serve_events is an input to the bounce pre-gates). Emits
`ml_analysis.ball_bounces` rows; silver builder (parallel agent's territory)
will eventually read those instead of `ml_analysis.ball_detections.is_bounce`.

Entry point for production:
    from ml_pipeline.bounce_detector import detect_bounces
    detect_bounces(task_id)

Entry point for offline validation:
    from ml_pipeline.bounce_detector.detector import detect_bounces_offline

# STOPGAP-untrained-stage1: v0 ships with random CNN weights and a
# threshold of 1.1 (impossible) so no rows are written; the plumbing
# is fully exercised but no bronze pollution. Next session: label
# audit + negative mining + training -> ship weights -> flip threshold.
"""
from ml_pipeline.bounce_detector.detector import (
    detect_bounces,
    detect_bounces_offline,
)
from ml_pipeline.bounce_detector.db import (
    init_bounce_schema,
    delete_bounces_for_task,
)
from ml_pipeline.bounce_detector.models import (
    BounceEvent,
    PlayerSide,
    SignalSource,
)

__all__ = [
    "detect_bounces",
    "detect_bounces_offline",
    "init_bounce_schema",
    "delete_bounces_for_task",
    "BounceEvent",
    "PlayerSide",
    "SignalSource",
]
