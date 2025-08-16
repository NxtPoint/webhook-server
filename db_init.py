from sqlalchemy import text

DDL_CREATE = [
    # ---------------------- dim_session ----------------------
    """
    CREATE TABLE IF NOT EXISTS dim_session (
        session_id SERIAL PRIMARY KEY,
        session_uid TEXT NOT NULL,
        fps DOUBLE PRECISION,
        session_date TIMESTAMPTZ,
        meta JSONB
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_session_uid ON dim_session (session_uid);",

    # ---------------------- dim_player -----------------------
    """
    CREATE TABLE IF NOT EXISTS dim_player (
        player_id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        sportai_player_uid TEXT NOT NULL,
        full_name TEXT,
        handedness TEXT,
        age INTEGER,
        utr DOUBLE PRECISION,
        covered_distance DOUBLE PRECISION,
        fastest_sprint DOUBLE PRECISION,
        fastest_sprint_timestamp_s DOUBLE PRECISION,
        activity_score DOUBLE PRECISION,
        swing_type_distribution JSONB,
        location_heatmap JSONB
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_player_sess_uid ON dim_player(session_id, sportai_player_uid);",

    # ---------------------- dim_rally ------------------------
    """
    CREATE TABLE IF NOT EXISTS dim_rally (
        rally_id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        rally_number INTEGER NOT NULL,
        start_s DOUBLE PRECISION,
        end_s DOUBLE PRECISION,
        start_ts TIMESTAMPTZ,
        end_ts TIMESTAMPTZ
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_rally_sess_num ON dim_rally(session_id, rally_number);",

    # ---------------------- fact_swing -----------------------
    """
    CREATE TABLE IF NOT EXISTS fact_swing (
        swing_id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        player_id INTEGER REFERENCES dim_player(player_id) ON DELETE SET NULL,
        sportai_swing_uid TEXT,
        start_s DOUBLE PRECISION,
        end_s DOUBLE PRECISION,
        ball_hit_s DOUBLE PRECISION,
        start_ts TIMESTAMPTZ,
        end_ts TIMESTAMPTZ,
        ball_hit_ts TIMESTAMPTZ,
        ball_hit_x DOUBLE PRECISION,
        ball_hit_y DOUBLE PRECISION,
        ball_speed DOUBLE PRECISION,
        ball_player_distance DOUBLE PRECISION,
        is_in_rally BOOLEAN,
        serve BOOLEAN,
        serve_type TEXT,
        meta JSONB
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_swing_sess_suid ON fact_swing(session_id, sportai_swing_uid) WHERE sportai_swing_uid IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_swing_fallback ON fact_swing(session_id, player_id, start_s, end_s) WHERE sportai_swing_uid IS NULL;",

    # ---------------------- fact_bounce ----------------------
    """
    CREATE TABLE IF NOT EXISTS fact_bounce (
        bounce_id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        hitter_player_id INTEGER REFERENCES dim_player(player_id) ON DELETE SET NULL,
        rally_id INTEGER REFERENCES dim_rally(rally_id) ON DELETE SET NULL,
        bounce_s DOUBLE PRECISION,
        bounce_ts TIMESTAMPTZ,
        x DOUBLE PRECISION,
        y DOUBLE PRECISION,
        bounce_type TEXT
    );
    """,

    # ------------------- fact_ball_position ------------------
    """
    CREATE TABLE IF NOT EXISTS fact_ball_position (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        ts_s DOUBLE PRECISION,
        ts TIMESTAMPTZ,
        x DOUBLE PRECISION,
        y DOUBLE PRECISION
    );
    """,

    # ------------------ fact_player_position -----------------
    """
    CREATE TABLE IF NOT EXISTS fact_player_position (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        player_id INTEGER REFERENCES dim_player(player_id) ON DELETE SET NULL,
        ts_s DOUBLE PRECISION,
        ts TIMESTAMPTZ,
        x DOUBLE PRECISION,
        y DOUBLE PRECISION
    );
    """,

    # --------------------- team_session ----------------------
    """
    CREATE TABLE IF NOT EXISTS team_session (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        data JSONB
    );
    """,

    # ------------------------ highlight ----------------------
    """
    CREATE TABLE IF NOT EXISTS highlight (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        data JSONB
    );
    """,

    # --------------------- bounce_heatmap --------------------
    """
    CREATE TABLE IF NOT EXISTS bounce_heatmap (
        session_id INTEGER PRIMARY KEY REFERENCES dim_session(session_id) ON DELETE CASCADE,
        heatmap JSONB
    );
    """,

    # ------------------- session_confidences -----------------
    """
    CREATE TABLE IF NOT EXISTS session_confidences (
        session_id INTEGER PRIMARY KEY REFERENCES dim_session(session_id) ON DELETE CASCADE,
        data JSONB
    );
    """,

    # ------------------------- thumbnail ---------------------
    """
    CREATE TABLE IF NOT EXISTS thumbnail (
        session_id INTEGER PRIMARY KEY REFERENCES dim_session(session_id) ON DELETE CASCADE,
        crops JSONB
    );
    """,

    # ------------------------ raw_result ---------------------
    """
    CREATE TABLE IF NOT EXISTS raw_result (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        payload_json JSONB,
        created_at TIMESTAMPTZ DEFAULT (now() AT TIME ZONE 'utc')
    );
    """
]

DDL_MIGRATE = [
    # dim_rally columns
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS start_s DOUBLE PRECISION;",
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS end_s DOUBLE PRECISION;",
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS start_ts TIMESTAMPTZ;",
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS end_ts TIMESTAMPTZ;",

    # fact_swing columns
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS start_s DOUBLE PRECISION;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS end_s DOUBLE PRECISION;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_s DOUBLE PRECISION;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS start_ts TIMESTAMPTZ;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS end_ts TIMESTAMPTZ;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_ts TIMESTAMPTZ;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_x DOUBLE PRECISION;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_y DOUBLE PRECISION;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_speed DOUBLE PRECISION;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_player_distance DOUBLE PRECISION;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS is_in_rally BOOLEAN;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS serve BOOLEAN;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS serve_type TEXT;",
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS meta JSONB;",

    # fact_bounce columns + FK (with LANGUAGE plpgsql)
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS rally_id INTEGER;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_s DOUBLE PRECISION;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_ts TIMESTAMPTZ;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_type TEXT;",
    """
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fact_bounce_rally_id_fkey'
      ) THEN
        ALTER TABLE fact_bounce
          ADD CONSTRAINT fact_bounce_rally_id_fkey
          FOREIGN KEY (rally_id) REFERENCES dim_rally(rally_id) ON DELETE SET NULL;
      END IF;
    END $$ LANGUAGE plpgsql;
    """,

    # fact_ball_position columns
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS ts_s DOUBLE PRECISION;",
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ;",
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION;",
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION;",

    # fact_player_position columns
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS ts_s DOUBLE PRECISION;",
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ;",
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION;",
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION;",

    # ensure unique indexes
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_session_uid ON dim_session (session_uid);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_player_sess_uid ON dim_player(session_id, sportai_player_uid);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_rally_sess_num ON dim_rally(session_id, rally_number);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_swing_sess_suid ON fact_swing(session_id, sportai_swing_uid) WHERE sportai_swing_uid IS NOT NULL;",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_swing_fallback ON fact_swing(session_id, player_id, start_s, end_s) WHERE sportai_swing_uid IS NULL;"
]

def run_init(engine):
    with engine.begin() as conn:
        for stmt in DDL_CREATE:
            conn.execute(text(stmt))
        for stmt in DDL_MIGRATE:
            conn.execute(text(stmt))
