"""
ml_pipeline/db_schema.py — Idempotent DDL for ML analysis tables.

Creates tables in the `ml_analysis` schema:
  - video_analysis_jobs  (one row per pipeline invocation)
  - ball_detections      (per-frame ball positions)
  - player_detections    (per-frame player bounding boxes)
  - match_analytics      (aggregated stats per job)

Safe to call on every boot (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).
"""

import os
import logging
from sqlalchemy import create_engine, text as sql_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine (reuse db_init pattern)
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("DB_URL")


def _get_engine():
    url = DATABASE_URL
    if not url:
        raise RuntimeError("DATABASE_URL (or POSTGRES_URL / DB_URL) env var is required.")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800, future=True)


def ml_analysis_init(engine=None):
    """
    Public entrypoint: create ml_analysis schema + all tables + indexes.
    Safe to call on every boot (idempotent).
    """
    if engine is None:
        engine = _get_engine()
    with engine.begin() as conn:
        _create_schema(conn)
        _create_jobs_table(conn)
        _create_ball_detections_table(conn)
        _create_player_detections_table(conn)
        _create_match_analytics_table(conn)
        _create_practice_detail_table(conn)
        _create_indexes(conn)
    logger.info("ml_analysis schema init complete")


def _create_schema(conn):
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS ml_analysis;"))


def _create_jobs_table(conn):
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS ml_analysis.video_analysis_jobs (
            id              BIGSERIAL PRIMARY KEY,
            job_id          TEXT NOT NULL UNIQUE,
            task_id         TEXT,
            s3_key          TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'queued',
            current_stage   TEXT,
            progress_pct    INTEGER DEFAULT 0,
            error_message   TEXT,

            -- Video metadata
            video_duration_sec  DOUBLE PRECISION,
            video_fps           DOUBLE PRECISION,
            video_width         INTEGER,
            video_height        INTEGER,
            video_codec         TEXT,
            video_file_size     BIGINT,

            -- Processing metrics
            total_frames        INTEGER,
            frame_errors        INTEGER DEFAULT 0,
            processing_time_sec DOUBLE PRECISION,
            ms_per_frame        DOUBLE PRECISION,

            -- Court detection
            court_detected      BOOLEAN,
            court_confidence    DOUBLE PRECISION,
            court_used_fallback BOOLEAN,

            -- Heatmap S3 keys
            ball_heatmap_s3_key     TEXT,
            player_heatmap_s3_keys  JSONB,

            -- Cost tracking
            batch_job_id        TEXT,
            batch_start_at      TIMESTAMPTZ,
            batch_end_at        TIMESTAMPTZ,
            batch_duration_sec  DOUBLE PRECISION,
            estimated_cost_usd  DOUBLE PRECISION,

            -- AWS Batch metadata
            batch_job_arn       TEXT,
            compute_env         TEXT,

            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    # Idempotent column additions for existing tables
    conn.execute(sql_text(
        "ALTER TABLE ml_analysis.video_analysis_jobs "
        "ADD COLUMN IF NOT EXISTS bronze_s3_key TEXT"
    ))
    conn.execute(sql_text(
        "ALTER TABLE ml_analysis.video_analysis_jobs "
        "ADD COLUMN IF NOT EXISTS submitted_region TEXT"
    ))


def _create_ball_detections_table(conn):
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS ml_analysis.ball_detections (
            id          BIGSERIAL PRIMARY KEY,
            job_id      TEXT NOT NULL,
            frame_idx   INTEGER NOT NULL,
            x           DOUBLE PRECISION NOT NULL,
            y           DOUBLE PRECISION NOT NULL,
            court_x     DOUBLE PRECISION,
            court_y     DOUBLE PRECISION,
            speed_kmh   DOUBLE PRECISION,
            is_bounce   BOOLEAN NOT NULL DEFAULT FALSE,
            is_in       BOOLEAN,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))


def _create_player_detections_table(conn):
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS ml_analysis.player_detections (
            id          BIGSERIAL PRIMARY KEY,
            job_id      TEXT NOT NULL,
            frame_idx   INTEGER NOT NULL,
            player_id   INTEGER NOT NULL,
            bbox_x1     DOUBLE PRECISION NOT NULL,
            bbox_y1     DOUBLE PRECISION NOT NULL,
            bbox_x2     DOUBLE PRECISION NOT NULL,
            bbox_y2     DOUBLE PRECISION NOT NULL,
            center_x    DOUBLE PRECISION NOT NULL,
            center_y    DOUBLE PRECISION NOT NULL,
            court_x     DOUBLE PRECISION,
            court_y     DOUBLE PRECISION,
            keypoints   JSONB,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    # Idempotent column addition for existing tables
    conn.execute(sql_text(
        "ALTER TABLE ml_analysis.player_detections ADD COLUMN IF NOT EXISTS keypoints JSONB"
    ))
    conn.execute(sql_text(
        "ALTER TABLE ml_analysis.player_detections ADD COLUMN IF NOT EXISTS stroke_class TEXT"
    ))


def _create_match_analytics_table(conn):
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS ml_analysis.match_analytics (
            id                  BIGSERIAL PRIMARY KEY,
            job_id              TEXT NOT NULL UNIQUE,
            task_id             TEXT,

            -- Ball stats
            ball_detection_rate DOUBLE PRECISION,
            bounce_count        INTEGER,
            bounces_in          INTEGER,
            bounces_out         INTEGER,
            max_speed_kmh       DOUBLE PRECISION,
            avg_speed_kmh       DOUBLE PRECISION,

            -- Rally / serve
            rally_count         INTEGER,
            avg_rally_length    DOUBLE PRECISION,
            serve_count         INTEGER,
            first_serve_pct     DOUBLE PRECISION,

            -- Player stats
            player_count        INTEGER,

            -- Processing
            total_frames        INTEGER,
            frame_errors        INTEGER,
            processing_time_sec DOUBLE PRECISION,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))


def _create_practice_detail_table(conn):
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS silver;"))
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS silver.practice_detail (
            id              BIGSERIAL PRIMARY KEY,
            task_id         TEXT NOT NULL,
            practice_type   TEXT NOT NULL,
            player_id       INTEGER,
            sequence_num    INTEGER NOT NULL,
            shot_ix         INTEGER,

            -- Ball data
            ball_x          DOUBLE PRECISION,
            ball_y          DOUBLE PRECISION,
            ball_speed_kmh  DOUBLE PRECISION,
            is_bounce       BOOLEAN NOT NULL DEFAULT TRUE,
            is_in           BOOLEAN,
            frame_idx       INTEGER,
            timestamp_s     DOUBLE PRECISION,

            -- Serve-specific
            serve_zone      TEXT,
            serve_side      TEXT,
            serve_result    TEXT,
            serve_location  INTEGER,
            serve_bucket_d  TEXT,

            -- Rally-specific
            rally_length    INTEGER,
            rally_duration_s DOUBLE PRECISION,
            rally_length_bucket_d TEXT,

            -- Player position at this frame
            player_court_x  DOUBLE PRECISION,
            player_court_y  DOUBLE PRECISION,

            -- Derived analytics (aligned with match silver conventions)
            placement_zone  TEXT,
            depth_d         TEXT,
            stroke_d        TEXT,
            aggression_d    TEXT,

            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    # Idempotent column additions for existing tables
    for col_ddl in (
        "ALTER TABLE silver.practice_detail ADD COLUMN IF NOT EXISTS stroke_d TEXT",
        "ALTER TABLE silver.practice_detail ADD COLUMN IF NOT EXISTS serve_location INTEGER",
        "ALTER TABLE silver.practice_detail ADD COLUMN IF NOT EXISTS serve_bucket_d TEXT",
        "ALTER TABLE silver.practice_detail ADD COLUMN IF NOT EXISTS rally_length_bucket_d TEXT",
        "ALTER TABLE silver.practice_detail ADD COLUMN IF NOT EXISTS aggression_d TEXT",
    ):
        conn.execute(sql_text(col_ddl))


def _create_indexes(conn):
    # video_analysis_jobs
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_jobs_task_id
            ON ml_analysis.video_analysis_jobs (task_id);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_jobs_status
            ON ml_analysis.video_analysis_jobs (status);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_jobs_s3_key
            ON ml_analysis.video_analysis_jobs (s3_key);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_jobs_created
            ON ml_analysis.video_analysis_jobs (created_at);
    """))

    # ball_detections
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_ball_det_job
            ON ml_analysis.ball_detections (job_id);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_ball_det_job_frame
            ON ml_analysis.ball_detections (job_id, frame_idx);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_ball_det_bounce
            ON ml_analysis.ball_detections (job_id) WHERE is_bounce = TRUE;
    """))

    # player_detections
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_player_det_job
            ON ml_analysis.player_detections (job_id);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_player_det_job_frame
            ON ml_analysis.player_detections (job_id, frame_idx);
    """))

    # match_analytics
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_analytics_job
            ON ml_analysis.match_analytics (job_id);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ml_analytics_task
            ON ml_analysis.match_analytics (task_id);
    """))

    # practice_detail (silver schema)
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_practice_detail_task
            ON silver.practice_detail (task_id);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_practice_detail_task_seq
            ON silver.practice_detail (task_id, sequence_num);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_practice_detail_type
            ON silver.practice_detail (task_id, practice_type);
    """))
