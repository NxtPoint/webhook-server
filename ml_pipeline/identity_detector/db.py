"""DDL for the identity-detector module.

Two idempotent DDLs:

  1. `ALTER TABLE bronze.submission_context ADD COLUMN IF NOT EXISTS a_starts_near BOOLEAN DEFAULT TRUE`
     — captures the upload-form answer to "Player A is on the camera side
     at the start of the match." Defaults TRUE so legacy rows behave as if
     the (most common) "owner films from near baseline" case held.

  2. `CREATE TABLE IF NOT EXISTS ml_analysis.player_identity_segments`
     — one row per game per match recording which physical side (near/far)
     each labelled player (A, B) is on for that game. Wraps over changeovers
     so silver can join on game_number to recover stable identity.

Called by `init_identity_schema(conn)` — safe to call repeatedly. NOT
registered in `upload_app.py` boot in this commit; the parent session will
wire it into the boot sequence after reviewing the module.
"""
from __future__ import annotations

import logging
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)


_DDL_SUBMISSION_CONTEXT_ALTER = """
ALTER TABLE bronze.submission_context
  ADD COLUMN IF NOT EXISTS a_starts_near BOOLEAN DEFAULT TRUE
"""


_DDL_IDENTITY_SEGMENTS = """
CREATE TABLE IF NOT EXISTS ml_analysis.player_identity_segments (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID NOT NULL,
    game_number     INT NOT NULL,
    player_a_side   TEXT NOT NULL,    -- 'near' or 'far'
    player_b_side   TEXT NOT NULL,    -- 'near' or 'far'
    confidence      FLOAT NOT NULL,
    source          TEXT NOT NULL,    -- 'rule_v1' | 'rule_v1_anomaly'
                                      -- | 'rule_v1_terminated' | 'rule_v1_medical_break'
                                      -- | 'rule_v1_initial' | 'needs_review'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_pis_job_game UNIQUE (job_id, game_number)
);
CREATE INDEX IF NOT EXISTS ix_pis_job
    ON ml_analysis.player_identity_segments (job_id, game_number);
"""


def init_identity_schema(conn) -> None:
    """Create both DDLs. Safe to call repeatedly."""
    try:
        conn.execute(sql_text(_DDL_SUBMISSION_CONTEXT_ALTER))
        # CREATE INDEX cannot share a multi-statement with CREATE TABLE under
        # some psycopg2 driver paths; split them explicitly to stay safe.
        for stmt in _DDL_IDENTITY_SEGMENTS.split(";"):
            s = stmt.strip()
            if s:
                conn.execute(sql_text(s))
        logger.info("ml_analysis.player_identity_segments schema ready")
    except Exception:
        logger.exception("Failed to init identity_detector schema")
        raise


def delete_identity_for_job(conn, job_id: str) -> int:
    """Wipe any prior identity segments for this job. Called before re-detecting."""
    result = conn.execute(
        sql_text(
            "DELETE FROM ml_analysis.player_identity_segments WHERE job_id = :j"
        ),
        {"j": job_id},
    )
    return result.rowcount or 0
