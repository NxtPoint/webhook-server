# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
#
# Phase 1  (BRONZE → SILVER copy):
#   - Pure 1:1 mapping from bronze.player_swing into silver.point_detail
#   - Only rows with valid=TRUE
#   - Timestamps read directly from JSON objects' "timestamp" key
#   - XY read directly from ball_hit_location array [x, y]
#
# Phase 2  (Bounces; pure bronze pull + minimal placement helpers):
#   - For each swing, choose the FIRST bronze.ball_bounce strictly AFTER ball_hit_s
#   - Write: bounce_x_m, bounce_y_m, bounce_type_d, bounce_s
#   - For NON-SERVES ONLY: hit_x_resolved_m and hit_source_d
#       hit_x_resolved_m = bounce_x -> next_contact_x -> ball_hit_x
#       hit_source_d     = floor_bounce | any_bounce | next_contact | ball_hit
#   - Window cap: end at MIN(next_ball_hit_s, ball_hit_s + 2.5)
#
# Phase 3  (Serve-only derived fields):
#   - serve_d: swing_type LIKE '%overhead%' AND |ball_hit_y| ≥ baseline (± tolerance)
#   - serve_side_d: 'deuce'|'ad' from ball_hit_x with small ε around center
#   - server_end_d: 'near'/'far' from sign of ball_hit_y
#   - serve_try_ix_in_point: within each contiguous side segment:
#         side flips present:
#            cnt=1  → that 1 is first-serve in
#            cnt>=2 → last in segment is second-serve in
#         last segment (no flip):
#            cnt in {1,2} → last is decisive (1/2 respectively)
#            cnt > 2      → double_fault_d=TRUE on last (try_ix NULL)
#   - service_winner_d: TRUE on decisive serve when no opponent swing afterwards
#
# Phases 4–5: schema placeholders.
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
    "start_s":              "double precision",
    "end_s":                "double precision",
    "ball_hit_s":           "double precision"
})

# Phase 2 — bounce + minimal placement helpers (additive)
PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "hit_x_resolved_m": "double precision",
    "hit_source_d":     "text",              # floor_bounce | any_bounce | next_contact | ball_hit
    "bounce_x_m":       "double precision",
    "bounce_y_m":       "double precision",
    "bounce_type_d":    "text",
    "bounce_s":         "double precision"
})

# Phase 3 — serve-only (derived)
PHASE3_COLS: TOrderedDict[str, str] = OrderedDict({
    "serve_d":                 "boolean",
    "server_id":               "text",
    "serve_side_d":            "text",
    "serve_try_ix_in_point":   "integer",
    "double_fault_d":          "boolean",
    "service_winner_d":        "boolean",
    "server_end_d":            "text"      # 'near' | 'far'
})

# Phase 4 — serve/point logic (schema only for now)
PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({
    # placeholder
})

# Phase 5 — serve/rally locations (schema only for now)
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({
    # placeholder
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

# --------------------------------- schema ensure ---------------------------------

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"

def ensure_table_exists(conn: Connection):
    _exec(conn, DDL_CREATE_SCHEMA)
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in PHASE1_COLS.items()])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task       ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_swing ON {SILVER_SCHEMA}.{TABLE}(task_id, swing_id);")

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

# --------------------------------- Phase 1 loader (pure bronze copy) ---------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    """
    PHASE 1 — Exact 1:1 copy from bronze.player_swing (valid=TRUE only).
    No derived/surrogate IDs. swing_id is copied if present, else NULL.
    """
    bcols = _columns_types(conn, "bronze", "player_swing")
    has = lambda c: c in bcols

    swing_id_expr = "s.swing_id" if has("swing_id") else "NULL::bigint"

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id, player_id,
      valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit_x, ball_hit_y,
      start_s, end_s, ball_hit_s
    )
    SELECT
      s.task_id::uuid                         AS task_id,
      s.id                                    AS swing_id,      -- ← exact copy from bronze.id
      s.player_id                             AS player_id,
      s.valid                                 AS valid,
      s.serve                                 AS serve,
      s.swing_type                            AS swing_type,
      s.volley                                AS volley,
      s.is_in_rally                           AS is_in_rally,
      s.ball_player_distance::double precision AS ball_player_distance,
      s.ball_speed::double precision           AS ball_speed,
      NULL::text                              AS ball_impact_type,
      s.rally::int                            AS rally,
      s.ball_hit_location_x::double precision AS ball_hit_x,
      s.ball_hit_location_y::double precision AS ball_hit_y,
      s.start_ts::double precision            AS start_s,
      s.end_ts::double precision              AS end_s,
      (s.ball_hit->>'timestamp')::double precision AS ball_hit_s
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) IS TRUE;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


# --------------------------------- Phase 2 updater (pure bounce + helpers) ---------------------------------

# --------------------------------- Phase 2 Helpers ---------------------------------
# These parse numeric / JSON / array values from bronze.ball_bounce (“b” alias)

def _colref_b(name: str) -> str:
    n = name.lower()
    return f'b."end"' if n == "end" else f"b.{n}"

def _sec_b(cols: dict, name: str) -> str:
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

def _num_b(cols: dict, name: str) -> str:
    n = name.lower()
    if n not in cols: return "NULL::double precision"
    dt = cols[n]
    if "json" in dt:
        return f"""(CASE WHEN jsonb_typeof({_colref_b(n)})='number'
                   THEN ({_colref_b(n)}::text)::double precision
                   ELSE NULL::double precision END)"""
    return _colref_b(n)

def _text_b(cols: dict, name: str) -> str:
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

def _bronze_cols(conn: Connection, name: str) -> dict:
    q = """SELECT column_name, data_type
           FROM information_schema.columns
           WHERE table_schema='bronze' AND table_name=:t"""
    rows = conn.execute(text(q), {"t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _bb_src(conn: Connection):
    if _table_exists(conn, "bronze", "ball_bounce"):
        return "bronze.ball_bounce b", _bronze_cols(conn, "ball_bounce")
    raise RuntimeError("Bronze ball_bounce not found.")

def _bounce_time_expr(bcols: dict) -> str:
    for cand in ("timestamp","ts","time_s","bounce_s","t"):
        if cand in bcols:
            return _sec_b(bcols, cand)
    return "NULL::double precision"

def _bounce_x_expr(bcols: dict) -> str:
    exprs = []
    for cand in ("court_x","x","bounce_x","x_center","x_m","x_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            exprs.append(_num_b(bcols, cand))
    for arr in ("court_pos","location","pos"):
        if arr in bcols:
            exprs.append(_xy_from_json_array_b(_colref_b(arr), 0))
    for jcol in ("data","bounce"):
        if jcol in bcols and "json" in bcols[jcol]:
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_x')='number'
                 THEN ({_colref_b(jcol)}::jsonb->>'court_x')::double precision
                 ELSE NULL::double precision
               END)""")
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_pos')='array'
                      AND jsonb_array_length({_colref_b(jcol)}::jsonb->'court_pos')>0
                 THEN (({_colref_b(jcol)}::jsonb->'court_pos')->>0)::double precision
                 ELSE NULL::double precision
               END)""")
    return "COALESCE(" + ", ".join(exprs) + ", NULL::double precision)" if exprs else "NULL::double precision"

def _bounce_y_expr(bcols: dict) -> str:
    exprs = []
    for cand in ("court_y","y","bounce_y","y_center","y_m","y_pos"):
        if cand in bcols and "json" not in bcols[cand]:
            exprs.append(_num_b(bcols, cand))
    for arr in ("court_pos","location","pos"):
        if arr in bcols:
            exprs.append(_xy_from_json_array_b(_colref_b(arr), 1))
    for jcol in ("data","bounce"):
        if jcol in bcols and "json" in bcols[jcol]:
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_y')='number'
                 THEN ({_colref_b(jcol)}::jsonb->>'court_y')::double precision
                 ELSE NULL::double precision
               END)""")
            exprs.append(f"""
              (CASE
                 WHEN jsonb_typeof({_colref_b(jcol)}::jsonb)='object'
                      AND jsonb_typeof({_colref_b(jcol)}::jsonb->'court_pos')='array'
                      AND jsonb_array_length({_colref_b(jcol)}::jsonb->'court_pos')>1
                 THEN (({_colref_b(jcol)}::jsonb->'court_pos')->>1)::double precision
                 ELSE NULL::double precision
               END)""")
    return "COALESCE(" + ", ".join(exprs) + ", NULL::double precision)" if exprs else "NULL::double precision"

def _bounce_type_expr(bcols: dict) -> str:
    for cand in ("bounce_type","type"):
        if cand in bcols:
            return _text_b(bcols, cand)
    return "NULL::text"

def phase2_update(conn: Connection, task_id: str) -> int:
    """
    PHASE 2 — Bounce selection + classic hit resolution (additive, idempotent per task)
    Inputs: silver.point_detail (Phase 1)
            bronze.ball_bounce

    Rule summary
    ------------
    • Per swing, define window: [ball_hit_s + 0.005s, min(next_ball_hit_s, ball_hit_s + 2.5s)].
    • Choose the FIRST bounce within that window, preferring 'floor' over other types:
        ORDER BY (bounce_type='floor') ASC, bounce_s ASC
    • Write bounce coords/time/type for ALL swings (serves too): bounce_x_m, bounce_y_m, bounce_s, bounce_type_d
    • For NON-SERVE swings only:
        hit_x_resolved_m := bounce_x  → next_contact_x → own ball_hit_x
        hit_source_d     := floor_bounce | any_bounce | next_contact | ball_hit
    • Everything scoped to SAME task_id.
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
        LEAD(p0.ball_hit_s) OVER (
          PARTITION BY p0.task_id, p0.rally
          ORDER BY p0.ball_hit_s, p0.swing_id
        ) AS next_ball_hit_s,
        LEAD(p0.ball_hit_x) OVER (
          PARTITION BY p0.task_id, p0.rally
          ORDER BY p0.ball_hit_s, p0.swing_id
        ) AS next_ball_hit_x
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
          AND b.task_id = p2.task_id         -- keep within task
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

# --------------------------------- Phase 3 updater (serve logic) ---------------------------------

# Court constants (meters) & tolerances
# Court constants & tolerances
BASELINE_Y_M = 11.885         # baseline distance (m) from net
CENTER_EPS_M = 0.15           # ±X tolerance to disambiguate deuce/ad near middle
BASELINE_EPS_M = 0.25         # tolerance for "at/behind baseline"

def phase3_update(conn: Connection, task_id: str) -> int:
    """
    PHASE 3 — Serve-only features (derived) using only Phase-1/2 data

    Fields written:
      • serve_d (bool)         : our detector — fh_overhead AND |ball_hit_y| >= baseline - eps
      • server_id (text)       : player_id of serve row
      • serve_side_d (text)    : 'deuce'|'ad' from server contact X (normalized around center)
      • serve_try_ix_in_point  : 1|2 (first/second serve in). We only mark the decisive serve.
      • double_fault_d (bool)  : TRUE on last serve if >2 serves on initial side without side flip and rally ends there.
      • service_winner_d (bool): decisive serve where no opponent swing follows in the rally
      • server_end_d (text)    : 'near'|'far' by sign of ball_hit_y (near<0, far>0)
    """
    # Pass A: mark serves + side + server end
    sql_a = f"""
    WITH serves AS (
      SELECT
        p.swing_id,
        p.player_id,
        CASE
          WHEN p.ball_hit_x < -{CENTER_EPS_M} THEN 'deuce'
          WHEN p.ball_hit_x >  {CENTER_EPS_M} THEN 'ad'
          ELSE CASE WHEN p.ball_hit_x >= 0 THEN 'ad' ELSE 'deuce' END
        END AS serve_side_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
        AND lower(coalesce(p.swing_type,'')) LIKE '%overhead%'
        AND ABS(p.ball_hit_y) >= {BASELINE_Y_M - BASELINE_EPS_M}
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d      = TRUE,
      server_id    = s.player_id,
      serve_side_d = s.serve_side_d,
      server_end_d = CASE
                       WHEN p.ball_hit_y >= 1.0 THEN 'far'
                       WHEN p.ball_hit_y <= -1.0 THEN 'near'
                       ELSE NULL
                     END
    FROM serves s
    WHERE p.task_id = :tid
      AND p.swing_id = s.swing_id;
    """

    # Pass B: assign try index (1/2), double fault, and service winner
    sql_b = f"""
    WITH base AS (
      SELECT p.task_id, p.rally, p.swing_id, p.player_id, p.ball_hit_s, p.serve_side_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.valid, FALSE) IS TRUE
        AND COALESCE(p.serve_d, FALSE) IS TRUE
    ),
    rally_min AS (
      SELECT task_id, rally, MIN(ball_hit_s) AS init_t
      FROM base GROUP BY task_id, rally
    ),
    rally_first AS (
      SELECT b.task_id, b.rally, b.serve_side_d AS init_side, b.ball_hit_s AS init_t
      FROM base b
      JOIN rally_min m
        ON m.task_id=b.task_id AND m.rally=b.rally AND b.ball_hit_s=m.init_t
    ),
    first_opposite AS (
      SELECT b1.task_id, b1.rally, MIN(b2.ball_hit_s) AS first_opposite_t
      FROM base b1
      JOIN base b2
        ON b1.task_id=b2.task_id AND b1.rally=b2.rally
       AND b2.serve_side_d <> b1.serve_side_d
      GROUP BY b1.task_id, b1.rally
    ),
    block AS (
      SELECT
        b.task_id, b.rally, b.swing_id, b.player_id, b.ball_hit_s,
        ROW_NUMBER() OVER (PARTITION BY b.task_id, b.rally ORDER BY b.ball_hit_s, b.swing_id) AS rn_in_block,
        COUNT(*)    OVER (PARTITION BY b.task_id, b.rally) AS cnt_in_block
      FROM base b
      JOIN rally_first rf
        ON rf.task_id=b.task_id AND rf.rally=b.rally AND b.serve_side_d=rf.init_side
      LEFT JOIN first_opposite fo
        ON fo.task_id=b.task_id AND fo.rally=b.rally
      WHERE b.ball_hit_s >= rf.init_t
        AND (fo.first_opposite_t IS NULL OR b.ball_hit_s < fo.first_opposite_t)
    ),
    resolve_try AS (
      SELECT
        blk.task_id, blk.rally, blk.swing_id,
        CASE
          WHEN blk.cnt_in_block = 1 THEN 1
          WHEN blk.cnt_in_block = 2 AND blk.rn_in_block = 2 THEN 2
          ELSE NULL
        END AS serve_try_ix_in_point,
        CASE
          WHEN blk.cnt_in_block > 2
           AND blk.rn_in_block = blk.cnt_in_block
           AND NOT EXISTS (
                SELECT 1 FROM first_opposite fo
                WHERE fo.task_id = blk.task_id AND fo.rally = blk.rally
           )
          THEN TRUE ELSE FALSE
        END AS double_fault_d
      FROM block blk
    ),
    decisive AS (
      SELECT r.task_id, r.rally, r.swing_id
      FROM resolve_try r
      WHERE r.serve_try_ix_in_point IN (1,2)
    ),
    decisive_ts AS (
      SELECT d.task_id, d.rally, d.swing_id, p1.player_id, p1.ball_hit_s AS serve_t
      FROM decisive d
      JOIN {SILVER_SCHEMA}.{TABLE} p1
        ON p1.task_id=d.task_id AND p1.swing_id=d.swing_id
    ),
    opp_after AS (
      SELECT
        d.task_id, d.rally, d.swing_id,
        EXISTS (
          SELECT 1 FROM {SILVER_SCHEMA}.{TABLE} b2
          WHERE b2.task_id=d.task_id
            AND b2.rally=d.rally
            AND b2.ball_hit_s > d.serve_t
            AND b2.player_id <> d.player_id
        ) AS opponent_after
      FROM decisive_ts d
    ),
    winners AS (
      SELECT d.task_id, d.rally, d.swing_id,
             (CASE WHEN o.opponent_after IS FALSE THEN TRUE ELSE FALSE END) AS service_winner_d
      FROM decisive_ts d
      LEFT JOIN opp_after o
        ON o.task_id=d.task_id AND o.rally=d.rally AND o.swing_id=d.swing_id
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_try_ix_in_point = r.serve_try_ix_in_point,
      double_fault_d        = r.double_fault_d,
      service_winner_d      = w.service_winner_d
    FROM resolve_try r
    LEFT JOIN winners w
      ON w.task_id=r.task_id AND w.rally=r.rally AND w.swing_id=r.swing_id
    WHERE p.task_id = :tid
      AND p.swing_id = r.swing_id;
    """
    conn.execute(text(sql_a), {"tid": task_id})
    res = conn.execute(text(sql_b), {"tid": task_id})
    return res.rowcount or 0

# --------------------------------- Phase 2–5 (schema only now) ---------------------------------

def phase2_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection):  ensure_phase_columns(conn, PHASE5_COLS)

# --------------------------------- Orchestrator ---------------------------------

def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    """
    Orchestrate the build. Ensures schema for all phases up to `phase`.
    Phase 1 loads rows (replace deletes rows for this task_id first).
    Later phases update only their own columns.
    """
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

        # Phase 1 (copy)
        if phase in ("all","1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows"] = phase1_load(conn, task_id)

        # Phase 2 (bounces + helpers)
        if phase in ("all","2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)

        # Phase 3 (serve logic)
        if phase in ("all","3"):
            out["phase3_rows_updated"] = phase3_update(conn, task_id)

        # Stubs for next phases
        if phase in ("all","2"): out["phase2"] = "done-schema"
        if phase in ("all","3"): out["phase3"] = "done-schema"
        if phase in ("all","4"): out["phase4"] = "schema-ready"
        if phase in ("all","5"): out["phase5"] = "schema-ready"

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
