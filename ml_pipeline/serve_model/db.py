"""DDL for ml_analysis.serve_candidates -- scored far-serve candidate anchors.

The MODEL-layer serve fact (one-model-per-fact, north_star RULES). Written
Batch-side by the serve-candidates stage in __main__ (mirrors the bounce
stage / ml_analysis.ball_bounces pattern); consumed Render-side by
serve_detector.detect_serves_for_task behind SERVE_MODEL_ENABLED.

Survives the Render re-ingest the same way ball_bounces does: the
bronze_ingest_t5 DELETE+COPY touches only ball_detections /
player_detections / match_analytics (+ singletons), never this table.

Rows are RAW scored anchors above a low floor (no NMS): thresholding +
NMS happen at the Render consumer so the operating point is tunable via
env without a Batch rerun.

Idempotent. Safe to call on every stage run.
"""
from __future__ import annotations

import logging
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS ml_analysis.serve_candidates (
    id               BIGSERIAL PRIMARY KEY,
    job_id           UUID NOT NULL,
    ts               DOUBLE PRECISION NOT NULL,
    frame_idx        INTEGER NOT NULL,
    score            DOUBLE PRECISION NOT NULL,
    anchor_source    TEXT NOT NULL,            -- 'bounce' | 'pose'
    bounce_court_x   DOUBLE PRECISION,
    bounce_court_y   DOUBLE PRECISION,
    model_version    TEXT NOT NULL,            -- e.g. 'serve_model_v1'
    train_threshold  DOUBLE PRECISION,         -- operating point from training meta
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_serve_candidates_job
    ON ml_analysis.serve_candidates (job_id, ts);
"""


def init_serve_candidates_schema(conn) -> None:
    """Create the serve_candidates table + index if not present."""
    try:
        conn.execute(sql_text(_DDL))
        logger.info("ml_analysis.serve_candidates schema ready")
    except Exception:
        logger.exception("Failed to init ml_analysis.serve_candidates schema")
        raise


def delete_candidates_for_task(conn, task_id: str) -> int:
    """Wipe prior candidates for this task. Called before re-scoring."""
    result = conn.execute(
        sql_text("DELETE FROM ml_analysis.serve_candidates WHERE job_id::text = :tid"),
        {"tid": task_id},
    )
    return result.rowcount or 0


def persist_candidates(conn, task_id: str, candidates) -> int:
    """Insert ServeCandidate rows (see infer.ServeCandidate)."""
    n = 0
    for c in candidates:
        conn.execute(sql_text("""
            INSERT INTO ml_analysis.serve_candidates
                (job_id, ts, frame_idx, score, anchor_source,
                 bounce_court_x, bounce_court_y, model_version, train_threshold)
            VALUES (CAST(:job_id AS uuid), :ts, :frame_idx, :score, :src,
                    :bx, :by, :ver, :thr)
        """), {
            "job_id": task_id, "ts": float(c.ts), "frame_idx": int(c.frame_idx),
            "score": float(c.score), "src": c.anchor_source,
            "bx": c.bounce_court_x, "by": c.bounce_court_y,
            "ver": c.model_version, "thr": c.train_threshold,
        })
        n += 1
    return n
