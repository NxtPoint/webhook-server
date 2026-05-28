"""DDL for ml_analysis.swing_type_events — ADR-02 v2 swing-type predictions.

Schema per ADR-02 §"Q6 — output" (option B: separate table joined on
stroke_event timing, NOT new columns on ml_analysis.stroke_events).
Separate table preserves rule #8 ownership of stroke_detector/ by the
parallel agent; ADR-02 v1 spec allows either layout. Distinct from
silver's pose-keypoint STOPGAP swing_type (which lives in silver
projections, not bronze).

One row per swing-type prediction. Joinable to ml_analysis.stroke_events
on (job_id, hit_frame, player_id). `source` tags the producer so we can
audit which model ran (until trained weights ship, source='STOPGAP-no-weights'
and no row is written -- model returns empty).

Idempotent. Safe to call on every service boot. delete+reinsert per job
on rerun mirrors the stroke_events lifecycle.
"""
from __future__ import annotations

import logging
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS ml_analysis.swing_type_events (
    id            BIGSERIAL PRIMARY KEY,
    job_id        UUID NOT NULL,
    hit_frame     INTEGER NOT NULL,
    hit_ts        DOUBLE PRECISION,
    player_id     INTEGER NOT NULL,
    role          TEXT,                    -- 'NEAR' | 'FAR' (court_y > HALF_Y)
    swing_type    TEXT NOT NULL,           -- 'forehand' | 'backhand' | 'overhead'
    confidence    DOUBLE PRECISION NOT NULL,
    handedness    TEXT,                    -- 'right' | 'left' (input feature snapshot)
    source        TEXT NOT NULL,           -- 'swing_classifier_v2' / 'STOPGAP-no-weights'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_swing_type_events_job
    ON ml_analysis.swing_type_events (job_id, hit_frame);
"""


def init_swing_type_schema(conn) -> None:
    """Create the swing_type_events table + index if not present. Safe to call repeatedly."""
    try:
        conn.execute(sql_text(_DDL))
        logger.info("ml_analysis.swing_type_events schema ready")
    except Exception:
        logger.exception("Failed to init ml_analysis.swing_type_events schema")
        raise


def delete_swing_types_for_task(conn, task_id: str) -> int:
    """Wipe any prior swing-type predictions for this task. Called before re-detecting."""
    result = conn.execute(
        sql_text("DELETE FROM ml_analysis.swing_type_events WHERE job_id::text = :tid"),
        {"tid": task_id},
    )
    return result.rowcount or 0
