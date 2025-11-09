# build_silver_point_detail.py
# Silver point_detail — additive, phase-by-phase builder (single entrypoint)
#
# P1: Verbatim copy from Bronze (player_swing preferred, swing fallback)
#     EXCEPTIONS (temporary until Bronze is flattened):
#       - ball_hit_s: extract seconds from JSON {"timestamp": <number>} if no flat seconds
#       - ball_hit_x/y: extract from JSON array [x, y] if no flat *_x/*_y
# P2: Verbatim pull from Bronze.ball_bounce for first bounce after each swing:
#     Prefer flat columns; fallback to JSON object/array as needed.
# P3: Serve logic derived ONLY from columns created by P1+P2 (no extra sources).
#
# Usage:
#   python build_silver_point_detail.py --task-id <UUID> --replace --phase all
#   python build_silver_point_detail.py --task-id <UUID> --phase 1
#   python build_silver_point_detail.py --task-id <UUID> --phase 2
#   python build_silver_point_detail.py --task-id <UUID> --phase 3

from typing import Dict, Optional, OrderedDict as TOrderedDict
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

def _bronze_cols(conn: Connection, name: str) -> dict:
    return _columns_types(conn, "bronze", name)

def _ps_src(conn: Connection):
    # prefer player_swing; fallback to swing
    if _table_exists(conn, "bronze", "player_swing"):
        return "bronze.player_swing s", _bronze_cols(conn, "player_swing")
    if _table_exists(conn, "bronze", "swing"):
        return "bronze.swing s", _bronze_cols(conn, "swing")
    raise RuntimeError("Neither bronze.player_swing nor bronze.swing exists.")

def _colref_s(name: str) -> str:
    n = name.lower()
    return f's."end"' if n == "end" else f"s.{n}"

def _colref_b(name: str) -> str:
    n = name.lower()
    return f'b."end"' if n == "end" else f"b.{n}"

def _is_json(dt: str) -> bool:
    dt = dt or ""
    return "json" in dt

def _safe_json_number(colref: str) -> str:
    return (
        "(\n"
        f"  CASE WHEN jsonb_typeof({colref}::jsonb)='number'\n"
        f"       THEN ({colref}::text)::double precision\n"
        f"       ELSE NULL::double precision END\n"
        ")"
    )

def _safe_json_obj_ts(colref: str) -> str:
    # {"timestamp": <number>}
    return (
        "(\n"
        f"  CASE WHEN {colref} IS NOT NULL\n"
        f"        AND jsonb_typeof({colref}::jsonb)='object'\n"
        f"        AND ({colref}::jsonb ? 'timestamp')\n"
        f"        AND jsonb_typeof(({colref}::jsonb)->'timestamp')='number'\n"
        f"       THEN (({colref}::jsonb)->>'timestamp')::double precision\n"
        f"       ELSE NULL::double precision END\n"
        ")"
    )

def _xy_from_json_array(colref: str, idx: int) -> str:
    return (
        "(\n"
        f"  CASE WHEN {colref} IS NOT NULL\n"
        f"        AND jsonb_typeof({colref}::jsonb)='array'\n"
        f"        AND jsonb_array_length({colref}::jsonb)>{idx}\n"
        f"       THEN ({colref}::jsonb->>{idx})::double precision\n"
        f"       ELSE NULL::double precision END\n"
        ")"
    )

def _num_pref_flat_then_json_ps(cols: dict, name: str) -> str:
    n = name.lower()
    if n in cols and not _is_json(cols[n]):
        return _colref_s(n)
    if n in cols and _is_json(cols[n]):
        return _safe_json_number(_colref_s(n))
    return "NULL::double precision"

def _sec_pref_flat_then_json_ps(cols: dict, name: str, *alts: str) -> str:
    n = name.lower()
    if n in cols and not _is_json(cols[n]):
        return _colref_s(n)
    if n in cols and _is_json(cols[n]):
        return f"COALESCE({_safe_json_obj_ts(_colref_s(n))}, {_safe_json_number(_colref_s(n))})"
    for a in alts:
        a = a.lower()
        if a in cols and not _is_json(cols[a]):
            return _colref_s(a)
        if a in cols and _is_json(cols[a]):
            return f"COALESCE({_safe_json_obj_ts(_colref_s(a))}, {_safe_json_number(_colref_s(a))})"
    return "NULL::double precision"

def _text_copy_ps(cols: dict, name: str) -> str:
    return _colref_s(name) if name.lower() in cols else "NULL::text"

def _bool_copy_ps(cols: dict, name: str) -> str:
    return f"COALESCE({_colref_s(name)}, FALSE)" if name.lower() in cols else "FALSE"

def _int_from_text_digits_ps(cols: dict, name: str) -> str:
    n = name.lower()
    if n in cols:
        return (
            "(\n"
            f"  CASE WHEN {_colref_s(n)}::text ~ '^[0-9]+$'\n"
            f"       THEN {_colref_s(n)}::int\n"
            f"       ELSE NULL::int END\n"
            ")"
        )
    return "NULL::int"

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

# ------------------------------- PHASE 1 — loader (verbatim, with 2 safe exceptions) -------------------------------

def phase1_load(conn: Connection, task_id: str) -> int:
    """
    Phase 1 — exact Bronze copy (player_swing preferred) with two safe exceptions:
      - ball_hit_s: prefer flat seconds; else JSON object {"timestamp": number}; else numeric JSON
      - ball_hit_x/y: prefer flat *_x/*_y; else from JSON array [x, y]
    Everything else: direct copy (or NULL if missing).
    """
    src, cols = _ps_src(conn)

    task_id_expr = f"{_colref_s('task_id')}::uuid" if "task_id" in cols else ":tid::uuid"
    swing_id     = f"{_colref_s('id')}::bigint"    if "id" in cols else "NULL::bigint"

    player_id    = _text_copy_ps(cols, "player_id")
    valid        = _bool_copy_ps(cols, "valid")
    serve        = _bool_copy_ps(cols, "serve")
    volley       = _bool_copy_ps(cols, "volley")
    is_in_rally  = _bool_copy_ps(cols, "is_in_rally")
    swing_type   = _text_copy_ps(cols, "swing_type")
    ball_imp     = _text_copy_ps(cols, "ball_impact_type")
    rally        = _int_from_text_digits_ps(cols, "rally")

    # Prefer flat *_x/*_y; else array
    if "ball_hit_location_x" in cols and not _is_json(cols["ball_hit_location_x"]):
        ball_hit_x = f"NULLIF({_colref_s('ball_hit_location_x')}::text,'')::double precision"
    elif "ball_hit_location" in cols and _is_json(cols["ball_hit_location"]):
        ball_hit_x = _xy_from_json_array(_colref_s("ball_hit_location"), 0)
    else:
        ball_hit_x = "NULL::double precision"

    if "ball_hit_location_y" in cols and not _is_json(cols["ball_hit_location_y"]):
        ball_hit_y = f"NULLIF({_colref_s('ball_hit_location_y')}::text,'')::double precision"
    elif "ball_hit_location" in cols and _is_json(cols["ball_hit_location"]):
        ball_hit_y = _xy_from_json_array(_colref_s("ball_hit_location"), 1)
    else:
        ball_hit_y = "NULL::double precision"

    # Seconds: prefer flat start_ts/end_ts; else alternates; ball_hit from JSON object.timestamp if needed
    start_s = _sec_pref_flat_then_json_ps(cols, "start_ts", "start", "begin", "t_start")
    end_s   = _sec_pref_flat_then_json_ps(cols, "end_ts",   "end",   "t_end", "finish")
    ball_s  = _sec_pref_flat_then_json_ps(cols, "ball_hit")  # covers object.timestamp and numeric JSON

    ball_player_distance = _num_pref_flat_then_json_ps(cols, "ball_player_distance")
    ball_speed           = _num_pref_flat_then_json_ps(cols, "ball_speed")

    sql = (
        f"INSERT INTO {SILVER_SCHEMA}.{TABLE} (\n"
        "  task_id, swing_id, player_id,\n"
        "  valid, serve, swing_type, volley, is_in_rally,\n"
        "  ball_player_distance, ball_speed, ball_impact_type,\n"
        "  rally, ball_hit_x, ball_hit_y,\n"
        "  start_s, end_s, ball_hit_s\n"
        ")\n"
        "SELECT\n"
        f"  {task_id_expr} AS task_id,\n"
        f"  {swing_id}     AS swing_id,\n"
        f"  {player_id}    AS player_id,\n"
        f"  {valid}        AS valid,\n"
        f"  {serve}        AS serve,\n"
        f"  {swing_type}   AS swing_type,\n"
        f"  {volley}       AS volley,\n"
        f"  {is_in_rally}  AS is_in_rally,\n"
        f"  {ball_player_distance} AS ball_player_distance,\n"
        f"  {ball_speed}   AS ball_speed,\n"
        f"  {ball_imp}     AS ball_impact_type,\n"
        f"  {rally}        AS rally,\n"
        f"  {ball_hit_x}   AS ball_hit_x,\n"
        f"  {ball_hit_y}   AS ball_hit_y,\n"
        f"  {start_s}      AS start_s,\n"
        f"  {end_s}        AS end_s,\n"
        f"  {ball_s}       AS ball_hit_s\n"
        f"FROM {src}\n"
        f"WHERE {task_id_expr} = :tid\n"
        f"  AND COALESCE({valid}, FALSE) IS TRUE;\n"
    )
    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0

# ------------------------------- PHASE 2 — updater (verbatim bounce + helpers) -------------------------------

def phase2_update(conn: Connection, task_id: str) -> int:
    bb_cols = _columns_types(conn, "bronze", "ball_bounce")
    if not bb_cols:
        raise RuntimeError("bronze.ball_bounce not found")

    # ---- tolerant expressions (flat-first, JSON fallback) ----
    def _bb_time_expr() -> str:
        for c in ("bounce_s", "time_s", "timestamp_s", "ts", "t"):
            if c in bb_cols and "json" not in bb_cols[c]:
                return _colref_b(c)
        for c in ("timestamp", "data", "bounce"):
            if c in bb_cols and "json" in bb_cols[c]:
                return f"COALESCE({_safe_json_obj_ts(_colref_b(c))}, {_safe_json_number(_colref_b(c))})"
        return "NULL::double precision"

    def _bb_xy_x_expr() -> str:
        for c in ("court_x","x","x_m","x_center","x_center_m","x_pos","bounce_x"):
            if c in bb_cols and "json" not in bb_cols[c]:
                return _colref_b(c)
        for arr in ("court_pos","location","pos"):
            if arr in bb_cols and "json" in bb_cols[arr]:
                return _xy_from_json_array(_colref_b(arr), 0)
        for j in ("data","bounce"):
            if j in bb_cols and "json" in bb_cols[j]:
                return (
                    "COALESCE("
                    f"  (({_colref_b(j)}::jsonb)->>'court_x')::double precision,"
                    f"  (CASE WHEN jsonb_typeof(({_colref_b(j)}::jsonb)->'court_pos')='array'"
                    f"        AND jsonb_array_length(({_colref_b(j)}::jsonb)->'court_pos')>0"
                    f"        THEN (({_colref_b(j)}::jsonb)->'court_pos'->>0)::double precision"
                    f"        ELSE NULL::double precision END)"
                    ")"
                )
        return "NULL::double precision"

    def _bb_xy_y_expr() -> str:
        for c in ("court_y","y","y_m","y_center","y_center_m","y_pos","bounce_y"):
            if c in bb_cols and "json" not in bb_cols[c]:
                return _colref_b(c)
        for arr in ("court_pos","location","pos"):
            if arr in bb_cols and "json" in bb_cols[arr]:
                return _xy_from_json_array(_colref_b(arr), 1)
        for j in ("data","bounce"):
            if j in bb_cols and "json" in bb_cols[j]:
                return (
                    "COALESCE("
                    f"  (({_colref_b(j)}::jsonb)->>'court_y')::double precision,"
                    f"  (CASE WHEN jsonb_typeof(({_colref_b(j)}::jsonb)->'court_pos')='array'"
                    f"        AND jsonb_array_length(({_colref_b(j)}::jsonb)->'court_pos')>1"
                    f"        THEN (({_colref_b(j)}::jsonb)->'court_pos'->>1)::double precision"
                    f"        ELSE NULL::double precision END)"
                    ")"
                )
        return "NULL::double precision"

    def _bb_type_expr() -> str:
        for c in ("bounce_type","type"):
            if c in bb_cols and "json" not in bb_cols[c]:
                return _colref_b(c)
        for j in ("data","bounce"):
            if j in bb_cols and "json" in bb_cols[j]:
                return (
                    "("
                    f"  CASE WHEN jsonb_typeof({_colref_b(j)}::jsonb)='object'"
                    f"        AND jsonb_typeof(({_colref_b(j)}::jsonb)->'type')='string'"
                    f"       THEN ({_colref_b(j)}::jsonb->>'type')"
                    f"       ELSE NULL::text END"
                    ")"
                )
        return "NULL::text"

    bx_expr = _bb_xy_x_expr()
    by_expr = _bb_xy_y_expr()
    bs_expr = _bb_time_expr()
    bt_expr = _bb_type_expr()

    sql = (
        "WITH p0 AS ("
        f"  SELECT p.task_id, p.swing_id, p.player_id, p.rally,"
        "         COALESCE(p.valid, FALSE) AS valid,"
        "         COALESCE(p.serve, FALSE) AS serve,"
        "         p.ball_hit_s, p.ball_hit_x"
        f"  FROM {SILVER_SCHEMA}.{TABLE} p"
        "  WHERE p.task_id = :tid AND COALESCE(p.valid, FALSE) IS TRUE"
        "),"
        "p1 AS ("
        "  SELECT p0.*, "
        "         LEAD(p0.ball_hit_s) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_s,"
        "         LEAD(p0.ball_hit_x) OVER (PARTITION BY p0.task_id, p0.rally ORDER BY p0.ball_hit_s, p0.swing_id) AS next_ball_hit_x"
        "  FROM p0"
        "),"
        "p2 AS ("
        "  SELECT p1.*, (p1.ball_hit_s + 0.005) AS win_start,"
        "         LEAST(COALESCE(p1.next_ball_hit_s, p1.ball_hit_s + 2.5), p1.ball_hit_s + 2.5) AS win_end"
        "  FROM p1"
        "),"
        "chosen AS ("
        "  SELECT p2.swing_id, pick.bx, pick.by, pick.bt, pick.bs"
        "  FROM p2"
        "  LEFT JOIN LATERAL ("
        "    SELECT * FROM ("
        f"      SELECT {bx_expr} AS bx, {by_expr} AS by, {bt_expr} AS bt, {bs_expr} AS bs"
        "    ) q"
        "    WHERE q.bs IS NOT NULL"
        "      AND q.bs >  p2.win_start"
        "      AND q.bs <= p2.win_end"
        "    ORDER BY COALESCE(q.bt = 'floor', FALSE) DESC, q.bs"
        "    LIMIT 1"
        "  ) AS pick ON TRUE"
        ") "
        f"UPDATE {SILVER_SCHEMA}.{TABLE} p "
        "SET "
        "  bounce_x_m       = c.bx,"
        "  bounce_y_m       = c.by,"
        "  bounce_type_d    = c.bt,"
        "  bounce_s         = c.bs,"
        "  hit_x_resolved_m = CASE"
        "                       WHEN p.serve IS FALSE THEN COALESCE(c.bx, p2.next_ball_hit_x, p2.ball_hit_x)"
        "                       ELSE p.hit_x_resolved_m"
        "                     END,"
        "  hit_source_d     = CASE"
        "                       WHEN p.serve IS FALSE THEN"
        "                         CASE"
        "                           WHEN c.bx IS NOT NULL AND c.bt='floor' THEN 'floor_bounce'"
        "                           WHEN c.bx IS NOT NULL THEN 'any_bounce'"
        "                           WHEN p2.next_ball_hit_x IS NOT NULL THEN 'next_contact'"
        "                           ELSE 'ball_hit'"
        "                         END"
        "                       ELSE p.hit_source_d"
        "                     END "
        "FROM chosen c "
        "JOIN p2 ON p2.swing_id = c.swing_id "
        "WHERE p.task_id = :tid AND p.swing_id = c.swing_id;"
    )

    res = conn.execute(text(sql), {"tid": task_id})
    return res.rowcount or 0


# ------------------------------- PHASE 3 — updater (serve-only from P1+P2) -------------------------------

# Thresholds (strict): near if y > 23 ; far if y < 1
Y_NEAR_MIN = 23.0
Y_FAR_MAX  = 1.0
X_SIDE_ABS = 4.0

def phase3_update(conn: Connection, task_id: str) -> int:
    sql = (
        "-- Pass A: detect serves + set server_end/side/server_id\n"
        "WITH serves_a AS (\n"
        f"  SELECT p.task_id, p.rally, p.swing_id, p.player_id,\n"
        "         p.ball_hit_s, p.ball_hit_x, p.ball_hit_y,\n"
        "         CASE\n"
        "           WHEN lower(coalesce(p.swing_type,'')) LIKE '%overhead%'\n"
        f"                AND (p.ball_hit_y > {Y_NEAR_MIN} OR p.ball_hit_y < {Y_FAR_MAX})\n"
        "           THEN TRUE ELSE FALSE\n"
        "         END AS is_serve,\n"
        f"         CASE WHEN p.ball_hit_y > {Y_NEAR_MIN} THEN 'near'\n"
        f"              WHEN p.ball_hit_y < {Y_FAR_MAX}  THEN 'far'\n"
        "              ELSE NULL END AS server_end_d,\n"
        f"         CASE WHEN p.ball_hit_y > {Y_NEAR_MIN} THEN CASE WHEN p.ball_hit_x >= {X_SIDE_ABS}  THEN 'ad'    ELSE 'deuce' END\n"
        f"              WHEN p.ball_hit_y < {Y_FAR_MAX}  THEN CASE WHEN p.ball_hit_x <= -{X_SIDE_ABS} THEN 'deuce' ELSE 'ad'    END\n"
        "              ELSE NULL END AS serve_side_d\n"
        f"  FROM {SILVER_SCHEMA}.{TABLE} p\n"
        "  WHERE p.task_id = :tid AND COALESCE(p.valid, FALSE) IS TRUE\n"
        "),\n"
        "apply_a AS (\n"
        f"  UPDATE {SILVER_SCHEMA}.{TABLE} p\n"
        "  SET\n"
        "    serve_d      = s.is_serve,\n"
        "    server_id    = CASE WHEN s.is_serve THEN s.player_id ELSE p.server_id END,\n"
        "    server_end_d = CASE WHEN s.is_serve THEN s.server_end_d ELSE p.server_end_d END,\n"
        "    serve_side_d = CASE WHEN s.is_serve THEN s.serve_side_d ELSE p.serve_side_d END\n"
        "  FROM serves_a s\n"
        "  WHERE p.task_id = s.task_id AND p.swing_id = s.swing_id\n"
        "  RETURNING 1\n"
        "),\n"
        "-- Restrict to detected serves only\n"
        "base AS (\n"
        f"  SELECT p.task_id, p.rally, p.swing_id, p.player_id, p.ball_hit_s\n"
        f"  FROM {SILVER_SCHEMA}.{TABLE} p\n"
        "  WHERE p.task_id = :tid AND COALESCE(p.serve_d, FALSE) IS TRUE\n"
        "),\n"
        "rally_counts AS (\n"
        "  SELECT task_id, rally, COUNT(*) AS serve_cnt, MAX(ball_hit_s) AS last_serve_t\n"
        "  FROM base GROUP BY task_id, rally\n"
        "),\n"
        "try_assign AS (\n"
        "  SELECT b.task_id, b.rally, b.swing_id, b.player_id, b.ball_hit_s,\n"
        "         rc.serve_cnt,\n"
        "         CASE WHEN rc.serve_cnt = 1 THEN 1\n"
        "              WHEN rc.serve_cnt >= 2 THEN 2\n"
        "              ELSE NULL END AS try_ix,\n"
        "         rc.last_serve_t\n"
        "  FROM base b JOIN rally_counts rc USING (task_id, rally)\n"
        "),\n"
        "opp_after_last AS (\n"
        "  SELECT rc.task_id, rc.rally,\n"
        f"         EXISTS (\n"
        f"           SELECT 1 FROM {SILVER_SCHEMA}.{TABLE} q\n"
        "           WHERE q.task_id = rc.task_id AND q.rally = rc.rally\n"
        "             AND q.ball_hit_s > rc.last_serve_t\n"
        "             AND q.player_id <> (\n"
        "               SELECT player_id FROM base b2\n"
        "               WHERE b2.task_id = rc.task_id AND b2.rally = rc.rally\n"
        "               ORDER BY b2.ball_hit_s ASC LIMIT 1)\n"
        "         ) AS opponent_after_last\n"
        "  FROM rally_counts rc\n"
        "),\n"
        "df_flags AS (\n"
        "  SELECT ta.task_id, ta.rally, ta.swing_id,\n"
        "         CASE WHEN ta.serve_cnt > 2 AND o.opponent_after_last = FALSE THEN TRUE ELSE FALSE END AS is_df,\n"
        "         CASE WHEN ta.serve_cnt > 2 AND o.opponent_after_last = FALSE THEN NULL ELSE ta.try_ix END AS final_try_ix\n"
        "  FROM try_assign ta JOIN opp_after_last o USING (task_id, rally)\n"
        "),\n"
        "winners AS (\n"
        "  SELECT ta.task_id, ta.rally, ta.swing_id,\n"
        "         CASE WHEN df.is_df IS TRUE THEN FALSE\n"
        f"              ELSE NOT EXISTS (SELECT 1 FROM {SILVER_SCHEMA}.{TABLE} q\n"
        "                               WHERE q.task_id = ta.task_id AND q.rally = ta.rally\n"
        "                                 AND q.ball_hit_s > ta.ball_hit_s AND q.player_id <> ta.player_id)\n"
        "         END AS service_winner_d\n"
        "  FROM try_assign ta JOIN df_flags df USING (task_id, rally, swing_id)\n"
        ")\n"
        f"UPDATE {SILVER_SCHEMA}.{TABLE} p\n"
        "SET\n"
        "  serve_try_ix_in_point = df.final_try_ix,\n"
        "  double_fault_d        = df.is_df,\n"
        "  service_winner_d      = w.service_winner_d\n"
        "FROM df_flags df JOIN winners w USING (task_id, rally, swing_id)\n"
        "WHERE p.task_id = :tid AND p.swing_id = df.swing_id;\n"
    )
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
