# build_silver_phase1.py
# Phase 1 — Section 1 ONLY (player_swing) → silver.point_detail
# - Import ONLY the columns specified by the spec.
# - Filter: valid = TRUE (strict).
# - No joins, no heuristics, no derived fields.

from typing import Optional, Dict, Tuple
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"  # per your naming, with only the Section 1 columns

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  task_id               UUID,
  created_at            TIMESTAMPTZ,
  start_ts              TIMESTAMPTZ,
  end_ts                TIMESTAMPTZ,
  player_id             TEXT,
  valid                 BOOLEAN,
  serve                 BOOLEAN,
  swing_type            TEXT,
  volley                BOOLEAN,
  is_in_rally           BOOLEAN,
  ball_player_distance  DOUBLE PRECISION,
  ball_speed            DOUBLE PRECISION,
  ball_impact_type      TEXT,
  rally                 INTEGER,
  ball_hit              TIMESTAMPTZ,
  ball_hit_location     JSONB
);
"""

DDL_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_pd_task          ON {SILVER_SCHEMA}.{TABLE}(task_id);",
    f"CREATE INDEX IF NOT EXISTS ix_pd_start_ts      ON {SILVER_SCHEMA}.{TABLE}(start_ts);",
    f"CREATE INDEX IF NOT EXISTS ix_pd_ball_hit      ON {SILVER_SCHEMA}.{TABLE}(ball_hit);",
]

# --------------------- helpers ---------------------

def _exec(conn: Connection, sql: str, params: Optional[dict] = None) -> None:
    conn.execute(text(sql), params or {})

def _table_exists(conn: Connection, schema: str, name: str) -> bool:
    return bool(conn.execute(
        text("""SELECT 1 FROM information_schema.tables
                WHERE table_schema=:s AND table_name=:t"""),
        {"s": schema, "t": name}
    ).fetchone())

def _columns_types(conn: Connection, schema: str, name: str) -> Dict[str, str]:
    rows = conn.execute(text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema=:s AND table_name=:t
    """), {"s": schema, "t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _bronze_source_table(conn: Connection) -> Tuple[str, Dict[str, str]]:
    # Spec says player_swing; fallback to swing if needed.
    if _table_exists(conn, "bronze", "player_swing"):
        return "bronze.player_swing s", _columns_types(conn, "bronze", "player_swing")
    if _table_exists(conn, "bronze", "swing"):
        return "bronze.swing s", _columns_types(conn, "bronze", "swing")
    raise RuntimeError("Bronze source not found: expected bronze.player_swing (or bronze.swing).")

def _colref(name: str) -> str:
    n = name.lower()
    return 's."end"' if n == "end" else f"s.{n}"

def _ts_expr(cols: Dict[str, str], col_ts: str, fb_seconds: str) -> str:
    """
    Return an expression that yields TIMESTAMPTZ for a given timestamp column.
    If Bronze stored seconds instead, convert epoch seconds → timestamptz.
    """
    c = col_ts.lower(); fb = fb_seconds.lower()
    if c in cols:
        dt = cols[c]
        if "timestamp" in dt:
            return _colref(c)
        if any(k in dt for k in ("double", "real", "numeric", "integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(c)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(
              CASE WHEN jsonb_typeof({_colref(c)})='number'
                   THEN (TIMESTAMP 'epoch' + ({_colref(c)}::text)::double precision * INTERVAL '1 second')
                   ELSE NULL::timestamptz END)"""
    if fb in cols:
        dt = cols[fb]
        if any(k in dt for k in ("double", "real", "numeric", "integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(fb)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(
              CASE WHEN jsonb_typeof({_colref(fb)})='number'
                   THEN (TIMESTAMP 'epoch' + ({_colref(fb)}::text)::double precision * INTERVAL '1 second')
                   ELSE NULL::timestamptz END)"""
    return "NULL::timestamptz"

def _jsonb_expr(cols: Dict[str, str], name: str) -> str:
    return f"{_colref(name)}::jsonb" if name.lower() in cols else "NULL::jsonb"

def _num_expr(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols:
        return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(
          CASE WHEN jsonb_typeof({_colref(n)})='number'
               THEN ({_colref(n)}::text)::double precision
               ELSE NULL::double precision END)"""
    return _colref(n)

def _int_expr(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols:
        return "NULL::int"
    dt = cols[n]
    if "json" in dt:
        # rally may arrive as number or object with {index: N}
        return f"""(
          CASE WHEN jsonb_typeof({_colref(n)})='number'
                 THEN ({_colref(n)}::text)::int
               WHEN jsonb_typeof({_colref(n)})='object'
                 AND ({_colref(n)} ? 'index')
                 AND jsonb_typeof({_colref(n)}->'index')='number'
                 THEN ({_colref(n)}->>'index')::int
               ELSE NULL::int
          END)"""
    return _colref(n)

def _bool_expr(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::boolean"

def _text_expr(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::text"

# --------------------- DDL & load ---------------------

def ensure_schema_and_table(conn: Connection) -> None:
    _exec(conn, DDL_CREATE_SCHEMA)
    # Hard reset to guarantee only the specified columns exist
    if _table_exists(conn, SILVER_SCHEMA, TABLE):
        _exec(conn, f"DROP TABLE {SILVER_SCHEMA}.{TABLE} CASCADE;")
    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

def delete_for_task(conn: Connection, task_id: str) -> None:
    _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id = :tid;", {"tid": task_id})

def insert_phase1(conn: Connection, task_id: str) -> int:
    src, cols = _bronze_source_table(conn)

    created_at_expr = _ts_expr(cols, "created_at", "created_at")  # pass through / convert if needed
    start_ts_expr   = _ts_expr(cols, "start_ts", "start")
    end_ts_expr     = _ts_expr(cols, "end_ts",   "end")
    ball_hit_expr   = _ts_expr(cols, "ball_hit", "ball_hit_s")
    rally_expr      = _int_expr(cols, "rally")

    # ball_hit_location as JSON array/object
    ball_hit_loc_expr = _jsonb_expr(cols, "ball_hit_location")

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, created_at, start_ts, end_ts, player_id, valid, serve, swing_type,
      volley, is_in_rally, ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit, ball_hit_location
    )
    SELECT
      {_text_expr(cols, "task_id")}::uuid,
      {created_at_expr},
      {start_ts_expr},
      {end_ts_expr},
      {_text_expr(cols, "player_id")},
      {_bool_expr(cols, "valid")},
      {_bool_expr(cols, "serve")},
      {_text_expr(cols, "swing_type")},
      {_bool_expr(cols, "volley")},
      {_bool_expr(cols, "is_in_rally")},
      {_num_expr(cols, "ball_player_distance")},
      {_num_expr(cols, "ball_speed")},
      {_text_expr(cols, "ball_impact_type")},
      {rally_expr},
      {ball_hit_expr},
      {ball_hit_loc_expr}
    FROM {src}
    WHERE {_text_expr(cols, "task_id")}::uuid = :task_id
      AND COALESCE({_bool_expr(cols, "valid")}, FALSE) IS TRUE;
    """
    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount if res.rowcount is not None else 0

def build_phase1(task_id: str, replace: bool = False) -> dict:
    if not task_id:
        raise ValueError("task_id is required")
    with engine.begin() as conn:
        ensure_schema_and_table(conn) if replace else _exec(conn, DDL_CREATE_SCHEMA) or None
        if replace and _table_exists(conn, SILVER_SCHEMA, TABLE):
            delete_for_task(conn, task_id)
        elif not _table_exists(conn, SILVER_SCHEMA, TABLE):
            _exec(conn, DDL_CREATE_TABLE)
            for ddl in DDL_INDEXES:
                _exec(conn, ddl)
        rows = insert_phase1(conn, task_id)
    return {"ok": True, "task_id": task_id, "replaced": replace, "rows_written": rows}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Phase 1 — Section 1 only (bronze.player_swing → silver.point_detail)")
    p.add_argument("--task-id", required=True, help="Task ID (UUID) to load")
    p.add_argument("--replace", action="store_true", help="Delete existing rows for task_id before insert")
    args = p.parse_args()
    out = build_phase1(task_id=args.task_id, replace=args.replace)
    print(out)
