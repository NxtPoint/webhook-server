# db_init.py
import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dim_session (
    session_id        BIGSERIAL PRIMARY KEY,
    source_file       TEXT,
    session_uid       TEXT UNIQUE,
    session_date      TIMESTAMPTZ,
    court_surface     TEXT,
    venue             TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dim_player (
    player_id          BIGSERIAL PRIMARY KEY,
    sportai_player_uid TEXT UNIQUE,
    full_name          TEXT,
    handedness         TEXT,
    age                INT,
    utr                NUMERIC(4,2),
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dim_rally (
    rally_id               BIGSERIAL PRIMARY KEY,
    session_id             BIGINT REFERENCES dim_session(session_id) ON DELETE CASCADE,
    rally_number           INT,
    start_ts               TIMESTAMPTZ,
    end_ts                 TIMESTAMPTZ,
    point_winner_player_id BIGINT REFERENCES dim_player(player_id),
    length_shots           INT,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, rally_number)
);

CREATE TABLE IF NOT EXISTS fact_swing (
    swing_id          BIGSERIAL PRIMARY KEY,
    session_id        BIGINT REFERENCES dim_session(session_id) ON DELETE CASCADE,
    rally_id          BIGINT REFERENCES dim_rally(rally_id),
    player_id         BIGINT REFERENCES dim_player(player_id),

    swing_start_ts    TIMESTAMPTZ,
    swing_end_ts      TIMESTAMPTZ,
    ball_hit_ts       TIMESTAMPTZ,  -- Power BI Incremental Refresh

    swing_type        TEXT,
    is_serve          BOOLEAN,
    is_return         BOOLEAN,
    is_in_rally       BOOLEAN,
    valid             BOOLEAN,

    serve_number      SMALLINT,
    serve_location    SMALLINT,
    return_depth_box  TEXT,

    ball_x            DOUBLE PRECISION,
    ball_y            DOUBLE PRECISION,
    ball_speed        DOUBLE PRECISION,
    ball_player_dist  DOUBLE PRECISION,

    annotations_json  JSONB,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS fact_bounce (
    bounce_id         BIGSERIAL PRIMARY KEY,
    session_id        BIGINT REFERENCES dim_session(session_id) ON DELETE CASCADE,
    rally_id          BIGINT REFERENCES dim_rally(rally_id),
    bounce_ts         TIMESTAMPTZ,
    bounce_x          DOUBLE PRECISION,
    bounce_y          DOUBLE PRECISION,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_swing_session       ON fact_swing(session_id);
CREATE INDEX IF NOT EXISTS idx_fact_swing_ball_hit_ts   ON fact_swing(ball_hit_ts);
CREATE INDEX IF NOT EXISTS idx_fact_swing_player        ON fact_swing(player_id);
CREATE INDEX IF NOT EXISTS idx_dim_rally_session_rally  ON dim_rally(session_id, rally_number);
CREATE INDEX IF NOT EXISTS idx_fact_bounce_session      ON fact_bounce(session_id);
"""

def init_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text(SCHEMA_SQL))
    return "OK"

if __name__ == "__main__":
    print(init_db())
