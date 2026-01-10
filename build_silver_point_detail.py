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
    "game_winner_player_id":  "text",
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
    Phase 3 (production-grade, baseline-compatible, Postgres-safe):

    1) serve_d respects SportAI flag:
       - if serve = FALSE -> serve_d = FALSE
       - else serve_d = heuristic (overhead-ish AND (y < 1 OR y > 23))

    2) server_end_d business rule (as documented):
       - for serve rows:
           if y < 1 -> 'far'
           else     -> 'near'
       - persisted on every row by carrying forward the most recent *serve-derived* end
         using a LATERAL "last serve" lookup (stable; no MAX/MIN text fill).

    Everything else unchanged (serve_side_d, serve_try_ix_in_point, service_winner_d).
    """

    # -------------------
    # Preflight checks (non-fatal; only to avoid bad runs)
    # -------------------
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
        AND ((b.y)::double precision < 1.0 OR (b.y)::double precision > 23.0)
        AND b.ball_hit_s IS NOT NULL
      LIMIT 1
    )
    SELECT
      (SELECT COUNT(DISTINCT player_id) FROM base WHERE player_id IS NOT NULL) AS player_cnt,
      (SELECT COUNT(*) FROM base WHERE sportai_serve IS TRUE AND ball_hit_s IS NULL) AS serve_null_s_cnt,
      (SELECT COUNT(*) FROM base WHERE sportai_serve IS TRUE AND y IS NULL) AS serve_null_y_cnt,
      EXISTS (SELECT 1 FROM anchors) AS has_anchor;
    """
    chk = conn.execute(text(sql_checks), {"tid": task_id}).mappings().first() or {}
    # Fail-closed if we can’t compute anything meaningful
    if not chk.get("has_anchor", False):
        # no qualifying serve anchors => do nothing; keeps baseline stable
        return 0

    # -------------------
    # Midpoint for X (avg serve-hit x). Default to 5.6 if no serves.
    # -------------------
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
          (ball_hit_location_y)::double precision < 1.0
          OR (ball_hit_location_y)::double precision > 23.0
        )
    )
    SELECT COALESCE(AVG(x), 5.6) FROM srv;
    """
    mid_x = conn.execute(text(sql_mid), {"tid": task_id}).scalar()
    if mid_x is None:
        mid_x = 5.6

    # -------------------
    # Main Phase 3 update
    # -------------------
    sql = f"""
    WITH base AS (
      SELECT
        p.id,
        p.task_id,
        p.player_id,
        p.valid,
        p.serve,          -- SportAI serve flag
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

    -- compute serve_d_raw + server_end_raw only from SportAI-serve TRUE rows
    srv0 AS (
      SELECT
        b.*,

        CASE
          WHEN COALESCE(b.serve, FALSE) IS FALSE THEN FALSE
          WHEN (
            lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
            AND b.y IS NOT NULL
            AND ( (b.y)::double precision < 1.0 OR (b.y)::double precision > 23.0 )
          ) THEN TRUE
          ELSE FALSE
        END AS serve_d_raw,

        -- documented end rule: y < 1 => far else near (only for serve rows)
        CASE
          WHEN COALESCE(b.serve, FALSE) IS FALSE THEN NULL
          WHEN (
            lower(COALESCE(trim(b.swing_type), '')) IN ('fh_overhead','bh_overhead','overhead','smash','other')
            AND b.y IS NOT NULL
            AND ( (b.y)::double precision < 1.0 OR (b.y)::double precision > 23.0 )
          )
          THEN CASE WHEN (b.y)::double precision < 1.0 THEN 'far' ELSE 'near' END
          ELSE NULL
        END AS server_end_raw
      FROM base b
    ),

    -- persist server_end_d by last known serve-derived end at or before each row
    srv1 AS (
      SELECT
        s.*,
        s.serve_d_raw AS serve_d,
        COALESCE(s.server_end_raw, lastsrv.server_end_raw) AS server_end_d,

        CASE
          WHEN s.serve_d_raw IS TRUE
           AND s.x IS NOT NULL
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
        END AS serve_side_d
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

    -- opponent "real shots" (valid, with court coords)
    shots_valid AS (
      SELECT
        b.task_id,
        b.player_id,
        b.ball_hit_s AS t,
        b.id
      FROM srv1 b
      WHERE b.valid IS TRUE
        AND b.court_x IS NOT NULL
        AND b.court_y IS NOT NULL
    ),

    serves AS (
      SELECT
        s.*
      FROM srv1 s
      WHERE s.serve_d IS TRUE
    ),

    -- previous serve by same player on same end+side
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

    -- 2nd serve if no opponent shot occurs between prev serve and this serve
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
        CASE
          WHEN s.serve_d IS NOT TRUE THEN NULL
          WHEN sf.is_second_serve IS TRUE THEN
            CASE
              WHEN s.court_x IS NULL OR s.court_y IS NULL THEN 'Double'
              ELSE '2nd'
            END
          ELSE '1st'
        END AS serve_try_ix_in_point,

        CASE
          WHEN s.serve_d IS TRUE
           AND (CASE
                  WHEN sf.is_second_serve IS TRUE AND (s.court_x IS NULL OR s.court_y IS NULL) THEN 'Double'
                  WHEN sf.is_second_serve IS TRUE THEN '2nd'
                  ELSE '1st'
                END) <> 'Double'
           AND NOT EXISTS (
             SELECT 1
             FROM srv1 q
             WHERE q.task_id = s.task_id
               AND q.ball_hit_s > s.ball_hit_s
               AND q.valid IS TRUE
               AND q.court_x IS NOT NULL
               AND q.court_y IS NOT NULL
               AND q.serve_d IS NOT TRUE
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
      serve_d               = s1.serve_d,
      server_end_d          = s1.server_end_d,
      serve_side_d          = s1.serve_side_d,
      serve_try_ix_in_point = sl.serve_try_ix_in_point,
      service_winner_d      = sl.service_winner_d
    FROM srv1 s1
    LEFT JOIN serve_labels sl
      ON sl.id = s1.id
    WHERE p.task_id = :tid
      AND p.id = s1.id;
    """
    res = conn.execute(text(sql), {"tid": task_id, "mid": float(mid_x)})
    return res.rowcount or 0


# ------------------------------- PHASE 4 ---------------------------------
def phase4_update(conn: Connection, task_id: str) -> int:
    """
    Phase 4:
      - serve_location        : 1–8 (from court_x, server_end_d, serve_side_d)
      - rally_location_hit    : A–D (from ball_hit_location_x / y)
      - rally_location_bounce : A–D (from court_x + ball_hit_location_y, fallback to hit)
    """

    # 0) Ensure P4 columns exist
    ensure_phase_columns(conn, OrderedDict({
        "serve_location":        "integer",
        "rally_location_hit":    "text",
        "rally_location_bounce": "text",
    }))

    # 1) Serve location (1–8) — SPEC:
    # near + deuce  : court_x <1 →1; >3→4; >1 & <2→2; else→3; NULL→3
    # near + ad     : court_x <5 →5; >7→8; >5 & <6→6; else→7; NULL→7
    # far  + ad     : same as near+ad
    # far  + deuce  : same as near+deuce; NULL→3
    sql_srv = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET serve_location =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS NOT TRUE THEN NULL

        ELSE
          CASE
            -- NEAR + DEUCE
            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'near'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'deuce'
            THEN
              CASE
                WHEN NULLIF(TRIM(p.court_x::text), '') IS NULL
                  THEN 3
                WHEN (p.court_x)::double precision < 1
                  THEN 1
                WHEN (p.court_x)::double precision > 3
                  THEN 4
                WHEN (p.court_x)::double precision > 1
                     AND (p.court_x)::double precision < 2
                  THEN 2
                ELSE 3
              END

            -- NEAR + AD
            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'near'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'ad'
            THEN
              CASE
                WHEN NULLIF(TRIM(p.court_x::text), '') IS NULL
                  THEN 7
                WHEN (p.court_x)::double precision < 5
                  THEN 5
                WHEN (p.court_x)::double precision > 7
                  THEN 8
                WHEN (p.court_x)::double precision > 5
                     AND (p.court_x)::double precision < 6
                  THEN 6
                ELSE 7
              END

            -- FAR + AD (same bands as NEAR + AD)
            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'far'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'ad'
            THEN
              CASE
                WHEN NULLIF(TRIM(p.court_x::text), '') IS NULL
                  THEN 6
                WHEN (p.court_x)::double precision < 1
                  THEN 8
                WHEN (p.court_x)::double precision > 3
                  THEN 5
                WHEN (p.court_x)::double precision > 1
                     AND (p.court_x)::double precision < 2
                  THEN 7
                ELSE 6
              END

            -- FAR + DEUCE (same bands as NEAR + DEUCE)
            WHEN lower(COALESCE(TRIM(p.server_end_d), '')) = 'far'
             AND lower(COALESCE(TRIM(p.serve_side_d), '')) = 'deuce'
            THEN
              CASE
                WHEN NULLIF(TRIM(p.court_x::text), '') IS NULL
                  THEN 2
                WHEN (p.court_x)::double precision < 5
                  THEN 4
                WHEN (p.court_x)::double precision > 7
                  THEN 1
                WHEN (p.court_x)::double precision > 5
                     AND (p.court_x)::double precision < 6
                  THEN 3
                ELSE 2
              END

            -- If we somehow don't know side/end, leave NULL
            ELSE NULL
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(text(sql_srv), {"tid": task_id})

    # 2) Rally location (hit): A–D (this is the version you confirmed as correct)
    sql_rl_hit = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_hit =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL
        ELSE
          CASE
            WHEN NULLIF(TRIM(p.ball_hit_location_x::text), '') IS NULL THEN NULL
            WHEN NULLIF(TRIM(p.ball_hit_location_y::text), '') IS NULL THEN NULL

            -- y >= 11.6 → far half
            WHEN (p.ball_hit_location_y)::double precision >= 11.6 THEN
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 'D'
                WHEN (p.ball_hit_location_x)::double precision < 4 THEN 'C'
                WHEN (p.ball_hit_location_x)::double precision < 6 THEN 'B'
                ELSE 'A'
              END

            -- y < 11.6 → near half
            ELSE
              CASE
                WHEN (p.ball_hit_location_x)::double precision < 2 THEN 'A'
                WHEN (p.ball_hit_location_x)::double precision < 4 THEN 'B'
                WHEN (p.ball_hit_location_x)::double precision < 6 THEN 'C'
                ELSE 'D'
              END
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(text(sql_rl_hit), {"tid": task_id})

    # 3) Rally location (bounce): A–D from court_x + ball_hit_location_y
    #    If court_x NULL → fall back to rally_location_hit.
    sql_rl_bnc = f"""
    UPDATE {SILVER_SCHEMA}.{TABLE} p
    SET rally_location_bounce =
      CASE
        WHEN COALESCE(p.serve_d, FALSE) IS TRUE THEN NULL

        -- No bounce X → use the hit location band
        WHEN NULLIF(TRIM(p.court_x::text), '') IS NULL
          THEN p.rally_location_hit

        -- Need y to know side; if missing, return NULL
        WHEN NULLIF(TRIM(p.ball_hit_location_y::text), '') IS NULL
          THEN NULL

        ELSE
          CASE
            -- y > 11.6 → hitter on near side:
            --   court_x <2 'A', 2–4 'B', 4–6 'C', >6 'D'
            WHEN (p.ball_hit_location_y)::double precision > 11.6 THEN
              CASE
                WHEN (p.court_x)::double precision < 2 THEN 'A'
                WHEN (p.court_x)::double precision < 4 THEN 'B'
                WHEN (p.court_x)::double precision < 6 THEN 'C'
                ELSE 'D'
              END

            -- y <= 11.6 → hitter on far side:
            --   court_x <2 'D', 2–4 'C', 4–6 'B', >6 'A'
            ELSE
              CASE
                WHEN (p.court_x)::double precision < 2 THEN 'D'
                WHEN (p.court_x)::double precision < 4 THEN 'C'
                WHEN (p.court_x)::double precision < 6 THEN 'B'
                ELSE 'A'
              END
          END
      END
    WHERE p.task_id = :tid;
    """
    conn.execute(text(sql_rl_bnc), {"tid": task_id})

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
#   - fail-closed preflight: needs >=1 first-serve anchor and exactly 2 players

def phase5_update(conn: Connection, task_id: str) -> int:
    _phase5_preflight(conn, task_id)

    r1 = phase5_fix_point_number(conn, task_id)
    r2 = phase5_apply_exclusions(conn, task_id)
    r3 = phase5_set_point_winner(conn, task_id)
    r4 = phase5_set_game_winner(conn, task_id)
    r5 = phase5_fix_game_number(conn, task_id)
    r6 = phase5_set_server_id(conn, task_id)
    r7 = phase5_set_shot_ix_in_point(conn, task_id)
    r8 = phase5_set_shot_phase(conn, task_id)
    r9 = phase5_set_point_key(conn, task_id)
    r10 = phase5_set_shot_outcome(conn, task_id)

    return int((r1 or 0) + (r2 or 0) + (r3 or 0) + (r4 or 0) + (r5 or 0) + (r6 or 0) + (r7 or 0) + (r8 or 0) + (r9 or 0) + (r10 or 0))


def _phase5_preflight(conn: Connection, task_id: str) -> None:
    sql = f"""
    WITH base AS (
      SELECT
        task_id,
        player_id,
        COALESCE(serve_d, FALSE) AS serve_d,
        LOWER(COALESCE(serve_try_ix_in_point::text,'')) AS try_d,
        ball_hit_s
      FROM {SILVER_SCHEMA}.{TABLE}
      WHERE task_id = :tid
    ),
    first_serves AS (
      SELECT 1
      FROM base
      WHERE serve_d IS TRUE
        AND try_d = '1st'
        AND ball_hit_s IS NOT NULL
      LIMIT 1
    )
    SELECT
      (SELECT COUNT(DISTINCT player_id) FROM base WHERE player_id IS NOT NULL) AS player_cnt,
      EXISTS (SELECT 1 FROM first_serves) AS has_first_serve
    ;
    """
    row = conn.execute(text(sql), {"tid": task_id}).mappings().first() or {}
    player_cnt = int(row.get("player_cnt") or 0)
    has_first = bool(row.get("has_first_serve"))
    if player_cnt != 2:
        raise ValueError(f"Phase5 fail-closed: expected exactly 2 distinct player_id; got {player_cnt} (task_id={task_id})")
    if not has_first:
        raise ValueError(f"Phase5 fail-closed: no FIRST-serve anchors found (serve_d=TRUE and serve_try_ix_in_point='1st' with ball_hit_s not NULL) (task_id={task_id})")


def phase5_fix_point_number(conn: Connection, task_id: str) -> int:
    """
    point_number increments at FIRST serves when EITHER:
      - server (player_id) changes, OR
      - serve_side_d changes (deuce/ad)

    FIRST serves are rows with serve_d = TRUE and serve_try_ix_in_point = '1st'.
    Persist across all rows by time. First point = 1.

    Determinism:
      - anchor order uses (ball_hit_s NULLS LAST, id)
      - carry to rows with ball_hit_s NOT NULL only (NULL timestamps remain NULL point_number)
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
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND LOWER(COALESCE(p.serve_try_ix_in_point::text,'')) = '1st'
        AND p.ball_hit_s IS NOT NULL
      ORDER BY p.ball_hit_s, p.id
    ),
    incs AS (
      SELECT
        a.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY a.task_id ORDER BY a.anchor_s, a.id) = 1 THEN 1
          WHEN LAG(a.server_pid) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s, a.id)
               IS DISTINCT FROM a.server_pid THEN 1
          WHEN LAG(a.side) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s, a.id)
               IS DISTINCT FROM a.side THEN 1
          ELSE 0
        END AS inc
      FROM anchors a
    ),
    pn_rows AS (
      SELECT
        p.id,
        (SELECT SUM(i.inc)
         FROM incs i
         WHERE i.task_id = p.task_id
           AND i.anchor_s <= p.ball_hit_s) AS pn
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
    Minimal exclusions (reverted + tightened):

      1) non-serve BEFORE the last serve in the point -> exclude_d = TRUE
      2) gap > 5s AFTER the last serve in point -> exclude this + rest of point

    NOTE:
      - serve_side_d IS NULL is allowed (NOT an exclusion)
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
        COALESCE(p.serve_d, FALSE) AS serve_d
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
        ) AS gap_break
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
        f.r1_before_last_serve
      FROM flags f
      JOIN {SILVER_SCHEMA}.{TABLE} b
        ON b.id = f.id
    ),
    excl AS (
      SELECT
        id,
        (r1_before_last_serve OR r2_gap_chain) AS exclude_d
      FROM chain
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
    Winner priority per point:
      - any double-fault → receiver
      - else any service_winner_d → server
      - else last non-excluded, valid swing → that player
    """
    sql = f"""
    WITH base AS (
      SELECT
        p.id, p.task_id, p.player_id, p.valid,
        p.serve_d, p.serve_try_ix_in_point, p.service_winner_d,
        p.ball_hit_s, p.point_number,
        COALESCE(p.exclude_d, FALSE) AS exclude_d
      FROM {SILVER_SCHEMA}.{TABLE} p
      WHERE p.task_id = :tid
    ),
    point_server AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id, b.point_number, b.player_id AS server_id
      FROM base b
      WHERE b.point_number > 0
        AND COALESCE(b.serve_d, FALSE) IS TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s NULLS LAST, b.id
    ),
    point_receiver AS (
      SELECT
        ps.task_id, ps.point_number,
        MIN(tp.player_id) FILTER (WHERE tp.player_id <> ps.server_id) AS receiver_id
      FROM point_server ps
      JOIN (SELECT DISTINCT task_id, player_id FROM base) tp
        ON tp.task_id = ps.task_id
      GROUP BY ps.task_id, ps.point_number
    ),
    flags AS (
      SELECT
        b.task_id, b.point_number,
        BOOL_OR(COALESCE(b.serve_d, FALSE) IS TRUE
               AND LOWER(COALESCE(b.serve_try_ix_in_point::text,'')) LIKE 'double%') AS any_double,
        BOOL_OR(COALESCE(b.service_winner_d, FALSE)) AS any_sw
      FROM base b
      WHERE b.point_number > 0
      GROUP BY b.task_id, b.point_number
    ),
    last_valid AS (
      SELECT DISTINCT ON (b.task_id, b.point_number)
        b.task_id, b.point_number, b.player_id AS last_pid
      FROM base b
      WHERE b.point_number > 0
        AND b.exclude_d IS FALSE
        AND COALESCE(b.valid, TRUE) IS TRUE
      ORDER BY b.task_id, b.point_number, b.ball_hit_s DESC NULLS LAST, b.id DESC
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
    game_number increments when the SERVER changes at FIRST serves.
    FIRST serves are rows with serve_d = TRUE and serve_try_ix_in_point = '1st'.
    Persist to all rows by time. First game = 1.
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
        AND COALESCE(p.serve_d, FALSE) IS TRUE
        AND LOWER(COALESCE(p.serve_try_ix_in_point::text,'')) = '1st'
        AND p.ball_hit_s IS NOT NULL
      ORDER BY p.ball_hit_s, p.id
    ),
    incs AS (
      SELECT
        a.*,
        CASE
          WHEN ROW_NUMBER() OVER (PARTITION BY a.task_id ORDER BY a.anchor_s, a.id) = 1 THEN 1
          WHEN LAG(a.server_pid) OVER (PARTITION BY a.task_id ORDER BY a.anchor_s, a.id)
               IS DISTINCT FROM a.server_pid THEN 1
          ELSE 0
        END AS inc
      FROM anchors a
    ),
    g_rows AS (
      SELECT
        p.id,
        (SELECT SUM(i.inc)
         FROM incs i
         WHERE i.task_id = p.task_id
           AND i.anchor_s <= p.ball_hit_s) AS gnum
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
            WHEN (p.ball_hit_location_y)::double precision < 0
                 OR (p.ball_hit_location_y)::double precision > 23
              THEN 'Rally'
            WHEN (p.ball_hit_location_y)::double precision > 6
                 AND (p.ball_hit_location_y)::double precision < 18
              THEN 'Net'
            ELSE 'Transition'
          END
      END
    WHERE p.task_id = :tid
      AND p.point_number > 0;
    """
    return conn.execute(text(sql), {"tid": task_id}).rowcount or 0


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
            THEN CASE
                   WHEN p.player_id = p.point_winner_player_id
                     THEN 'Winner'
                   ELSE 'Error'
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
