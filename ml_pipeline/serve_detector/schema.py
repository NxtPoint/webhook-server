"""DDL for ml_analysis.serve_events. Idempotent — called on service boot.

One row per detected serve. Indexed by (task_id, frame_idx). Delete+reinsert
per task on re-detection (a task's serves are re-derived whole, not merged).
"""
from __future__ import annotations

import logging
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS ml_analysis.serve_events (
    id                 BIGSERIAL PRIMARY KEY,
    task_id            UUID NOT NULL,
    frame_idx          INTEGER NOT NULL,
    ts                 DOUBLE PRECISION NOT NULL,
    player_id          INTEGER NOT NULL,
    source             TEXT NOT NULL,
    confidence         DOUBLE PRECISION NOT NULL,

    pose_score         DOUBLE PRECISION,
    trophy_peak_frame  INTEGER,

    has_ball_toss      BOOLEAN NOT NULL DEFAULT FALSE,
    bounce_frame       INTEGER,
    bounce_court_x     DOUBLE PRECISION,
    bounce_court_y     DOUBLE PRECISION,

    rally_state        TEXT NOT NULL DEFAULT 'unknown',

    hitter_court_x     DOUBLE PRECISION,
    hitter_court_y     DOUBLE PRECISION,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_serve_events_task_frame_player
        UNIQUE (task_id, frame_idx, player_id)
);

CREATE INDEX IF NOT EXISTS ix_serve_events_task_ts
    ON ml_analysis.serve_events (task_id, ts);
"""


def init_serve_events_schema(conn) -> None:
    """Create the table + indexes if not present. Safe to call repeatedly."""
    try:
        conn.execute(sql_text(_DDL))
        logger.info("ml_analysis.serve_events schema ready")
    except Exception:
        logger.exception("Failed to init ml_analysis.serve_events schema")
        raise


def delete_serves_for_task(conn, task_id: str) -> int:
    """Wipe any prior serve events for this task. Called before re-detecting."""
    result = conn.execute(
        sql_text("DELETE FROM ml_analysis.serve_events WHERE task_id = :tid"),
        {"tid": task_id},
    )
    return result.rowcount or 0
