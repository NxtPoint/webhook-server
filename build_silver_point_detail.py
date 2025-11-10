# build_silver_point_detail.py
# NextPoint Silver point_detail — P1 (with ball_hit_s and x/y split), P2 (bounce), P3 (serve)
# All logic in Python-executed SQL; no ad-hoc DB DDL beyond CREATE/ALTER as needed.

from typing import Dict, Optional, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Column specs -------------------------------
# Phase 1 (14): strict bronze fields + requested transforms
PHASE1_COLS = OrderedDict({
    "id":                    "bigint",            # swing id (bronze.id)
    "task_id":               "uuid",
    "player_id":             "text",
    "valid":                 "boolean",
    "serve":                 "boolean",
    "swing_type":            "text",
    "volley":                "boolean",
    "is_in_rally":           "boolean",
    "ball_player_distance":  "double precision",
    "ball_speed":            "double precision",
    "ball_impact_type":      "text",
    # transformed from raw JSON:
    "ball_hit_s":            "double precision",  # <- ball_hit -> 'timestamp'
    "ball_hit_location_x":   "double precision",  # <- ball_hit_location[0]
    "ball_hit_location_y":   "double precision"   # <- ball_hit_location[1]
})

# Phase 2 (4): exact bronze.ball_bounce names
PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "type":      "text",
    "timestamp": "double precision",
    "court_x":   "double precision",
    "court_y":   "double precision"
})

# Phase 3 (5): serve summary fields (per your spec)
PHASE3_COLS = OrderedDict({
    "serve_d":               "boolean",
    "server_id":             "text",
    "serve_side_d":          "text",
    "serve_try_ix_in_point": "integer",
    "server_end_d":          "text"
})

PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({})
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({})

# ------------------------------- helpers ---------------------------------
def _exec(conn: Connection, sql: str, params: Optional[dict] = None):
    conn.execute(text(sql), params or {})

def _table_exists(conn: Connection, schema: str, name: str) -> bool:
    q = """SELECT 1 FROM information_schema.tables
           WHERE table_schema=:s AND table_name=:t"""
    return bool(conn.execute(text(q), {"s": schema, "t": name}).fetchone())

def _columns_types(conn: Connection, schema: str, name: str) -> Dict[str, str]:
    q = """SELECT column_name, data_type
           FROM information_schema.columns
           WHERE table_schema=:s AND table_name=:t"""
    rows = conn.execute(text(q), {"s": schema, "t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

# ------------------------------- schema ensure -------------------------------
DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

def ensure_table_exists(conn: Connection):
    _exec(conn, DDL_CREATE_SCHEMA)
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in PHASE1_COLS.items()])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task    ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_id ON {SILVER_SCHEMA}.{TABLE}(task_id, id);")

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

# ------------------------------- PHASE 1 — bronze.player_swing → strict + x/y/t -------------------------------
def phase1_load(conn: Connection, task_id: str) -> int:
    """
    Copy core fields verbatim; extract:
      - ball_hit_s from ball_hit->'timestamp'
      - ball_hit_location_x / ball_hit_location_y from ball_hit_location[0/1]
    Safely guarded so non-JSON strings won't be cast unless they look like JSON.
    """
    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      id, task_id, player_id, valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      ball_hit_s, ball_hit_location_x, ball_hit_location_y
    )
    SELECT
      s.id::bigint                               AS id,
      s.task_id::uuid                            AS task_id,
      s.player_id                                AS player_id,
      COALESCE(s.valid, FALSE)                   AS valid,
      COALESCE(s.serve, FALSE)                   AS serve,
      s.swing_type                               AS swing_type,
      COALESCE(s.volley, FALSE)                  AS volley,
      COALESCE(s.is_in_rally, FALSE)             AS is_in_rally,
      s.ball_player_distance::double precision   AS ball_player_distance,
      s.ball_speed::double precision             AS ball_speed,
      s.ball_impact_type                         AS ball_impact_type,
      -- ball_hit_s
      CASE
        WHEN s.ball_hit IS NOT NULL
         AND s.ball_hit::text LIKE '{{%%'
         AND s.ball_hit::text LIKE '%%"timestamp"%%'
        THEN (s.ball_hit::jsonb ->> 'timestamp')::double precision
        ELSE NULL::double precision
      END                                         AS ball_hit_s,
      -- ball_hit_location_x
      CASE
        WHEN s.ball_hit_location IS NOT NULL
         AND s.ball_hit_location::text LIKE '[%%'
      THEN (s.ball_hit_location::jsonb ->> 0)::double precision
      ELSE NULL::double precision END             AS ball_hit_location_x,
      -- ball_hit_location_y
      CASE
        WHEN s.ball_hit_location IS NOT NULL
         AND s.ball_hit_location::text LIKE '[%%'
      THEN (s.ball_hit_location::jsonb ->> 1)::double precision
      ELSE NULL::double precision END             AS ball_hit_location_y
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) = TRUE;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- PHASE 2 — bronze.ball_bounce (exact 4 cols) -------------------------------
def phase2_update(conn: Connection, task_id: str) -> int:
    """
    Pick FIRST bounce strictly after contact time within:
      (ball_hit_s + 0.005,  min(next_ball_hit_s, ball_hit_s + 2.5)]
    """
    sql = f"""
    WITH p AS (
      SELECT
        p1.id, p1.task_id, p1.ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE} p1
      WHERE p1.task_id = :tid
    ),
    p_lead AS (
      SELECT
        p.*,
        LEAD(p.ball_hit_s) OVER (
          PARTITION BY p.task_id
          ORDER BY p.ball_hit_s, p.id
        ) AS next_ball_hit_s
      FROM p
    ),
    p_win AS (
      SELECT
        p_lead.*,
        (p_lead.ball_hit_s + 0.005) AS win_start,
        LEAST(COALESCE(p_lead.next_ball_hit_s, p_lead.ball_hit_s + 2.5), p_lead.ball_hit_s + 2.5) AS win_end
      FROM p_lead
    ),
    chosen AS (
      SELECT
        w.id,
        b.type       AS type,
        b.timestamp  AS timestamp,
        b.court_x    AS court_x,
        b.court_y    AS court_y
      FROM p_win w
      LEFT JOIN LATERAL (
        SELECT type, timestamp, court_x, court_y
        FROM bronze.ball_bounce b
        WHERE b.task_id::uuid = w.task_id
          AND w.ball_hit_s IS NOT NULL
          AND b.timestamp IS NOT NULL
          AND b.timestamp >  w.win_start
          AND b.timestamp <= w.win_end
        ORDER BY (type = 'floor') DESC, timestamp
        LIMIT 1
      ) b ON TRUE
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      type      = c.type,
      timestamp = c.timestamp,
      court_x   = c.court_x,
      court_y   = c.court_y
    FROM chosen c
    WHERE p.task_id = :tid
      AND p.id = c.id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- PHASE 3 — serve logic (finalized) -------------------------------
Y_NEAR_MIN = 23.0   # y > 23 → near
Y_FAR_MAX  = 1.0    # y < 1  → far
X_SIDE_ABS = 4.0    # side threshold

def phase3_update(conn: Connection, task_id: str) -> int:
    """
    serve_d: overhead & (y > 23 or y < 1)
    server_end_d: near if y > 23 ; far if y < 1
    serve_side_d (UPDATED):
        near (y > 23): x < 4  → 'deuce' , else 'ad'
        far  (y < 1) : x > 4  → 'deuce' , else 'ad'
    serve_try_ix_in_point: row_number over consecutive serves (resets on any non-serve)
    Forward-fill server_end_d and serve_side_d to all rows after a serve until the next serve.
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id, p.player_id, p.swing_type,
        -- order key: prefer contact time; if missing, fall back to id to keep a stable order
        p.ball_hit_s AS t,
        COALESCE(p.ball_hit_s, 1e15) AS ord_t,
        p.ball_hit_location_x AS x,
        p.ball_hit_location_y AS y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),
    marks AS (
      SELECT
        b.*,
        (lower(coalesce(b.swing_type,'')) LIKE '%overhead%' AND (b.y > {Y_NEAR_MIN} OR b.y < {Y_FAR_MAX})) AS is_serve,
        CASE
          WHEN b.y > {Y_NEAR_MIN} THEN 'near'
          WHEN b.y < {Y_FAR_MAX}  THEN 'far'
          ELSE NULL
        END AS server_end_d_calc,
        CASE
          WHEN b.y > {Y_NEAR_MIN} THEN (CASE WHEN b.x <  {X_SIDE_ABS} THEN 'deuce' ELSE 'ad' END)
          WHEN b.y < {Y_FAR_MAX}  THEN (CASE WHEN b.x >  {X_SIDE_ABS} THEN 'deuce' ELSE 'ad' END)
          ELSE NULL
        END AS serve_side_d_calc
      FROM base b
    ),
    ordered AS (
      SELECT
        m.*,
        -- running count of serves defines segments between serves
        SUM(CASE WHEN m.is_serve THEN 1 ELSE 0 END)
          OVER (PARTITION BY m.task_id ORDER BY m.ord_t, m.id
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS serve_grp,
        -- most recent serve id to forward-fill attributes
        MAX(CASE WHEN m.is_serve THEN m.id END)
          OVER (PARTITION BY m.task_id ORDER BY m.ord_t, m.id
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_serve_id
      FROM marks m
    ),
    try_ix AS (
      SELECT
        o.*,
        CASE WHEN o.is_serve THEN
          ROW_NUMBER() OVER (PARTITION BY o.task_id, o.serve_grp ORDER BY o.ord_t, o.id)
        ELSE NULL END AS serve_try_ix
      FROM ordered o
    ),
    serve_rows AS (
      SELECT
        t.id AS serve_row_id, t.task_id,
        t.player_id AS server_id_calc,
        t.server_end_d_calc, t.serve_side_d_calc
      FROM try_ix t
      WHERE t.is_serve
    ),
    ff AS (
      SELECT
        t.id, t.task_id, t.is_serve, t.serve_try_ix,
        s.server_id_calc,
        s.server_end_d_calc,
        s.serve_side_d_calc
      FROM try_ix t
      LEFT JOIN serve_rows s
        ON s.task_id = t.task_id
       AND s.serve_row_id = t.last_serve_id
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d               = ff.is_serve,
      -- server_id only written on actual serve rows; not required on non-serves
      server_id             = CASE WHEN ff.is_serve THEN ff.server_id_calc ELSE p.server_id END,
      -- forward-fill end/side to all rows after the last serve, until the next serve
      server_end_d          = COALESCE(ff.server_end_d_calc, p.server_end_d),
      serve_side_d          = COALESCE(ff.serve_side_d_calc, p.serve_side_d),
      -- try index only on serve rows (NULL for non-serves)
      serve_try_ix_in_point = ff.serve_try_ix
    FROM ff
    WHERE p.task_id = :tid
      AND p.id = ff.id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- Phase 2–5 (schema only adds) -------------------------------
def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# ------------------------------- Orchestrator -------------------------------
def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    if not task_id:
        raise ValueError("task_id is required")
    out: Dict = {"ok": True, "task_id": task_id, "phase": phase}

    with engine.begin() as conn:
        ensure_table_exists(conn)
        ensure_phase_columns(conn, PHASE1_COLS)
        if phase in ("all","2","3","4","5"): phase2_add_schema(conn)
        if phase in ("all","3","4","5"):     phase3_add_schema(conn)
        if phase in ("all","4","5"):         phase4_add_schema(conn)
        if phase in ("all","5"):             phase5_add_schema(conn)

        if phase in ("all","1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows"] = phase1_load(conn, task_id)

        if phase in ("all","2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)

        if phase in ("all","3"):
            out["phase3_rows_updated"] = phase3_update(conn, task_id)

        if phase in ("all","4"): out["phase4"] = "schema-ready"
        if phase in ("all","5"): out["phase5"] = "schema-ready"

    return out

# ------------------------------- CLI -------------------------------
if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser(description="Silver point_detail — P1(14 with x/y/t) + P2(4) + P3(5)")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--phase", choices=["1","2","3","4","5","all"], default="all", help="which phase(s) to run")
    p.add_argument("--replace", action="store_true", help="delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
