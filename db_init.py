# db_init.py — Database engine + idempotent schema bootstrap for Bronze and Gold layers.
#
# Exposes: `engine` (shared SQLAlchemy engine), `bronze_init()`, `gold_init()`.
# Called on service boot by every service that touches the database.
#
# Bronze init creates the bronze schema and all tables required by ingest_bronze.py:
#   - raw_result (JSONB snapshot store), session (task registry)
#   - Array tables: player, player_swing, rally, ball_position, ball_bounce, player_position, etc.
#   - Singleton tables: session_confidences, submission_context, team_session, etc.
#   - Typed columns extracted from JSONB for query performance (player_swing scalars, ball_bounce
#     court coords, submission_context metadata, etc.)
#   - Generated columns (STORED) for ball_position x/y/timestamp and ball_bounce court_x/y
#   - Hot-path indexes on (task_id, timestamp) and (task_id, player_id)
#
# Gold init creates gold.vw_client_match_summary — the client-facing view that joins
# bronze.submission_context with silver.point_detail to produce per-match aggregate stats
# (points/games/sets won, aces, double faults, rally length, serve %, winners, scores).
# Player mapping uses first_server to resolve internal player_id → player_a/player_b.
#
# Business rules:
#   - DATABASE_URL is required (falls back to POSTGRES_URL or DB_URL)
#   - Connection string is normalized to postgresql+psycopg:// for psycopg v3
#   - All DDL is idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)
#   - Pool uses pre_ping=True and recycle=1800 for Render's connection lifecycle

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
          ADD COLUMN IF NOT EXISTS end_frame INT,
          -- extracted scalars from ball_hit / ball_hit_location blobs
          ADD COLUMN IF NOT EXISTS ball_hit_s              DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS ball_hit_frame          INTEGER,
          ADD COLUMN IF NOT EXISTS ball_hit_location_x     DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS ball_hit_location_y     DOUBLE PRECISION,
          -- extracted scalars from ball_impact_location blob
          ADD COLUMN IF NOT EXISTS ball_impact_location_x  DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS ball_impact_location_y  DOUBLE PRECISION,
          -- extracted scalars from rally blob [start_s, end_s]
          ADD COLUMN IF NOT EXISTS rally_start_s           DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS rally_end_s             DOUBLE PRECISION;
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
          ADD COLUMN IF NOT EXISTS player_id TEXT,
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
          ADD COLUMN IF NOT EXISTS sport_type TEXT,
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
          ADD COLUMN IF NOT EXISTS ingest_error TEXT,
          ADD COLUMN IF NOT EXISTS last_status_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS last_result_url TEXT,
          ADD COLUMN IF NOT EXISTS ingest_started_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS ingest_finished_at TIMESTAMPTZ;
    """))

    # ---------------- session_confidences (extracted quality scores) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.session_confidences
          ADD COLUMN IF NOT EXISTS tracking_confidence        DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS court_detection_confidence DOUBLE PRECISION;
    """))

    # ---------------- team_session (player identity info) ----------------
    conn.execute(sql_text("""
        ALTER TABLE bronze.team_session
          ADD COLUMN IF NOT EXISTS player_count  INTEGER,
          ADD COLUMN IF NOT EXISTS player_a_id   TEXT,
          ADD COLUMN IF NOT EXISTS player_b_id   TEXT;
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


# -----------------------------------------------------------------------------
# Gold Init (idempotent)
# -----------------------------------------------------------------------------

def gold_init():
    """
    Create the gold schema and client-facing views.
    Safe to call on every boot (idempotent CREATE OR REPLACE).
    """
    with engine.begin() as conn:
        conn.execute(sql_text("CREATE SCHEMA IF NOT EXISTS gold;"))
        # Ensure columns the view depends on exist before CREATE OR REPLACE.
        # `deleted_at` powers the soft-delete filter for the match list.
        conn.execute(sql_text(
            "ALTER TABLE bronze.submission_context "
            "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"
        ))
        conn.execute(sql_text("""
            CREATE OR REPLACE VIEW gold.vw_client_match_summary AS
            WITH first_server_pid AS (
                -- player_a = whoever served the first point of the match.
                -- silver.point_detail.server_id is the player_id who served that point
                -- (assigned in build_silver_v2 pass-3). Pick the earliest point.
                SELECT DISTINCT ON (pd.task_id)
                    pd.task_id,
                    pd.server_id AS player_a_pid
                FROM silver.point_detail pd
                WHERE pd.server_id IS NOT NULL
                  AND pd.exclude_d IS NOT TRUE
                  AND pd.set_number IS NOT NULL
                  AND pd.game_number IS NOT NULL
                  AND pd.point_number IS NOT NULL
                ORDER BY pd.task_id, pd.set_number, pd.game_number, pd.point_number
            ),
            mapped AS (
                SELECT
                    fs.task_id,
                    fs.player_a_pid,
                    -- player_b = the other distinct player_id observed in this match.
                    -- Singles-only view (sport_type filter below) so exactly two pids.
                    (SELECT MIN(pd2.player_id)
                       FROM silver.point_detail pd2
                      WHERE pd2.task_id = fs.task_id
                        AND pd2.player_id IS NOT NULL
                        AND pd2.player_id <> fs.player_a_pid) AS player_b_pid
                FROM first_server_pid fs
            ),
            stats AS (
                SELECT
                    pd.task_id,
                    m.player_a_pid,
                    m.player_b_pid,

                    -- Points
                    MAX(pd.point_number) FILTER (WHERE pd.exclude_d IS NOT TRUE) AS total_points,
                    MAX(pd.game_number)  FILTER (WHERE pd.exclude_d IS NOT TRUE) AS total_games,
                    MAX(pd.set_number)   FILTER (WHERE pd.exclude_d IS NOT TRUE) AS total_sets,

                    COUNT(DISTINCT pd.point_number)
                        FILTER (WHERE pd.point_winner_player_id = m.player_a_pid
                                  AND pd.exclude_d IS NOT TRUE)
                        AS player_a_points_won,
                    COUNT(DISTINCT pd.point_number)
                        FILTER (WHERE pd.point_winner_player_id = m.player_b_pid
                                  AND pd.exclude_d IS NOT TRUE)
                        AS player_b_points_won,

                    -- Games
                    COUNT(DISTINCT pd.game_number)
                        FILTER (WHERE pd.game_winner_player_id = m.player_a_pid
                                  AND pd.exclude_d IS NOT TRUE)
                        AS player_a_games_won,
                    COUNT(DISTINCT pd.game_number)
                        FILTER (WHERE pd.game_winner_player_id = m.player_b_pid
                                  AND pd.exclude_d IS NOT TRUE)
                        AS player_b_games_won,

                    -- Aces & double faults — count DISTINCT points because silver
                    -- stamps ace_d on every shot row of the point (EXISTS semantics)
                    -- and a DF point reclassifies BOTH its serve rows to 'Double'.
                    COUNT(DISTINCT pd.point_number)
                        FILTER (WHERE pd.ace_d = TRUE AND pd.exclude_d IS NOT TRUE) AS total_aces,
                    COUNT(DISTINCT pd.point_number)
                        FILTER (WHERE pd.serve_try_ix_in_point = 'Double' AND pd.exclude_d IS NOT TRUE) AS total_double_faults,

                    -- Rally length
                    AVG(pd.rally_length_point) FILTER (WHERE pd.shot_ix_in_point = 1 AND pd.exclude_d IS NOT TRUE) AS avg_rally_length,
                    MAX(pd.rally_length_point) FILTER (WHERE pd.shot_ix_in_point = 1 AND pd.exclude_d IS NOT TRUE) AS max_rally_length,

                    -- First serve %  (numerator: 1st-serve attempts that went in;
                    -- denominator: distinct service points). Silver writes
                    -- '1st'/'2nd'/'Double'; a DF point has both serve rows = 'Double'.
                    COUNT(*) FILTER (WHERE pd.serve_d = TRUE AND pd.serve_try_ix_in_point = '1st'
                                       AND pd.shot_outcome_d <> 'Error'
                                       AND pd.player_id = m.player_a_pid AND pd.exclude_d IS NOT TRUE)
                        AS player_a_first_serves,
                    COUNT(DISTINCT pd.point_key) FILTER (WHERE pd.serve_d = TRUE
                                       AND pd.player_id = m.player_a_pid AND pd.exclude_d IS NOT TRUE)
                        AS player_a_total_serves,
                    COUNT(*) FILTER (WHERE pd.serve_d = TRUE AND pd.serve_try_ix_in_point = '1st'
                                       AND pd.shot_outcome_d <> 'Error'
                                       AND pd.player_id = m.player_b_pid AND pd.exclude_d IS NOT TRUE)
                        AS player_b_first_serves,
                    COUNT(DISTINCT pd.point_key) FILTER (WHERE pd.serve_d = TRUE
                                       AND pd.player_id = m.player_b_pid AND pd.exclude_d IS NOT TRUE)
                        AS player_b_total_serves,

                    -- Winners
                    COUNT(*) FILTER (WHERE pd.shot_outcome_d = 'Winner'
                                       AND pd.player_id = m.player_a_pid AND pd.exclude_d IS NOT TRUE)
                        AS player_a_winners,
                    COUNT(*) FILTER (WHERE pd.shot_outcome_d = 'Winner'
                                       AND pd.player_id = m.player_b_pid AND pd.exclude_d IS NOT TRUE)
                        AS player_b_winners

                FROM silver.point_detail pd
                JOIN mapped m ON m.task_id = pd.task_id
                GROUP BY pd.task_id, m.player_a_pid, m.player_b_pid
            )
            SELECT
                sc.task_id,
                sc.match_date,
                sc.location,
                sc.player_a_name,
                sc.player_b_name,
                sc.sport_type,
                sc.video_url,
                sc.share_url,
                sc.email,
                sc.last_status,
                sc.created_at,

                COALESCE(s.total_points, 0)   AS total_points,
                COALESCE(s.total_games, 0)    AS total_games,
                COALESCE(s.total_sets, 0)     AS total_sets,
                COALESCE(s.player_a_points_won, 0) AS player_a_points_won,
                COALESCE(s.player_b_points_won, 0) AS player_b_points_won,
                COALESCE(s.player_a_games_won, 0)  AS player_a_games_won,
                COALESCE(s.player_b_games_won, 0)  AS player_b_games_won,
                COALESCE(s.total_aces, 0)     AS total_aces,
                COALESCE(s.total_double_faults, 0) AS total_double_faults,
                ROUND(COALESCE(s.avg_rally_length, 0)::numeric, 1) AS avg_rally_length,
                COALESCE(s.max_rally_length, 0) AS max_rally_length,
                CASE WHEN s.player_a_total_serves > 0
                     THEN ROUND(100.0 * s.player_a_first_serves / s.player_a_total_serves, 1)
                     ELSE 0 END AS player_a_first_serve_pct,
                CASE WHEN s.player_b_total_serves > 0
                     THEN ROUND(100.0 * s.player_b_first_serves / s.player_b_total_serves, 1)
                     ELSE 0 END AS player_b_first_serve_pct,
                COALESCE(s.player_a_winners, 0) AS player_a_winners,
                COALESCE(s.player_b_winners, 0) AS player_b_winners,

                -- Score (from submission_context typed columns)
                sc.player_a_set1_games, sc.player_b_set1_games,
                sc.player_a_set2_games, sc.player_b_set2_games,
                sc.player_a_set3_games, sc.player_b_set3_games

            FROM bronze.submission_context sc
            LEFT JOIN stats s ON s.task_id = sc.task_id::uuid
            WHERE sc.email IS NOT NULL
              -- legacy rows (pre-sport_type column) had NULL — treat as singles,
              -- otherwise the historical match list silently drops them
              AND (sc.sport_type IS NULL OR sc.sport_type = 'tennis_singles')
              AND sc.deleted_at IS NULL;
        """))
