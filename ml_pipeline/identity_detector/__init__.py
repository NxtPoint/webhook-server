"""T5 identity detector — rule-based per-game A/B identity flip detection.

Architecture (per ADR-03 §"Build spec v1", 2026-05-28):
  - game_boundaries: server-alternation derivation of per-game time windows
  - changeover_rule: per-game ITF + dual-cross decision matrix
  - detector: orchestrator that combines them into IdentitySegment rows
  - schema/db: ml_analysis.player_identity_segments + the
               bronze.submission_context.a_starts_near column (idempotent)

Identity detection runs AFTER serve_detector (it consumes serve_events) and
BEFORE silver build (silver consumes the resulting per-game side mapping
to stabilise A/B across changeovers).

Entry point for production:
    from ml_pipeline.identity_detector import detect_identity_for_task
    detect_identity_for_task(conn, task_id)

Entry point for offline validation (no DB):
    from ml_pipeline.identity_detector.detector import detect_identity_offline
"""
from ml_pipeline.identity_detector.detector import (
    detect_identity_for_task,
    detect_identity_offline,
)
from ml_pipeline.identity_detector.db import init_identity_schema
from ml_pipeline.identity_detector.models import (
    GameBoundary,
    IdentitySegment,
    IdentitySource,
    Side,
)

__all__ = [
    "detect_identity_for_task",
    "detect_identity_offline",
    "init_identity_schema",
    "GameBoundary",
    "IdentitySegment",
    "IdentitySource",
    "Side",
]


# Public single-callable per spec ("detect_identity_segments(task_id) ->
# list[IdentitySegment]") — convenience wrapper that obtains its own
# connection from db_init.engine.
def detect_identity_segments(task_id: str):
    """Public API per ADR-03 build spec. Opens a connection, runs the v1
    detector against the live DB, returns the persisted IdentitySegment list."""
    from db_init import engine  # local import to avoid Flask-boot side-effects
    with engine.begin() as conn:
        return detect_identity_for_task(conn, task_id)
