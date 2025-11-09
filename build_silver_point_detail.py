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



# Phase 3 — serve-only (derived detector, side, try index, DF, service winner)
PHASE3_COLS: TOrderedDict[str, str] = OrderedDict({
    "serve_d":                 "boolean",          # our detector
    "server_id":               "text",             # player_id of server
    "serve_side_d":            "text",             # 'deuce' | 'ad'
    "serve_try_ix_in_point":   "integer",          # 1 | 2 | NULL (DF uses flag below)
    "double_fault_d":          "boolean",          # TRUE on last serve in side-seq when >2 and no side flip
    "service_winner_d":        "boolean"           # TRUE if decisive serve (1/2) and no opponent swing after
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
    """
    Returns a COALESCE(...) string that tries multiple sources for bounce X:
      1) direct numeric columns: court_x, x, bounce_x, x_center(_m), x_m, x_pos
      2) array columns: court_pos[0], location[0], pos[0]
      3) JSON object columns ('data' or 'bounce'): court_x (number) or court_pos[0]
    """
    exprs = []

    # 1) Direct numeric columns
    for cand in ("court_x", "x", "bounce_x", "x_center", "x_center_m", "x_m", "x_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            exprs.append(_num_b(bcols, cand))

    # 2) Arrays stored as columns
    for arr in ("court_pos", "location", "pos"):
        if arr in bcols:
            exprs.append(_xy_from_json_array_b(_colref_b(arr), 0))

    # 3) JSON object columns (common: 'data', sometimes 'bounce')
    for jcol in ("data", "bounce"):
        if jcol in bcols and "json" in bcols[jcol]:
            # court_x as number
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_x')='number'
                 THEN ({_colref_b(jcol)}::jsonb->>'court_x')::double precision
                 ELSE NULL::double precision
               END)""".strip())
            # court_pos array [x,y]
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_pos')='array'
                      AND jsonb_array_length({_colref_b(jcol)}::jsonb->'court_pos')>0
                 THEN (({_colref_b(jcol)}::jsonb->'court_pos')->>0)::double precision
                 ELSE NULL::double precision
               END)""".strip())

    if not exprs:
        return "NULL::double precision"

    return "COALESCE(" + ", ".join(exprs) + ", NULL::double precision)"


def _bounce_y_expr(bcols: Dict[str, str]) -> str:
    """
    Returns a COALESCE(...) string that tries multiple sources for bounce Y:
      1) direct numeric columns: court_y, y, bounce_y, y_center(_m), y_m, y_pos
      2) array columns: court_pos[1], location[1], pos[1]
      3) JSON object columns ('data' or 'bounce'): court_y (number) or court_pos[1]
    """
    exprs = []

    # 1) Direct numeric columns
    for cand in ("court_y", "y", "bounce_y", "y_center", "y_center_m", "y_m", "y_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            exprs.append(_num_b(bcols, cand))

    # 2) Arrays stored as columns
    for arr in ("court_pos", "location", "pos"):
        if arr in bcols:
            exprs.append(_xy_from_json_array_b(_colref_b(arr), 1))

    # 3) JSON object columns (common: 'data', sometimes 'bounce')
    for jcol in ("data", "bounce"):
        if jcol in bcols and "json" in bcols[jcol]:
            # court_y as number
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_y')='number'
                 THEN ({_colref_b(jcol)}::jsonb->>'court_y')::double precision
                 ELSE NULL::double precision
               END)""".strip())
            # court_pos array [x,y]
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_pos')='array'
                      AND jsonb_array_length({_colref_b(jcol)}::jsonb->'court_pos')>1
                 THEN (({_colref_b(jcol)}::jsonb->'court_pos')->>1)::double precision
                 ELSE NULL::double precision
               END)""".strip())

    if not exprs:
        return "NULL::double precision"

    return "COALESCE(" + ", ".join(exprs) + ", NULL::double precision)"

def _bounce_type_expr(bcols: Dict[str, str]) -> str:
    for cand in ("bounce_type", "type"):
        if cand in bcols:
            return _text_b(bcols, cand)
    return "NULL::text"

# --------------------------------- Phase 3 Helpers ---------------------------------
# --- Court constants (meters) & tolerances ---
BASELINE_Y_M = 11.885         # distance from net to baseline
CENTER_EPS_M = 0.15           # small epsilon for near-center side disambiguation
BASELINE_EPS_M = 0.25         # tolerance for "at/behind baseline"

def _sign_expr(col: str) -> str:
    return f"CASE WHEN {col} > 0 THEN 1 WHEN {col} < 0 THEN -1 ELSE 0 END"

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

# ------------------------------------------------------------------------------------------
# PHASE 3 — Serve-only (derived detection, side, try index, double fault, service winner)
#
# Inputs (Phase-1/2 only):
#   silver.point_detail cols: player_id, rally, swing_id, swing_type, ball_hit_s, ball_hit_x, ball_hit_y
#   (We do not rely on SportAI 'serve' or on legal/in bounce-box checks in this phase.)
#
# Rules (your spec):
#   serve_d: swing_type == 'fh_overhead' AND contact at/behind baseline on player's near/far end.
#   serve_side_d: from server contact X using old near/far normalization; use X (avg/median not needed here).
#   try index per rally *and within the initial side sequence only*:
#       - 1 serve on that side before side flips  -> first serve in  (mark 1 on that serve)
#       - 2 serves on that side before side flips -> second serve in (mark 2 on the second)
#       - >2 serves and side never flips          -> double fault (mark DF TRUE on last; try_ix=NULL)
#       - >2 serves and side flips                -> assume let(s); last before flip is decisive with 1 or 2 by order
#   service_winner_d: TRUE on the decisive serve (try_ix in (1,2)) if there is no later opponent swing in the rally.
# ------------------------------------------------------------------------------------------
def phase3_update(conn: Connection, task_id: str) -> int:
    sql = f"""
    WITH base AS (
      SELECT
        p.task_id, p.rally, p.swing_id, p.player_id, p.swing_type,
        p.ball_hit_s, p.ball_hit_x, p.ball_hit_y,
        COALESCE(p.valid, FALSE) AS valid
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
    ),
    -- near/far by player (sign of median Y across the task)
    orient AS (
      SELECT
        b.player_id,
        CASE
          WHEN percentile_cont(0.5) WITHIN GROUP (ORDER BY b.ball_hit_y) > 0 THEN 1
          WHEN percentile_cont(0.5) WITHIN GROUP (ORDER BY b.ball_hit_y) < 0 THEN -1
          ELSE 0
        END AS player_far_sign
      FROM base b
      GROUP BY b.player_id
    ),
    -- derive serve_d per swing (our detector)
    serve_det AS (
      SELECT
        b.*,
        o.player_far_sign,
        CASE
          WHEN b.swing_type = 'fh_overhead'
           AND (
                 (o.player_far_sign =  1 AND b.ball_hit_y >= {BASELINE_Y_M - BASELINE_EPS_M})
              OR (o.player_far_sign = -1 AND b.ball_hit_y <= {-BASELINE_Y_M + BASELINE_EPS_M})
              OR (o.player_far_sign =  0 AND ABS(b.ball_hit_y) >= {BASELINE_Y_M - BASELINE_EPS_M})
               )
          THEN TRUE ELSE FALSE END AS serve_d_raw
      FROM base b
      LEFT JOIN orient o ON o.player_id = b.player_id
    ),
    -- assign side for serve rows (deuce/ad) using contact X; resolve near-center with previous side per server/rally
    serve_rows AS (
      SELECT
        s.task_id, s.rally, s.swing_id, s.player_id, s.ball_hit_s,
        s.serve_d_raw AS serve_d,
        CASE
          WHEN s.serve_d_raw IS TRUE THEN
            CASE
              WHEN s.ball_hit_x < -{CENTER_EPS_M} THEN 'deuce'
              WHEN s.ball_hit_x >  {CENTER_EPS_M} THEN 'ad'
              ELSE NULL  -- to be filled by carry-forward below
            END
          ELSE NULL
        END AS serve_side0
      FROM serve_det s
      WHERE s.serve_d_raw IS TRUE
    ),
    -- carry-forward side within (task_id, rally, player) when near-center (NULL)
    side_ff AS (
      SELECT
        sr.*,
        COALESCE(
          sr.serve_side0,
          LAG(sr.serve_side0) OVER (PARTITION BY sr.task_id, sr.rally, sr.player_id ORDER BY sr.ball_hit_s, sr.swing_id)
        ) AS serve_side_d
      FROM serve_rows sr
    ),
    -- find the time of first serve on the OPPOSITE side in the same rally (side flip marker)
    first_opposite AS (
      SELECT
        s1.task_id, s1.rally,
        MIN(s2.ball_hit_s) AS first_opposite_t
      FROM side_ff s1
      JOIN side_ff s2
        ON s1.task_id = s2.task_id
       AND s1.rally   = s2.rally
       AND s2.serve_side_d IS NOT NULL
       AND s1.serve_side_d IS NOT NULL
       AND s2.serve_side_d <> s1.serve_side_d
      GROUP BY s1.task_id, s1.rally
    ),
    -- Identify the initial side of the rally (side of the first serve in that rally)
    rally_first AS (
      SELECT DISTINCT ON (s.task_id, s.rally)
        s.task_id, s.rally, s.serve_side_d AS init_side, s.ball_hit_s AS init_t
      FROM side_ff s
      ORDER BY s.task_id, s.rally, s.ball_hit_s, s.swing_id
    ),
    -- block: all serves in the rally that are on the initial side and occur before the first opposite-side serve (if any)
    block AS (
      SELECT
        s.task_id, s.rally, s.swing_id, s.player_id, s.ball_hit_s, s.serve_side_d,
        ROW_NUMBER() OVER (PARTITION BY s.task_id, s.rally ORDER BY s.ball_hit_s, s.swing_id) AS rn_in_block,
        COUNT(*)    OVER (PARTITION BY s.task_id, s.rally) AS cnt_in_block
      FROM side_ff s
      JOIN rally_first rf
        ON rf.task_id = s.task_id AND rf.rally = s.rally AND s.serve_side_d = rf.init_side
      LEFT JOIN first_opposite fo
        ON fo.task_id = s.task_id AND fo.rally = s.rally
      WHERE s.ball_hit_s >= rf.init_t
        AND (fo.first_opposite_t IS NULL OR s.ball_hit_s < fo.first_opposite_t)
    ),
    -- decide try index and DF per block
    resolve_try AS (
      SELECT
        b.task_id, b.rally, b.swing_id, b.player_id, b.serve_side_d,
        CASE
          WHEN b.cnt_in_block = 1 THEN 1
          WHEN b.cnt_in_block = 2 AND b.rn_in_block = 2 THEN 2
          ELSE NULL
        END AS serve_try_ix_in_point,
        CASE
          WHEN b.cnt_in_block > 2
           AND b.rn_in_block = b.cnt_in_block
           AND NOT EXISTS (
                SELECT 1 FROM first_opposite fo
                WHERE fo.task_id = b.task_id AND fo.rally = b.rally
           )
          THEN TRUE
          ELSE FALSE
        END AS double_fault_d
      FROM block b
    ),
    -- find opponent swing existence after decisive serve for service_winner
    decisive AS (
      SELECT r.*
      FROM resolve_try r
      WHERE r.serve_try_ix_in_point IN (1,2)
    ),
    opp_after AS (
      SELECT
        d.task_id, d.rally, d.swing_id,
        EXISTS (
          SELECT 1
          FROM base b2
          WHERE b2.task_id = d.task_id
            AND b2.rally   = d.rally
            AND b2.ball_hit_s > (SELECT b1.ball_hit_s FROM base b1 WHERE b1.swing_id = d.swing_id)
            AND b2.player_id <> d.player_id
        ) AS opponent_after
      FROM decisive d
    ),
    winners AS (
      SELECT
        d.task_id, d.rally, d.swing_id,
        CASE WHEN o.opponent_after IS FALSE THEN TRUE ELSE FALSE END AS service_winner_d
      FROM decisive d
      LEFT JOIN opp_after o
        ON o.task_id = d.task_id AND o.rally = d.rally AND o.swing_id = d.swing_id
    ),
    -- union of all serve rows to be written (both block and out-of-block serves)
    all_serves AS (
      SELECT
        s.task_id, s.rally, s.swing_id, s.player_id, s.serve_side_d
      FROM side_ff s
    ),
    final as (
      SELECT
        a.task_id, a.rally, a.swing_id, a.player_id, a.serve_side_d,
        COALESCE(rt.serve_try_ix_in_point, NULL) AS serve_try_ix_in_point,
        COALESCE(rt.double_fault_d, FALSE)        AS double_fault_d,
        COALESCE(w.service_winner_d, NULL)        AS service_winner_d
      FROM all_serves a
      LEFT JOIN resolve_try rt
        ON rt.task_id=a.task_id AND rt.rally=a.rally AND rt.swing_id=a.swing_id
      LEFT JOIN winners w
        ON w.task_id=a.task_id AND w.rally=a.rally AND w.swing_id=a.swing_id
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d               = TRUE,
      server_id             = f.player_id,
      serve_side_d          = f.serve_side_d,
      serve_try_ix_in_point = f.serve_try_ix_in_point,
      double_fault_d        = f.double_fault_d,
      service_winner_d      = f.service_winner_d
    FROM final f
    WHERE p.task_id = :tid
      AND p.rally   = f.rally
      AND p.swing_id= f.swing_id;

    -- For non-serve rows, ensure serve_* are NULL/FALSE as appropriate (no destructive changes to Phase 1/2)
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

        # Phase 3 updater (serve-only)
        if phase in ("all","3"):
            # ensure schema for phase 3 columns
            phase3_add_schema(conn)
            out["phase3_rows_updated"] = phase3_update(conn, task_id)

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
