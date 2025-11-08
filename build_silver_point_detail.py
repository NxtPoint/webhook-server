# build_silver_point_detail.py
# Unified runner for Silver point_detail — additive phases, no rewrites
# Phase 1: import ONLY the Section 1 columns from bronze.player_swing (valid=TRUE)
# Phases 2–5: schema added incrementally; loaders can be filled later.

from typing import Dict, Optional, Tuple
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Phase specs (schema only) -------------------------------

# Phase 1 — Section 1 (exactly from your sheet)
PHASE1_COLS = OrderedDict({
    "task_id":              "uuid",
    "created_at":           "timestamptz",
    "start_ts":             "timestamptz",
    "end_ts":               "timestamptz",
    "player_id":            "text",
    "valid":                "boolean",
    "serve":                "boolean",
    "swing_type":           "text",
    "volley":               "boolean",
    "is_in_rally":          "boolean",
    "ball_player_distance": "double precision",
    "ball_speed":           "double precision",
    "ball_impact_type":     "text",
    "rally":                "integer",
    "ball_hit":             "timestamptz",
    "ball_hit_location":    "jsonb",
})

# Phase 2 — player info (additive columns; loader TBD)
PHASE2_COLS = OrderedDict({
    # fill as needed later, e.g. "player_hand": "text",
})

# Phase 3 — bounce fields (additive columns; loader TBD)
PHASE3_COLS = OrderedDict({
    # e.g. "bounce_id": "bigint", "bounce_ts": "timestamptz", ...
})

# Phase 4 — serve/point logic (derived; additive columns; updater TBD)
PHASE4_COLS = OrderedDict({
    # e.g. "server_id":"text","serving_side":"text","point_number":"integer","game_number":"integer",
})

# Phase 5 — locations (derived; additive columns; updater TBD)
PHASE5_COLS = OrderedDict({
    # e.g. "play_d":"text","serve_try_ix_in_point":"integer","first_rally_shot_ix":"integer",
})

PHASE_COLSETS = [PHASE1_COLS, PHASE2_COLS, PHASE3_COLS, PHASE4_COLS, PHASE5_COLS]

# --------------------------------- helpers ---------------------------------

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

def _bronze_src(conn: Connection) -> Tuple[str, Dict[str, str]]:
    # Prefer player_swing; fallback swing
    for cand in ("player_swing", "swing"):
        if _table_exists(conn, "bronze", cand):
            return f"bronze.{cand} s", _columns_types(conn, "bronze", cand)
    raise RuntimeError("Bronze source not found (expected bronze.player_swing or bronze.swing).")

def _colref(name: str) -> str:
    n = name.lower()
    return 's."end"' if n == "end" else f"s.{n}"

def _ts_expr(cols: Dict[str, str], ts_col: str, fb_seconds: str) -> str:
    c, fb = ts_col.lower(), fb_seconds.lower()
    if c in cols:
        dt = cols[c]
        if "timestamp" in dt:  return _colref(c)
        if any(k in dt for k in ("double","real","numeric","integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(c)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(CASE WHEN jsonb_typeof({_colref(c)})='number'
                       THEN (TIMESTAMP 'epoch' + ({_colref(c)}::text)::double precision * INTERVAL '1 second')
                       ELSE NULL::timestamptz END)"""
    if fb in cols:
        dt = cols[fb]
        if any(k in dt for k in ("double","real","numeric","integer")):
            return f"(TIMESTAMP 'epoch' + {_colref(fb)} * INTERVAL '1 second')"
        if "json" in dt:
            return f"""(CASE WHEN jsonb_typeof({_colref(fb)})='number'
                       THEN (TIMESTAMP 'epoch' + ({_colref(fb)}::text)::double precision * INTERVAL '1 second')
                       ELSE NULL::timestamptz END)"""
    return "NULL::timestamptz"

def _jsonb(cols: Dict[str, str], name: str) -> str:
    return f"{_colref(name)}::jsonb" if name.lower() in cols else "NULL::jsonb"

def _num(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(CASE WHEN jsonb_typeof({_colref(n)})='number'
                   THEN ({_colref(n)}::text)::double precision
                   ELSE NULL::double precision END)"""
    return _colref(n)

def _int(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::int"
    dt = cols[n]
    if "json" in dt:
        return f"""(CASE
           WHEN jsonb_typeof({_colref(n)})='number' THEN ({_colref(n)}::text)::int
           WHEN jsonb_typeof({_colref(n)})='object' AND ({_colref(n)} ? 'index')
                AND jsonb_typeof({_colref(n)}->'index')='number'
             THEN ({_colref(n)}->>'index')::int
           ELSE NULL::int END)"""
    return _colref(n)

def _bool(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::boolean"

def _text(cols: Dict[str, str], name: str) -> str:
    return _colref(name) if name.lower() in cols else "NULL::text"

# --------------------------------- schema ensure ---------------------------------

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

def ensure_table_exists(conn: Connection):
    _exec(conn, DDL_CREATE_SCHEMA)
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        # Create with phase 1 columns only
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in PHASE1_COLS.items()])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_start ON {SILVER_SCHEMA}.{TABLE}(start_ts);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_hit   ON {SILVER_SCHEMA}.{TABLE}(ball_hit);")

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

# --------------------------------- Phase 1 loader ---------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    src, bcols = _bronze_src(conn)

    created_at = _ts_expr(bcols, "created_at", "created_at")
    start_ts   = _ts_expr(bcols, "start_ts", "start")
    end_ts     = _ts_expr(bcols, "end_ts",   "end")
    ball_hit   = _ts_expr(bcols, "ball_hit", "ball_hit_s")

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, created_at, start_ts, end_ts, player_id, valid, serve, swing_type,
      volley, is_in_rally, ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit, ball_hit_location
    )
    SELECT
      {_text(bcols, "task_id")}::uuid,
      {created_at},
      {start_ts},
      {end_ts},
      {_text(bcols, "player_id")},
      {_bool(bcols, "valid")},
      {_bool(bcols, "serve")},
      {_text(bcols, "swing_type")},
      {_bool(bcols, "volley")},
      {_bool(bcols, "is_in_rally")},
      {_num(bcols, "ball_player_distance")},
      {_num(bcols, "ball_speed")},
      {_text(bcols, "ball_impact_type")},
      {_int(bcols, "rally")},
      {ball_hit},
      {_jsonb(bcols, "ball_hit_location")}
    FROM {src}
    WHERE {_text(bcols, "task_id")}::uuid = :task_id
      AND COALESCE({_bool(bcols, "valid")}, FALSE) IS TRUE;
    """
    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount or 0

# --------------------------------- Phase 2–5 (schema only now) ---------------------------------

def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# --------------------------------- Orchestrator ---------------------------------

def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    if not task_id:
        raise ValueError("task_id is required")
    out: Dict = {"ok": True, "task_id": task_id}

    with engine.begin() as conn:
        ensure_table_exists(conn)
        # Always ensure schema up to selected phase (additive, no drops)
        ensure_phase_columns(conn, PHASE1_COLS)
        if phase in ("all","2","3","4","5"): phase2_add_schema(conn)
        if phase in ("all","3","4","5"):     phase3_add_schema(conn)
        if phase in ("all","4","5"):         phase4_add_schema(conn)
        if phase in ("all","5"):             phase5_add_schema(conn)

        if phase in ("all","1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows"] = phase1_load(conn, task_id)
        # Stubs for later loaders:
        if phase in ("all","2"):
            out["phase2"] = "schema-ready"
        if phase in ("all","3"):
            out["phase3"] = "schema-ready"
        if phase in ("all","4"):
            out["phase4"] = "schema-ready"
        if phase in ("all","5"):
            out["phase5"] = "schema-ready"
    return out

# --------------------------------- CLI ---------------------------------

if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser(description="Silver point_detail — additive phases runner")
    p.add_argument("--task-id", required=True)
    p.add_argument("--phase", choices=["1","2","3","4","5","all"], default="all")
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
