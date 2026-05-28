"""DDL for ml_analysis.ball_bounces — curated, confidence-scored bounce events.

Schema per ADR-01 §"Q4 — schema" (option B: new table). Distinct from
`ml_analysis.ball_detections.is_bounce` which carries the raw TrackNet
velocity-reversal signal. This table is the output of the dedicated
bounce model — one row per real ground bounce, with confidence and
provenance.

Idempotent. Safe to call on every service boot. NOT registered in
upload_app.py yet — parent session lands the registration as a
follow-up commit (ADR-01 v0 scope).
"""
from __future__ import annotations

import logging
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS ml_analysis.ball_bounces (
    id            BIGSERIAL PRIMARY KEY,
    job_id        UUID NOT NULL,
    ts            DOUBLE PRECISION NOT NULL,
    frame_idx     INTEGER NOT NULL,
    court_x       DOUBLE PRECISION,
    court_y       DOUBLE PRECISION,
    player_side   TEXT,            -- 'near' / 'far' / 'net_cord' / NULL
    confidence    DOUBLE PRECISION NOT NULL,
    in_point      BOOLEAN NOT NULL,
    source        TEXT NOT NULL,   -- e.g. 'bounce_detector_v1' / 'STOPGAP-untrained'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ball_bounces_job
    ON ml_analysis.ball_bounces (job_id, ts);
"""


def init_bounce_schema(conn) -> None:
    """Create the ball_bounces table + index if not present. Safe to call repeatedly."""
    try:
        conn.execute(sql_text(_DDL))
        logger.info("ml_analysis.ball_bounces schema ready")
    except Exception:
        logger.exception("Failed to init ml_analysis.ball_bounces schema")
        raise


def delete_bounces_for_task(conn, task_id: str) -> int:
    """Wipe any prior bounces for this task. Called before re-detecting."""
    result = conn.execute(
        sql_text("DELETE FROM ml_analysis.ball_bounces WHERE job_id::text = :tid"),
        {"tid": task_id},
    )
    return result.rowcount or 0
