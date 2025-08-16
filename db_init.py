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

    # ---------------------- fact_bounce ----------------------
    """
    CREATE TABLE IF NOT EXISTS fact_bounce (
        bounce_id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES dim_session(session_id) ON DELETE CASCADE,
        hitter_player_id INTEGER REFERENCES dim_player(player_id) ON DELETE SET NULL,
        rally_id INTEGER, -- FK ensured later
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

# add/repair columns (safe on existing DBs)
DDL_MIGRATE = [
    # dim_rally
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS start_s DOUBLE PRECISION;",
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS end_s DOUBLE PRECISION;",
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS start_ts TIMESTAMPTZ;",
    "ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS end_ts TIMESTAMPTZ;",

    # fact_swing (make sure the column exists BEFORE index creation)
    "ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS sportai_swing_uid TEXT;",
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

    # fact_bounce
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS rally_id INTEGER;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_s DOUBLE PRECISION;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_ts TIMESTAMPTZ;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION;",
    "ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_type TEXT;",

    # fact_ball_position
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS ts_s DOUBLE PRECISION;",
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ;",
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION;",
    "ALTER TABLE fact_ball_position ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION;",

    # fact_player_position
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS ts_s DOUBLE PRECISION;",
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ;",
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS x DOUBLE PRECISION;",
    "ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS y DOUBLE PRECISION;",

    # other unique indexes (safe)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_session_uid ON dim_session (session_uid);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_player_sess_uid ON dim_player(session_id, sportai_player_uid);",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_rally_sess_num ON dim_rally(session_id, rally_number);",
]

def _column_exists(conn, table_name: str, column_name: str) -> bool:
    sql = text("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t AND column_name=:c
        LIMIT 1
    """)
    return conn.execute(sql, {"t": table_name, "c": column_name}).first() is not None

def _index_exists(conn, index_name: str) -> bool:
    sql = text("""
        SELECT 1
        FROM pg_class
        WHERE relkind='i' AND relname=:n
        LIMIT 1
    """)
    return conn.execute(sql, {"n": index_name}).first() is not None

def _ensure_fact_bounce_fk(conn):
    # Create FK only if no FK from fact_bounce -> dim_rally exists (any name)
    check = conn.execute(text("""
        SELECT COUNT(*) FROM pg_constraint c
        JOIN pg_class r ON r.oid = c.conrelid
        JOIN pg_namespace nr ON nr.oid = r.relnamespace
        JOIN pg_class f ON f.oid = c.confrelid
        JOIN pg_namespace nf ON nf.oid = f.relnamespace
        WHERE c.contype = 'f'
          AND nr.nspname = 'public' AND r.relname = 'fact_bounce'
          AND nf.nspname = 'public' AND f.relname = 'dim_rally';
    """)).scalar_one()
    if int(check or 0) == 0:
        conn.execute(text("""
            ALTER TABLE public.fact_bounce
            ADD CONSTRAINT fact_bounce_rally_id_fkey
            FOREIGN KEY (rally_id) REFERENCES public.dim_rally(rally_id) ON DELETE SET NULL;
        """))

def _ensure_fact_swing_indexes(conn):
    # Only create indexes after columns exist
    if _column_exists(conn, "fact_swing", "session_id") and _column_exists(conn, "fact_swing", "sportai_swing_uid"):
        if not _index_exists(conn, "uq_fact_swing_sess_suid"):
            conn.execute(text("""
                CREATE UNIQUE INDEX uq_fact_swing_sess_suid
                ON fact_swing(session_id, sportai_swing_uid)
                WHERE sportai_swing_uid IS NOT NULL;
            """))
    if all(_column_exists(conn, "fact_swing", c) for c in ("session_id","player_id","start_s","end_s")):
        if not _index_exists(conn, "uq_fact_swing_fallback"):
            conn.execute(text("""
                CREATE UNIQUE INDEX uq_fact_swing_fallback
                ON fact_swing(session_id, player_id, start_s, end_s)
                WHERE sportai_swing_uid IS NULL;
            """))

def run_init(engine):
    with engine.begin() as conn:
        # Create base tables
        for stmt in DDL_CREATE:
            conn.execute(text(stmt))
        # Run additive migrations (columns, safe indexes)
        for stmt in DDL_MIGRATE:
            conn.execute(text(stmt))
        # Ensure FK & the two swing indexes in the correct order
        _ensure_fact_bounce_fk(conn)
        _ensure_fact_swing_indexes(conn)
