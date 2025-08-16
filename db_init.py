# db_init.py
import os
from sqlalchemy import create_engine, text

def _engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return create_engine(url, pool_pre_ping=True)

DDL_CREATE = [
# ------------------ core dims ------------------
"""
CREATE TABLE IF NOT EXISTS dim_session (
  session_id           SERIAL PRIMARY KEY,
  session_uid          TEXT UNIQUE,
  source_file          TEXT,
  session_date         TIMESTAMPTZ,
  fps                  REAL,
  court_surface        TEXT,
  venue                TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS dim_player (
  player_id                  BIGSERIAL PRIMARY KEY,
  session_id                 INT,
  sportai_player_uid         TEXT NOT NULL,
  full_name                  TEXT,
  handedness                 TEXT,
  age                        REAL,
  utr                        REAL,
  covered_distance           REAL,
  fastest_sprint             REAL,
  fastest_sprint_timestamp_s REAL,
  activity_score             REAL,
  swing_type_distribution    JSONB,
  location_heatmap           JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS dim_rally (
  rally_id                 BIGSERIAL PRIMARY KEY,
  session_id               INT,
  rally_number             INT NOT NULL,
  start_ts                 TIMESTAMPTZ,
  end_ts                   TIMESTAMPTZ,
  point_winner_player_id   INT,
  length_shots             INT
);
""",

# ------------------ facts ------------------
"""
CREATE TABLE IF NOT EXISTS fact_swing (
  swing_id               BIGSERIAL PRIMARY KEY,
  session_id             INT,
  rally_id               INT,
  player_id              INT,
  start_ts               TIMESTAMPTZ,
  end_ts                 TIMESTAMPTZ,
  ball_hit_ts            TIMESTAMPTZ,
  start_s                REAL,
  end_s                  REAL,
  ball_hit_s             REAL,
  start_frame            INT,
  end_frame              INT,
  ball_hit_frame         INT,
  swing_type             TEXT,
  serve                  BOOLEAN,
  volley                 BOOLEAN,
  is_in_rally            BOOLEAN,
  confidence             REAL,
  confidence_swing_type  REAL,
  confidence_volley      REAL,
  rally_start_s          REAL,
  rally_end_s            REAL,
  ball_hit_x             REAL,
  ball_hit_y             REAL,
  ball_player_distance   REAL,
  ball_speed             REAL,
  ball_impact_location_x REAL,
  ball_impact_location_y REAL,
  ball_impact_type       TEXT,
  intercepting_player_uid TEXT,
  ball_trajectory        JSONB,
  annotations_json       JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS fact_bounce (
  bounce_id            BIGSERIAL PRIMARY KEY,
  session_id           INT,
  rally_id             INT,
  timestamp_s          REAL,
  bounce_ts            TIMESTAMPTZ,
  bounce_x             REAL,
  bounce_y             REAL,
  hitter_player_id     INT,
  bounce_type          TEXT
);
""",
"""
CREATE TABLE IF NOT EXISTS fact_ball_position (
  id             BIGSERIAL PRIMARY KEY,
  session_id     INT,
  timestamp_s    REAL,
  ts             TIMESTAMPTZ,
  x_image        REAL,
  y_image        REAL
);
""",
"""
CREATE TABLE IF NOT EXISTS fact_player_position (
  id             BIGSERIAL PRIMARY KEY,
  session_id     INT,
  player_id      INT,
  timestamp_s    REAL,
  ts             TIMESTAMPTZ,
  img_x          REAL,
  img_y          REAL,
  court_x        REAL,
  court_y        REAL
);
""",

# ------------------ other top-levels ------------------
"""
CREATE TABLE IF NOT EXISTS team_session (
  id            BIGSERIAL PRIMARY KEY,
  session_id    INT,
  start_s       REAL,
  end_s         REAL,
  front_team    INT[],
  back_team     INT[]
);
""",
"""
CREATE TABLE IF NOT EXISTS highlight (
  id                  BIGSERIAL PRIMARY KEY,
  session_id          INT,
  type                TEXT,
  start_s             REAL,
  end_s               REAL,
  duration            REAL,
  swing_count         INT,
  ball_speed          REAL,
  ball_distance       REAL,
  players_distance    REAL,
  players_speed       REAL,
  dynamic_score       REAL,
  players_json        JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS bounce_heatmap (
  session_id   INT PRIMARY KEY,
  heatmap      JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS session_confidences (
  session_id           INT PRIMARY KEY,
  pose                 REAL,
  swing                REAL,
  swing_ball           REAL,
  ball                 REAL,
  final                REAL,
  pose_confidences     JSONB,
  swing_confidences    JSONB,
  ball_confidences     JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS thumbnail (
  id            BIGSERIAL PRIMARY KEY,
  session_id    INT,
  player_uid    TEXT,
  frame_nr      INT,
  timestamp_s   REAL,
  score         REAL,
  bbox          JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS raw_result (
  session_id   INT PRIMARY KEY,
  payload      JSONB
);
"""
]

MIGRATION = [
# ---------- Add any missing columns (covers older installs) ----------
# dim_player
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS session_id INT",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS handedness TEXT",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS age REAL",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS utr REAL",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS covered_distance REAL",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS fastest_sprint REAL",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS fastest_sprint_timestamp_s REAL",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS activity_score REAL",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS swing_type_distribution JSONB",
"ALTER TABLE dim_player ADD COLUMN IF NOT EXISTS location_heatmap JSONB",

# dim_rally
"ALTER TABLE dim_rally ADD COLUMN IF NOT EXISTS point_winner_player_id INT",

# fact_swing – ensure *all* v2 columns exist
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS session_id INT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS rally_id INT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS player_id INT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS start_ts TIMESTAMPTZ",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS end_ts TIMESTAMPTZ",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_ts TIMESTAMPTZ",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS start_s REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS end_s REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_s REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS start_frame INT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS end_frame INT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_frame INT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS swing_type TEXT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS serve BOOLEAN",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS volley BOOLEAN",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS is_in_rally BOOLEAN",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS confidence REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS confidence_swing_type REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS confidence_volley REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS rally_start_s REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS rally_end_s REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_x REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_hit_y REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_player_distance REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_speed REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_impact_location_x REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_impact_location_y REAL",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_impact_type TEXT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS intercepting_player_uid TEXT",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS ball_trajectory JSONB",
"ALTER TABLE fact_swing ADD COLUMN IF NOT EXISTS annotations_json JSONB",

# fact_bounce – ensure columns exist
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS session_id INT",
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS rally_id INT",
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS timestamp_s REAL",
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_ts TIMESTAMPTZ",
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_x REAL",
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_y REAL",
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS hitter_player_id INT",
"ALTER TABLE fact_bounce ADD COLUMN IF NOT EXISTS bounce_type TEXT",

# fact_player_position
"ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS session_id INT",
"ALTER TABLE fact_player_position ADD COLUMN IF NOT EXISTS player_id INT",

# ---------- Backfill from legacy columns (if present) ----------
# If old 'is_serve' exists, copy values into new 'serve'
"""
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='fact_swing' AND column_name='is_serve'
  ) THEN
    UPDATE fact_swing SET serve = COALESCE(serve, is_serve);
  END IF;
END$$;
""",

# ---------- Clean up old uniqueness that might conflict ----------
"ALTER TABLE dim_player DROP CONSTRAINT IF EXISTS dim_player_sportai_player_uid_key",
"DROP INDEX IF EXISTS dim_player_sportai_player_uid_key",

# ---------- Constraints (FKs) ----------
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='dim_player_session_fk') THEN
    ALTER TABLE dim_player
      ADD CONSTRAINT dim_player_session_fk
      FOREIGN KEY (session_id) REFERENCES dim_session(session_id) ON DELETE CASCADE;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='dim_rally_session_fk') THEN
    ALTER TABLE dim_rally
      ADD CONSTRAINT dim_rally_session_fk
      FOREIGN KEY (session_id) REFERENCES dim_session(session_id) ON DELETE CASCADE;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='dim_rally_winner_fk') THEN
    ALTER TABLE dim_rally
      ADD CONSTRAINT dim_rally_winner_fk
      FOREIGN KEY (point_winner_player_id) REFERENCES dim_player(player_id);
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_swing_session_fk') THEN
    ALTER TABLE fact_swing
      ADD CONSTRAINT fact_swing_session_fk
      FOREIGN KEY (session_id) REFERENCES dim_session(session_id) ON DELETE CASCADE;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_swing_rally_fk') THEN
    ALTER TABLE fact_swing
      ADD CONSTRAINT fact_swing_rally_fk
      FOREIGN KEY (rally_id) REFERENCES dim_rally(rally_id) ON DELETE SET NULL;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_swing_player_fk') THEN
    ALTER TABLE fact_swing
      ADD CONSTRAINT fact_swing_player_fk
      FOREIGN KEY (player_id) REFERENCES dim_player(player_id) ON DELETE SET NULL;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_bounce_session_fk') THEN
    ALTER TABLE fact_bounce
      ADD CONSTRAINT fact_bounce_session_fk
      FOREIGN KEY (session_id) REFERENCES dim_session(session_id) ON DELETE CASCADE;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_bounce_rally_fk') THEN
    ALTER TABLE fact_bounce
      ADD CONSTRAINT fact_bounce_rally_fk
      FOREIGN KEY (rally_id) REFERENCES dim_rally(rally_id) ON DELETE SET NULL;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_bounce_player_fk') THEN
    ALTER TABLE fact_bounce
      ADD CONSTRAINT fact_bounce_player_fk
      FOREIGN KEY (hitter_player_id) REFERENCES dim_player(player_id) ON DELETE SET NULL;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_player_pos_session_fk') THEN
    ALTER TABLE fact_player_position
      ADD CONSTRAINT fact_player_pos_session_fk
      FOREIGN KEY (session_id) REFERENCES dim_session(session_id) ON DELETE CASCADE;
  END IF;
END$$;
""",
"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fact_player_pos_player_fk') THEN
    ALTER TABLE fact_player_position
      ADD CONSTRAINT fact_player_pos_player_fk
      FOREIGN KEY (player_id) REFERENCES dim_player(player_id) ON DELETE CASCADE;
  END IF;
END$$;
""",

# ---------- Composite unique + performance indexes ----------
"CREATE UNIQUE INDEX IF NOT EXISTS uq_dim_player_sess_uid ON dim_player(session_id, sportai_player_uid)",
"CREATE INDEX IF NOT EXISTS idx_dim_player_session ON dim_player(session_id)",
"CREATE INDEX IF NOT EXISTS idx_dim_rally_session ON dim_rally(session_id)",
"CREATE INDEX IF NOT EXISTS idx_fact_bounce_session ON fact_bounce(session_id)",
"CREATE INDEX IF NOT EXISTS idx_fact_bounce_rally_ts ON fact_bounce(rally_id, bounce_ts)",
"CREATE INDEX IF NOT EXISTS idx_fact_swing_session ON fact_swing(session_id)",
"CREATE INDEX IF NOT EXISTS idx_fact_swing_rally_ts ON fact_swing(rally_id, ball_hit_ts)",
"CREATE INDEX IF NOT EXISTS idx_player_pos_session ON fact_player_position(session_id, player_id)",
"CREATE INDEX IF NOT EXISTS idx_ball_pos_session ON fact_ball_position(session_id, ts)"
]

def init_db():
    eng = _engine()
    with eng.begin() as conn:
        # 1) Create tables if missing
        for stmt in DDL_CREATE:
            conn.execute(text(stmt))
        # 2) Run migration / ensure columns, constraints, indexes
        for stmt in MIGRATION:
            conn.execute(text(stmt))
    return "schema v2 ready (auto-migrated)"
