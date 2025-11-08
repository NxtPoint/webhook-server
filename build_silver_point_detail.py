# build_silver_point_detail.py
# Phase 1 (FIELDS ONLY): copy raw swing fields from Bronze for a given task_id.
# - NO business logic, NO sequencing, NO bounce matching.
# - ONLY task_id is used (PK = task_id, swing_id).
# - Auto-detects source table: bronze.player_swing (preferred) or bronze.swing.
# - Best-practice DDL creation, idempotent upsert, safe auto-heal of table shape.

from typing import Optional, List, Tuple
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine  # reuse your existing engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"
PK = "(task_id, swing_id)"

# ---------- DDL ----------
DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  -- Identity (task-scoped)
  task_id                   UUID               NOT NULL,
  swing_id                  BIGINT             NOT NULL,   -- maps from bronze swing id

  -- Verbatim swing fields (aligned to Bronze)
  player_id                 TEXT,
  start_ts                  TIMESTAMPTZ,
  start_frame               INTEGER,
  end_ts                    TIMESTAMPTZ,
  end_frame                 INTEGER,
  valid                     BOOLEAN,
  serve                     BOOLEAN,
  swing_type                TEXT,
  volley                    BOOLEAN,
  is_in_rally               BOOLEAN,
  confidence_swing_type     DOUBLE PRECISION,
  confidence                DOUBLE PRECISION,
  confidence_volley         DOUBLE PRECISION,
  ball_player_distance      DOUBLE PRECISION,
  ball_speed                DOUBLE PRECISION,
  ball_impact_type          TEXT,
  intercepting_player_id    TEXT,
  rally                     INTEGER,
  ball_hit                  TIMESTAMPTZ,
  ball_hit_location         JSONB,
  ball_impact_location      JSONB,
  ball_trajectory           JSONB,
  annotations               JSONB,
  start                     DOUBLE PRECISION,
  "end"                     DOUBLE PRECISION,

  -- Placeholders for Phase 2 (remain NULL here)
  bounce_id                 BIGINT,
  bounce_ts                 TIMESTAMPTZ,
  bounce_s                  DOUBLE PRECISION,
  bounce_type               TEXT,
  court_x                   DOUBLE PRECISION,
  court_y                   DOUBLE PRECISION,

  server_id                 TEXT,
  serving_side              TEXT,
  shot_ix                   INTEGER,
  point_number              INTEGER,
  game_number               INTEGER,
  point_in_game             INTEGER,

  created_at                TIMESTAMPTZ DEFAULT NOW(),

  PRIMARY KEY {PK}
);
"""

DDL_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_task        ON {SILVER_SCHEMA}.{TABLE} (task_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_player      ON {SILVER_SCHEMA}.{TABLE} (task_id, player_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_time_start  ON {SILVER_SCHEMA}.{TABLE} (task_id, start_ts);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_time_hit    ON {SILVER_SCHEMA}.{TABLE} (task_id, ball_hit);",
]

# ---------- SQL templates (fields-only) ----------
INSERT_BASE_TEMPLATE = f"""
INSERT INTO {SILVER_SCHEMA}.{TABLE} (
  task_id, swing_id,
  player_id, start_ts, start_frame, end_ts, end_frame,
  valid, serve, swing_type, volley, is_in_rally,
  confidence_swing_type, confidence, confidence_volley,
  ball_player_distance, ball_speed, ball_impact_type,
  intercepting_player_id, rally, ball_hit,
  ball_hit_location, ball_impact_location, ball_trajectory, annotations,
  start, "end",
  bounce_id, bounce_ts, bounce_s, bounce_type, court_x, court_y,
  server_id, serving_side, shot_ix, point_number, game_number, point_in_game
)
SELECT
  :task_id                    AS task_id,
  s.id                        AS swing_id,

  s.player_id,
  s.start_ts,
  s.start_frame,
  s.end_ts,
  s.end_frame,
  s.valid,
  s.serve,
  s.swing_type,
  s.volley,
  s.is_in_rally,
  s.confidence_swing_type,
  s.confidence,
  s.confidence_volley,
  s.ball_player_distance,
  s.ball_speed,
  s.ball_impact_type,
  s.intercepting_player_id,
  s.rally,
  s.ball_hit,
  s.ball_hit_location::jsonb,
  s.ball_impact_location::jsonb,
  s.ball_trajectory::jsonb,
  s.annotations::jsonb,
  s.start,
  s."end",

  NULL::bigint, NULL::timestamptz, NULL::double precision, NULL::text,
  NULL::double precision, NULL::double precision,

  NULL::text, NULL::text, NULL::int, NULL::int, NULL::int, NULL::int
FROM {{}}
WHERE s.task_id = :task_id
ON CONFLICT {PK} DO UPDATE SET
  player_id                 = EXCLUDED.player_id,
  start_ts                  = EXCLUDED.start_ts,
  start_frame               = EXCLUDED.start_frame,
  end_ts                    = EXCLUDED.end_ts,
  end_frame                 = EXCLUDED.end_frame,
  valid                     = EXCLUDED.valid,
  serve                     = EXCLUDED.serve,
  swing_type                = EXCLUDED.swing_type,
  volley                    = EXCLUDED.volley,
  is_in_rally               = EXCLUDED.is_in_rally,
  confidence_swing_type     = EXCLUDED.confidence_swing_type,
  confidence                = EXCLUDED.confidence,
  confidence_volley         = EXCLUDED.confidence_volley,
  ball_player_distance      = EXCLUDED.ball_player_distance,
  ball_speed                = EXCLUDED.ball_speed,
  ball_impact_type          = EXCLUDED.ball_impact_type,
  intercepting_player_id    = EXCLUDED.intercepting_player_id,
  rally                     = EXCLUDED.rally,
  ball_hit                  = EXCLUDED.ball_hit,
  ball_hit_location         = EXCLUDED.ball_hit_location,
  ball_impact_location      = EXCLUDED.ball_impact_location,
  ball_trajectory           = EXCLUDED.ball_trajectory,
  annotations               = EXCLUDED.annotations,
  start                     = EXCLUDED.start,
  "end"                     = EXCLUDED."end",
  -- placeholders remain managed by Phase 2
  bounce_id                 = EXCLUDED.bounce_id,
  bounce_ts                 = EXCLUDED.bounce_ts,
  bounce_s                  = EXCLUDED.bounce_s,
  bounce_type               = EXCLUDED.bounce_type,
  court_x                   = EXCLUDED.court_x,
  court_y                   = EXCLUDED.court_y,
  server_id                 = EXCLUDED.server_id,
  serving_side              = EXCLUDED.serving_side,
  shot_ix                   = EXCLUDED.shot_ix,
  point_number              = EXCLUDED.point_number,
  game_number               = EXCLUDED.game_number,
  point_in_game             = EXCLUDED.point_in_game;
"""

# ---------- Helpers ----------
def _exec(conn: Connection, sql: str, params: Optional[dict] = None) -> None:
    conn.execute(text(sql), params or {})

def _table_exists(conn: Connection, schema: str, name: str) -> bool:
    row = conn.execute(text("""
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = :s AND table_name = :t
    """), {"s": schema, "t": name}).fetchone()
    return bool(row)

def _columns(conn: Connection, schema: str, name: str) -> List[str]:
    rows = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = :s AND table_name = :t
    """), {"s": schema, "t": name}).fetchall()
    return [r[0].lower() for r in rows]

def _bronze_source_table(conn: Connection) -> Tuple[str, str]:
    """Return ('bronze', 'player_swing' or 'swing') choosing what's available, preferring player_swing."""
    if _table_exists(conn, "bronze", "player_swing"):
        return ("bronze", "player_swing")
    if _table_exists(conn, "bronze", "swing"):
        return ("bronze", "swing")
    raise RuntimeError("Neither bronze.player_swing nor bronze.swing exists")

def ensure_schema_and_table(conn: Connection) -> None:
    _exec(conn, DDL_CREATE_SCHEMA)

    # auto-heal: ensure correct shape (must have task_id & swing_id, and no legacy columns)
    if _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols = set(_columns(conn, SILVER_SCHEMA, TABLE))
        required = {"task_id", "swing_id"}
        legacy_blocklist = {"session_id", "session_uid"}  # ensure these do not exist
        if not required.issubset(cols) or (cols & legacy_blocklist):
            _exec(conn, f"DROP TABLE {SILVER_SCHEMA}.{TABLE} CASCADE;")

    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

def delete_for_task(conn: Connection, task_id: str) -> None:
    _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id = :tid;", {"tid": task_id})

def insert_base(conn: Connection, task_id: str) -> int:
    schema, name = _bronze_source_table(conn)
    source_ref = f"{schema}.{name} s"
    sql = INSERT_BASE_TEMPLATE.format(source_ref)
    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount if res.rowcount is not None else 0

def build_point_detail(task_id: str, replace: bool = False) -> dict:
    """
    Phase 1 builder (FIELDS ONLY, task_id-only):
      - Creates silver.point_detail and indexes if needed
      - Optionally deletes existing rows for task_id
      - Inserts raw swing-level fields from Bronze for task_id
    """
    if not task_id:
        raise ValueError("task_id is required")
    with engine.begin() as conn:
        ensure_schema_and_table(conn)
        if replace:
            delete_for_task(conn, task_id)
        affected = insert_base(conn, task_id)
    return {"ok": True, "task_id": task_id, "replaced": replace, "rows_written": affected}

# ---------- CLI ----------
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Build silver.point_detail (Phase 1 — fields only, task_id-only)")
    p.add_argument("--task-id", required=True, help="Task UUID to build (required)")
    p.add_argument("--replace", action="store_true", help="Delete existing rows for this task_id before insert")
    args = p.parse_args()
    out = build_point_detail(task_id=args.task_id, replace=args.replace)
    print(out)
