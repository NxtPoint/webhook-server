# technique/db_schema.py — Idempotent bronze table creation for technique analysis.
#
# Tables live in the bronze.* schema (same as match analysis).
# Called on boot via technique_bronze_init() — safe to call every time.

from __future__ import annotations

import logging

from sqlalchemy import text as sql_text

log = logging.getLogger(__name__)


def technique_bronze_init(engine) -> None:
    """Create technique bronze tables idempotently. Safe to call on every boot."""
    with engine.begin() as conn:
        conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze"))
        conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS silver"))

        # ── technique_analysis_metadata ────────────────────────────
        # Top-level fields: uid, status, warnings, errors, request metadata.
        # One row per analysis.
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS bronze.technique_analysis_metadata (
                id          BIGSERIAL PRIMARY KEY,
                task_id     TEXT NOT NULL,
                uid         TEXT,
                status      TEXT,
                sport       TEXT,
                swing_type  TEXT,
                dominant_hand TEXT,
                player_height_mm INT,
                warnings    JSONB,
                errors      JSONB,
                raw_meta    JSONB,
                created_at  TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(sql_text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_technique_meta_task
                ON bronze.technique_analysis_metadata (task_id)
        """))

        # ── technique_features ─────────────────────────────────────
        # One row per feature per analysis. From result.features[].
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS bronze.technique_features (
                id                  BIGSERIAL PRIMARY KEY,
                task_id             TEXT NOT NULL,
                feature_name        TEXT,
                feature_human_readable TEXT,
                level               TEXT,
                score               DOUBLE PRECISION,
                value               DOUBLE PRECISION,
                observation         TEXT,
                suggestion          TEXT,
                feature_categories  JSONB,
                highlight_joints    JSONB,
                highlight_limbs     JSONB,
                event_name          TEXT,
                event_timestamp     DOUBLE PRECISION,
                event_frame_nr      INT,
                score_ranges        JSONB,
                value_ranges        JSONB,
                created_at          TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(sql_text("""
            CREATE INDEX IF NOT EXISTS ix_technique_features_task
                ON bronze.technique_features (task_id)
        """))

        # ── technique_feature_categories ───────────────────────────
        # Aggregated category scores. From result.feature_categories{}.
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS bronze.technique_feature_categories (
                id              BIGSERIAL PRIMARY KEY,
                task_id         TEXT NOT NULL,
                category_name   TEXT,
                category_score  DOUBLE PRECISION,
                feature_names   JSONB,
                raw_data        JSONB,
                created_at      TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(sql_text("""
            CREATE INDEX IF NOT EXISTS ix_technique_categories_task
                ON bronze.technique_feature_categories (task_id)
        """))

        # ── technique_kinetic_chain ────────────────────────────────
        # Body segment speed data. From result.kinetic_chain.speed_dict{}.
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS bronze.technique_kinetic_chain (
                id              BIGSERIAL PRIMARY KEY,
                task_id         TEXT NOT NULL,
                segment_name    TEXT,
                peak_speed      DOUBLE PRECISION,
                peak_timestamp  DOUBLE PRECISION,
                plot_values     JSONB,
                raw_data        JSONB,
                created_at      TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(sql_text("""
            CREATE INDEX IF NOT EXISTS ix_technique_kinetic_task
                ON bronze.technique_kinetic_chain (task_id)
        """))

        # ── technique_wrist_speed ──────────────────────────────────
        # Wrist speed analysis. From result.wrist_speed{}.
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS bronze.technique_wrist_speed (
                id          BIGSERIAL PRIMARY KEY,
                task_id     TEXT NOT NULL,
                raw_data    JSONB,
                created_at  TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(sql_text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_technique_wrist_task
                ON bronze.technique_wrist_speed (task_id)
        """))

        # ── technique_pose_2d ──────────────────────────────────────
        # 2D pose estimation per frame. From video_entry_2D_json.
        # Stored as JSONB blob per task (too large for row-per-frame).
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS bronze.technique_pose_2d (
                id          BIGSERIAL PRIMARY KEY,
                task_id     TEXT NOT NULL,
                frame_count INT,
                raw_data    JSONB,
                created_at  TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(sql_text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_technique_pose2d_task
                ON bronze.technique_pose_2d (task_id)
        """))

        # ── technique_pose_3d ──────────────────────────────────────
        # 3D pose estimation per frame. From video_entry_3D_json.
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS bronze.technique_pose_3d (
                id          BIGSERIAL PRIMARY KEY,
                task_id     TEXT NOT NULL,
                frame_count INT,
                raw_data    JSONB,
                created_at  TIMESTAMPTZ DEFAULT now()
            )
        """))
        conn.execute(sql_text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_technique_pose3d_task
                ON bronze.technique_pose_3d (task_id)
        """))

    log.info("[technique_bronze_init] technique tables created/verified")
