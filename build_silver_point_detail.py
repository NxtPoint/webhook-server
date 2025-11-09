# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
# Phase 1: Bronze -> Silver (Section 1 only; valid=TRUE), including swing_id
# Phase 2–5: schema placeholders (loaders/derivers added later)
#
# Usage:
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase all
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase 1

from typing import Dict, Optional, Tuple, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Phase specs (schema only) -------------------------------
# Phase 1 — Section 1 (exactly from your sheet; plus swing_id)
PHASE1_COLS = OrderedDict({
    "task_id":              "uuid",
    "swing_id":             "bigint",
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
    "ball_hit_x":           "double precision",
    "ball_hit_y":           "double precision",
    "start_s":              "double precision",   # ← add
    "end_s":                "double precision",   # ← add
    "ball_hit_s":           "double precision"    # ← add
})

# Phase 2 — classic hit resolution + chosen bounce coords/details (additive)
PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "hit_x_resolved_m": "double precision",  # non-serve resolved X
    "hit_source_d":     "text",              # floor_bounce | any_bounce | next_contact | ball_hit
    "bounce_x_m":       "double precision",  # chosen bounce X (any swing)
    "bounce_y_m":       "double precision",  # chosen bounce Y (any swing)
    "bounce_type_d":    "text",              # 'floor', 'swing', etc. from bronze
    "bounce_s":         "double precision"   # chosen bounce timestamp (seconds)
})



# Phase 3 — ball_bounce join (schema only for now)
PHASE3_COLS: TOrderedDict[str, str] = OrderedDict({
    # e.g. "bounce_id": "bigint", "bounce_ts": "timestamptz", "bounce_x_m": "double precision", "bounce_y_m": "double precision",
})

# Phase 4 — serve/point logic (schema only for now)
PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({
    # e.g. "server_id":"text","serving_side":"text","point_number":"integer","game_number":"integer","point_in_game":"integer","shot_ix":"integer",
})

# Phase 5 — serve/rally locations (schema only for now)
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({
    # e.g. "play_d":"text","serve_try_ix_in_point":"integer","first_rally_shot_ix":"integer",
})

PHASE_COLSETS = [PHASE1_COLS, PHASE2_COLS, PHASE3_COLS, PHASE4_COLS, PHASE5_COLS]

# --------------------------------- helpers ---------------------------------
# -----------Phase 1 Helpers ----------------------------

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

def _sec(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols:
        return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(
          CASE
            WHEN jsonb_typeof({_colref(n)}::jsonb)='object'
                 AND ({_colref(n)}::jsonb ? 'timestamp')
                 AND jsonb_typeof(({_colref(n)}::jsonb)->'timestamp')='number'
              THEN (({_colref(n)}::jsonb)->>'timestamp')::double precision
            WHEN jsonb_typeof({_colref(n)}::jsonb)='number'
              THEN ({_colref(n)}::text)::double precision
            ELSE NULL::double precision
          END)"""
    if any(k in dt for k in ("double","real","numeric","integer")):
        return _colref(n)
    return "NULL::double precision"

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

def _swing_id_expr(cols: Dict[str, str]) -> str:
    """Return an expression for swing_id from Bronze, raising if missing."""
    if "id" in cols:
        return _int(cols, "id")
    if "swing_id" in cols:
        return _int(cols, "swing_id")
    raise RuntimeError("Bronze swing identifier not found (need column 'id' or 'swing_id').")

def _xy_from_json_array(colref: str, index: int) -> str:
    return f"""(
      CASE
        WHEN {colref} IS NOT NULL
         AND jsonb_typeof({colref}::jsonb)='array'
         AND jsonb_array_length({colref}::jsonb) > {index}
        THEN ({colref}::jsonb->>{index})::double precision
        ELSE NULL::double precision
      END)"""

def _ball_hit_x_expr(cols: Dict[str, str]) -> str:
    if "ball_hit_x" in cols and "json" not in cols["ball_hit_x"]:
        return _num(cols, "ball_hit_x")
    return _xy_from_json_array(_colref("ball_hit_location"), 0)

def _ball_hit_y_expr(cols: Dict[str, str]) -> str:
    if "ball_hit_y" in cols and "json" not in cols["ball_hit_y"]:
        return _num(cols, "ball_hit_y")
    return _xy_from_json_array(_colref("ball_hit_location"), 1)
    

# -----------Phase 2 Helpers ----------------------------

# --- Helpers that reference alias `b` (for bronze.ball_bounce) -----------------
def _colref_b(name: str) -> str:
    n = name.lower()
    return 'b."end"' if n == "end" else f"b.{n}"

def _sec_b(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols:
        return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(
          CASE
            WHEN jsonb_typeof({_colref_b(n)}::jsonb)='object'
                 AND ({_colref_b(n)}::jsonb ? 'timestamp')
                 AND jsonb_typeof(({_colref_b(n)}::jsonb)->'timestamp')='number'
              THEN (({_colref_b(n)}::jsonb)->>'timestamp')::double precision
            WHEN jsonb_typeof({_colref_b(n)}::jsonb)='number'
              THEN ({_colref_b(n)}::text)::double precision
            ELSE NULL::double precision
          END)"""
    if any(k in dt for k in ("double","real","numeric","integer")):
        return _colref_b(n)
    return "NULL::double precision"

def _num_b(cols: Dict[str, str], name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(CASE WHEN jsonb_typeof({_colref_b(n)})='number'
                   THEN ({_colref_b(n)}::text)::double precision
                   ELSE NULL::double precision END)"""
    return _colref_b(n)

def _text_b(cols: Dict[str, str], name: str) -> str:
    return _colref_b(name) if name.lower() in cols else "NULL::text"

def _xy_from_json_array_b(colref: str, index: int) -> str:
    return f"""(
      CASE
        WHEN {colref} IS NOT NULL
         AND jsonb_typeof({colref}::jsonb)='array'
         AND jsonb_array_length({colref}::jsonb) > {index}
        THEN ({colref}::jsonb->>{index})::double precision
        ELSE NULL::double precision
      END)"""

def _bronze_cols(conn: Connection, name: str) -> Dict[str, str]:
    return _columns_types(conn, "bronze", name)

def _bb_src(conn: Connection) -> Tuple[str, Dict[str, str]]:
    if _table_exists(conn, "bronze", "ball_bounce"):
        return "bronze.ball_bounce b", _bronze_cols(conn, "ball_bounce")
    raise RuntimeError("Bronze ball_bounce not found.")

def _bounce_time_expr(bcols: Dict[str, str]) -> str:
    # Prefer explicit second-ish fields if present; otherwise parse json 'timestamp'
    for cand in ("timestamp","ts","time_s","bounce_s","t"):
        if cand in bcols:
            return _sec_b(bcols, cand)
    # Fallback: NULL
    return "NULL::double precision"

def _bounce_x_expr(bcols: Dict[str, str]) -> str:
    # Prefer explicit court-space X first
    for cand in ("court_x", "x", "bounce_x", "x_center", "x_center_m", "x_m", "x_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            return _num_b(bcols, cand)
    # Array fields
    for arr in ("court_pos", "location", "pos"):
        if arr in bcols:
            return _xy_from_json_array_b(_colref_b(arr), 0)
    return "NULL::double precision"

def _bounce_y_expr(bcols: Dict[str, str]) -> str:
    # Prefer explicit court-space Y first
    for cand in ("court_y", "y", "bounce_y", "y_center", "y_center_m", "y_m", "y_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            return _num_b(bcols, cand)
    # Array fields
    for arr in ("court_pos", "location", "pos"):
        if arr in bcols:
            return _xy_from_json_array_b(_colref_b(arr), 1)
    return "NULL::double precision"


def _bounce_type_expr(bcols: Dict[str, str]) -> str:
    for cand in ("bounce_type", "type"):
        if cand in bcols:
            return _text_b(bcols, cand)
    return "NULL::text"

# --------------------------------- schema ensure ---------------------------------

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

def ensure_table_exists(conn: Connection):
    _exec(conn, DDL_CREATE_SCHEMA)
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in PHASE1_COLS.items()])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        # Core indexes (no time-based indexes since those columns were dropped)
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task       ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_swing ON {SILVER_SCHEMA}.{TABLE}(task_id, swing_id);")

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

# --------------------------------- Phase 1 loader ---------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    """
    Import exactly the Section 1 fields from Bronze (valid=TRUE only).
    """
    src, bcols = _bronze_src(conn)
    swing_id = _swing_id_expr(bcols)
    bhx      = _ball_hit_x_expr(bcols)
    bhy      = _ball_hit_y_expr(bcols)

    start_s    = f"COALESCE({_sec(bcols,'start_ts')}, {_sec(bcols,'start')})"
    end_s      = f"COALESCE({_sec(bcols,'end_ts')},   {_sec(bcols,'end')})"
    ball_hit_s = f"COALESCE({_sec(bcols,'ball_hit')}, {_sec(bcols,'ball_hit_s')})"

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id, player_id,
      valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit_x, ball_hit_y,
      start_s, end_s, ball_hit_s
    )
    SELECT
      {_text(bcols, "task_id")}::uuid,
      {swing_id},
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
      {bhx},
      {bhy},
      {start_s},
      {end_s},
      {ball_hit_s}
    FROM {src}
    WHERE {_text(bcols, "task_id")}::uuid = :task_id
      AND COALESCE({_bool(bcols, "valid")}, FALSE) IS TRUE;
    """

    res = conn.execute(text(sql), {"task_id": task_id})
    return res.rowcount or 0

# ------------------------------------------------------------------------------------------
# Phase 2 — Business rules (classic placement logic)
#
# For each NON-SERVE swing in silver.point_detail (valid=TRUE):
# 1) Build a time window from ball_hit_s:
#       start = ball_hit_s + 0.005s
#       end   = LEAST( next_ball_hit_s, ball_hit_s + 2.5s )   // if next_ball_hit_s is NULL, use ball_hit_s + 2.5s
# 2) From bronze.ball_bounce pick the FIRST bounce in that window, prioritizing floor:
#       ORDER BY (bounce_type='floor') ASC, bounce_s ASC
#    i.e., any floor bounce wins ties; otherwise the earliest bounce of any type.
# 3) Resolve hit_x_resolved_m via fallback chain:
#       terminal-in-point → still the same rule as (2); if none → next_contact_x → own ball_hit_x
#       non-terminal     → floor_bounce_x → any_bounce_x → next_contact_x → own ball_hit_x
# 4) Set hit_source_d to: floor_bounce | any_bounce | next_contact | ball_hit
#
# Notes:
# - We do NOT compute serve location zones here (that arrives in Phases 4–5).
# - Placement bucketing and far/near mirroring are deferred to a later phase.
# - Phase 2 is additive and only updates the new Phase-2 columns for the chosen task_id.

# ------------------------------------------------------------------------------------------
# PHASE 2 — Ball-Hit Location (Classic) + Bounce Coordinates
#
# Window per swing: [ball_hit_s+0.005, min(next_ball_hit_s, ball_hit_s+2.5)]
# Choose first bounce in window, preferring floor (floor-first, then earliest).
#
# Writes:
#   - bounce_x_m, bounce_y_m for ALL swings (including serves).
#   - For NON-SERVES only:
#       hit_x_resolved_m = bounce_x → next_contact_x → ball_hit_x
#       hit_source_d     = floor_bounce | any_bounce | next_contact | ball_hit
#
# Additive, idempotent per task_id. Phase-1 data never overwritten.
# ------------------------------------------------------------------------------------------

def phase2_update(conn: Connection, task_id: str) -> int:
    """
    PHASE 2 — Classic placement + expose chosen bounce coords/type/time.

    Window per swing: [ball_hit_s+0.005, min(next_ball_hit_s, ball_hit_s+2.5)]
    Pick first bounce in window (prefer floor).
    Write:
      - bounce_x_m, bounce_y_m, bounce_type_d, bounce_s for ALL swings.
      - For NON-SERVES:
          hit_x_resolved_m = bounce_x -> next_contact_x -> ball_hit_x
          hit_source_d     = floor_bounce | any_bounce | next_contact | ball_hit
    """
    bb_src, bcols = _bb_src(conn)
    bounce_s   = _bounce_time_expr(bcols)
    bounce_x   = _bounce_x_expr(bcols)
    bounce_y   = _bounce_y_expr(bcols)
    bounce_typ = _bounce_type_expr(bcols)

    sql = f"""
    WITH p0 AS (
      SELECT
        p.task_id, p.swing_id, p.player_id, p.rally,
        COALESCE(p.valid, FALSE) AS valid,
        COALESCE(p.serve, FALSE) AS serve,
        p.ball_hit_s, p.ball_hit_x
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
    ),
    p1 AS (
      SELECT
        p0.*,
        LEAD(p0.ball_hit_s) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_s,
        LEAD(p0.ball_hit_x) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_x
      FROM p0
    ),
    p2 AS (
      SELECT
        p1.*,
        (p1.ball_hit_s + 0.005) AS win_start,
        LEAST(COALESCE(p1.next_ball_hit_s, p1.ball_hit_s + 2.5), p1.ball_hit_s + 2.5) AS win_end
      FROM p1
    ),
    chosen AS (
      SELECT
        p2.swing_id,
        pick.bounce_x,
        pick.bounce_y,
        pick.bounce_type,
        pick.bounce_s
      FROM p2
      LEFT JOIN LATERAL (
        SELECT
          {bounce_x}   AS bounce_x,
          {bounce_y}   AS bounce_y,
          {bounce_typ} AS bounce_type,
          {bounce_s}   AS bounce_s
        FROM {bb_src}
        WHERE {bounce_s} IS NOT NULL
          AND {bounce_s} >  p2.win_start
          AND {bounce_s} <= p2.win_end
        ORDER BY
          CASE WHEN {bounce_typ} = 'floor' THEN 0 ELSE 1 END,
          {bounce_s}
        LIMIT 1
      ) AS pick ON TRUE
    ),
    resolved AS (
      SELECT
        p2.swing_id,
        p2.serve,
        p2.next_ball_hit_x,
        p2.ball_hit_x,
        chosen.bounce_x,
        chosen.bounce_y,
        chosen.bounce_type,
        chosen.bounce_s
      FROM p2
      LEFT JOIN chosen ON chosen.swing_id = p2.swing_id
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      bounce_x_m    = r.bounce_x,
      bounce_y_m    = r.bounce_y,
      bounce_type_d = r.bounce_type,
      bounce_s      = r.bounce_s,
      hit_x_resolved_m = CASE
        WHEN p.serve IS FALSE THEN COALESCE(r.bounce_x, r.next_ball_hit_x, r.ball_hit_x)
        ELSE p.hit_x_resolved_m
      END,
      hit_source_d = CASE
        WHEN p.serve IS FALSE THEN
          CASE
            WHEN r.bounce_x IS NOT NULL AND r.bounce_type = 'floor' THEN 'floor_bounce'
            WHEN r.bounce_x IS NOT NULL THEN 'any_bounce'
            WHEN r.next_ball_hit_x IS NOT NULL THEN 'next_contact'
            ELSE 'ball_hit'
          END
        ELSE p.hit_source_d
      END
    FROM resolved r
    WHERE p.task_id = :tid
      AND p.swing_id = r.swing_id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


# --------------------------------- Phase 2–5 (schema only now) ---------------------------------

def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# --------------------------------- Orchestrator ---------------------------------

def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    """
    Orchestrate the build. Always ensures schema for all phases up to `phase`.
    Phase 1 loads rows (replace deletes rows for this task_id first).
    Later phases currently only ensure columns (ready for their loaders).
    """
    if not task_id:
        raise ValueError("task_id is required")
    out: Dict = {"ok": True, "task_id": task_id, "phase": phase}

    with engine.begin() as conn:
        # Ensure table exists
        ensure_table_exists(conn)
        # Ensure schema up to selected phase (additive, no drops)
        ensure_phase_columns(conn, PHASE1_COLS)
        if phase in ("all","2","3","4","5"): phase2_add_schema(conn)
        if phase in ("all","3","4","5"):     phase3_add_schema(conn)
        if phase in ("all","4","5"):         phase4_add_schema(conn)
        if phase in ("all","5"):             phase5_add_schema(conn)

        # Phase 1 load
        if phase in ("all","1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows"] = phase1_load(conn, task_id)

        # Phase 2 updater (classic ball-hit resolution)
        if phase in ("all","2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)


        # Stubs for next phases (fill in later with loaders/updaters)
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
    p = argparse.ArgumentParser(description="Silver point_detail — additive phases, single entrypoint")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--phase", choices=["1","2","3","4","5","all"], default="all", help="which phase(s) to run")
    p.add_argument("--replace", action="store_true", help="delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
