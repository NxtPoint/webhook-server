# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
#
# PHASE 1  (BRONZE → SILVER copy; 1:1):
#   • Source: bronze.player_swing
#   • Exact field copy for Section-1 columns (valid=TRUE only)
#   • ball_hit_x / ball_hit_y from ball_hit_location_x/y OR parsed from ball_hit_location [x,y]
#   • ball_hit_s from ball_hit.timestamp (json), else numeric/text cast
#   • rally cast to int when integer-like
#
# PHASE 2  (BRONZE → SILVER bounces; minimal placement helpers):
#   • Source: bronze.ball_bounce
#   • For each swing: pick FIRST bounce strictly after ball_hit_s within window
#       window = [ball_hit_s+0.005, min(next_ball_hit_s, ball_hit_s+2.5)]
#     Prefer bounce_type='floor' in ties
#   • Write: bounce_x_m, bounce_y_m, bounce_type_d, bounce_s
#   • For NON-SERVES: hit_x_resolved_m and hit_source_d (floor_bounce | any_bounce | next_contact | ball_hit)
#
# PHASE 3  (Serve only — derived from Phase 1/2 data; your thresholds):
#   • serve_d: swing_type LIKE '%overhead%' AND (ball_hit_y <= 1.0 OR ball_hit_y >= 23.0)
#   • server_end_d: near if y <= 1.0, far if y >= 23.0
#   • serve_side_d:
#       - if near: x >= 4.0 → 'ad' else 'deuce'
#       - if  far: x <= -4.0 → 'deuce' else 'ad'
#   • serve_try_ix_in_point / double_fault_d / service_winner_d:
#       - consider contiguous serves on the initial side (before side flips)
#       - cnt=1 → mark 1 on that row
#       - cnt=2 → mark 2 on the second
#       - cnt>2 → mark double_fault_d=TRUE on the last (try_ix=NULL)
#       - service_winner_d TRUE on decisive serve (try_ix in (1,2)) if no later opponent swing
#
# PHASES 4–5: schema placeholders.
#
# Usage:
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase 1
#   python build_silver_point_detail.py --task-id <UUID> --phase 2
#   python build_silver_point_detail.py --task-id <UUID> --phase 3
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase all

from typing import Dict, Optional, Tuple, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"

# ------------------------------- Column specs -------------------------------

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

PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "hit_x_resolved_m": "double precision",
    "hit_source_d":     "text",
    "bounce_x_m":       "double precision",
    "bounce_y_m":       "double precision",
    "bounce_type_d":    "text",
    "bounce_s":         "double precision"
})

PHASE3_COLS = OrderedDict({
    "serve_d":               "boolean",
    "server_id":             "text",
    "serve_side_d":          "text",
    "serve_try_ix_in_point": "integer",
    "double_fault_d":        "boolean",
    "service_winner_d":      "boolean",
    "server_end_d":          "text"
})


PHASE4_COLS: TOrderedDict[str, str] = OrderedDict({})
PHASE5_COLS: TOrderedDict[str, str] = OrderedDict({})

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

# ------------------------------- schema ensure -------------------------------

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

# ------------------------------- PHASE 1 — loader (pure 1:1) -------------------------------

# ---------- PHASE 1 (pure 1:1 from bronze.player_swing; safe JSON extraction) ----------
def phase1_load(conn: Connection, task_id: str) -> int:
    """
    PHASE 1 — Exact copy from bronze.player_swing (valid=TRUE).
    Notes:
      - ball_hit_location is a JSON array [x, y] → extract with ->> 0/1 then cast
      - ball_hit is a JSON object with "timestamp" (number) → extract with ->> 'timestamp' then cast
      - Nothing else is derived. swing_id = bronze.id (verbatim)
    """
    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      task_id, swing_id, player_id,
      valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      rally, ball_hit_x, ball_hit_y,
      start_s, end_s, ball_hit_s
    )
    SELECT
      s.task_id::uuid                                        AS task_id,
      s.id::bigint                                           AS swing_id,         -- exact copy
      s.player_id                                            AS player_id,
      s.valid                                                AS valid,
      s.serve                                                AS serve,
      s.swing_type                                           AS swing_type,
      s.volley                                               AS volley,
      s.is_in_rally                                          AS is_in_rally,
      s.ball_player_distance::double precision               AS ball_player_distance,
      s.ball_speed::double precision                         AS ball_speed,
      NULL::text                                             AS ball_impact_type,
      s.rally::int                                           AS rally,

      /* ball_hit_x */
      COALESCE(
        NULLIF(s.ball_hit_location_x::text, '')::double precision,
        CASE
          WHEN s.ball_hit_location IS NOT NULL
           AND jsonb_typeof(s.ball_hit_location::jsonb) = 'array'
           AND jsonb_array_length(s.ball_hit_location::jsonb) > 0
          THEN (s.ball_hit_location::jsonb ->> 0)::double precision
          ELSE NULL
        END
      ) AS ball_hit_x,

      /* ball_hit_y */
      COALESCE(
        NULLIF(s.ball_hit_location_y::text, '')::double precision,
        CASE
          WHEN s.ball_hit_location IS NOT NULL
           AND jsonb_typeof(s.ball_hit_location::jsonb) = 'array'
           AND jsonb_array_length(s.ball_hit_location::jsonb) > 1
          THEN (s.ball_hit_location::jsonb ->> 1)::double precision
          ELSE NULL
        END
      ) AS ball_hit_y,

      /* seconds fields */
      s.start_ts::double precision                           AS start_s,
      s.end_ts::double precision                             AS end_s,
      CASE
        WHEN s.ball_hit IS NOT NULL
         AND jsonb_typeof(s.ball_hit::jsonb) = 'object'
         AND (s.ball_hit::jsonb ? 'timestamp')
         AND jsonb_typeof(s.ball_hit::jsonb->'timestamp') = 'number'
        THEN (s.ball_hit::jsonb ->> 'timestamp')::double precision
        ELSE NULL::double precision
      END                                                    AS ball_hit_s
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) IS TRUE;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


# ------------------------------- PHASE 2 — updater (bounces + helpers) -------------------------------

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
    rows = conn.execute(text("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema='bronze' AND table_name=:t
    """), {"t": name}).fetchall()
    return {r[0].lower(): r[1].lower() for r in rows}

def _bb_src(conn: Connection):
    if _table_exists(conn, "bronze", "ball_bounce"):
        return "bronze.ball_bounce b", _bronze_cols(conn, "ball_bounce")
    raise RuntimeError("Bronze ball_bounce not found.")

def _bounce_time_expr(bcols: dict) -> str:
    for cand in ("timestamp","ts","time_s","bounce_s","t"):
        if cand in bcols:
            return _sec_b(bcols, cand)
    if "data" in bcols and "json" in bcols["data"]:
        return f"""
          (CASE
             WHEN jsonb_typeof({_colref_b('data')}::jsonb)='object'
                  AND jsonb_typeof(({_colref_b('data')}::jsonb)->'timestamp')='number'
               THEN (({_colref_b('data')}::jsonb)->>'timestamp')::double precision
             ELSE NULL::double precision
           END)
        """.strip()
    return "NULL::double precision"

def _bounce_x_expr(bcols: dict) -> str:
    exprs = []
    for cand in ("court_x","x","bounce_x","x_center","x_center_m","x_m","x_pos"):
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
    for cand in ("court_y","y","bounce_y","y_center","y_center_m","y_m","y_pos"):
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
    if "data" in bcols and "json" in bcols["data"]:
        return f"""
          (CASE
             WHEN jsonb_typeof({_colref_b('data')}::jsonb)='object'
                  AND jsonb_typeof(({_colref_b('data')}::jsonb)->'type')='string'
               THEN ({_colref_b('data')}::jsonb->>'type')
             ELSE NULL::text
           END)
        """.strip()
    return "NULL::text"

def phase2_update(conn: Connection, task_id: str) -> int:
    """
    PHASE 2 — For each swing, choose the FIRST bounce strictly AFTER ball_hit_s,
    bounded to [ball_hit_s+0.005, min(next_ball_hit_s, ball_hit_s+2.5)].
    Write: bounce_x_m, bounce_y_m, bounce_type_d, bounce_s for ALL swings.
    For NON-SERVES only, also write hit_x_resolved_m and hit_source_d.
    """
    # Bronze ball_bounce columns we can rely on:
    #   task_id, timestamp (json->'timestamp') OR bounce_s,
    #   court_x / court_y OR data->court_pos / data->court_x / data->court_y,
    #   bounce_type (or type)
    bb_cols = _columns_types(conn, "bronze", "ball_bounce")
    if not bb_cols:
        raise RuntimeError("bronze.ball_bounce not found")

    # Build time/x/y/type expressions tolerant to schema variants
    bounce_s   = _bounce_time_expr(bb_cols)
    bounce_x   = _bounce_x_expr(bb_cols)
    bounce_y   = _bounce_y_expr(bb_cols)
    bounce_typ = _bounce_type_expr(bb_cols)

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
        FROM bronze.ball_bounce b
        WHERE b.task_id::uuid = p2.task_id
          AND {bounce_s} IS NOT NULL
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


# ------------------------------- PHASE 3 — updater (serve-only) -------------------------------

# Your thresholds:
Y_NEAR_MAX = 1.0     # y <= 1.0 → near end
Y_FAR_MIN  = 23.0    # y >= 23.0 → far end
X_SIDE_ABS = 4.0     # x threshold to classify ad/deuce given end

def phase3_update(conn: Connection, task_id: str) -> int:
    """
    Phase 3 — Serve-only fields using Phase 1/2 data only (sheet-spec):
      serve_d: fh_overhead AND (ball_hit_y < 1 OR ball_hit_y > 23)
      server_end_d: CASE ball_hit_y < 1 → 'far' ELSE 'near'
      serve_side_d: near:  x>4 → deuce else ad ;  far: x<4 → deuce else ad
      serve_try_ix_in_point:
          cnt=1 → 1
          cnt>=2 → 2
          cnt>2 AND no opponent swing after last serve → double_fault_d=TRUE, try_ix=NULL
      service_winner_d: decisive serve (1/2, not DF) AND no opponent swing after that serve
    """
    sql = f"""
    -- Pass A: mark serves + server_end + side + server_id
    WITH serves_a AS (
      SELECT
        p.task_id, p.rally, p.swing_id, p.player_id,
        p.ball_hit_s, p.ball_hit_x, p.ball_hit_y,
        CASE WHEN lower(coalesce(p.swing_type,'')) LIKE '%overhead%'
               AND (p.ball_hit_y < 1 OR p.ball_hit_y > 23)
             THEN TRUE ELSE FALSE END AS is_serve,
        CASE WHEN p.ball_hit_y < 1 THEN 'far' ELSE 'near' END AS server_end_d,
        CASE
          WHEN p.ball_hit_y < 1 THEN  -- far
            CASE WHEN p.ball_hit_x < 4 THEN 'deuce' ELSE 'ad' END
          ELSE                         -- near
            CASE WHEN p.ball_hit_x > 4 THEN 'deuce' ELSE 'ad' END
        END AS serve_side_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid AND COALESCE(p.valid, FALSE) IS TRUE
    ),
    apply_a AS (
      UPDATE {SILVER_SCHEMA}.{TABLE} p
      SET
        serve_d      = s.is_serve,
        server_id    = CASE WHEN s.is_serve THEN s.player_id ELSE p.server_id END,
        server_end_d = CASE WHEN s.is_serve THEN s.server_end_d ELSE p.server_end_d END,
        serve_side_d = CASE WHEN s.is_serve THEN s.serve_side_d ELSE p.serve_side_d END
      FROM serves_a s
      WHERE p.task_id = s.task_id AND p.swing_id = s.swing_id
      RETURNING 1
    ),

    -- Pass B: restrict to rows detected as serves
    base AS (
      SELECT p.task_id, p.rally, p.swing_id, p.player_id, p.ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid AND COALESCE(p.serve_d, FALSE) IS TRUE
    ),

    -- Count serves in each rally & find last-serve time
    rally_counts AS (
      SELECT
        task_id, rally,
        COUNT(*) AS serve_cnt,
        MAX(ball_hit_s) AS last_serve_t
      FROM base
      GROUP BY task_id, rally
    ),

    -- For decisive try (1 or 2): choose by serve_cnt; for >2 we will decide DF later
    try_assign AS (
      SELECT
        b.task_id, b.rally, b.swing_id, b.player_id, b.ball_hit_s,
        rc.serve_cnt,
        CASE
          WHEN rc.serve_cnt = 1 THEN 1
          WHEN rc.serve_cnt >= 2 THEN 2
          ELSE NULL
        END AS try_ix,
        rc.last_serve_t
      FROM base b
      JOIN rally_counts rc USING (task_id, rally)
    ),

    -- Check if any opponent swing occurred after the last serve in each rally
    opp_after_last AS (
      SELECT
        rc.task_id, rc.rally,
        EXISTS (
          SELECT 1
          FROM {SILVER_SCHEMA}.{TABLE} q
          WHERE q.task_id = rc.task_id
            AND q.rally   = rc.rally
            AND q.ball_hit_s > rc.last_serve_t
            AND q.player_id <> (
              SELECT player_id FROM base b2
              WHERE b2.task_id = rc.task_id AND b2.rally = rc.rally
              ORDER BY b2.ball_hit_s ASC LIMIT 1
            )
        ) AS opponent_after_last
      FROM rally_counts rc
    ),

    -- Double fault rule: >2 serves AND no opponent swing after last serve
    df_flags AS (
      SELECT
        ta.task_id, ta.rally, ta.swing_id,
        CASE
          WHEN ta.serve_cnt > 2 AND o.opponent_after_last = FALSE THEN TRUE
          ELSE FALSE
        END AS is_df,
        CASE
          WHEN ta.serve_cnt > 2 AND o.opponent_after_last = FALSE THEN NULL
          ELSE ta.try_ix
        END AS final_try_ix
      FROM try_assign ta
      JOIN opp_after_last o USING (task_id, rally)
    ),

    -- Decide service winner only for decisive (try 1/2, not DF):
    winners AS (
      SELECT
        ta.task_id, ta.rally, ta.swing_id,
        CASE
          WHEN df.is_df IS TRUE THEN FALSE
          ELSE NOT EXISTS (
                 SELECT 1 FROM {SILVER_SCHEMA}.{TABLE} q
                 WHERE q.task_id = ta.task_id
                   AND q.rally   = ta.rally
                   AND q.ball_hit_s > ta.ball_hit_s
                   AND q.player_id <> ta.player_id
               )
        END AS service_winner_d
      FROM try_assign ta
      JOIN df_flags   df USING (task_id, rally, swing_id)
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_try_ix_in_point = df.final_try_ix,
      double_fault_d        = df.is_df,
      service_winner_d      = w.service_winner_d
    FROM df_flags df
    JOIN winners  w USING (task_id, rally, swing_id)
    WHERE p.task_id = :tid
      AND p.swing_id = df.swing_id;
    """
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


# ------------------------------- Phase 2–5 (schema only) -------------------------------

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
    p = argparse.ArgumentParser(description="Silver point_detail — additive phases, single entrypoint")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--phase", choices=["1","2","3","4","5","all"], default="all", help="which phase(s) to run")
    p.add_argument("--replace", action="store_true", help="delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
