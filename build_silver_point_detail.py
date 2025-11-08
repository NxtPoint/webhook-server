# build_silver_point_detail.py
# Phase 1: Create & populate silver.point_detail (BASE FIELDS ONLY)
# - Python is the source of truth; all DDL + DML live here.
# - Reads directly from Bronze (dim_session, fact_swing, fact_bounce, dim_rally if present).
# - No derived metrics yet (serve buckets, scoring, outcomes, etc.) — that's Phase 2.
# - Safe to rerun; supports replace and per-session builds.

from typing import Optional, Sequence
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine  # uses your existing engine (Bronze-only module)

SILVER_SCHEMA = "silver"
TABLE = "point_detail"
PK = "(session_id, swing_id)"

DDL_CREATE_SCHEMA = f"""
CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};
"""

DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  -- Identity
  session_id              INTEGER            NOT NULL,
  session_uid             TEXT,
  swing_id                BIGINT             NOT NULL,
  rally_index             INTEGER,

  -- Who & when (timestamps)
  player_id               TEXT,
  start_ts                TIMESTAMPTZ,
  end_ts                  TIMESTAMPTZ,
  ball_hit_ts             TIMESTAMPTZ,

  -- Who & when (seconds)
  start_s                 DOUBLE PRECISION,
  end_s                   DOUBLE PRECISION,
  ball_hit_s              DOUBLE PRECISION,

  -- Ball at hit (for later serve/location logic)
  ball_hit_x              DOUBLE PRECISION,
  ball_hit_y              DOUBLE PRECISION,
  ball_speed              DOUBLE PRECISION,
  swing_type_raw          TEXT,

  -- Primary bounce resolved within a bounded window after the swing
  bounce_id               BIGINT,
  bounce_ts               TIMESTAMPTZ,
  bounce_s                DOUBLE PRECISION,
  bounce_type_raw         TEXT,
  bounce_x_center_m       DOUBLE PRECISION,
  bounce_y_center_m       DOUBLE PRECISION,
  bounce_y_norm_m         DOUBLE PRECISION,

  -- Base flags/placeholders we may fill/overwrite in Phase 2
  is_valid                BOOLEAN,
  server_id               TEXT,
  serving_side_raw        TEXT,

  -- Sequencing placeholders (computed in Phase 2)
  shot_ix                 INTEGER,
  point_number            INTEGER,
  game_number             INTEGER,
  point_in_game           INTEGER,

  -- Optional provenance/debug (can be left NULL in Phase 1)
  src_swing_meta          JSONB,
  src_bounce_meta         JSONB,

  -- Audit
  created_at              TIMESTAMPTZ DEFAULT NOW(),

  -- PK (idempotent upsert target)
  PRIMARY KEY (session_id, swing_id)
);
"""

DDL_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_session    ON {SILVER_SCHEMA}.{TABLE} (session_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_player     ON {SILVER_SCHEMA}.{TABLE} (session_id, player_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_bounce     ON {SILVER_SCHEMA}.{TABLE} (session_id, bounce_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_time       ON {SILVER_SCHEMA}.{TABLE} (session_id, ball_hit_ts);",
]

# Core INSERT ... SELECT.
# Notes:
# - We use a LATERAL join to pick the first bounce after the swing (guarded by a small epsilon and a 2.5s cap).
# - This is just a *staging* association to ensure the base fields needed for serve/location later are present.
# - Phase 2 may recompute/override this association with more nuanced logic.
INSERT_SELECT_BASE = f"""
INSERT INTO {SILVER_SCHEMA}.{TABLE} (
  session_id, session_uid, swing_id, rally_index,
  player_id, start_ts, end_ts, ball_hit_ts,
  start_s, end_s, ball_hit_s,
  ball_hit_x, ball_hit_y, ball_speed, swing_type_raw,
  bounce_id, bounce_ts, bounce_s, bounce_type_raw,
  bounce_x_center_m, bounce_y_center_m, bounce_y_norm_m,
  is_valid, server_id, serving_side_raw,
  shot_ix, point_number, game_number, point_in_game,
  src_swing_meta, src_bounce_meta
)
SELECT
  fs.session_id,
  ds.session_uid,                -- from dim_session if present
  fs.swing_id,
  fs.rally_index,                -- present if your bronze ingest populated it
  fs.player_id,
  fs.start_ts,
  fs.end_ts,
  fs.ball_hit_ts,
  fs.start_s,
  fs.end_s,
  fs.ball_hit_s,
  fs.ball_hit_x,
  fs.ball_hit_y,
  fs.ball_speed,
  fs.swing_type                  AS swing_type_raw,

  b.bounce_id,
  b.bounce_ts,
  b.bounce_s,
  b.bounce_type                  AS bounce_type_raw,
  b.x                            AS bounce_x_center_m,
  b.y                            AS bounce_y_center_m,
  /* Normalize Y to a 0..court_length frame later; we keep raw + a 'norm' placeholder here.
     If your bronze already stores normalized y, substitute that here. */
  b.y                            AS bounce_y_norm_m,

  fs.is_valid,                   -- straight from bronze if populated
  NULL::text   AS server_id,     -- to be computed in Phase 2
  NULL::text   AS serving_side_raw,

  NULL::int    AS shot_ix,
  NULL::int    AS point_number,
  NULL::int    AS game_number,
  NULL::int    AS point_in_game,

  NULL::jsonb  AS src_swing_meta,
  NULL::jsonb  AS src_bounce_meta
FROM bronze.fact_swing fs
LEFT JOIN bronze.dim_session ds
  ON ds.session_id = fs.session_id
/* Associate the *first* bounce strictly after the swing within a 2.5s window (and 5ms guard).
   Phase 2 may refine/override this association; for now it's a practical base pull. */
LEFT JOIN LATERAL (
  SELECT
    fb.bounce_id, fb.bounce_ts, fb.bounce_s, fb.bounce_type, fb.x, fb.y
  FROM bronze.fact_bounce fb
  WHERE fb.session_id = fs.session_id
    AND (
      COALESCE(fb.bounce_ts, (TIMESTAMP 'epoch' + fb.bounce_s * INTERVAL '1 second'))
      >
      COALESCE(fs.ball_hit_ts, (TIMESTAMP 'epoch' + fs.ball_hit_s * INTERVAL '1 second')) + INTERVAL '5 milliseconds'
    )
    AND (
      COALESCE(fb.bounce_ts, (TIMESTAMP 'epoch' + fb.bounce_s * INTERVAL '1 second'))
      <=
      COALESCE(fs.ball_hit_ts, (TIMESTAMP 'epoch' + fs.ball_hit_s * INTERVAL '1 second')) + INTERVAL '2.5 seconds'
    )
  ORDER BY COALESCE(fb.bounce_ts, (TIMESTAMP 'epoch' + fb.bounce_s * INTERVAL '1 second')), fb.bounce_id
  LIMIT 1
) b ON TRUE
WHERE (:sid IS NULL OR fs.session_id = :sid)
ON CONFLICT {PK} DO UPDATE SET
  session_uid       = EXCLUDED.session_uid,
  rally_index       = EXCLUDED.rally_index,
  player_id         = EXCLUDED.player_id,
  start_ts          = EXCLUDED.start_ts,
  end_ts            = EXCLUDED.end_ts,
  ball_hit_ts       = EXCLUDED.ball_hit_ts,
  start_s           = EXCLUDED.start_s,
  end_s             = EXCLUDED.end_s,
  ball_hit_s        = EXCLUDED.ball_hit_s,
  ball_hit_x        = EXCLUDED.ball_hit_x,
  ball_hit_y        = EXCLUDED.ball_hit_y,
  ball_speed        = EXCLUDED.ball_speed,
  swing_type_raw    = EXCLUDED.swing_type_raw,
  bounce_id         = EXCLUDED.bounce_id,
  bounce_ts         = EXCLUDED.bounce_ts,
  bounce_s          = EXCLUDED.bounce_s,
  bounce_type_raw   = EXCLUDED.bounce_type_raw,
  bounce_x_center_m = EXCLUDED.bounce_x_center_m,
  bounce_y_center_m = EXCLUDED.bounce_y_center_m,
  bounce_y_norm_m   = EXCLUDED.bounce_y_norm_m,
  is_valid          = EXCLUDED.is_valid,
  server_id         = EXCLUDED.server_id,
  serving_side_raw  = EXCLUDED.serving_side_raw,
  shot_ix           = EXCLUDED.shot_ix,
  point_number      = EXCLUDED.point_number,
  game_number       = EXCLUDED.game_number,
  point_in_game     = EXCLUDED.point_in_game,
  src_swing_meta    = EXCLUDED.src_swing_meta,
  src_bounce_meta   = EXCLUDED.src_bounce_meta;
"""

def _exec(conn: Connection, sql: str, params: Optional[dict] = None) -> None:
    conn.execute(text(sql), params or {})

def ensure_schema_and_table(conn: Connection) -> None:
    _exec(conn, DDL_CREATE_SCHEMA)
    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

def truncate_target(conn: Connection, session_id: Optional[int]) -> None:
    if session_id is None:
        _exec(conn, f"TRUNCATE TABLE {SILVER_SCHEMA}.{TABLE};")
    else:
        _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE session_id = :sid;", {"sid": session_id})

def insert_base(conn: Connection, session_id: Optional[int]) -> int:
    res = conn.execute(text(INSERT_SELECT_BASE), {"sid": session_id})
    # rowcount for INSERT ... ON CONFLICT is driver-dependent; return 0/None safely
    return res.rowcount if res.rowcount is not None else 0

def build_point_detail(session_id: Optional[int] = None, replace: bool = False) -> dict:
    """
    Phase 1 builder:
      - Creates table if needed
      - Optionally replaces rows (truncate or targeted delete)
      - Inserts/Upserts base records from Bronze
    """
    with engine.begin() as conn:
        ensure_schema_and_table(conn)
        if replace:
            truncate_target(conn, session_id)
        affected = insert_base(conn, session_id)
    return {"ok": True, "session_id": session_id, "replaced": replace, "rows_written": affected}

# CLI usage: python build_silver_point_detail.py [--replace] [--session 7]
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Build silver.point_detail (Phase 1 — base fields only)")
    p.add_argument("--session", type=int, default=None, help="Optional session_id to build only that session")
    p.add_argument("--replace", action="store_true", help="Replace rows (truncate all or delete for session)")
    args = p.parse_args()
    out = build_point_detail(session_id=args.session, replace=args.replace)
    print(out)
