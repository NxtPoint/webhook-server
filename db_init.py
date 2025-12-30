# db_init.py — Bronze-only initialization (clean, Nov 2025)
# - Exposes: `engine`, `bronze_init()`
# - Creates only the Bronze schema + tables + columns + indexes required by ingest_bronze.py
# - No Silver, no views, no other schemas.

import os
from sqlalchemy import create_engine, text as sql_text

# -----------------------------------------------------------------------------
# Engine
# -----------------------------------------------------------------------------
# Use DATABASE_URL (Render/Heroku-style, e.g. postgres:// or postgresql+psycopg2://)
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("DB_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL (or POSTGRES_URL / DB_URL) env var is required.")

# Normalize scheme + force psycopg v3 driver
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,      # keep connections fresh on Render
    future=True,
)

# -----------------------------------------------------------------------------
# Bronze Init (idempotent)
# -----------------------------------------------------------------------------
BRONZE_ARRAY_TABLES = [
    "player", "player_swing", "rally",
    "ball_position", "ball_bounce",
    "unmatched_field", "debug_event",
    "player_position",
]

BRONZE_SINGLETONS = [
    "session_confidences", "thumbnail", "highlight",
    "team_session", "bounce_heatmap", "submission_context",
]

def _create_core(conn):
    # Schema
    conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS bronze;"))

    # RAW snapshot store
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.raw_result (
            id BIGSERIAL PRIMARY KEY,
            task_id TEXT NOT NULL,
            payload_json JSONB,
            payload_gzip BYTEA,
            payload_sha256 TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    conn.execute(sql_text("CREATE INDEX IF NOT EXISTS ix_bronze_raw_result_task ON bronze.raw_result(task_id);"))

    # Session registry
    conn.execute(sql_text("""
        CREATE TABLE IF NOT EXISTS bronze.session (
            task_id TEXT PRIMARY KEY,
            session_uid TEXT,
            meta JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))

    # Array tables: (id, task_id, data, created_at)
    for t in BRONZE_ARRAY_TABLES:
        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS bronze.{t} (
                id BIGSERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                data JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))
        conn.execute(sql_text(f"CREATE INDEX IF NOT EXISTS ix_bronze_{t}_task ON bronze.{t}(task_id);"))

    # Singletons: (task_id PK, data, created_at)
    for t in BRONZE_SINGLETONS:
        conn.execute(sql_text(f"""
            CREATE TABLE IF NOT EXISTS bronze.{t} (
                task_id TEXT PRIMARY KEY,
                data JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))

def _add_typed_columns(conn):
    # ---------------- player (real columns) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.player
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS activity_score DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS covered_distance DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS fastest_sprint DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS fastest_sprint_timestamp DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS location_heatmap JSONB,
          ADD COLUMN IF NOT EXISTS swing_count INT,
          ADD COLUMN IF NOT EXISTS swing_type_distribution JSONB;
    """))

    # ---------------- player_swing (real columns) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.player_swing
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS valid BOOLEAN,
          ADD COLUMN IF NOT EXISTS serve BOOLEAN,
          ADD COLUMN IF NOT EXISTS swing_type TEXT,
          ADD COLUMN IF NOT EXISTS volley BOOLEAN,
          ADD COLUMN IF NOT EXISTS is_in_rally BOOLEAN,
          ADD COLUMN IF NOT EXISTS start JSONB,
          ADD COLUMN IF NOT EXISTS "end" JSONB,
          ADD COLUMN IF NOT EXISTS ball_hit JSONB,
          ADD COLUMN IF NOT EXISTS ball_hit_location JSONB,
          ADD COLUMN IF NOT EXISTS ball_player_distance DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS ball_speed DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS ball_impact_location JSONB,
          ADD COLUMN IF NOT EXISTS ball_impact_type TEXT,
          ADD COLUMN IF NOT EXISTS ball_trajectory JSONB,
          ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS confidence_swing_type DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS confidence_volley DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS intercepting_player_id INT,
          ADD COLUMN IF NOT EXISTS rally JSONB,
          ADD COLUMN IF NOT EXISTS annotations JSONB,
          ADD COLUMN IF NOT EXISTS start_ts DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS start_frame INT,
          ADD COLUMN IF NOT EXISTS end_ts DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS end_frame INT;
    """))

    # ---------------- rally (real columns; data may be {id,start,end} or {value:{...}}) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.rally
          ADD COLUMN IF NOT EXISTS rally_id TEXT,
          ADD COLUMN IF NOT EXISTS start_ts DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS end_ts   DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS len_s    DOUBLE PRECISION;
    """))

    # ---------------- ball_position (GENERATED ALWAYS from JSON) ----------------
    # Keep JSON keys 'X','Y','timestamp' inside data; scalars materialize automatically.
    conn.execute(sql_text("""
        ALTER TABLE bronze.ball_position
          ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION
            GENERATED ALWAYS AS (NULLIF(data->>'X','')::double precision) STORED,
          ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION
            GENERATED ALWAYS AS (NULLIF(data->>'Y','')::double precision) STORED,
          ADD COLUMN IF NOT EXISTS "timestamp" DOUBLE PRECISION
            GENERATED ALWAYS AS (NULLIF(data->>'timestamp','')::double precision) STORED;
    """))

    # ---------------- player_position (real columns; we insert numbers directly) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.player_position
          ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_x DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_y DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS "timestamp" DOUBLE PRECISION;
    """))

    # ---------------- ball_bounce (real columns + derived scalars from arrays) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.ball_bounce
          ADD COLUMN IF NOT EXISTS type TEXT,
          ADD COLUMN IF NOT EXISTS frame_nr INT,
          ADD COLUMN IF NOT EXISTS player_id INT,
          ADD COLUMN IF NOT EXISTS "timestamp" DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_pos JSONB,
          ADD COLUMN IF NOT EXISTS image_pos JSONB;
    """))
    # Derived scalars from array JSON:
    conn.execute(sql_text("""
        ALTER TABLE bronze.ball_bounce
          ADD COLUMN IF NOT EXISTS court_x DOUBLE PRECISION
            GENERATED ALWAYS AS ((court_pos->>0)::double precision) STORED,
          ADD COLUMN IF NOT EXISTS court_y DOUBLE PRECISION
            GENERATED ALWAYS AS ((court_pos->>1)::double precision) STORED,
          ADD COLUMN IF NOT EXISTS image_x DOUBLE PRECISION
            GENERATED ALWAYS AS ((image_pos->>0)::double precision) STORED,
          ADD COLUMN IF NOT EXISTS image_y DOUBLE PRECISION
            GENERATED ALWAYS AS ((image_pos->>1)::double precision) STORED;
    """))

    # ---------------- submission_context (flatten common + runtime/status fields) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.submission_context
          ADD COLUMN IF NOT EXISTS email TEXT,
          ADD COLUMN IF NOT EXISTS location TEXT,
          ADD COLUMN IF NOT EXISTS video_url TEXT,
          ADD COLUMN IF NOT EXISTS share_url TEXT,
          ADD COLUMN IF NOT EXISTS match_date DATE,
          ADD COLUMN IF NOT EXISTS start_time TEXT,
          ADD COLUMN IF NOT EXISTS player_a_name TEXT,
          ADD COLUMN IF NOT EXISTS player_b_name TEXT,
          ADD COLUMN IF NOT EXISTS player_a_utr TEXT,
          ADD COLUMN IF NOT EXISTS player_b_utr TEXT,
          ADD COLUMN IF NOT EXISTS customer_name TEXT,
          ADD COLUMN IF NOT EXISTS last_status TEXT,
          ADD COLUMN IF NOT EXISTS ingest_error JSONB,
          ADD COLUMN IF NOT EXISTS last_status_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS last_result_url TEXT,
          ADD COLUMN IF NOT EXISTS ingest_started_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ;
    """))

def _add_indexes(conn):
    # Hot-path indexes (no-ops if they already exist)
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ball_position_task_ts
            ON bronze.ball_position (task_id, "timestamp");
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_player_position_task_ts
            ON bronze.player_position (task_id, "timestamp");
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_player_swing_task_pid
            ON bronze.player_swing (task_id, player_id);
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_ball_bounce_task_ts
            ON bronze.ball_bounce ("timestamp");
    """))
    conn.execute(sql_text("""
        CREATE INDEX IF NOT EXISTS ix_rally_task_start
            ON bronze.rally (task_id, start_ts);
    """))

def bronze_init():
    """
    Public entrypoint: create ONLY the Bronze schema & objects required by ingest_bronze.py.
    Safe to call on every boot (idempotent).
    """
    with engine.begin() as conn:
        _create_core(conn)
        _add_typed_columns(conn)
        _add_indexes(conn)
