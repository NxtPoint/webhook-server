"""DDL for ml_analysis.stroke_events. Idempotent — called on service boot.

One row per detected stroke. Indexed by (task_id, predicted_hit_frame).
Delete+reinsert per task on re-detection (a task's strokes are re-derived
whole, not merged) — matches the serve_events lifecycle.
"""
from __future__ import annotations

import logging
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS ml_analysis.stroke_events (
    id                          BIGSERIAL PRIMARY KEY,
    task_id                     UUID NOT NULL,
    frame_idx                   INTEGER NOT NULL,
    ts                          DOUBLE PRECISION NOT NULL,
    predicted_hit_frame         INTEGER NOT NULL,
    player_id                   INTEGER NOT NULL,
    confidence                  DOUBLE PRECISION NOT NULL,

    peak_velocity_px_per_frame  DOUBLE PRECISION NOT NULL,
    pre_peak_v                  DOUBLE PRECISION,
    post_peak_v                 DOUBLE PRECISION,
    decel_ratio                 DOUBLE PRECISION,

    -- The COMPLETE hit fact (stroke = ball-hit). Silver projects these verbatim
    -- (rule #1/#2) instead of reconstructing them. Nullable: far-court court_y is
    -- ~50% NULL (calibration tail) -> emit NULL, never block the row.
    ball_hit_location_x         DOUBLE PRECISION,   -- hitter court_x at the hit
    ball_hit_location_y         DOUBLE PRECISION,   -- hitter court_y at the hit
    hitter_side_near            BOOLEAN,            -- resolved side (near=court_y>HALF_Y); bounce-opposite, attribution fallback
    volley                      BOOLEAN,            -- no ball bounce since the previous hit (struck out of the air)

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_stroke_events_task_frame_player
        UNIQUE (task_id, predicted_hit_frame, player_id)
);

CREATE INDEX IF NOT EXISTS ix_stroke_events_task_ts
    ON ml_analysis.stroke_events (task_id, ts);

-- Idempotent migrations for existing tables (CREATE TABLE IF NOT EXISTS won't
-- add columns to a table that already exists, e.g. rev-80 task ea085d50).
ALTER TABLE ml_analysis.stroke_events ADD COLUMN IF NOT EXISTS ball_hit_location_x DOUBLE PRECISION;
ALTER TABLE ml_analysis.stroke_events ADD COLUMN IF NOT EXISTS ball_hit_location_y DOUBLE PRECISION;
ALTER TABLE ml_analysis.stroke_events ADD COLUMN IF NOT EXISTS hitter_side_near BOOLEAN;
ALTER TABLE ml_analysis.stroke_events ADD COLUMN IF NOT EXISTS volley BOOLEAN;
"""


def init_stroke_events_schema(conn) -> None:
    """Create the table + indexes if not present. Safe to call repeatedly."""
    try:
        conn.execute(sql_text(_DDL))
        logger.info("ml_analysis.stroke_events schema ready")
    except Exception:
        logger.exception("Failed to init ml_analysis.stroke_events schema")
        raise


def delete_strokes_for_task(conn, task_id: str) -> int:
    """Wipe any prior stroke events for this task. Called before re-detecting."""
    result = conn.execute(
        sql_text("DELETE FROM ml_analysis.stroke_events WHERE task_id = :tid"),
        {"tid": task_id},
    )
    return result.rowcount or 0
