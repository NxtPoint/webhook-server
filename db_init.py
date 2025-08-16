# db_init.py
import os
from sqlalchemy import create_engine, text

def _engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return create_engine(url, pool_pre_ping=True)

DDL = [
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
  player_id              BIGSERIAL PRIMARY KEY,
  session_id             INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  sportai_player_uid     TEXT NOT NULL,
  full_name              TEXT,
  handedness             TEXT,
  age                    REAL,
  utr                    REAL,
  covered_distance       REAL,
  fastest_sprint         REAL,
  fastest_sprint_timestamp_s REAL,
  activity_score         REAL,
  swing_type_distribution JSONB,
  location_heatmap       JSONB,
  UNIQUE (session_id, sportai_player_uid)
);
""",
"""
CREATE TABLE IF NOT EXISTS dim_rally (
  rally_id                 BIGSERIAL PRIMARY KEY,
  session_id               INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  rally_number             INT NOT NULL,
  start_ts                 TIMESTAMPTZ,
  end_ts                   TIMESTAMPTZ,
  point_winner_player_id   INT REFERENCES dim_player(player_id),
  length_shots             INT,
  UNIQUE (session_id, rally_number)
);
""",

# ------------------ facts ------------------
"""
CREATE TABLE IF NOT EXISTS fact_swing (
  swing_id               BIGSERIAL PRIMARY KEY,
  session_id             INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  rally_id               INT REFERENCES dim_rally(rally_id) ON DELETE SET NULL,
  player_id              INT REFERENCES dim_player(player_id) ON DELETE SET NULL,
  -- absolute timestamps (derived)
  start_ts               TIMESTAMPTZ,
  end_ts                 TIMESTAMPTZ,
  ball_hit_ts            TIMESTAMPTZ,
  -- seconds/frame as in API
  start_s                REAL,
  end_s                  REAL,
  ball_hit_s             REAL,
  start_frame            INT,
  end_frame              INT,
  ball_hit_frame         INT,
  -- attributes
  swing_type             TEXT,
  serve                  BOOLEAN,
  volley                 BOOLEAN,
  is_in_rally            BOOLEAN,
  -- confidences
  confidence             REAL,
  confidence_swing_type  REAL,
  confidence_volley      REAL,
  -- rally window on the swing (seconds)
  rally_start_s          REAL,
  rally_end_s            REAL,
  -- at-impact info
  ball_hit_x             REAL,
  ball_hit_y             REAL,
  ball_player_distance   REAL,
  ball_speed             REAL,
  -- future fields (nullable)
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
  session_id           INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  rally_id             INT REFERENCES dim_rally(rally_id) ON DELETE SET NULL,
  timestamp_s          REAL,       -- as delivered by API
  bounce_ts            TIMESTAMPTZ, -- absolute (derived from seconds)
  bounce_x             REAL,
  bounce_y             REAL,
  hitter_player_id     INT REFERENCES dim_player(player_id) ON DELETE SET NULL,
  bounce_type          TEXT        -- "floor" | "swing"
);
""",
"""
CREATE TABLE IF NOT EXISTS fact_ball_position (
  id             BIGSERIAL PRIMARY KEY,
  session_id     INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  timestamp_s    REAL,
  ts             TIMESTAMPTZ,
  x_image        REAL,
  y_image        REAL
);
""",
"""
CREATE TABLE IF NOT EXISTS fact_player_position (
  id             BIGSERIAL PRIMARY KEY,
  session_id     INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  player_id      INT REFERENCES dim_player(player_id) ON DELETE CASCADE,
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
  session_id    INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  start_s       REAL,
  end_s         REAL,
  front_team    INT[],
  back_team     INT[]
);
""",
"""
CREATE TABLE IF NOT EXISTS highlight (
  id                  BIGSERIAL PRIMARY KEY,
  session_id          INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
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
  session_id   INT PRIMARY KEY REFERENCES dim_session(session_id) ON DELETE CASCADE,
  heatmap      JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS session_confidences (
  session_id           INT PRIMARY KEY REFERENCES dim_session(session_id) ON DELETE CASCADE,
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
  session_id    INT REFERENCES dim_session(session_id) ON DELETE CASCADE,
  player_uid    TEXT,
  frame_nr      INT,
  timestamp_s   REAL,
  score         REAL,
  bbox          JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS raw_result (
  session_id   INT PRIMARY KEY REFERENCES dim_session(session_id) ON DELETE CASCADE,
  payload      JSONB
);
""",

# ------------------ indexes for speed ------------------
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
        for stmt in DDL:
            conn.execute(text(stmt))
    return "schema v2 ready"
