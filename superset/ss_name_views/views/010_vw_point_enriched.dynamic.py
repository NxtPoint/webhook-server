def make_sql(cur):
    return """
    CREATE SCHEMA IF NOT EXISTS ss_;

    -- Build ss_.vw_point_enriched by joining points with the latest submission_context per session
    CREATE OR REPLACE VIEW ss_.vw_point_enriched AS
    WITH ctx_pre AS (
      SELECT
        sc.session_id,                       -- may be text in the table; we'll cast later
        sc.task_id,
        sc.created_at,
        sc.raw_meta::jsonb AS m,             -- robust even if raw_meta is jsonb already
        ROW_NUMBER() OVER (
          PARTITION BY sc.session_id
          ORDER BY sc.created_at DESC
        ) AS rn
      FROM public.submission_context sc
      WHERE sc.session_id IS NOT NULL
    ),
    ctx AS (
      SELECT
        sc.session_id::bigint AS session_id,               -- normalize to bigint
        sc.task_id,
        sc.created_at,

        NULLIF(sc.m->>'email', '')          AS email,
        NULLIF(sc.m->>'customer_name', '')  AS customer_name,

        -- Accept both 2025-09-14 and 2025/09/14
        CASE
          WHEN COALESCE(sc.m->>'match_date','') ~ '^\d{4}[-/]\d{2}[-/]\d{2}$'
            THEN REPLACE(sc.m->>'match_date','/','-')::date
          ELSE NULL
        END AS match_date,

        -- Accept HH:MM or HH:MM:SS, ignore blanks
        CASE
          WHEN COALESCE(sc.m->>'start_time','') ~ '^\d{2}:\d{2}(:\d{2})?$'
            THEN (sc.m->>'start_time')::time
          ELSE NULL
        END AS start_time,

        NULLIF(sc.m->>'location', '')        AS location,
        NULLIF(sc.m->>'player_a_name', '')   AS player_a_name,
        NULLIF(sc.m->>'player_b_name', '')   AS player_b_name,

        CASE
          WHEN (sc.m->>'player_a_utr') ~ '^\d+(\.\d+)?$'
            THEN (sc.m->>'player_a_utr')::numeric
          ELSE NULL
        END AS player_a_utr,

        CASE
          WHEN (sc.m->>'player_b_utr') ~ '^\d+(\.\d+)?$'
            THEN (sc.m->>'player_b_utr')::numeric
          ELSE NULL
        END AS player_b_utr
      FROM ctx_pre sc
      WHERE sc.rn = 1                         -- keep the latest form per session
    )
    SELECT
      p.*,
      c.task_id,
      c.created_at,
      c.email,
      c.customer_name,
      c.match_date       AS match_date_meta,  -- keep the name expected by your other views
      c.start_time,
      c.location,
      c.player_a_name,
      c.player_a_utr,
      c.player_b_name,
      c.player_b_utr
    FROM silver.vw_point_silver p
    LEFT JOIN ctx c
      ON c.session_id = p.session_id::bigint;
    """
