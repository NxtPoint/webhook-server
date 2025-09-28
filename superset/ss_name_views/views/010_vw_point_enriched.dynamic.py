def make_sql(cur):
    return """
    CREATE SCHEMA IF NOT EXISTS ss_;

    -- Drop first to avoid column-rename conflicts on CREATE OR REPLACE
    DROP VIEW IF EXISTS ss_.vw_point_enriched CASCADE;

    -- Join vw_point_silver to latest submission_context per session
    CREATE VIEW ss_.vw_point_enriched AS
    WITH sc_base AS (
      SELECT
        -- session_id may be a column or only inside raw_meta
        COALESCE(NULLIF(sc.session_id::text,''), NULLIF(sc.raw_meta->>'session_id','')) AS session_id_txt,
        COALESCE(NULLIF(sc.task_id::text,''),    NULLIF(sc.raw_meta->>'task_id',''))    AS task_id_txt,
        COALESCE(sc.created_at, NULLIF(sc.raw_meta->>'created_at','')::timestamptz)     AS created_at_ts,
        sc.raw_meta::jsonb AS m
      FROM public.submission_context sc
    ),
    sc_latest AS (
      SELECT
        b.*,
        ROW_NUMBER() OVER (PARTITION BY b.session_id_txt ORDER BY b.created_at_ts DESC NULLS LAST) AS rn
      FROM sc_base b
      WHERE b.session_id_txt IS NOT NULL
    ),
    ctx AS (
      SELECT
        sl.session_id_txt,
        sl.task_id_txt      AS task_id,
        sl.created_at_ts    AS created_at,

        NULLIF(sl.m->>'email','')           AS email,
        NULLIF(sl.m->>'customer_name','')   AS customer_name,

        CASE
          WHEN COALESCE(sl.m->>'match_date','') ~ '^[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}$'
          THEN REPLACE(sl.m->>'match_date','/','-')::date
          ELSE NULL
        END                                 AS match_date_meta,

        CASE
          WHEN COALESCE(sl.m->>'start_time','') ~ '^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$'
          THEN (sl.m->>'start_time')::time
          ELSE NULL
        END                                 AS start_time,

        NULLIF(sl.m->>'location','')        AS location,
        NULLIF(sl.m->>'player_a_name','')   AS player_a_name,
        NULLIF(sl.m->>'player_b_name','')   AS player_b_name,

        CASE WHEN (sl.m->>'player_a_utr') ~ '^[0-9]+(\\.[0-9]+)?$'
             THEN (sl.m->>'player_a_utr')::numeric END AS player_a_utr,
        CASE WHEN (sl.m->>'player_b_utr') ~ '^[0-9]+(\\.[0-9]+)?$'
             THEN (sl.m->>'player_b_utr')::numeric END AS player_b_utr
      FROM sc_latest sl
      WHERE sl.rn = 1
    )
    SELECT
      p.*,
      c.task_id,
      c.created_at,
      c.email,
      c.customer_name,
      c.match_date_meta,  -- keep expected downstream name
      c.start_time,
      c.location,
      c.player_a_name,
      c.player_a_utr,
      c.player_b_name,
      c.player_b_utr
    FROM silver.vw_point_silver p
    LEFT JOIN ctx c
      ON c.session_id_txt = p.session_id::text;
    """
