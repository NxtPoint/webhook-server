# build_silver_point_detail.py
# NextPoint Silver: silver.point_detail
# Phase 1: bronze.player_swing -> core + ball_hit_s + ball_hit_location_x/y
# Phase 2: bronze.ball_bounce -> type/timestamp/court_x/court_y (first bounce after contact)
# Phase 3: serve context (serve_d, server_end_d, serve_side_d, serve_try_ix_in_point, service_winner_d)
# Phase 4: serve_location + rally_location_hit/bounce
# Phase 5: point_number + exclusions + point_winner + game_number + (optional) server_id/shot_ix/etc if you later extend
# Phase 6: normalization (invert flags + normalized coordinates)
# Phase 7: analytics/presentation features (serve buckets, rally length, stroke, shot sequence)

from typing import Dict, Optional, OrderedDict as TOrderedDict
from collections import OrderedDict
from sqlalchemy import text
from sqlalchemy.engine import Connection
from db_init import engine

SILVER_SCHEMA = "silver"
TABLE = "point_detail"


# ------------------------------- Column specs -------------------------------
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
    "ball_hit_s":            "double precision",
    "ball_hit_location_x":   "double precision",
    "ball_hit_location_y":   "double precision",
})

PHASE2_COLS: TOrderedDict[str, str] = OrderedDict({
    "type":      "text",
    "timestamp": "double precision",
    "court_x":   "double precision",
    "court_y":   "double precision",
})

PHASE3_COLS = OrderedDict({
    "serve_d":               "boolean",
    "server_end_d":          "text",     # 'near' | 'far'
    "serve_side_d":          "text",     # 'deuce' | 'ad'
    "serve_try_ix_in_point": "text",     # '1st' | '2nd' | 'Double'
    "service_winner_d":      "boolean",  # TRUE on (approx) ace/service winner; NULL elsewhere
})

PHASE4_COLS = OrderedDict({
    "serve_location":        "integer",  # 1..8
    "rally_location_hit":    "text",     # 'A'|'B'|'C'|'D'
    "rally_location_bounce": "text",     # 'A'|'B'|'C'|'D'
})

PHASE5_COLS = OrderedDict({
    "exclude_d":              "boolean",
    "point_number":           "integer",
    "point_winner_player_id": "text",
    "game_number":            "integer",
    "game_winner_player_id":  "text",
    "server_id":              "text",
    "shot_ix_in_point":       "integer",
    "shot_phase_d":           "text",
    "shot_outcome_d":         "text",
    "point_key":              "text",
    "set_number":             "integer",
    "set_game_number":        "integer",
    "ace_d":                  "boolean",
})

PHASE6_COLS = OrderedDict({
    "invert_hit":          "boolean",
    "invert_bounce":       "boolean",
    "ball_hit_x_norm":     "double precision",
    "ball_hit_y_norm":     "double precision",
    "ball_bounce_x_norm":  "double precision",
    "ball_bounce_y_norm":  "double precision",
})

PHASE7_COLS = OrderedDict({
    "serve_bucket_d":           "text",
    "rally_length":             "integer",
    "rally_length_point":       "integer",
    "rally_length_bucket_d":    "text",
    "stroke_d":                 "text",
    "shot_q":                   "integer",
    "shot_key_q":               "text",
    "aggression_d":             "text",
    "depth_d":                  "text",
})

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

DDL_CREATE_SCHEMA = f"CREATE SCHEMA IF NOT EXISTS {SILVER_SCHEMA};"


# ------------------------------- schema ensure -------------------------------
def ensure_table_exists(conn: Connection):
    _exec(conn, DDL_CREATE_SCHEMA)
    if not _table_exists(conn, SILVER_SCHEMA, TABLE):
        cols_sql = ",\n  ".join([f"{k} {v}" for k, v in PHASE1_COLS.items()])
        _exec(conn, f"CREATE TABLE {SILVER_SCHEMA}.{TABLE} (\n  {cols_sql}\n);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task    ON {SILVER_SCHEMA}.{TABLE}(task_id);")
        _exec(conn, f"CREATE INDEX IF NOT EXISTS ix_pd_task_id ON {SILVER_SCHEMA}.{TABLE}(task_id, id);")

    # Structural fix A: enforce idempotency to prevent duplicates
    _exec(conn, f"""
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1
        FROM   pg_constraint c
        JOIN   pg_class t ON t.oid = c.conrelid
        JOIN   pg_namespace n ON n.oid = t.relnamespace
        WHERE  n.nspname = '{SILVER_SCHEMA}'
          AND  t.relname = '{TABLE}'
          AND  c.conname = 'uq_point_detail_task_id'
      ) THEN
        ALTER TABLE {SILVER_SCHEMA}.{TABLE}
        ADD CONSTRAINT uq_point_detail_task_id UNIQUE (task_id, id);
      END IF;
    END $$;
    """)

def ensure_phase_columns(conn: Connection, spec: Dict[str, str]):
    existing = _columns_types(conn, SILVER_SCHEMA, TABLE)
    for col, typ in spec.items():
        if col.lower() not in existing:
            _exec(conn, f"ALTER TABLE {SILVER_SCHEMA}.{TABLE} ADD COLUMN {col} {typ};")

def phase2_add_schema(conn: Connection): ensure_phase_columns(conn, PHASE2_COLS)
def phase3_add_schema(conn: Connection): ensure_phase_columns(conn, PHASE3_COLS)
def phase4_add_schema(conn: Connection): ensure_phase_columns(conn, PHASE4_COLS)
def phase5_add_schema(conn: Connection): ensure_phase_columns(conn, PHASE5_COLS)
def phase6_add_schema(conn: Connection): ensure_phase_columns(conn, PHASE6_COLS)
def phase7_add_schema(conn: Connection): ensure_phase_columns(conn, PHASE7_COLS)

# ------------------------------- PHASE 0: clean playerids and de-dup repeat ids------------------------------
def _player_id_canonical_map(conn: Connection, task_id: str) -> dict:
    """
    SportAI sometimes emits a 3rd/ghost player_id with tiny counts.
    Canonical rule (deterministic, fail-closed-ish):
      - Find top 2 player_id values by COUNT(*) in bronze.player_swing for this task_id (valid only).
      - Keep those as-is.
      - Map any other player_id to the 2nd-most-common player (p2).
    This matches your case: 242(1) -> 234(51) when 234 is p2.
    """
    sql = """
    WITH ranked AS (
      SELECT
        player_id,
        COUNT(*) AS n
      FROM bronze.player_swing
      WHERE task_id::uuid = :tid
        AND COALESCE(valid, FALSE) = TRUE
        AND player_id IS NOT NULL
      GROUP BY player_id
      ORDER BY n DESC, player_id
    )
    SELECT player_id, n
    FROM ranked
    LIMIT 10;
    """
    rows = conn.execute(text(sql), {"tid": task_id}).fetchall()
    if not rows:
        return {}

    # top 2
    p1 = rows[0][0]
    p2 = rows[1][0] if len(rows) > 1 else rows[0][0]

    mapping = {p1: p1, p2: p2}
    for pid, _n in rows[2:]:
        mapping[pid] = p2  # map all extras to p2
    return mapping


# ------------------------------- PHASE 1 ---------------------------------
def phase1_load(conn: Connection, task_id: str) -> int:
    """
    Insert core fields + split x/y + ball_hit_s.

    Fix: canonicalize SportAI player_id to exactly 2 players:
      - map any extra/ghost player_id to the 2nd most common player_id for the task.
    """
    pid_map = _player_id_canonical_map(conn, task_id)

    # Build CASE expression safely (no string interpolation of values)
    # We pass mapping as bind params.
    case_lines = []
    params = {"tid": task_id}
    i = 0
    for src_pid, dst_pid in pid_map.items():
        i += 1
        params[f"src_{i}"] = src_pid
        params[f"dst_{i}"] = dst_pid
        case_lines.append(f"WHEN s.player_id = :src_{i} THEN :dst_{i}")

    # If mapping is empty, keep original player_id
    if case_lines:
        player_id_expr = "CASE " + " ".join(case_lines) + " ELSE s.player_id END"
    else:
        player_id_expr = "s.player_id"

    sql = f"""
    INSERT INTO {SILVER_SCHEMA}.{TABLE} (
      id, task_id, player_id, valid, serve, swing_type, volley, is_in_rally,
      ball_player_distance, ball_speed, ball_impact_type,
      ball_hit_s, ball_hit_location_x, ball_hit_location_y
    )
    SELECT
      s.id::bigint                               AS id,
      s.task_id::uuid                            AS task_id,
      {player_id_expr}                           AS player_id,
      COALESCE(s.valid, FALSE)                   AS valid,
      COALESCE(s.serve, FALSE)                   AS serve,
      s.swing_type                               AS swing_type,
      COALESCE(s.volley, FALSE)                  AS volley,
      COALESCE(s.is_in_rally, FALSE)             AS is_in_rally,
      s.ball_player_distance::double precision   AS ball_player_distance,
      s.ball_speed::double precision             AS ball_speed,
      s.ball_impact_type                         AS ball_impact_type,

      CASE
        WHEN s.ball_hit IS NOT NULL
         AND s.ball_hit::text LIKE '{{%%'
         AND s.ball_hit::text LIKE '%%"timestamp"%%'
        THEN (s.ball_hit::jsonb ->> 'timestamp')::double precision
        ELSE NULL::double precision
      END                                         AS ball_hit_s,

      CASE
        WHEN s.ball_hit_location IS NOT NULL
         AND s.ball_hit_location::text LIKE '[%%'
        THEN (s.ball_hit_location::jsonb ->> 0)::double precision
        ELSE NULL::double precision
      END                                         AS ball_hit_location_x,

      CASE
        WHEN s.ball_hit_location IS NOT NULL
         AND s.ball_hit_location::text LIKE '[%%'
        THEN (s.ball_hit_location::jsonb ->> 1)::double precision
        ELSE NULL::double precision
      END                                         AS ball_hit_location_y
    FROM bronze.player_swing s
    WHERE s.task_id::uuid = :tid
      AND COALESCE(s.valid, FALSE) = TRUE
    ON CONFLICT (task_id, id) DO NOTHING;
    """
    res = conn.execute(text(sql), params)
    return res.rowcount or 0


# ------------------------------- PHASE 2 ---------------------------------
def phase2_update(conn: Connection, task_id: str) -> int:
    """
    Pick FIRST bounce strictly after contact time within:
      (ball_hit_s + 0.005,  min(next_ball_hit_s, ball_hit_s + 2.5]]
    """
    sql = f"""
    WITH p AS (
      SELECT p1.id, p1.task_id, p1.ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE} p1
      WHERE p1.task_id = :tid
      -- Optional perf tightening (safe): skip null-hit rows
      -- AND p1.ball_hit_s IS NOT NULL
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
        LEAST(
          COALESCE(p_lead.next_ball_hit_s, p_lead.ball_hit_s + 2.5),
          p_lead.ball_hit_s + 2.5
        ) AS win_end
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


# ------------------------------- PHASE 3 ---------------------------------
def phase3_update(conn: Connection, task_id: str) -> int:
    """
    Phase 3 (singles court rules; coordinate origin still doubles):

    - x origin: 0 at OUTSIDE doubles sideline (data coordinate frame)
    - Singles in-play region for x: [1.37, 9.60]
    - serve_side uses midline 5.485 (singles center line)
    - point_number increments when serve_side changes (serve rows), then forward-filled
    - serve_try computed within point (prevents point starting with '2nd'), then forward-filled (persisted)
    - service_winner computed on serve rows; persisted PER POINT (no leaks across points)
      Fix: do NOT require bounce coords to detect a return (prevents false service winners).
    """

    COURT_LENGTH_M = 23.77
    DOUBLES_WIDTH_M = 10.97
    EPS_BASELINE_M = 0.30

    # Singles boundaries in doubles-origin frame
    SINGLES_LEFT_X = (DOUBLES_WIDTH_M - 8.23) / 2.0  # 1.37
    SINGLES_RIGHT_X = SINGLES_LEFT_X + 8.23          # 9.60
    MID_X_DEFAULT = SINGLES_LEFT_X + 8.23 / 2.0      # 5.485

    sql_checks = f"""
    WITH base AS (
      SELECT
        task_id,
        player_id,
        COALESCE(serve, FALSE) AS sportai_serve,
        swing_type,
        ball_hit_s,
        ball_hit_location_x AS x,
        ball_hit_location_y AS y
      FROM {SILVER_SCHEMA}.{TABLE}
      WHERE task_id = :tid
    ),
    anchors AS (
      SELECT 1
      FROM base b
      WHERE b.sportai_serve IS TRUE
        AND lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
        AND b.y IS NOT NULL
        AND (
          (b.y)::double precision < :eps
          OR (b.y)::double precision > (:y_max - :eps)
        )
        AND b.ball_hit_s IS NOT NULL
      LIMIT 1
    )
    SELECT EXISTS (SELECT 1 FROM anchors) AS has_anchor;
    """
    chk = conn.execute(
        text(sql_checks),
        {"tid": task_id, "eps": float(EPS_BASELINE_M), "y_max": float(COURT_LENGTH_M)},
    ).mappings().first() or {}

    if not chk.get("has_anchor", False):
        return 0

    # Midpoint for X (avg serve-hit x). Default to singles midline.
    sql_mid = f"""
    WITH srv AS (
      SELECT NULLIF(TRIM(ball_hit_location_x::text), '')::double precision AS x
      FROM {SILVER_SCHEMA}.{TABLE}
      WHERE task_id = :tid
        AND ball_hit_location_x IS NOT NULL
        AND ball_hit_location_y IS NOT NULL
        AND COALESCE(serve, FALSE) IS TRUE
        AND lower(COALESCE(trim(swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
        AND (
          (ball_hit_location_y)::double precision < :eps
          OR (ball_hit_location_y)::double precision > (:y_max - :eps)
        )
        AND (ball_hit_location_x)::double precision BETWEEN :sx_left AND :sx_right
    )
    SELECT COALESCE(AVG(x), :mid_default) FROM srv;
    """
    mid_x = conn.execute(
        text(sql_mid),
        {
            "tid": task_id,
            "eps": float(EPS_BASELINE_M),
            "y_max": float(COURT_LENGTH_M),
            "sx_left": float(SINGLES_LEFT_X),
            "sx_right": float(SINGLES_RIGHT_X),
            "mid_default": float(MID_X_DEFAULT),
        },
    ).scalar()
    if mid_x is None:
        mid_x = float(MID_X_DEFAULT)

    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.player_id,
        p.valid,
        p.serve,
        p.swing_type,
        p.ball_hit_s,
        p.ball_hit_location_x AS x,
        p.ball_hit_location_y AS y,
        p.court_x,
        p.court_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.ball_hit_s IS NOT NULL
    ),

    srv0 AS (
      SELECT
        b.*,
        CASE
          WHEN COALESCE(b.serve, FALSE) IS FALSE THEN FALSE
          WHEN (
            lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
            AND b.y IS NOT NULL
            AND (
              (b.y)::double precision < :eps
              OR (b.y)::double precision > (:y_max - :eps)
            )
          ) THEN TRUE
          ELSE FALSE
        END AS serve_d_raw,

        CASE
          WHEN COALESCE(b.serve, FALSE) IS FALSE THEN NULL
          WHEN (
            lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
            AND b.y IS NOT NULL
            AND (
              (b.y)::double precision < :eps
              OR (b.y)::double precision > (:y_max - :eps)
            )
          )
          THEN CASE WHEN (b.y)::double precision < :eps THEN 'far' ELSE 'near' END
          ELSE NULL
        END AS server_end_raw
      FROM base b
    ),

    srv1 AS (
      SELECT
        s.*,
        s.serve_d_raw AS serve_d,
        COALESCE(s.server_end_raw, lastsrv.server_end_raw) AS server_end_d,

        -- Singles guard: only compute side if x is inside singles sidelines
        CASE
          WHEN s.serve_d_raw IS TRUE
           AND s.x IS NOT NULL
           AND (s.x)::double precision BETWEEN :sx_left AND :sx_right
           AND COALESCE(s.server_end_raw, lastsrv.server_end_raw) IN ('near','far')
          THEN
            CASE
              WHEN COALESCE(s.server_end_raw, lastsrv.server_end_raw) = 'near' THEN
                CASE
                  WHEN (s.x)::double precision > :mid THEN 'deuce'
                  WHEN (s.x)::double precision < :mid THEN 'ad'
                  ELSE 'deuce'
                END
              ELSE
                CASE
                  WHEN (s.x)::double precision < :mid THEN 'deuce'
                  WHEN (s.x)::double precision > :mid THEN 'ad'
                  ELSE 'deuce'
                END
            END
          ELSE NULL
        END AS serve_side_raw
      FROM srv0 s
      LEFT JOIN LATERAL (
        SELECT s2.server_end_raw
        FROM srv0 s2
        WHERE s2.task_id = s.task_id
          AND s2.server_end_raw IN ('near','far')
          AND (s2.ball_hit_s < s.ball_hit_s OR (s2.ball_hit_s = s.ball_hit_s AND s2.id <= s.id))
        ORDER BY s2.ball_hit_s DESC, s2.id DESC
        LIMIT 1
      ) lastsrv ON TRUE
    ),

    srv2 AS (
      SELECT
        s1.*,
        COALESCE(
          s1.serve_side_raw,
          (
            SELECT s0.serve_side_raw
            FROM srv1 s0
            WHERE s0.task_id = s1.task_id
              AND s0.serve_side_raw IS NOT NULL
              AND (s0.ball_hit_s < s1.ball_hit_s OR (s0.ball_hit_s = s1.ball_hit_s AND s0.id <= s1.id))
            ORDER BY s0.ball_hit_s DESC, s0.id DESC
            LIMIT 1
          )
        ) AS serve_side_d
      FROM srv1 s1
    ),

    -- compute point_number on serve rows by serve_side changes (no nested windows)
    serve_points0 AS (
      SELECT
        s.id,
        s.task_id,
        s.ball_hit_s,
        s.serve_side_d,
        LAG(s.serve_side_d) OVER (
          PARTITION BY s.task_id
          ORDER BY s.ball_hit_s, s.id
        ) AS prev_serve_side_d
      FROM srv2 s
      WHERE s.serve_d IS TRUE
        AND s.serve_side_d IS NOT NULL
    ),

    serve_points AS (
      SELECT
        sp0.id,
        sp0.task_id,
        1
        + SUM(
            CASE
              WHEN sp0.prev_serve_side_d IS DISTINCT FROM sp0.serve_side_d THEN 1
              ELSE 0
            END
          ) OVER (
            PARTITION BY sp0.task_id
            ORDER BY sp0.ball_hit_s, sp0.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
          )::integer AS point_number_serves
      FROM serve_points0 sp0
    ),

    t_point AS (
      SELECT
        s2.*,
        MAX(sp.point_number_serves) OVER (
          PARTITION BY s2.task_id
          ORDER BY s2.ball_hit_s, s2.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )::integer AS point_number
      FROM srv2 s2
      LEFT JOIN serve_points sp
        ON sp.id = s2.id
    ),

    -- serve rows within points
    serves AS (
      SELECT
        s.*,
        ROW_NUMBER() OVER (
          PARTITION BY s.task_id, s.point_number
          ORDER BY s.ball_hit_s, s.id
        ) AS serve_seq,
        COUNT(*) OVER (
          PARTITION BY s.task_id, s.point_number
        ) AS serve_cnt
      FROM t_point s
      WHERE s.serve_d IS TRUE
        AND s.point_number IS NOT NULL
    ),

    serve_labels AS (
      SELECT
        s.id,
        CASE
          WHEN s.serve_d IS NOT TRUE THEN NULL
          WHEN s.serve_seq = 1 THEN '1st'
          WHEN s.serve_seq = 2 THEN '2nd'
          ELSE '2nd'
        END AS serve_try_raw,

        CASE
          WHEN s.serve_d IS TRUE
           AND s.serve_seq = s.serve_cnt
           AND NOT EXISTS (
             SELECT 1
             FROM t_point q
             WHERE q.task_id = s.task_id
               AND q.point_number = s.point_number
               AND q.ball_hit_s > s.ball_hit_s
               AND q.serve_d IS NOT TRUE
               AND COALESCE(q.valid, TRUE) IS TRUE
               AND q.player_id <> s.player_id
           )
          THEN TRUE
          ELSE FALSE
        END AS service_winner_raw
      FROM serves s
    ),

    t0 AS (
      SELECT
        tp.*,
        sl.serve_try_raw,
        sl.service_winner_raw
      FROM t_point tp
      LEFT JOIN serve_labels sl
        ON sl.id = tp.id
    ),

    t1 AS (
      SELECT
        t0.*,

        (
          ARRAY_AGG(t0.serve_try_raw ORDER BY t0.ball_hit_s, t0.id)
          FILTER (WHERE t0.serve_try_raw IS NOT NULL)
        )[
          CARDINALITY(
            ARRAY_AGG(t0.serve_try_raw ORDER BY t0.ball_hit_s, t0.id)
            FILTER (WHERE t0.serve_try_raw IS NOT NULL)
          )
        ] AS serve_try_ix_in_point,

        (
          MAX(CASE WHEN t0.service_winner_raw IS TRUE THEN 1 ELSE 0 END)
          OVER (PARTITION BY t0.task_id, t0.point_number)
        ) = 1 AS service_winner_d

      FROM t0
      GROUP BY
        t0.id, t0.task_id, t0.player_id, t0.valid, t0.serve, t0.swing_type, t0.ball_hit_s, t0.x, t0.y,
        t0.court_x, t0.court_y, t0.serve_d_raw, t0.server_end_raw, t0.serve_d, t0.server_end_d,
        t0.serve_side_raw, t0.serve_side_d, t0.point_number, t0.serve_try_raw, t0.service_winner_raw
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d               = t1.serve_d,
      server_end_d          = t1.server_end_d,
      serve_side_d          = t1.serve_side_d,
      serve_try_ix_in_point = t1.serve_try_ix_in_point,
      service_winner_d      = t1.service_winner_d,
      point_number          = t1.point_number
    FROM t1
    WHERE p.task_id = :tid
      AND p.id = t1.id;
    """

    res = conn.execute(
        text(sql),
        {
            "tid": task_id,
            "mid": float(mid_x),
            "eps": float(EPS_BASELINE_M),
            "y_max": float(COURT_LENGTH_M),
            "sx_left": float(SINGLES_LEFT_X),
            "sx_right": float(SINGLES_RIGHT_X),
        },
    )
    return res.rowcount or 0

# ------------------------------- PHASE 4 ---------------------------------

def phase4_update(conn: Connection, task_id: str) -> int:
    """
    Phase 4 (SINGLES court, SportAI x origin still at OUTSIDE doubles sideline):
      - serve_location        : 1–8 (from court_x, server_end_d, serve_side_d)  [persisted]
      - rally_location_hit    : A–D (from ball_hit_location_x / y)              [unchanged behavior; coords updated]
      - rally_location_bounce : A–D (from court_x + ball_hit_location_y, fallback to hit) [unchanged behavior; coords updated]

    Notes:
      - Singles boundaries in doubles-origin x:
          singles_left_x  = 1.37
          singles_right_x = 9.60
          singles_width   = 8.23
          singles_mid_x   = 5.485
          service_box_half_width = 4.115
      - Half court (net) y:
          half_y = 11.885
    """

    # 0) Ensure P4 columns exist
    ensure_phase_columns(conn, OrderedDict({
        "serve_location":        "integer",
        "rally_location_hit":    "text",
        "rally_location_bounce": "text",
    }))

    # -------------------
    # Exact singles constants (meters) in doubles-origin coordinate frame
    # -------------------
    SINGLES_LEFT_X = 1.37
    SINGLES_RIGHT_X = 9.60
    SINGLES_WIDTH = 8.23
    HALF_Y = 11.885

    # Service box, measured from singles wide sideline towards center line:
    # half-width of singles court = 8.23/2 = 4.115
    BOX_HALF_W = SINGLES_WIDTH / 2.0  # 4.115
    Q1 = BOX_HALF_W / 4.0             # 1.02875
    Q2 = BOX_HALF_W / 2.0             # 2.0575
    Q3 = 3.0 * BOX_HALF_W / 4.0       # 3.08625

    # Rally bands across singles width (0..8.23) into 4 equal lanes:
    L1 = SINGLES_WIDTH / 4.0          # 2.0575
    L2 = SINGLES_WIDTH / 2.0          # 4.115
    L3 = 3.0 * SINGLES_WIDTH / 4.0    # 6.1725

    # =========================================================================
    # 1) Serve location (1–8) — corrected mapping + safe carry-forward
    # =========================================================================
    #
    # Singles x range in doubles-origin coordinates:
    #   left sideline  = 1.37
    #   midline        = 5.485
    #   right sideline = 9.60
    #
    # Mapping:
    #   near + deuce : 1.37 -> 5.485  => 1..4
    #   near + ad    : 5.485 -> 9.60  => 5..8
    #   far  + deuce : 9.60 -> 5.485  => 1..4
    #   far  + ad    : 5.485 -> 1.37  => 5..8
    #
    # Defaults for invalid / missing serve bounce x:
    #   deuce -> 2
    #   ad    -> 7
    #
    HALF_W = SINGLES_WIDTH / 2.0      # 4.115
    B1 = HALF_W / 4.0                 # 1.02875
    B2 = HALF_W / 2.0                 # 2.0575
    B3 = 3.0 * HALF_W / 4.0           # 3.08625
    MID_X = SINGLES_LEFT_X + HALF_W   # 5.485

    sql_srv = f"""
    WITH base AS (
      SELECT
        id,
        task_id,
        ball_hit_s,
        serve_d,
        server_end_d,
        serve_side_d,
        court_x
      FROM {SILVER_SCHEMA}.{TABLE}
      WHERE task_id = :tid
        AND ball_hit_s IS NOT NULL
    ),

    srv_loc_raw AS (
      SELECT
        b.id,
        CASE
          WHEN COALESCE(b.serve_d, FALSE) IS NOT TRUE THEN NULL
          WHEN lower(COALESCE(TRIM(b.server_end_d), '')) NOT IN ('near','far') THEN NULL
          WHEN lower(COALESCE(TRIM(b.serve_side_d), '')) NOT IN ('deuce','ad') THEN NULL

          -- ==========================================================
          -- INVALID / MISSING X FOR SERVE ROWS -> use defaults
          -- deuce => 2
          -- ad    => 7
          -- ==========================================================
          WHEN NULLIF(TRIM(b.court_x::text), '') IS NULL THEN
            CASE
              WHEN lower(TRIM(b.serve_side_d)) = 'deuce' THEN 2
              ELSE 7
            END

          WHEN (b.court_x)::double precision < :sx_left
            OR (b.court_x)::double precision > :sx_right THEN
            CASE
              WHEN lower(TRIM(b.serve_side_d)) = 'deuce' THEN 2
              ELSE 7
            END

          -- ==========================================================
          -- NEAR + DEUCE : 1.37 -> 5.485  => 1..4
          -- ==========================================================
          WHEN lower(TRIM(b.server_end_d)) = 'near'
           AND lower(TRIM(b.serve_side_d)) = 'deuce'
          THEN
            CASE
              WHEN ((b.court_x)::double precision - :sx_left) < :b1 THEN 1
              WHEN ((b.court_x)::double precision - :sx_left) < :b2 THEN 2
              WHEN ((b.court_x)::double precision - :sx_left) < :b3 THEN 3
              ELSE 4
            END

          -- ==========================================================
          -- NEAR + AD : 5.485 -> 9.60  => 5..8
          -- ==========================================================
          WHEN lower(TRIM(b.server_end_d)) = 'near'
           AND lower(TRIM(b.serve_side_d)) = 'ad'
          THEN
            CASE
              WHEN ((b.court_x)::double precision - :mid_x) < :b1 THEN 5
              WHEN ((b.court_x)::double precision - :mid_x) < :b2 THEN 6
              WHEN ((b.court_x)::double precision - :mid_x) < :b3 THEN 7
              ELSE 8
            END

          -- ==========================================================
          -- FAR + DEUCE : 9.60 -> 5.485  => 1..4
          -- ==========================================================
          WHEN lower(TRIM(b.server_end_d)) = 'far'
           AND lower(TRIM(b.serve_side_d)) = 'deuce'
          THEN
            CASE
              WHEN (:sx_right - (b.court_x)::double precision) < :b1 THEN 1
              WHEN (:sx_right - (b.court_x)::double precision) < :b2 THEN 2
              WHEN (:sx_right - (b.court_x)::double precision) < :b3 THEN 3
              ELSE 4
            END

          -- ==========================================================
          -- FAR + AD : 5.485 -> 1.37  => 5..8
          -- ==========================================================
          WHEN lower(TRIM(b.server_end_d)) = 'far'
           AND lower(TRIM(b.serve_side_d)) = 'ad'
          THEN
            CASE
              WHEN (:mid_x - (b.court_x)::double precision) < :b1 THEN 5
              WHEN (:mid_x - (b.court_x)::double precision) < :b2 THEN 6
              WHEN (:mid_x - (b.court_x)::double precision) < :b3 THEN 7
              ELSE 8
            END

          ELSE NULL
        END AS serve_location_raw
      FROM base b
    ),

    ordered AS (
      SELECT
        b.id,
        b.task_id,
        b.ball_hit_s,
        r.serve_location_raw,
        COUNT(r.serve_location_raw) OVER (
          PARTITION BY b.task_id
          ORDER BY b.ball_hit_s, b.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS grp
      FROM base b
      LEFT JOIN srv_loc_raw r
        ON r.id = b.id
    ),

    filled AS (
      SELECT
        o.id,
        MAX(o.serve_location_raw) OVER (
          PARTITION BY o.task_id, o.grp
        ) AS serve_location
      FROM ordered o
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET serve_location = f.serve_location
    FROM filled f
    WHERE p.task_id = :tid
      AND p.id = f.id;
    """
    conn.execute(
        text(sql_srv),
        {
            "tid": task_id,
            "sx_left": float(SINGLES_LEFT_X),
            "sx_right": float(SINGLES_RIGHT_X),
            "mid_x": float(MID_X),
            "b1": float(B1),
            "b2": float(B2),
            "b3": float(B3),
        },
    )

    # =========================================================================
    # 2) Rally location (hit): A–D — corrected singles mapping
    # =========================================================================
    #
    # Rules:
    #   - x always runs left -> right in database coordinates
    #   - far side (y < 11.885):  A | B | C | D
    #   - near side (y >= 11.885): D | C | B | A
    #
    # Simplified bands:
    #   x <  3.4275  => edge-left category
    #   x >= 7.5425  => edge-right category
    #   middle bands only decide B/C
    #
    # Hit logic:
    #   - use ball_hit_location_y to determine side of hitter
    #   - apply same mirrored A-D mapping
    #
    sql_rl_hit = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_hit =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        WHEN NULLIF(TRIM(p.ball_hit_location_x::text), '') IS NULL THEN NULL
        WHEN NULLIF(TRIM(p.ball_hit_location_y::text), '') IS NULL THEN NULL

        -- FAR SIDE HIT: A | B | C | D
        WHEN (p.ball_hit_location_y)::double precision < :half_y THEN
          CASE
            WHEN (p.ball_hit_location_x)::double precision < :z2 THEN 'A'
            WHEN (p.ball_hit_location_x)::double precision < :z3 THEN 'B'
            WHEN (p.ball_hit_location_x)::double precision < :z4 THEN 'C'
            ELSE 'D'
          END

        -- NEAR SIDE HIT: D | C | B | A
        ELSE
          CASE
            WHEN (p.ball_hit_location_x)::double precision < :z2 THEN 'D'
            WHEN (p.ball_hit_location_x)::double precision < :z3 THEN 'C'
            WHEN (p.ball_hit_location_x)::double precision < :z4 THEN 'B'
            ELSE 'A'
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(
        text(sql_rl_hit),
        {
            "tid": task_id,
            "half_y": float(HALF_Y),
            "z2": 3.4275,
            "z3": 5.485,
            "z4": 7.5425,
        },
    )

    # =========================================================================
    # 3) Rally location (bounce): A–D — corrected singles mapping
    # =========================================================================
    #
    # Rules:
    #   - use court_x as bounce x
    #   - use court_y to determine whether bounce was on far or near side
    #   - clamp out-of-range x to edge category:
    #       far side:  x < 1.37 => A, x > 9.60 => D
    #       near side: x < 1.37 => D, x > 9.60 => A
    #
    sql_rl_bnc = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_bounce =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        WHEN NULLIF(TRIM(p.court_x::text), '') IS NULL THEN p.rally_location_hit
        WHEN NULLIF(TRIM(p.court_y::text), '') IS NULL THEN NULL

        -- FAR SIDE BOUNCE: A | B | C | D
        WHEN (p.court_y)::double precision < :half_y THEN
          CASE
            WHEN (p.court_x)::double precision < :sx_left THEN 'A'
            WHEN (p.court_x)::double precision < :z2 THEN 'A'
            WHEN (p.court_x)::double precision < :z3 THEN 'B'
            WHEN (p.court_x)::double precision < :z4 THEN 'C'
            ELSE 'D'
          END

        -- NEAR SIDE BOUNCE: D | C | B | A
        ELSE
          CASE
            WHEN (p.court_x)::double precision < :sx_left THEN 'D'
            WHEN (p.court_x)::double precision < :z2 THEN 'D'
            WHEN (p.court_x)::double precision < :z3 THEN 'C'
            WHEN (p.court_x)::double precision < :z4 THEN 'B'
            ELSE 'A'
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(
        text(sql_rl_bnc),
        {
            "tid": task_id,
            "sx_left": float(SINGLES_LEFT_X),
            "half_y": float(HALF_Y),
            "z2": 3.4275,
            "z3": 5.485,
            "z4": 7.5425,
        },
    )

    return 1

# ------------------------------- PHASE 5 ---------------------------------
# IMPORTANT: This Phase 5 expects Phase 3 to emit serve_try_ix_in_point in: '1st' | '2nd' | 'Double'
# and service_winner_d as boolean.
#
# REQUIRED SCHEMA FIX:
#   game_winner_player_id MUST be TEXT (player_id is text).
#   PHASE5_COLS should be:
#       "game_winner_player_id":  "text"
#
# Fixes in this version:
#   - point_number increments when (server player changes) OR (serve_side_d changes) at FIRST serves.
#   - game_number increments when (server player changes) at FIRST serves.
#
# Production-grade hardening:
#   - deterministic ordering (ball_hit_s NULLS LAST, then id)
#   - all anchor logic is based ONLY on FIRST serves with non-null ball_hit_s
#   - exclude_d logic is stable (no dependence on serve_side_d non-null)
#   - game_winner_player_id stays TEXT (no casts)
#   - preflight resolves 2 primary players even if extra player_id values exist

def phase5_update(conn: Connection, task_id: str) -> int:
    pf = _phase5_preflight(conn, task_id)

    r1  = phase5_fix_point_number(conn, task_id, pf)
    r2  = phase5_apply_exclusions(conn, task_id)
    r3  = phase5_fix_game_number(conn, task_id, pf)
    r4  = phase5_set_set_number(conn, task_id)
    r5  = phase5_set_server_id(conn, task_id)
    r6  = phase5_set_shot_ix_in_point(conn, task_id)
    r7  = phase5_set_shot_phase(conn, task_id)
    r8  = phase5_set_point_key(conn, task_id)
    r9  = phase5_set_shot_outcome(conn, task_id)

    # serve outcomes AFTER shot_outcome exists
    r9b = phase5_finalize_serve_labels(conn, task_id)

    r10 = phase5_set_point_winner(conn, task_id, pf)
    r11 = phase5_set_game_winner(conn, task_id)

    return int(
        (r1 or 0) + (r2 or 0) + (r3 or 0) + (r4 or 0) + (r5 or 0)
        + (r6 or 0) + (r7 or 0) + (r8 or 0) + (r9 or 0)
        + (r9b or 0)
        + (r10 or 0) + (r11 or 0)
    )

def _phase5_preflight(conn: Connection, task_id: str) -> dict:
    """
    Resolve the 2 "real" players even if extra player_id values exist.
    Fail-closed only if we cannot resolve 2 players.

    Returns: {"p1": <player_id>, "p2": <player_id>}
    """
    sql = f"""
    WITH base AS (
      SELECT
        player_id,
        COALESCE(valid, TRUE) AS valid,
        COALESCE(exclude_d, FALSE) AS exclude_d
      FROM {SILVER_SCHEMA}.{TABLE}
      WHERE task_id = :tid
        AND player_id IS NOT NULL
    ),
    ranked AS (
      SELECT
        player_id,
        COUNT(*) AS n
      FROM base
      WHERE valid IS TRUE
        AND exclude_d IS FALSE
      GROUP BY player_id
      ORDER BY n DESC, player_id
      LIMIT 2
    )
    SELECT
      (SELECT COUNT(*) FROM ranked) AS top2_cnt,
      (SELECT player_id FROM ranked ORDER BY n DESC, player_id LIMIT 1) AS p1,
      (SELECT player_id FROM ranked ORDER BY n DESC, player_id OFFSET 1 LIMIT 1) AS p2;
    """
    r = conn.execute(text(sql), {"tid": task_id}).mappings().first() or {}
    if int(r.get("top2_cnt") or 0) != 2:
        raise ValueError(
            f"Phase5 fail-closed: could not resolve 2 primary players (task_id={task_id})"
        )
    return {"p1": r["p1"], "p2": r["p2"]}


def phase5_fix_point_number(conn: Connection, task_id: str, pf: dict) -> int:
    """
    point_number increments at FIRST serves when EITHER:
      - server (player_id) changes, OR
      - serve_side_d changes (deuce/ad)

    Anchors only consider the 2 resolved players (pf['p1'], pf['p2'])
    so any extra/ghost player_id cannot create false point increments.
    """
    sql = f"""
    WITH anchors AS (
      SELECT
        p.task_id,
        p.ball_hit_s AS anchor_s,
        p.id,
        p.player_id AS server_pid,
        p.serve_side_d AS side
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.player_id IN (:p1, :p2)
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND LOWER(COALESCE(p.serve_try_ix_in_point::text,'')) = '1st'
        AND p.ball_hit_s IS NOT NULL
      ORDER BY p.ball_hit_s NULLS LAST, p.id
    ),
    incs AS (
      SELECT
        a.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY a.task_id ORDER BY a.anchor_s NULLS LAST, a.id) = 1 THEN 1
          WHEN LAG(a.server_pid) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s NULLS LAST, a.id)
               IS DISTINCT FROM a.server_pid THEN 1
          WHEN LAG(a.side) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s NULLS LAST, a.id)
               IS DISTINCT FROM a.side THEN 1
          ELSE 0
        END AS inc
      FROM anchors a
    ),
    pn_rows AS (
      SELECT
        p.id,
        COALESCE(
          (SELECT SUM(i.inc)
           FROM incs i
           WHERE i.task_id = p.task_id
             AND i.anchor_s <= p.ball_hit_s),
          0
        ) AS pn
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.ball_hit_s IS NOT NULL
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET point_number = r.pn
    FROM pn_rows r
    WHERE p.id = r.id
      AND p.task_id = :tid;
    """
    res = conn.execute(
        text(sql),
        {"tid": task_id, "p1": pf["p1"], "p2": pf["p2"]},
    )
    return res.rowcount or 0


def phase5_apply_exclusions(conn: Connection, task_id: str) -> int:
    """
    Minimal exclusions (tightened):

      1) non-serve BEFORE the last serve in the point -> exclude_d = TRUE
      2) gap > 5s AFTER the last serve in point -> exclude this + rest of point
      3) non-serve rows with no hit coordinates at all -> exclude_d = TRUE
         (phantom / empty rows inserted by SportAI between points or after point end)

    NOTE:
      - serve_side_d IS NULL is allowed
      - no same-player-back-to-back exclusion
      - no pre-point blanket exclusion
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.point_number,
        p.ball_hit_s,
        COALESCE(p.serve_d, FALSE) AS serve_d,
        p.ball_hit_location_x,
        p.ball_hit_location_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.ball_hit_s IS NOT NULL
        AND p.point_number > 0
    ),
    last_serve AS (
      SELECT
        b.task_id,
        b.point_number,
        MAX(CASE WHEN b.serve_d THEN b.ball_hit_s END) AS last_serve_s
      FROM base b
      GROUP BY b.task_id, b.point_number
    ),
    ordered AS (
      SELECT
        b.*,
        ls.last_serve_s,
        LAG(b.ball_hit_s) OVER (
          PARTITION BY b.task_id, b.point_number
          ORDER BY b.ball_hit_s, b.id
        ) AS prev_s
      FROM base b
      LEFT JOIN last_serve ls
        ON ls.task_id = b.task_id
       AND ls.point_number = b.point_number
    ),
    flags AS (
      SELECT
        o.id,
        (
          NOT o.serve_d
          AND o.last_serve_s IS NOT NULL
          AND o.ball_hit_s < o.last_serve_s
        ) AS r1_before_last_serve,

        (
          o.prev_s IS NOT NULL
          AND o.last_serve_s IS NOT NULL
          AND o.ball_hit_s > o.last_serve_s
          AND (o.ball_hit_s - o.prev_s) > 5.0
        ) AS gap_break,

        (
          NOT o.serve_d
          AND o.ball_hit_location_x IS NULL
          AND o.ball_hit_location_y IS NULL
        ) AS r3_empty_non_serve
      FROM ordered o
    ),
    chain AS (
      SELECT
        f.id,
        BOOL_OR(f.gap_break) OVER (
          PARTITION BY b.task_id, b.point_number
          ORDER BY b.ball_hit_s, b.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS r2_gap_chain,
        f.r1_before_last_serve,
        f.r3_empty_non_serve
      FROM flags f
      JOIN {SILVER_SCHEMA}.{TABLE} b
        ON b.id = f.id
    ),
    excl AS (
      SELECT
        id,
        (r1_before_last_serve OR r2_gap_chain OR r3_empty_non_serve) AS exclude_d
      FROM chain
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET exclude_d = e.exclude_d
    FROM excl e
    WHERE p.task_id = :tid
      AND p.id = e.id;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


def phase5_set_point_winner(conn: Connection, task_id: str, pf: dict) -> int:
    """
    Winner per point (updated business rule):

      1) If any double-fault in the point -> receiver wins.
      2) Else use LAST non-excluded, valid shot in the point:
           - if that shot_outcome_d = 'Winner' -> shooter (player_id) wins
           - else (Error / In / NULL) -> opponent wins

    Notes:
      - exclude_d rule remains untouched (we only consume it).
      - "last shot" remains the last non-excluded, valid row by (ball_hit_s, id).
      - Opponent is resolved using pf['p1'], pf['p2'] only (fail-closed to NULL if unknown).
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.point_number,
        p.player_id,
        COALESCE(p.valid, TRUE) AS valid,
        COALESCE(p.exclude_d, FALSE) AS exclude_d,
        COALESCE(p.serve_d, FALSE) AS serve_d,
        p.serve_try_ix_in_point,
        p.shot_outcome_d,
        p.ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number > 0
    ),

    -- server = first serve row in point
    point_server AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id,
        b.point_number,
        b.player_id AS server_id
      FROM base b
      WHERE b.serve_d IS TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s NULLS LAST, b.id
    ),

    -- receiver = other player among the two resolved players
    point_receiver AS (
      SELECT
        ps.task_id,
        ps.point_number,
        CASE
          WHEN ps.server_id = :p1 THEN :p2
          WHEN ps.server_id = :p2 THEN :p1
          ELSE NULL
        END AS receiver_id
      FROM point_server ps
    ),

    -- double-fault flag (uses serve_try_ix_in_point; assumes it is persisted to serve rows)
    flags AS (
      SELECT
        b.task_id,
        b.point_number,
        BOOL_OR(
          b.serve_d IS TRUE
          AND LOWER(COALESCE(b.serve_try_ix_in_point::text,'')) LIKE 'double%'
        ) AS any_double
      FROM base b
      GROUP BY b.task_id, b.point_number
    ),

    -- last non-excluded, valid row (this is your "last shot" anchor)
    last_valid AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id,
        b.point_number,
        b.player_id AS last_pid,
        b.shot_outcome_d AS last_outcome
      FROM base b
      WHERE b.exclude_d IS FALSE
        AND b.valid IS TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s DESC NULLS LAST, b.id DESC
    ),

    winners AS (
      SELECT
        ps.task_id,
        ps.point_number,
        CASE
          -- Rule 1: any double fault -> receiver
          WHEN f.any_double IS TRUE THEN pr.receiver_id

          -- Rule 2: last shot decides
          WHEN LOWER(COALESCE(lv.last_outcome::text,'')) = 'winner' THEN lv.last_pid

          -- otherwise opponent of last shooter
          WHEN lv.last_pid = :p1 THEN :p2
          WHEN lv.last_pid = :p2 THEN :p1
          ELSE NULL
        END AS winner_pid
      FROM point_server ps
      LEFT JOIN point_receiver pr
        ON pr.task_id = ps.task_id
       AND pr.point_number = ps.point_number
      LEFT JOIN flags f
        ON f.task_id = ps.task_id
       AND f.point_number = ps.point_number
      LEFT JOIN last_valid lv
        ON lv.task_id = ps.task_id
       AND lv.point_number = ps.point_number
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET point_winner_player_id = w.winner_pid
    FROM winners w
    WHERE p.task_id = :tid
      AND p.point_number = w.point_number;
    """
    return conn.execute(
        text(sql),
        {"tid": task_id, "p1": pf["p1"], "p2": pf["p2"]},
    ).rowcount or 0


def phase5_fix_game_number(conn: Connection, task_id: str, pf: dict) -> int:
    """
    game_number increments when the SERVER changes at FIRST serves.
    Anchors restricted to the 2 resolved players.
    """
    sql = f"""
    WITH anchors AS (
      SELECT
        p.task_id,
        p.ball_hit_s AS anchor_s,
        p.id,
        p.player_id AS server_pid
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.player_id IN (:p1, :p2)
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND LOWER(COALESCE(p.serve_try_ix_in_point::text,'')) = '1st'
        AND p.ball_hit_s IS NOT NULL
      ORDER BY p.ball_hit_s NULLS LAST, p.id
    ),
    incs AS (
      SELECT
        a.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY a.task_id ORDER BY a.anchor_s NULLS LAST, a.id) = 1 THEN 1
          WHEN LAG(a.server_pid) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s NULLS LAST, a.id)
               IS DISTINCT FROM a.server_pid THEN 1
          ELSE 0
        END AS inc
      FROM anchors a
    ),
    g_rows AS (
      SELECT
        p.id,
        COALESCE(
          (SELECT SUM(i.inc)
           FROM incs i
           WHERE i.task_id = p.task_id
             AND i.anchor_s <= p.ball_hit_s),
          0
        ) AS gnum
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.ball_hit_s IS NOT NULL
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET game_number = r.gnum
    FROM g_rows r
    WHERE p.id = r.id
      AND p.task_id = :tid;
    """
    res = conn.execute(
        text(sql),
        {"tid": task_id, "p1": pf["p1"], "p2": pf["p2"]},
    )
    return res.rowcount or 0


def phase5_set_game_winner(conn: Connection, task_id: str) -> int:
    """
    game_winner_player_id:
      - winner of the LAST point in each game_number (by max ball_hit_s).
      - Stored as TEXT (player_id is text). DO NOT cast to int.
    """
    sql = f"""
    WITH pts AS (
      SELECT
        p.task_id,
        p.game_number,
        p.point_number,
        p.point_winner_player_id,
        MAX(p.ball_hit_s) AS last_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number > 0
        AND COALESCE(p.exclude_d, FALSE) = FALSE
        AND p.game_number IS NOT NULL
        AND p.point_winner_player_id IS NOT NULL
        AND p.ball_hit_s IS NOT NULL
      GROUP BY p.task_id, p.game_number, p.point_number, p.point_winner_player_id
    ),
    last_points AS (
      SELECT DISTINCT ON (task_id, game_number)
        task_id,
        game_number,
        point_winner_player_id AS winner_pid
      FROM pts
      ORDER BY task_id, game_number, last_s DESC
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET game_winner_player_id = lp.winner_pid
    FROM last_points lp
    WHERE p.task_id = :tid
      AND p.game_number = lp.game_number;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


def phase5_set_server_id(conn: Connection, task_id: str) -> int:
    """
    server_id per point:
      - player who hits the FIRST serve in each (task_id, point_number)
      - persisted on ALL rows in that point
    """
    sql = f"""
    WITH first_serves AS (
      SELECT
        p.task_id,
        p.point_number,
        MIN(p.ball_hit_s) AS first_serve_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number > 0
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND p.ball_hit_s IS NOT NULL
      GROUP BY p.task_id, p.point_number
    ),
    point_server AS (
      SELECT
        fs.task_id,
        fs.point_number,
        MIN(p.id) AS min_id_at_first_s,
        fs.first_serve_s
      FROM first_serves fs
      JOIN {SILVER_SCHEMA}.{TABLE} p
        ON p.task_id = fs.task_id
       AND p.point_number = fs.point_number
       AND p.ball_hit_s = fs.first_serve_s
      GROUP BY fs.task_id, fs.point_number, fs.first_serve_s
    ),
    server_ids AS (
      SELECT
        ps.task_id,
        ps.point_number,
        p.player_id AS server_id
      FROM point_server ps
      JOIN {SILVER_SCHEMA}.{TABLE} p
        ON p.task_id = ps.task_id
       AND p.point_number = ps.point_number
       AND p.ball_hit_s = ps.first_serve_s
       AND p.id = ps.min_id_at_first_s
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET server_id = s.server_id
    FROM server_ids s
    WHERE p.task_id = :tid
      AND p.point_number = s.point_number;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


def phase5_set_shot_ix_in_point(conn: Connection, task_id: str) -> int:
    """
    shot_ix_in_point:
      - anchor at the LAST serve in each point
      - that last serve = 1
      - subsequent non-excluded shots in the point = 2,3,...
      - shots before the last serve keep NULL
    """
    sql = f"""
    WITH last_serve AS (
      SELECT
        p.task_id,
        p.point_number,
        MAX(p.ball_hit_s) AS last_serve_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number > 0
        AND COALESCE(p.exclude_d, FALSE) = FALSE
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND p.ball_hit_s IS NOT NULL
      GROUP BY p.task_id, p.point_number
    ),
    ordered AS (
      SELECT
        p.id,
        ROW_NUMBER() OVER (
          PARTITION BY p.task_id, p.point_number
          ORDER BY p.ball_hit_s, p.id
        ) AS shot_ix
      FROM {SILVER_SCHEMA}.{TABLE} p
      JOIN last_serve ls
        ON p.task_id      = ls.task_id
       AND p.point_number = ls.point_number
       AND p.ball_hit_s >= ls.last_serve_s
      WHERE p.task_id = :tid
        AND p.point_number > 0
        AND p.ball_hit_s IS NOT NULL
        AND COALESCE(p.exclude_d, FALSE) = FALSE
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET shot_ix_in_point = o.shot_ix
    FROM ordered o
    WHERE p.id = o.id
      AND p.task_id = :tid;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


def phase5_set_shot_phase(conn: Connection, task_id: str) -> int:
    # Singles exact geometry (meters). y=0 at near baseline; y increases to far baseline.
    COURT_LENGTH_M = 23.77
    SERVICE_LINE_M = 6.40
    FAR_SERVICE_LINE_M = COURT_LENGTH_M - SERVICE_LINE_M  # 17.37

    sql = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET shot_phase_d =
      CASE
        WHEN COALESCE(p.exclude_d, FALSE) = TRUE
             OR p.shot_ix_in_point IS NULL THEN NULL
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN 'Serve'
        WHEN p.shot_ix_in_point = 2 THEN 'Return'
        ELSE
          CASE
            WHEN p.ball_hit_location_y IS NULL THEN NULL

            -- exact court bounds
            WHEN (p.ball_hit_location_y)::double precision < 0
                 OR (p.ball_hit_location_y)::double precision > :court_len
              THEN 'Rally'

            -- "Net" band: between service lines (exact)
            WHEN (p.ball_hit_location_y)::double precision > :svc
                 AND (p.ball_hit_location_y)::double precision < :far_svc
              THEN 'Net'

            ELSE 'Transition'
          END
      END
    WHERE p.task_id = :tid
      AND p.point_number > 0;
    """
    return conn.execute(
        text(sql),
        {
            "tid": task_id,
            "court_len": float(COURT_LENGTH_M),
            "svc": float(SERVICE_LINE_M),
            "far_svc": float(FAR_SERVICE_LINE_M),
        },
    ).rowcount or 0


def phase5_set_point_key(conn: Connection, task_id: str) -> int:
    sql = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET point_key =
      p.task_id::text
      || '|' || LPAD(p.point_number::text, 4, '0')
      || '|' || COALESCE(p.server_id::text, '')
    WHERE p.task_id = :tid
      AND p.point_number > 0;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


def phase5_set_shot_outcome(conn: Connection, task_id: str) -> int:
    # Singles geometry (meters) with SportAI x-origin at outside doubles sideline
    COURT_LEN = 23.77
    HALF_Y = 11.885
    SINGLES_LEFT_X = 1.37
    SINGLES_RIGHT_X = 9.60
    SERVE_NET_BAND = 1.60

    sql = f"""
    WITH last_shot AS (
      SELECT
        p.task_id,
        p.point_number,
        MAX(p.shot_ix_in_point) AS last_ix
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number > 0
        AND COALESCE(p.exclude_d, FALSE) = FALSE
        AND p.shot_ix_in_point IS NOT NULL
      GROUP BY p.task_id, p.point_number
    ),
    outcomes AS (
      SELECT
        p.id,
        CASE
          WHEN p.shot_ix_in_point < ls.last_ix
            THEN 'In'

          WHEN p.shot_ix_in_point = ls.last_ix
            THEN
              CASE
                -- no bounce = terminal error
                WHEN p.court_x IS NULL OR p.court_y IS NULL
                  THEN 'Error'

                -- bounce outside singles court = error
                WHEN (p.court_x)::double precision < :sx_left
                  OR (p.court_x)::double precision > :sx_right
                  OR (p.court_y)::double precision < 0
                  OR (p.court_y)::double precision > :court_len
                  THEN 'Error'

                -- serve-specific net fault
                WHEN COALESCE(p.serve_d, FALSE) IS TRUE
                 AND ABS((p.court_y)::double precision - :half_y) <= :serve_net_band
                  THEN 'Error'

                -- non-serve terminal shot must land on OPPOSITE side of the net
                -- far side hit (0..11.885) must land near side (>11.885)
                WHEN COALESCE(p.serve_d, FALSE) IS NOT TRUE
                 AND p.ball_hit_location_y IS NOT NULL
                 AND (p.ball_hit_location_y)::double precision < :half_y
                 AND (p.court_y)::double precision <= :half_y
                  THEN 'Error'

                -- near side hit (11.885..23.77) must land far side (<11.885)
                WHEN COALESCE(p.serve_d, FALSE) IS NOT TRUE
                 AND p.ball_hit_location_y IS NOT NULL
                 AND (p.ball_hit_location_y)::double precision > :half_y
                 AND (p.court_y)::double precision >= :half_y
                  THEN 'Error'

                ELSE 'Winner'
              END

          ELSE NULL
        END AS shot_outcome_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      JOIN last_shot ls
        ON p.task_id      = ls.task_id
       AND p.point_number = ls.point_number
      WHERE p.task_id = :tid
        AND p.point_number > 0
        AND COALESCE(p.exclude_d, FALSE) = FALSE
        AND p.shot_ix_in_point IS NOT NULL
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET shot_outcome_d = o.shot_outcome_d
    FROM outcomes o
    WHERE p.id = o.id
      AND p.task_id = :tid;
    """
    return conn.execute(
        text(sql),
        {
            "tid": task_id,
            "court_len": float(COURT_LEN),
            "half_y": float(HALF_Y),
            "sx_left": float(SINGLES_LEFT_X),
            "sx_right": float(SINGLES_RIGHT_X),
            "serve_net_band": float(SERVE_NET_BAND),
        },
    ).rowcount or 0

def phase5_finalize_serve_labels(conn: Connection, task_id: str) -> int:
    """
    Finalize serve-related outcome labels AFTER shot_outcome_d exists.

    Outputs:
      - serve_try_ix_in_point : keep 1st / 2nd / Double
      - ace_d                 : TRUE when valid serve wins point with no opponent return row
      - service_winner_d      : TRUE when valid serve is followed by opponent return row that ends in Error

    Double rule:
      - If last non-excluded valid row in point is a serve
      - and current serve_try_ix_in_point = '2nd'
      - and serve is not a valid in-serve
      => mark whole point as Double

    Valid in-serve means:
      - bounce present
      - bounce inside singles court
      - not in net band
    """
    COURT_LEN = 23.77
    SINGLES_LEFT_X = 1.37
    SINGLES_RIGHT_X = 9.60
    NET_Y = 11.885
    NET_BAND = 1.60

    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.point_number,
        p.player_id,
        p.ball_hit_s,
        COALESCE(p.valid, TRUE) AS valid,
        COALESCE(p.exclude_d, FALSE) AS exclude_d,
        COALESCE(p.serve_d, FALSE) AS serve_d,
        LOWER(COALESCE(p.serve_try_ix_in_point::text, '')) AS serve_try_lc,
        LOWER(COALESCE(p.shot_outcome_d::text, '')) AS shot_outcome_lc,
        p.shot_ix_in_point,
        p.court_x,
        p.court_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number > 0
    ),

    -- last non-excluded valid row in point
    last_valid AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id,
        b.point_number,
        b.id,
        b.player_id,
        b.serve_d,
        b.serve_try_lc,
        b.shot_outcome_lc,
        b.court_x,
        b.court_y,
        b.shot_ix_in_point
      FROM base b
      WHERE b.exclude_d IS FALSE
        AND b.valid IS TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s DESC NULLS LAST, b.id DESC
    ),

    -- points that must become Double
    dbl AS (
      SELECT
        lv.task_id,
        lv.point_number
      FROM last_valid lv
      WHERE lv.serve_d IS TRUE
        AND lv.serve_try_lc = '2nd'
        AND (
             lv.court_x IS NULL
          OR lv.court_y IS NULL
          OR (lv.court_x)::double precision < :sx_left
          OR (lv.court_x)::double precision > :sx_right
          OR (lv.court_y)::double precision < 0
          OR (lv.court_y)::double precision > :court_len
          OR ABS((lv.court_y)::double precision - :net_y) <= :net_band
        )
    ),

    -- first valid serve row in point
    serve_rows AS (
      SELECT
        b.*,
        ROW_NUMBER() OVER (
          PARTITION BY b.task_id, b.point_number
          ORDER BY b.ball_hit_s, b.id
        ) AS serve_rn
      FROM base b
      WHERE b.exclude_d IS FALSE
        AND b.valid IS TRUE
        AND b.serve_d IS TRUE
    ),

    first_serve AS (
      SELECT
        s.task_id,
        s.point_number,
        s.id AS serve_id,
        s.player_id AS server_id,
        s.serve_try_lc,
        s.shot_outcome_lc
      FROM serve_rows s
      WHERE s.serve_rn = 1
    ),

    -- first opponent non-serve row after the serve (i.e. return attempt)
    first_return AS (
      SELECT DISTINCT ON (fs.task_id, fs.point_number)
        fs.task_id,
        fs.point_number,
        r.id AS return_id,
        r.player_id AS returner_id,
        r.shot_outcome_lc AS return_outcome_lc,
        r.shot_ix_in_point
      FROM first_serve fs
      JOIN base r
        ON r.task_id = fs.task_id
       AND r.point_number = fs.point_number
       AND r.exclude_d IS FALSE
       AND r.valid IS TRUE
       AND r.serve_d IS NOT TRUE
       AND r.player_id <> fs.server_id
      ORDER BY fs.task_id, fs.point_number, r.ball_hit_s, r.id
    ), BY fs.task_id, fs.point_number, r.ball_hit_s, r.id
    ),

    point_stats AS (
      SELECT
        b.task_id,
        b.point_number,
        COUNT(*) FILTER (
          WHERE b.exclude_d IS FALSE
            AND b.valid IS TRUE
            AND b.serve_d IS NOT TRUE
        ) AS non_serve_rows
      FROM base b
      GROUP BY b.task_id, b.point_number
    ),

    ace_points AS (
      SELECT
        fs.task_id,
        fs.point_number
      FROM first_serve fs
      JOIN last_valid lv
        ON lv.task_id = fs.task_id
       AND lv.point_number = fs.point_number
      JOIN point_stats ps
        ON ps.task_id = fs.task_id
       AND ps.point_number = fs.point_number
      LEFT JOIN dbl d
        ON d.task_id = fs.task_id
       AND d.point_number = fs.point_number
      WHERE d.point_number IS NULL
        AND fs.serve_try_lc IN ('1st','2nd')
        AND lv.id = fs.serve_id
        AND lv.shot_outcome_lc = 'winner'
        AND ps.non_serve_rows = 0
    ),

    service_winner_points AS (
      SELECT
        fs.task_id,
        fs.point_number
      FROM first_serve fs
      JOIN first_return fr
        ON fr.task_id = fs.task_id
       AND fr.point_number = fs.point_number
      JOIN last_valid lv
        ON lv.task_id = fs.task_id
       AND lv.point_number = fs.point_number
      LEFT JOIN dbl d
        ON d.task_id = fs.task_id
       AND d.point_number = fs.point_number
      WHERE d.point_number IS NULL
        AND fs.serve_try_lc IN ('1st','2nd')
        AND fr.return_outcome_lc = 'error'
        AND lv.id = fr.return_id
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_try_ix_in_point =
        CASE
          WHEN EXISTS (
            SELECT 1
            FROM dbl d
            WHERE d.task_id = p.task_id
              AND d.point_number = p.point_number
          ) THEN 'Double'
          ELSE p.serve_try_ix_in_point
        END,

      ace_d =
        CASE
          WHEN EXISTS (
            SELECT 1
            FROM ace_points a
            WHERE a.task_id = p.task_id
              AND a.point_number = p.point_number
          ) THEN TRUE
          ELSE FALSE
        END,

      service_winner_d =
        CASE
          WHEN EXISTS (
            SELECT 1
            FROM service_winner_points s
            WHERE s.task_id = p.task_id
              AND s.point_number = p.point_number
          ) THEN TRUE
          ELSE FALSE
        END
    WHERE p.task_id = :tid
      AND p.point_number > 0;
    """
    return conn.execute(
        text(sql),
        {
            "tid": task_id,
            "court_len": float(COURT_LEN),
            "sx_left": float(SINGLES_LEFT_X),
            "sx_right": float(SINGLES_RIGHT_X),
            "net_y": float(NET_Y),
            "net_band": float(NET_BAND),
        },
    ).rowcount or 0

def phase5_set_set_number(conn: Connection, task_id: str) -> int:
    """
    set_number derived from final set scores in bronze.submission_context and game_number.

    Uses totals:
      g1 = a1+b1, g2 = a2+b2, g3 = a3+b3

    set_number:
      1 if game_number in [1..g1]
      2 if game_number in [g1+1..g1+g2]
      3 if game_number in [g1+g2+1..g1+g2+g3]
      else NULL (fail-closed)

    set_game_number:
      game number within the set.
    """
    sql = f"""
    WITH sc AS (
      SELECT
        sc.task_id::uuid AS task_id,
        (COALESCE(sc.player_a_set1_games,0) + COALESCE(sc.player_b_set1_games,0))::int AS g1,
        (COALESCE(sc.player_a_set2_games,0) + COALESCE(sc.player_b_set2_games,0))::int AS g2,
        (COALESCE(sc.player_a_set3_games,0) + COALESCE(sc.player_b_set3_games,0))::int AS g3
      FROM bronze.submission_context sc
      WHERE sc.task_id::uuid = :tid
      LIMIT 1
    ),
    bounds AS (
      SELECT
        task_id,
        g1,
        (g1 + g2)::int AS g12,
        (g1 + g2 + g3)::int AS g123
      FROM sc
    ),
    mapped AS (
      SELECT
        p.id,
        p.game_number,
        b.g1, b.g12, b.g123,
        CASE
          WHEN p.game_number IS NULL OR p.game_number <= 0 THEN NULL
          WHEN b.g1   IS NULL OR b.g1   <= 0 THEN NULL
          WHEN p.game_number <= b.g1 THEN 1
          WHEN b.g12 > b.g1   AND p.game_number <= b.g12 THEN 2
          WHEN b.g123 > b.g12 AND p.game_number <= b.g123 THEN 3
          ELSE NULL
        END AS set_number,
        CASE
          WHEN p.game_number IS NULL OR p.game_number <= 0 THEN NULL
          WHEN b.g1   IS NULL OR b.g1   <= 0 THEN NULL
          WHEN p.game_number <= b.g1 THEN p.game_number
          WHEN b.g12 > b.g1   AND p.game_number <= b.g12 THEN p.game_number - b.g1
          WHEN b.g123 > b.g12 AND p.game_number <= b.g123 THEN p.game_number - b.g12
          ELSE NULL
        END AS set_game_number
      FROM {SILVER_SCHEMA}.{TABLE} p
      CROSS JOIN bounds b
      WHERE p.task_id = :tid
        AND p.point_number > 0
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      set_number = m.set_number,
      set_game_number = m.set_game_number
    FROM mapped m
    WHERE p.task_id = :tid
      AND p.id = m.id;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


def phase5_add_schema(conn: Connection):
    ensure_phase_columns(conn, PHASE5_COLS)

    # schema repair: if legacy column is integer, convert to text (skip safely if blocked by deps)
    sql_fix = f"""
    DO $$
    DECLARE t text;
    BEGIN
      SELECT data_type INTO t
      FROM information_schema.columns
      WHERE table_schema = '{SILVER_SCHEMA}'
        AND table_name   = '{TABLE}'
        AND column_name  = 'game_winner_player_id';

      IF t = 'integer' THEN
        BEGIN
          ALTER TABLE {SILVER_SCHEMA}.{TABLE}
            ALTER COLUMN game_winner_player_id TYPE text
            USING game_winner_player_id::text;
        EXCEPTION WHEN OTHERS THEN
          RAISE NOTICE 'Skipping type change for game_winner_player_id due to dependency/lock: %', SQLERRM;
        END;
      END IF;
    END $$;
    """
    _exec(conn, sql_fix)

# ------------------------------- Phase 6 Normalised Co-ordinates -------------------------------

def phase6_update(conn: Connection, task_id: str) -> int:
    """
    Phase 6: camera-normalized coordinates

    Locked business rule:
      - Normalize everything to the near-side camera view.
      - Keep raw coordinate frame in doubles-origin coordinates.
      - DO NOT convert x into singles-local by subtracting 1.37.

    Invert flags (already correct):
      - invert_hit    = TRUE  when hit came from far side  (ball_hit_location_y < 11.885)
      - invert_hit    = FALSE when hit came from near side (ball_hit_location_y >= 11.885)

      - invert_bounce = TRUE  when hit came from near side (ball_hit_location_y > 11.885)
      - invert_bounce = FALSE when hit came from far side  (ball_hit_location_y <= 11.885)

    Normalization math:
      - if invert = FALSE => norm = raw
      - if invert = TRUE  => flip across full court:
            x_norm = 10.97 - x
            y_norm = 23.77 - y
    """
    HALF_Y = 11.885
    COURT_LEN = 23.77
    DOUBLES_W = 10.97

    sql = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      invert_hit =
        CASE
          WHEN p.ball_hit_location_y IS NOT NULL
           AND (p.ball_hit_location_y)::double precision < :half_y
            THEN TRUE
          ELSE FALSE
        END,

      invert_bounce =
        CASE
          WHEN p.ball_hit_location_y IS NOT NULL
           AND (p.ball_hit_location_y)::double precision > :half_y
            THEN TRUE
          ELSE FALSE
        END,

      ball_hit_x_norm =
        CASE
          WHEN p.ball_hit_location_x IS NULL THEN NULL
          WHEN p.ball_hit_location_y IS NOT NULL
           AND (p.ball_hit_location_y)::double precision < :half_y
            THEN :court_w - (p.ball_hit_location_x)::double precision
          ELSE (p.ball_hit_location_x)::double precision
        END,

      ball_hit_y_norm =
        CASE
          WHEN p.ball_hit_location_y IS NULL THEN NULL
          WHEN p.ball_hit_location_y IS NOT NULL
           AND (p.ball_hit_location_y)::double precision < :half_y
            THEN :court_len - (p.ball_hit_location_y)::double precision
          ELSE (p.ball_hit_location_y)::double precision
        END,

      ball_bounce_x_norm =
        CASE
          WHEN p.court_x IS NULL THEN NULL
          WHEN p.ball_hit_location_y IS NOT NULL
           AND (p.ball_hit_location_y)::double precision > :half_y
            THEN :court_w - (p.court_x)::double precision
          ELSE (p.court_x)::double precision
        END,

      ball_bounce_y_norm =
        CASE
          WHEN p.court_y IS NULL THEN NULL
          WHEN p.ball_hit_location_y IS NOT NULL
           AND (p.ball_hit_location_y)::double precision > :half_y
            THEN :court_len - (p.court_y)::double precision
          ELSE (p.court_y)::double precision
        END
    WHERE p.task_id = :tid;
    """
    return conn.execute(
        text(sql),
        {
            "tid": task_id,
            "half_y": float(HALF_Y),
            "court_len": float(COURT_LEN),
            "court_w": float(DOUBLES_W),
        },
    ).rowcount or 0

# ------------------------------- Phase 7 Analytical Views -------------------------------
def phase7_update(conn: Connection, task_id: str) -> int:
    """
    Phase 7: analytics / presentation features only
      - serve buckets
      - rally length
      - stroke classification
      - shot sequence keys

    Notes:
      - rally_length = pure rally length AFTER serve
      - serves excluded from aggression_d and depth_d
    """

    # 1) Serve buckets + stroke + row-level rally length
    sql_1 = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_bucket_d =
        CASE
          WHEN p.serve_location IN (1, 8) THEN 'wide'
          WHEN p.serve_location IN (2, 3, 6, 7) THEN 'body'
          WHEN p.serve_location IN (4, 5) THEN 'T'
          ELSE NULL
        END,

      rally_length =
        CASE
          WHEN p.shot_ix_in_point IS NULL THEN NULL
          WHEN p.shot_ix_in_point = 1 THEN 0
          ELSE p.shot_ix_in_point - 1
        END,

      stroke_d =
        CASE
          WHEN p.serve_d IS TRUE THEN 'Serve'

          WHEN p.volley IS TRUE THEN 'Volley'

          WHEN lower(COALESCE(p.swing_type,'')) IN ('fh_overhead','bh_overhead','overhead','smash')
            THEN 'Overhead'

          WHEN lower(COALESCE(p.swing_type,'')) = 'fh'
            THEN 'Forehand'

          WHEN lower(COALESCE(p.swing_type,'')) IN ('2h_bh','1h_bh')
            THEN 'Backhand'

          WHEN lower(COALESCE(p.swing_type,'')) IN ('slice','bh_slice','fh_slice')
            THEN 'Slice'

          WHEN lower(COALESCE(p.swing_type,'')) = 'other'
            THEN 'Other'

          ELSE 'Other'
        END,

      aggression_d =
        CASE
          WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
          WHEN p.ball_hit_y_norm IS NULL THEN NULL
          WHEN p.ball_hit_y_norm <= 24 THEN 'Attack'
          WHEN p.ball_hit_y_norm > 24 AND p.ball_hit_y_norm < 26 THEN 'Neutral'
          WHEN p.ball_hit_y_norm >= 26 THEN 'Defence'
          ELSE NULL
        END,

      depth_d =
        CASE
          WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
          WHEN p.ball_bounce_y_norm IS NULL THEN NULL
          WHEN p.ball_bounce_y_norm > 20 THEN 'Deep'
          WHEN p.ball_bounce_y_norm > 18 AND p.ball_bounce_y_norm <= 20 THEN 'Middle'
          WHEN p.ball_bounce_y_norm <= 18 THEN 'Short'
          ELSE NULL
        END
    WHERE p.task_id = :tid;
    """
    r1 = conn.execute(text(sql_1), {"tid": task_id}).rowcount or 0

    # 2) rally_length_point + bucket
    sql_2 = f"""
    WITH rl AS (
      SELECT
        p.id,
        MAX(p.rally_length) OVER (PARTITION BY p.task_id, p.point_key) AS rally_length_point
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      rally_length_point = rl.rally_length_point,
      rally_length_bucket_d =
        CASE
          WHEN rl.rally_length_point IS NULL THEN NULL
          WHEN rl.rally_length_point <= 4 THEN '0–4 shots'
          WHEN rl.rally_length_point <= 8 THEN '5–8 shots'
          ELSE '9+ shots'
        END
    FROM rl
    WHERE p.task_id = :tid
      AND p.id = rl.id;
    """
    r2 = conn.execute(text(sql_2), {"tid": task_id}).rowcount or 0

    # 3) shot_q + shot_key_q
    sql_3 = f"""
    WITH seq AS (
      SELECT
        p.id,
        ROW_NUMBER() OVER (
          PARTITION BY p.task_id
          ORDER BY p.ball_hit_s, p.player_id, p.shot_ix_in_point, p."timestamp", p.id
        ) AS shot_q
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      shot_q = s.shot_q,
      shot_key_q = p.task_id::text || '|' || s.shot_q::text
    FROM seq s
    WHERE p.task_id = :tid
      AND p.id = s.id;
    """
    r3 = conn.execute(text(sql_3), {"tid": task_id}).rowcount or 0

    return int((r1 or 0) + (r2 or 0) + (r3 or 0))

# ------------------------------- Orchestrator -------------------------------
def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    if not task_id:
        raise ValueError("task_id is required")

    out: Dict = {"ok": True, "task_id": task_id, "phase": phase}

    with engine.begin() as conn:
        ensure_table_exists(conn)

        # Ensure all columns upfront
        ensure_phase_columns(conn, PHASE1_COLS)
        if phase in ("all", "2", "3", "4", "5", "6", "7"):
            phase2_add_schema(conn)
        if phase in ("all", "3", "4", "5", "6", "7"):
            phase3_add_schema(conn)
        if phase in ("all", "4", "5", "6", "7"):
            phase4_add_schema(conn)
        if phase in ("all", "5", "6", "7"):
            phase5_add_schema(conn)
        if phase in ("all", "6", "7"):
            phase6_add_schema(conn)
        if phase in ("all", "7"):
            phase7_add_schema(conn)

        # Execution order
        if phase in ("all", "1"):
            if replace:
                _exec(
                    conn,
                    f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid",
                    {"tid": task_id},
                )
            out["phase1_rows_inserted"] = phase1_load(conn, task_id)

        if phase in ("all", "2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)

        if phase in ("all", "3"):
            out["phase3_rows_updated"] = phase3_update(conn, task_id)

        if phase in ("all", "4"):
            out["phase4_rows_updated"] = phase4_update(conn, task_id)

        if phase in ("all", "5"):
            out["phase5_rows_updated"] = phase5_update(conn, task_id)

        if phase in ("all", "6"):
            out["phase6_rows_updated"] = phase6_update(conn, task_id)

        if phase in ("all", "7"):
            out["phase7_rows_updated"] = phase7_update(conn, task_id)

    return out

# ------------------------------- CLI -------------------------------
if __name__ == "__main__":
    import argparse, json

    p = argparse.ArgumentParser(
        description="Silver point_detail — P1..P5 (serve context + point/game)"
    )
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument(
        "--phase",
        choices=["1", "2", "3", "4", "5", "6", "7","all"],
        default="all",
        help="which phase(s) to run",
    )
    p.add_argument(
        "--replace",
        action="store_true",
        help="delete existing rows for this task_id before Phase 1 load",
    )
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
