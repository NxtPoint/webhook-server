# build_silver_point_detail.py
# Phase 1 (FIELDS ONLY): copy raw swing fields from Bronze for a given task_id.
# - No logic, no bounce matching, no sequencing.
# - task_id is the ONLY key. PK = (task_id, swing_id).

from typing import Optional
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"
PK = "(task_id, swing_id)"

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  task_id                 UUID               NOT NULL,
  swing_id                BIGINT             NOT NULL,

  -- Optional metadata placeholders
  session_uid             TEXT,
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

  -- Ball at hit (raw)
  ball_hit_x              DOUBLE PRECISION,
  ball_hit_y              DOUBLE PRECISION,
  ball_speed              DOUBLE PRECISION,
  swing_type_raw          TEXT,

  -- Placeholders for Phase 2 (remain NULL here)
  bounce_id               BIGINT,
  bounce_ts               TIMESTAMPTZ,
  bounce_s                DOUBLE PRECISION,
  bounce_type_raw         TEXT,
  bounce_x_center_m       DOUBLE PRECISION,
  bounce_y_center_m       DOUBLE PRECISION,
  bounce_y_norm_m         DOUBLE PRECISION,

  is_valid                BOOLEAN,
  server_id               TEXT,
  serving_side_raw        TEXT,

  shot_ix                 INTEGER,
  point_number            INTEGER,
  game_number             INTEGER,
  point_in_game           INTEGER,

  src_swing_meta          JSONB,
  src_bounce_meta         JSONB,

  created_at              TIMESTAMPTZ DEFAULT NOW(),

  PRIMARY KEY {PK}
);
"""

DDL_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_task   ON {SILVER_SCHEMA}.{TABLE} (task_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_player ON {SILVER_SCHEMA}.{TABLE} (task_id, player_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_time   ON {SILVER_SCHEMA}.{TABLE} (task_id, ball_hit_ts);",
]

# FIELDS ONLY: pull from bronze.swing (no joins; all placeholders stay NULL)
INSERT_SELECT_BASE = f"""
INSERT INTO {SILVER_SCHEMA}.{TABLE} (
  task_id, swing_id, session_uid, rally_index,
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
  :task_id                                      AS task_id,
  s.swing_id,
  NULL::text                                    AS session_uid,
  s.rally_index,
  s.player_id,
  s.start_ts,
  s.end_ts,
  s.ball_hit_ts,
  s.start_s,
  s.end_s,
  s.ball_hit_s,
  s.ball_hit_x,
  s.ball_hit_y,
  s.ball_speed,
  s.swing_type                                  AS swing_type_raw,

  NULL::bigint, NULL::timestamptz, NULL::double precision, NULL::text,
  NULL::double precision, NULL::double precision, NULL::double precision,

  s.is_valid,
  NULL::text, NULL::text,

  NULL::int, NULL::int, NULL::int, NULL::int,

  NULL::jsonb, NULL::jsonb
FROM bronze.swing s
WHERE s.task_id = :task_id
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

    # auto-heal: if table exists but missing task_id (or has legacy session_id), drop & recreate
    tbl = conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema=:s AND table_name=:t
    """), {"s": SILVER_SCHEMA, "t": TABLE}).fetchone()
    if tbl:
        cols = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=:s AND table_name=:t
        """), {"s": SILVER_SCHEMA, "t": TABLE}).scalars().all()
        cols = {c.lower() for c in cols}
        if "task_id" not in cols or "session_id" in cols:
            _exec(conn, f"DROP TABLE {SILVER_SCHEMA}.{TABLE} CASCADE;")

    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

def delete_for_task(conn: Connection, task_id: str) -> None:
    _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid;", {"tid": task_id})

def insert_base(conn: Connection, task_id: str) -> int:
    res = conn.execute(text(INSERT_SELECT_BASE), {"task_id": task_id})
    return res.rowcount if res.rowcount is not None else 0

def build_point_detail(task_id: str, replace: bool = False) -> dict:
    if not task_id:
        raise ValueError("task_id is required")
    with engine.begin() as conn:
        ensure_schema_and_table(conn)
        if replace:
            delete_for_task(conn, task_id)
        affected = insert_base(conn, task_id)
    return {"ok": True, "task_id": task_id, "replaced": replace, "rows_written": affected}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Build silver.point_detail (Phase 1 — fields only, task_id-only)")
    p.add_argument("--task-id", required=True)
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    out = build_point_detail(task_id=args.task_id, replace=args.replace)
    print(out)
