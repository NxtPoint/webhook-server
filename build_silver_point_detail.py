# build_silver_point_detail.py
# NextPoint Silver: silver.point_detail
# Phase 1: bronze.player_swing -> core + ball_hit_s + ball_hit_location_x/y
# Phase 2: bronze.ball_bounce -> type/timestamp/court_x/court_y (first bounce after contact)
# Phase 3: serve context (serve_d, server_end_d, serve_side_d, serve_try_ix_in_point, service_winner_d)
# Phase 4: serve_location + rally_location_hit/bounce
# Phase 5: point_number + exclusions + point_winner + game_number + (optional) server_id/shot_ix/etc if you later extend

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
    "game_winner_player_id":  "integer",
    "server_id":              "text",
    "shot_ix_in_point":       "integer",
    "shot_phase_d":           "text",
    "shot_outcome_d":         "text",
    "point_key":              "text",
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


# ------------------------------- PHASE 1 ---------------------------------
def phase1_load(conn: Connection, task_id: str) -> int:
    """
    Insert core fields + split x/y + ball_hit_s.
    Structural fix B: ON CONFLICT DO NOTHING (together with UNIQUE(task_id,id)) makes reruns safe.
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
    res = conn.execute(text(sql), {"tid": task_id})
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


# ------------------------------- PHASE 3 ---------------------------------
def phase3_update(conn: Connection, task_id: str) -> int:
    """
    Implements requested Phase 3 rules:

    serve_d:
      TRUE when swing_type is overhead-ish AND ball_hit_location_y is <1 OR >23.

    server_end_d:
      if serve_d TRUE:
        y < 1  -> 'far'
        else   -> 'near'

    serve_side_d:
      Use midpoint = AVG(ball_hit_location_x) over serves in task.
      Mapping relative to end:
        near: x > mid -> deuce, x < mid -> ad
        far : x < mid -> deuce, x > mid -> ad

    serve_try_ix_in_point:
      Detect 2nd serve when a serve is preceded by another serve by same player on same end+side
      with NO opponent shot in between.
      Label:
        first serve in that micro-point -> '1st'
        second serve -> '2nd' unless bounce coords missing then 'Double'

    service_winner_d:
      TRUE on last serve in that micro-point when:
        - not Double
        - no later non-serve valid shot (with court_x & court_y present) occurs before the next serve by anyone
      NULL otherwise.
    """

    # 1) Midpoint for X (avg of serve hit x). Default to 5.6 if no serves.
    sql_mid = f"""
    WITH srv AS (
      SELECT NULLIF(TRIM(ball_hit_location_x::text), '')::double precision AS x
      FROM {SILVER_SCHEMA}.{TABLE}
      WHERE task_id = :tid
        AND ball_hit_location_x IS NOT NULL
        AND ball_hit_location_y IS NOT NULL
        AND (
          lower(COALESCE(trim(swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
        )
        AND (
          (ball_hit_location_y)::double precision < 1.0
          OR (ball_hit_location_y)::double precision > 23.0
        )
    )
    SELECT COALESCE(AVG(x), 5.6) FROM srv;
    """
    mid_x = conn.execute(text(sql_mid), {"tid": task_id}).scalar()
    if mid_x is None:
        mid_x = 5.6

    # 2) Full Phase 3 update in one pass.
    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.player_id,
        p.valid,
        p.swing_type,
        p.ball_hit_s,
        p.ball_hit_location_x AS x,
        p.ball_hit_location_y AS y,
        p.court_x,
        p.court_y
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),

    srv0 AS (
      SELECT
        b.*,
        (
          lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
          AND b.y IS NOT NULL
          AND ( (b.y)::double precision < 1.0 OR (b.y)::double precision > 23.0 )
        ) AS serve_d,

        CASE
          WHEN (
            lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
            AND b.y IS NOT NULL
            AND ( (b.y)::double precision < 1.0 OR (b.y)::double precision > 23.0 )
          )
          THEN CASE WHEN (b.y)::double precision < 1.0 THEN 'far' ELSE 'near' END
          ELSE NULL
        END AS server_end_d
      FROM base b
    ),

    srv1 AS (
      SELECT
        s.*,
        CASE
          WHEN s.serve_d IS TRUE
           AND s.x IS NOT NULL
           AND s.server_end_d IN ('near','far')
          THEN
            CASE
              WHEN s.server_end_d = 'near' THEN
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
        END AS serve_side_d
      FROM srv0 s
    ),

    -- mark opponent "real shots" (non-serve, valid, with court coords)
    shots_valid AS (
      SELECT
        b.task_id,
        b.player_id,
        b.ball_hit_s AS t,
        b.id
      FROM base b
      WHERE b.valid IS TRUE
        AND b.court_x IS NOT NULL
        AND b.court_y IS NOT NULL
    ),

    serves AS (
      SELECT
        s.*,
        ROW_NUMBER() OVER (PARTITION BY s.task_id ORDER BY s.ball_hit_s, s.id) AS serve_global_ix
      FROM srv1 s
      WHERE s.serve_d IS TRUE
        AND s.ball_hit_s IS NOT NULL
    ),

    -- For each serve, find the immediately previous serve by same player with same end+side
    prev_same AS (
      SELECT
        a.id AS serve_id,
        (
          SELECT p.id
          FROM serves p
          WHERE p.task_id = a.task_id
            AND p.player_id = a.player_id
            AND p.server_end_d = a.server_end_d
            AND p.serve_side_d = a.serve_side_d
            AND (p.ball_hit_s < a.ball_hit_s OR (p.ball_hit_s = a.ball_hit_s AND p.id < a.id))
          ORDER BY p.ball_hit_s DESC, p.id DESC
          LIMIT 1
        ) AS prev_serve_id
      FROM serves a
    ),

    prev_detail AS (
      SELECT
        ps.serve_id,
        ps.prev_serve_id,
        p.ball_hit_s AS prev_t
      FROM prev_same ps
      LEFT JOIN serves p
        ON p.id = ps.prev_serve_id
    ),

    -- determine if there is any opponent shot between prev serve and this serve
    second_serve_flag AS (
      SELECT
        s.id AS serve_id,
        CASE
          WHEN pd.prev_serve_id IS NULL THEN FALSE
          ELSE
            NOT EXISTS (
              SELECT 1
              FROM shots_valid r
              WHERE r.task_id = s.task_id
                AND r.t > pd.prev_t
                AND r.t < s.ball_hit_s
                AND r.player_id <> s.player_id
            )
        END AS is_second_serve
      FROM serves s
      LEFT JOIN prev_detail pd
        ON pd.serve_id = s.id
    ),

    serve_labels AS (
      SELECT
        s.id,
        s.serve_d,
        s.server_end_d,
        s.serve_side_d,

        CASE
          WHEN s.serve_d IS NOT TRUE THEN NULL
          WHEN sf.is_second_serve IS TRUE THEN
            CASE
              WHEN s.court_x IS NULL OR s.court_y IS NULL THEN 'Double'
              ELSE '2nd'
            END
          ELSE '1st'
        END AS serve_try_ix_in_point,

        -- service winner: last serve of the micro-point
        CASE
          WHEN s.serve_d IS TRUE
           AND (CASE
                  WHEN sf.is_second_serve IS TRUE AND (s.court_x IS NULL OR s.court_y IS NULL)
                    THEN 'Double'
                  WHEN sf.is_second_serve IS TRUE
                    THEN '2nd'
                  ELSE '1st'
                END) <> 'Double'
           AND NOT EXISTS (
             -- any later non-serve valid court shot before the next serve by anyone?
             SELECT 1
             FROM base q
             WHERE q.task_id = s.task_id
               AND q.ball_hit_s > s.ball_hit_s
               AND q.valid IS TRUE
               AND q.court_x IS NOT NULL
               AND q.court_y IS NOT NULL
               AND NOT (
                 lower(COALESCE(trim(q.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
                 AND q.ball_hit_location_y IS NOT NULL
                 AND ( (q.ball_hit_location_y)::double precision < 1.0 OR (q.ball_hit_location_y)::double precision > 23.0 )
               )
               AND q.ball_hit_s < COALESCE(
                 (SELECT MIN(z.ball_hit_s) FROM serves z
                  WHERE z.task_id = s.task_id AND z.ball_hit_s > s.ball_hit_s),
                 1e15
               )
           )
          THEN TRUE
          ELSE NULL
        END AS service_winner_d
      FROM serves s
      LEFT JOIN second_serve_flag sf
        ON sf.serve_id = s.id
    )

    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET
      serve_d               = sl.serve_d,
      server_end_d          = sl.server_end_d,
      serve_side_d          = sl.serve_side_d,
      serve_try_ix_in_point = sl.serve_try_ix_in_point,
      service_winner_d      = sl.service_winner_d
    FROM serve_labels sl
    WHERE p.task_id = :tid
      AND p.id = sl.id;
    """
    res = conn.execute(text(sql), {"tid": task_id, "mid": float(mid_x)})
    return res.rowcount or 0


# ------------------------------- PHASE 4 ---------------------------------
def phase4_update(conn: Connection, task_id: str) -> int:
    # Serve location bands per your later baseline (uses court_x + server_end_d + serve_side_d)
    sql_srv = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET serve_location =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS NOT TRUE THEN NULL
        ELSE
          CASE
            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'near'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'deuce'
            THEN
              CASE
                WHEN p.court_x IS NULL THEN 3
                WHEN p.court_x < 1 THEN 1
                WHEN p.court_x > 3 THEN 4
                WHEN p.court_x > 1 AND p.court_x < 2 THEN 2
                ELSE 3
              END

            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'near'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'ad'
            THEN
              CASE
                WHEN p.court_x IS NULL THEN 7
                WHEN p.court_x < 5 THEN 5
                WHEN p.court_x > 7 THEN 8
                WHEN p.court_x > 5 AND p.court_x < 6 THEN 6
                ELSE 7
              END

            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'far'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'ad'
            THEN
              CASE
                WHEN p.court_x IS NULL THEN 6
                WHEN p.court_x < 1 THEN 8
                WHEN p.court_x > 3 THEN 5
                WHEN p.court_x > 1 AND p.court_x < 2 THEN 7
                ELSE 6
              END

            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'far'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'deuce'
            THEN
              CASE
                WHEN p.court_x IS NULL THEN 2
                WHEN p.court_x < 5 THEN 4
                WHEN p.court_x > 7 THEN 1
                WHEN p.court_x > 5 AND p.court_x < 6 THEN 3
                ELSE 2
              END

            ELSE NULL
          END
      END
    WHERE p.task_id = :tid;
    """
    r1 = conn.execute(text(sql_srv), {"tid": task_id}).rowcount or 0

    sql_rl_hit = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_hit =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        ELSE
          CASE
            WHEN p.ball_hit_location_x IS NULL OR p.ball_hit_location_y IS NULL THEN NULL
            WHEN p.ball_hit_location_y >= 11.6 THEN
              CASE
                WHEN p.ball_hit_location_x < 2 THEN 'D'
                WHEN p.ball_hit_location_x < 4 THEN 'C'
                WHEN p.ball_hit_location_x < 6 THEN 'B'
                ELSE 'A'
              END
            ELSE
              CASE
                WHEN p.ball_hit_location_x < 2 THEN 'A'
                WHEN p.ball_hit_location_x < 4 THEN 'B'
                WHEN p.ball_hit_location_x < 6 THEN 'C'
                ELSE 'D'
              END
          END
      END
    WHERE p.task_id = :tid;
    """
    r2 = conn.execute(text(sql_rl_hit), {"tid": task_id}).rowcount or 0

    sql_rl_bnc = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_bounce =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        WHEN p.court_x IS NULL THEN p.rally_location_hit
        WHEN p.ball_hit_location_y IS NULL THEN NULL
        ELSE
          CASE
            WHEN p.ball_hit_location_y > 11.6 THEN
              CASE
                WHEN p.court_x < 2 THEN 'A'
                WHEN p.court_x < 4 THEN 'B'
                WHEN p.court_x < 6 THEN 'C'
                ELSE 'D'
              END
            ELSE
              CASE
                WHEN p.court_x < 2 THEN 'D'
                WHEN p.court_x < 4 THEN 'C'
                WHEN p.court_x < 6 THEN 'B'
                ELSE 'A'
              END
          END
      END
    WHERE p.task_id = :tid;
    """
    r3 = conn.execute(text(sql_rl_bnc), {"tid": task_id}).rowcount or 0

    return int(r1 + r2 + r3)


# ------------------------------- PHASE 5 ---------------------------------
def phase5_fix_point_number(conn: Connection, task_id: str) -> int:
    """
    point_number increments ONLY when serve_side_d changes on FIRST serves.
    Here, FIRST serves are serve_d=TRUE AND serve_try_ix_in_point='1st'.
    """
    sql = f"""
    WITH anchors AS (
      SELECT
        p.task_id,
        p.ball_hit_s AS anchor_s,
        p.serve_side_d AS side
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND LOWER(COALESCE(p.serve_try_ix_in_point::text,'')) = '1st'
        AND p.serve_side_d IN ('deuce','ad')
        AND p.ball_hit_s IS NOT NULL
      ORDER BY p.ball_hit_s, p.id
    ),
    incs AS (
      SELECT
        a.*,
        CASE
          WHEN LAG(a.side) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s) IS DISTINCT FROM a.side THEN 1
          ELSE 0
        END AS inc0
      FROM anchors a
    ),
    incs_norm AS (
      SELECT
        i.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY i.task_id ORDER BY i.anchor_s) = 1 THEN 1
          ELSE i.inc0
        END AS inc
      FROM incs i
    ),
    pn_rows AS (
      SELECT
        p.id,
        COALESCE(
          (SELECT SUM(n.inc)
           FROM incs_norm n
           WHERE n.task_id = p.task_id
             AND n.anchor_s <= p.ball_hit_s),
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
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0

def phase5_apply_exclusions(conn: Connection, task_id: str) -> int:
    """
    Minimal exclusions aligned to your later baseline:
      1) if serve_side_d is NULL -> exclude_d=TRUE
      2) non-serve before last serve in point -> exclude_d=TRUE
      3) gap > 5s after last serve in point -> exclude this + rest of point
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id, p.point_number, p.player_id, p.ball_hit_s,
        COALESCE(p.serve_d, FALSE) AS serve_d,
        p.serve_side_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.ball_hit_s IS NOT NULL
    ),
    pls AS (
      SELECT
        b.task_id, b.point_number,
        MAX(CASE WHEN b.serve_d THEN b.ball_hit_s END) AS last_serve_s
      FROM base b
      GROUP BY b.task_id, b.point_number
    ),
    ordered AS (
      SELECT
        b.*,
        pls.last_serve_s,
        LAG(b.ball_hit_s) OVER (PARTITION BY b.task_id, b.point_number ORDER BY b.ball_hit_s, b.id) AS prev_s
      FROM base b
      LEFT JOIN pls
        ON pls.task_id = b.task_id
       AND pls.point_number = b.point_number
    ),
    flagged AS (
      SELECT
        o.*,
        (o.serve_side_d IS NULL) AS r1_side_null,
        (NOT o.serve_d AND o.last_serve_s IS NOT NULL AND o.ball_hit_s < o.last_serve_s) AS r2_before_last_serve,
        CASE
          WHEN o.prev_s IS NULL OR o.last_serve_s IS NULL OR o.ball_hit_s <= o.last_serve_s THEN FALSE
          ELSE (o.ball_hit_s - o.prev_s) > 5.0
        END AS gap_break
      FROM ordered o
    ),
    chain AS (
      SELECT
        f.*,
        BOOL_OR(f.gap_break) OVER (
          PARTITION BY f.task_id, f.point_number
          ORDER BY f.ball_hit_s, f.id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS r3_gap_chain
      FROM flagged f
    ),
    excl AS (
      SELECT
        c.id,
        (c.r1_side_null OR c.r2_before_last_serve OR c.r3_gap_chain) AS exclude_d
      FROM chain c
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET exclude_d = e.exclude_d
    FROM excl e
    WHERE p.task_id = :tid
      AND p.id = e.id;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0

def phase5_set_point_winner(conn: Connection, task_id: str) -> int:
    """
    Winner (simple + stable):
      - if any 'Double' serve in point -> receiver wins
      - else if any service_winner_d TRUE in point -> server wins
      - else last non-excluded valid shot -> last hitter
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.task_id, p.point_number, p.id, p.player_id,
        COALESCE(p.exclude_d, FALSE) AS exclude_d,
        COALESCE(p.valid, TRUE) AS valid,
        COALESCE(p.serve_d, FALSE) AS serve_d,
        LOWER(COALESCE(p.serve_try_ix_in_point::text,'')) AS try_l,
        COALESCE(p.service_winner_d, FALSE) AS sw,
        p.ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND p.point_number IS NOT NULL
        AND p.point_number > 0
    ),
    players AS (
      SELECT DISTINCT task_id, player_id FROM base
    ),
    point_server AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id, b.point_number, b.player_id AS server_id
      FROM base b
      WHERE b.serve_d IS TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s, b.id
    ),
    point_receiver AS (
      SELECT
        ps.task_id, ps.point_number,
        MIN(p.player_id) FILTER (WHERE p.player_id <> ps.server_id) AS receiver_id
      FROM point_server ps
      JOIN players p ON p.task_id = ps.task_id
      GROUP BY ps.task_id, ps.point_number
    ),
    flags AS (
      SELECT
        b.task_id, b.point_number,
        BOOL_OR(b.serve_d AND b.try_l LIKE 'double%') AS any_double,
        BOOL_OR(b.sw) AS any_sw
      FROM base b
      GROUP BY b.task_id, b.point_number
    ),
    last_valid AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id, b.point_number, b.player_id AS last_pid
      FROM base b
      WHERE b.exclude_d IS FALSE
        AND b.valid IS TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s DESC, b.id DESC
    ),
    winners AS (
      SELECT
        ps.task_id, ps.point_number,
        CASE
          WHEN f.any_double IS TRUE THEN pr.receiver_id
          WHEN f.any_sw IS TRUE THEN ps.server_id
          ELSE lv.last_pid
        END AS winner_pid
      FROM point_server ps
      LEFT JOIN point_receiver pr
        ON pr.task_id = ps.task_id AND pr.point_number = ps.point_number
      LEFT JOIN flags f
        ON f.task_id = ps.task_id AND f.point_number = ps.point_number
      LEFT JOIN last_valid lv
        ON lv.task_id = ps.task_id AND lv.point_number = ps.point_number
    )
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET point_winner_player_id = w.winner_pid
    FROM winners w
    WHERE p.task_id = :tid
      AND p.point_number = w.point_number;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0

def phase5_fix_game_number(conn: Connection, task_id: str) -> int:
    """
    game_number increments when server_end_d flips near↔far on FIRST serves (try='1st').
    """
    sql = f"""
    WITH anchors AS (
      SELECT
        p.task_id,
        p.ball_hit_s AS anchor_s,
        p.server_end_d AS end_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND LOWER(COALESCE(p.serve_try_ix_in_point::text,'')) = '1st'
        AND p.server_end_d IN ('near','far')
        AND p.ball_hit_s IS NOT NULL
      ORDER BY p.ball_hit_s, p.id
    ),
    incs AS (
      SELECT
        a.*,
        CASE
          WHEN LAG(a.end_d) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s) IS DISTINCT FROM a.end_d THEN 1
          ELSE 0
        END AS inc0
      FROM anchors a
    ),
    incs_norm AS (
      SELECT
        i.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY i.task_id ORDER BY i.anchor_s) = 1 THEN 1
          ELSE i.inc0
        END AS inc
      FROM incs i
    ),
    g_rows AS (
      SELECT
        p.id,
        COALESCE(
          (SELECT SUM(n.inc)
           FROM incs_norm n
           WHERE n.task_id = p.task_id
             AND n.anchor_s <= p.ball_hit_s),
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
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0

def phase5_update(conn: Connection, task_id: str) -> int:
    r1 = phase5_fix_point_number(conn, task_id)
    r2 = phase5_apply_exclusions(conn, task_id)
    r3 = phase5_set_point_winner(conn, task_id)
    r4 = phase5_fix_game_number(conn, task_id)
    return int((r1 or 0) + (r2 or 0) + (r3 or 0) + (r4 or 0))


# ------------------------------- Orchestrator -------------------------------
def build_silver(task_id: str, phase: str = "all", replace: bool = False) -> Dict:
    if not task_id:
        raise ValueError("task_id is required")

    out: Dict = {"ok": True, "task_id": task_id, "phase": phase}

    with engine.begin() as conn:
        ensure_table_exists(conn)

        # Ensure all columns upfront (safe)
        ensure_phase_columns(conn, PHASE1_COLS)
        if phase in ("all", "2", "3", "4", "5"):
            phase2_add_schema(conn)
        if phase in ("all", "3", "4", "5"):
            phase3_add_schema(conn)
        if phase in ("all", "4", "5"):
            phase4_add_schema(conn)
        if phase in ("all", "5"):
            phase5_add_schema(conn)

        # Phase execution in correct dependency order
        if phase in ("all", "1"):
            if replace:
                _exec(conn, f"DELETE FROM {SILVER_SCHEMA}.{TABLE} WHERE task_id=:tid", {"tid": task_id})
            out["phase1_rows_inserted"] = phase1_load(conn, task_id)

        if phase in ("all", "2"):
            out["phase2_rows_updated"] = phase2_update(conn, task_id)

        if phase in ("all", "3"):
            out["phase3_rows_updated"] = phase3_update(conn, task_id)

        if phase in ("all", "4"):
            out["phase4_rows_updated"] = phase4_update(conn, task_id)

        if phase in ("all", "5"):
            out["phase5_rows_updated"] = phase5_update(conn, task_id)

    return out


# ------------------------------- CLI -------------------------------
if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser(description="Silver point_detail — P1..P5 (serve context + point/game)")
    p.add_argument("--task-id", required=True, help="task UUID")
    p.add_argument("--phase", choices=["1", "2", "3", "4", "5", "all"], default="all", help="which phase(s) to run")
    p.add_argument("--replace", action="store_true", help="delete existing rows for this task_id before Phase 1 load")
    args = p.parse_args()
    print(json.dumps(build_silver(task_id=args.task_id, phase=args.phase, replace=args.replace)))
