# build_silver_point_detail.py
# Phase 1 (FIELDS ONLY, task_id-only)
# - Copy raw swing fields from Bronze into silver.point_detail
# - NO business logic, NO sequencing, NO bounce matching
# - Auto-detects Bronze source table (player_swing or swing) and each column's type
# - Converts seconds->timestamptz when needed
# - Safely converts JSON numeric fields (e.g., start/end if stored as json/jsonb)
# - PK = (task_id, swing_id)

from typing import Optional, List, Dict, Tuple
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"
PK = "(task_id, swing_id)"

# ---------- DDL ----------
DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  task_id                   UUID               NOT NULL,
  swing_id                  BIGINT             NOT NULL,

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

  -- rally: prefer integer; keep raw JSON separately if source is json/jsonb
  rally                     INTEGER,
  rally_json                JSONB,

  ball_hit                  TIMESTAMPTZ,
  ball_hit_location         JSONB,
  ball_impact_location      JSONB,
  ball_trajectory           JSONB,
  annotations               JSONB,

  -- raw seconds (some sources store as numeric or json)
  start                     DOUBLE PRECISION,
  "end"                     DOUBLE PRECISION,

  -- Phase 2 placeholders (stay NULL here)
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

def _columns_types(conn: Connection, schema: str, name: str) -> Dict[str, str]:
    rows = conn.execute(text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = :s AND table_name = :t
    """), {"s": schema, "t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _bronze_source_table(conn: Connection) -> Tuple[str, str, Dict[str, str]]:
    if _table_exists(conn, "bronze", "player_swing"):
        cols = _columns_types(conn, "bronze", "player_swing")
        return ("bronze", "player_swing", cols)
    if _table_exists(conn, "bronze", "swing"):
        cols = _columns_types(conn, "bronze", "swing")
        return ("bronze", "swing", cols)
    raise RuntimeError("Neither bronze.player_swing nor bronze.swing exists")

def _colref(name: str) -> str:
    """Return a safe table column reference s.<col>, quoting if reserved (e.g., end)."""
    n = name.lower()
    if n in {"end"}:
        return 's."end"'
    return f"s.{n}"

def _ts_expr(cols: Dict[str, str], col_ts: str, fallback_seconds: str) -> str:
    """
    Build a timestamptz expression:
      - If col_ts exists and is timestamptz => use it
      - If col_ts exists and is numeric => epoch + seconds
      - Else if fallback_seconds exists and is numeric/json-number => epoch + seconds
      - Else NULL
    """
    col_ts_l = col_ts.lower()
    fb_l = fallback_seconds.lower()
    if col_ts_l in cols:
        dt = cols[col_ts_l]
        if "timestamp" in dt:
            return _colref(col_ts_l)
        if any(k in dt for k in ("double", "real", "numeric", "integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(col_ts_l)} * INTERVAL '1 second')"
        if "json" in dt:
            # Use only when JSON is a number
            return f"""(
                CASE
                  WHEN jsonb_typeof({_colref(col_ts_l)})='number'
                    THEN (TIMESTAMP 'epoch' + ({_colref(col_ts_l)}::text)::double precision * INTERVAL '1 second')
                  ELSE NULL::timestamptz
                END
            )"""
    if fb_l in cols:
        dt = cols[fb_l]
        if any(k in dt for k in ("double", "real", "numeric", "integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(fb_l)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(
                CASE
                  WHEN jsonb_typeof({_colref(fb_l)})='number'
                    THEN (TIMESTAMP 'epoch' + ({_colref(fb_l)}::text)::double precision * INTERVAL '1 second')
                  ELSE NULL::timestamptz
                END
            )"""
    return "NULL::timestamptz"

def _jsonb_expr(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n in cols:
        return f"{_colref(n)}::jsonb"
    return "NULL::jsonb"

def _num_expr(cols: Dict[str, str], name: str) -> str:
    """Return numeric expression; if source is json/jsonb, extract number only; else NULL."""
    n = name.lower()
    if n not in cols:
        return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(
            CASE
              WHEN jsonb_typeof({_colref(n)})='number'
                THEN ({_colref(n)}::text)::double precision
              ELSE NULL::double precision
            END
        )"""
    return _colref(n)

def _int_expr(cols: Dict[str, str], name: str) -> str:
    """Return integer expression; supports json number -> int."""
    n = name.lower()
    if n not in cols:
        return "NULL::int"
    dt = cols[n]
    if "json" in dt:
        return f"""(
            CASE
              WHEN jsonb_typeof({_colref(n)})='number'
                THEN ({_colref(n)}::text)::int
              ELSE NULL::int
            END
        )"""
    return _colref(n)

def _bool_expr(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n in cols:
        return _colref(n)
    return "NULL::boolean"

def _text_expr(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n in cols:
        return _colref(n)
    return "NULL::text"

def ensure_schema_and_table(conn: Connection) -> None:
    _exec(conn, DDL_CREATE_SCHEMA)
    # auto-heal shape (must have task_id,swing_id; must NOT have legacy session_id/session_uid)
    if _table_exists(conn, SILVER_SCHEMA, TABLE):
        colset = set(_columns_types(conn, SILVER_SCHEMA, TABLE).keys())
        if "task_id" not in colset or "swing_id" not in colset or ("session_id" in colset or "session_uid" in colset):
            _exec(conn, f"DROP TABLE {SILVER_SCHEMA}.{TABLE} CASCADE;")
    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

def insert_base(conn: Connection, task_id: str) -> int:
    schema, name, cols = _bronze_source_table(conn)
    source_ref = f"{schema}.{name} s"

    swing_id_expr = "s.id" if "id" in cols else "NULL::bigint"
    start_ts_expr = _ts_expr(cols, "start_ts", "start")
    end_ts_expr   = _ts_expr(cols, "end_ts",   "end")
    ball_hit_expr = _ts_expr(cols, "ball_hit", "ball_hit_s")

    # rally handling: if JSON/JSONB -> rally=NULL::int, rally_json=raw; else rally=int and rally_json=NULL
    rally_is_json = "rally" in cols and ("json" in cols["rally"])
    rally_int_expr  = "NULL::int" if rally_is_json else _int_expr(cols, "rally")
    rally_json_expr = f"{_colref('rally')}::jsonb" if rally_is_json else "NULL::jsonb"

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id,
      player_id, start_ts, start_frame, end_ts, end_frame,
      valid, serve, swing_type, volley, is_in_rally,
      confidence_swing_type, confidence, confidence_volley,
      ball_player_distance, ball_speed, ball_impact_type,
      intercepting_player_id, rally, rally_json, ball_hit,
      ball_hit_location, ball_impact_location, ball_trajectory, annotations,
      start, "end",
      bounce_id, bounce_ts, bounce_s, bounce_type, court_x, court_y,
      server_id, serving_side, shot_ix, point_number, game_number, point_in_game
    )
    SELECT
      :task_id,
      {swing_id_expr},

      {_text_expr(cols, "player_id")},
      {start_ts_expr},
      {_int_expr(cols, "start_frame")},
      {end_ts_expr},
      {_int_expr(cols, "end_frame")},
      {_bool_expr(cols, "valid")},
      {_bool_expr(cols, "serve")},
      {_text_expr(cols, "swing_type")},
      {_bool_expr(cols, "volley")},
      {_bool_expr(cols, "is_in_rally")},
      {_num_expr(cols, "confidence_swing_type")},
      {_num_expr(cols, "confidence")},
      {_num_expr(cols, "confidence_volley")},
      {_num_expr(cols, "ball_player_distance")},
      {_num_expr(cols, "ball_speed")},
      {_text_expr(cols, "ball_impact_type")},
      {_text_expr(cols, "intercepting_player_id")},
      {rally_int_expr},
      {rally_json_expr},
      {ball_hit_expr},
      {_jsonb_expr(cols, "ball_hit_location")},
      {_jsonb_expr(cols, "ball_impact_location")},
      {_jsonb_expr(cols, "ball_trajectory")},
      {_jsonb_expr(cols, "annotations")},
      {_num_expr(cols, "start")},
      {_num_expr(cols, "end")},

      NULL::bigint, NULL::timestamptz, NULL::double precision, NULL::text,
      NULL::double precision, NULL::double precision,

      NULL::text, NULL::text, NULL::int, NULL::int, NULL::int, NULL::int
    FROM {source_ref}
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
      rally_json                = EXCLUDED.rally_json,
      ball_hit                  = EXCLUDED.ball_hit,
      ball_hit_location         = EXCLUDED.ball_hit_location,
      ball_impact_location      = EXCLUDED.ball_impact_location,
      ball_trajectory           = EXCLUDED.ball_trajectory,
      annotations               = EXCLUDED.annotations,
      start                     = EXCLUDED.start,
      "end"                     = EXCLUDED."end",
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
    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount if res.rowcount is not None else 0

def delete_for_task(conn: Connection, task_id: str) -> None:
    _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id = :tid;", {"tid": task_id})

def build_point_detail(task_id: str, replace: bool = False) -> dict:
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
    p.add_argument("--task-id", required=True)
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    out = build_point_detail(task_id=args.task_id, replace=args.replace)
    print(out)
