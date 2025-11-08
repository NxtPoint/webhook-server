# build_silver_point_detail.py
# Phase 1 (FIELDS ONLY): pull raw swing-level fields from Bronze for a given task_id.
# - NO business logic: no bounce association, no timing windows, no sequencing.
# - Leaves placeholders (bounce/serve/scoring) as NULL for Phase 2 to compute.
# - Primary key: (task_id, swing_id)  -- task-scoped rows.

from typing import Optional
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine  # reuse existing engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"
PK = "(task_id, swing_id)"

DDL_CREATE_SCHEMA = f"""
CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};
"""

DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  -- Identity (task-scoped)
  task_id                 UUID               NOT NULL,
  swing_id                BIGINT             NOT NULL,

  -- Optional session-ish metadata (if present on dim_session for this task)
  session_uid             TEXT,

  -- Position in rally (only if Bronze provides on fact_swing; else stays NULL)
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

  -- Ball at hit (used later for serve/location etc.)
  ball_hit_x              DOUBLE PRECISION,
  ball_hit_y              DOUBLE PRECISION,
  ball_speed              DOUBLE PRECISION,
  swing_type_raw          TEXT,

  -- Placeholders for later logic (kept NULL in Phase 1)
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
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_task      ON {SILVER_SCHEMA}.{TABLE} (task_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_player    ON {SILVER_SCHEMA}.{TABLE} (task_id, player_id);",
    f"CREATE INDEX IF NOT EXISTS ix_point_detail_time      ON {SILVER_SCHEMA}.{TABLE} (task_id, ball_hit_ts);",
]

# Phase 1 INSERT: strictly raw fields from Bronze.fact_swing (+ optional dim_session.session_uid)
# No lateral joins, no time windows, no bounce association.
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
  :task_id                  AS task_id,
  fs.swing_id,
  ds.session_uid,           -- may be NULL if not present for this task
  fs.rally_index,           -- may be NULL if not present
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
  fs.swing_type            AS swing_type_raw,

  NULL::bigint    AS bounce_id,
  NULL::timestamptz AS bounce_ts,
  NULL::double precision AS bounce_s,
  NULL::text      AS bounce_type_raw,
  NULL::double precision AS bounce_x_center_m,
  NULL::double precision AS bounce_y_center_m,
  NULL::double precision AS bounce_y_norm_m,

  fs.is_valid,             -- if Bronze populated it; else NULL
  NULL::text      AS server_id,
  NULL::text      AS serving_side_raw,

  NULL::int       AS shot_ix,
  NULL::int       AS point_number,
  NULL::int       AS game_number,
  NULL::int       AS point_in_game,

  NULL::jsonb     AS src_swing_meta,
  NULL::jsonb     AS src_bounce_meta
FROM bronze.fact_swing fs
LEFT JOIN bronze.dim_session ds
  ON ds.task_id = fs.task_id
WHERE fs.task_id = :task_id
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
  -- bounce_* remain managed by Phase 2 (stay NULL here)
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

def delete_for_task(conn: Connection, task_id: str) -> None:
    _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id = :tid;", {"tid": task_id})

def insert_base(conn: Connection, task_id: str) -> int:
    res = conn.execute(text(INSERT_SELECT_BASE), {"task_id": task_id})
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

# CLI:
#   python build_silver_point_detail.py --task-id <uuid> [--replace]
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Build silver.point_detail (Phase 1 — fields only, task_id-only)")
    p.add_argument("--task-id", required=True, help="Task UUID to build (required)")
    p.add_argument("--replace", action="store_true", help="Delete existing rows for this task_id before insert")
    args = p.parse_args()
    out = build_point_detail(task_id=args.task_id, replace=args.replace)
    print(out)
