# build_silver_point_detail.py — Phase 1 (Section 1 only)
# - Pull EXACTLY the listed fields from bronze.player_swing
# - Filter to valid = TRUE (strict)
# - No extra columns, no heuristics, no joins
# - Idempotent per task_id; supports --replace

from typing import Optional, Dict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine  # your existing SQLAlchemy Engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"  # keeps the familiar name, but now it's Section-1-only

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

# EXACT columns per your spec (order preserved)
DDL_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {SILVER_SCHEMA}.{TABLE} (
  task_id               UUID               NOT NULL,
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

# Practical indexes for reading later (don’t add new columns)
DDL_INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_pd_task      ON {SILVER_SCHEMA}.{TABLE}(task_id);",
    f"CREATE INDEX IF NOT EXISTS ix_pd_player    ON {SILVER_SCHEMA}.{TABLE}(task_id, player_id);",
    f"CREATE INDEX IF NOT EXISTS ix_pd_time      ON {SILVER_SCHEMA}.{TABLE}(task_id, start_ts);",
]

# --- helpers (safe casting from bronze types) --------------------------------

def _table_exists(conn: Connection, schema: str, name: str) -> bool:
    return bool(conn.execute(text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_schema=:s AND table_name=:t
    """), {"s": schema, "t": name}).fetchone())

def _columns_types(conn: Connection, schema: str, name: str) -> Dict[str, str]:
    rows = conn.execute(text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema=:s AND table_name=:t
    """), {"s": schema, "t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _colref(name: str) -> str:
    n = name.lower()
    return f's."end"' if n == "end" else f"s.{n}"

def _ts_expr(cols: Dict[str, str], col_ts: str, fb_seconds: str) -> str:
    """Return timestamptz from a native ts column OR from a seconds float/json fallback."""
    c = col_ts.lower(); fb = fb_seconds.lower()
    if c in cols:
        dt = cols[c]
        if "timestamp" in dt:
            return _colref(c)
        if any(k in dt for k in ("double","real","numeric","integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(c)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(
              CASE WHEN jsonb_typeof({_colref(c)})='number'
                   THEN (TIMESTAMP 'epoch' + ({_colref(c)}::text)::double precision * INTERVAL '1 second')
                   ELSE NULL::timestamptz END)"""
    if fb in cols:
        dt = cols[fb]
        if any(k in dt for k in ("double","real","numeric","integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(fb)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(
              CASE WHEN jsonb_typeof({_colref(fb)})='number'
                   THEN (TIMESTAMP 'epoch' + ({_colref(fb)}::text)::double precision * INTERVAL '1 second')
                   ELSE NULL::timestamptz END)"""
    return "NULL::timestamptz"

def _bool_expr(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::boolean"

def _text_expr(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::text"

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
        # allow { "index": <num> } objects too
        return f"""(
          CASE WHEN jsonb_typeof({_colref(n)})='number'
               THEN ({_colref(n)}::text)::int
               ELSE
                 CASE
                   WHEN jsonb_typeof({_colref(n)})='object'
                        AND ({_colref(n)} ? 'index')
                        AND jsonb_typeof({_colref(n)}->'index')='number'
                   THEN ({_colref(n)}->>'index')::int
                   ELSE NULL::int
                 END
          END)"""
    return _colref(n)

def _jsonb_expr(cols: Dict[str, str], name: str) -> str:
    return f"{_colref(name)}::jsonb" if name.lower() in cols else "NULL::jsonb"

def _exec(conn: Connection, sql: str, params: Optional[dict] = None) -> None:
    conn.execute(text(sql), params or {})

# --- core DDL -----------------------------------------------------------------

def ensure_schema_and_table(conn: Connection) -> None:
    _exec(conn, DDL_CREATE_SCHEMA)
    # Drop/recreate if table exists with different shape (we're rebuilding clean)
    if _table_exists(conn, SILVER_SCHEMA, TABLE):
        # simple: drop and recreate to ensure exact shape
        _exec(conn, f"DROP TABLE {SILVER_SCHEMA}.{TABLE} CASCADE;")
    _exec(conn, DDL_CREATE_TABLE)
    for ddl in DDL_INDEXES:
        _exec(conn, ddl)

# --- insert from bronze.player_swing (Section 1 only) -------------------------

def insert_from_bronze(conn: Connection, task_id: str) -> int:
    schema, name = "bronze", "player_swing"
    if not _table_exists(conn, schema, name):
        raise RuntimeError("bronze.player_swing not found")

    cols = _columns_types(conn, schema, name)
    src = f"{schema}.{name} s"

    created_at_expr = _ts_expr(cols, "created_at", "created_at_s")
    start_ts_expr   = _ts_expr(cols, "start_ts",   "start")
    end_ts_expr     = _ts_expr(cols, "end_ts",     "end")
    ball_hit_expr   = _ts_expr(cols, "ball_hit",   "ball_hit_s")

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, created_at, start_ts, end_ts, player_id, valid, serve, swing_type, volley,
      is_in_rally, ball_player_distance, ball_speed, ball_impact_type, rally, ball_hit, ball_hit_location
    )
    SELECT
      s.task_id,
      {created_at_expr}                      AS created_at,
      {start_ts_expr}                        AS start_ts,
      {end_ts_expr}                          AS end_ts,
      {_text_expr(cols, "player_id")}        AS player_id,
      {_bool_expr(cols, "valid")}            AS valid,
      {_bool_expr(cols, "serve")}            AS serve,
      {_text_expr(cols, "swing_type")}       AS swing_type,
      {_bool_expr(cols, "volley")}           AS volley,
      {_bool_expr(cols, "is_in_rally")}      AS is_in_rally,
      {_num_expr(cols, "ball_player_distance")} AS ball_player_distance,
      {_num_expr(cols, "ball_speed")}        AS ball_speed,
      {_text_expr(cols, "ball_impact_type")} AS ball_impact_type,
      {_int_expr(cols, "rally")}             AS rally,
      {ball_hit_expr}                        AS ball_hit,
      {_jsonb_expr(cols, "ball_hit_location")} AS ball_hit_location
    FROM {src}
    WHERE s.task_id = :task_id
      AND s.valid IS TRUE
    """
    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount or 0

def delete_task(conn: Connection, task_id: str) -> None:
    _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})

def build_point_detail(task_id: str, replace: bool=False) -> dict:
    if not task_id:
        raise ValueError("task_id is required")
    with engine.begin() as conn:
        ensure_schema_and_table(conn)
        if replace:
            delete_task(conn, task_id)
        written = insert_from_bronze(conn, task_id)
    return {"ok": True, "task_id": task_id, "replaced": replace, "rows_written": written}

# --- CLI ----------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Phase 1 — Section 1 only (bronze.player_swing → silver.point_detail)")
    p.add_argument("--task-id", required=True)
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    out = build_point_detail(task_id=args.task_id, replace=args.replace)
    print(out)
