def make_sql(cur):
    return """
    CREATE SCHEMA IF NOT EXISTS ss_;

    -- Build ss_.vw_point_enriched by joining vw_point_silver with the latest submission_context per session
    CREATE OR REPLACE VIEW ss_.vw_point_enriched AS
    WITH base AS (
      SELECT
        -- session_id may be a column OR only inside raw_meta
        COALESCE(NULLIF(sc.session_id::text, ''),
                 NULLIF((sc.raw_meta->>'session_id'), ''))                    AS session_id_txt,

        -- task_id may be a column OR only inside raw_meta
        COALESCE(NULLIF(sc.task_id::text, ''),
                 NULLIF((sc.raw_meta->>'task_id'), ''))                       AS task_id_txt,

        -- created_at may be a column OR only inside raw_meta
        COALESCE(sc.created_at,
                 NULLIF(sc.raw_meta->>'created_at','')::timestamptz)          AS created_at_ts,

        sc.raw_meta::jsonb                                                   AS m
      FROM public.submission_context sc
    ),
    ctx_pre AS (
      SELECT
        b.*,
        CASE WHEN b.session_id_txt ~ '^[0-9]+$' THEN b.session_id_txt::bigint END AS session_id_bigint,
        ROW_NUMBER() OVER (
          PARTITION BY b.session_id_txt
          ORDER BY b.created_at_ts DESC NULLS LAST
        ) AS rn
      FROM base b
      WHERE b.session_id_txt IS NOT NULL
    ),
    ctx AS (
      SELECT
        cp.session_id_bigint                           AS session_id,
        cp.task_id_txt                                 AS task_id,
        cp.created_at_ts                               AS created_at,

        NULLIF(cp.m->>'email', '')                     AS email,
        NULLIF(cp.m->>'customer_name', '')             AS customer_name,

        -- store date under the meta name your downstream expects
        CASE
          WHEN COALESCE(cp.m->>'match_date','') ~ '^\d{4}[-/]\d{2}[-/]\d{2}$'
            THEN REPLACE(cp.m->>'match_date','/','-')::date
          ELSE NULL
        END                                            AS match_date_meta,

        CASE
          WHEN COALESCE(cp.m->>'start_time','') ~ '^\d{2}:\d{2}(:\d{2})?$'
            THEN (cp.m->>'start_time')::time
          ELSE NULL
        END                                            AS start_time,

        NULLIF(cp.m->>'location', '')                  AS location,
        NULLIF(cp.m->>'player_a_name', '')             AS player_a_name,
        NULLIF(cp.m->>'player_b_name', '')             AS player_b_name,

        CASE WHEN (cp.m->>'player_a_utr') ~ '^\d+(\\.\d+)?$'
             THEN (cp.m->>'player_a_utr')::numeric END AS player_a_utr,
        CASE WHEN (cp.m->>'player_b_utr') ~ '^\d+(\\.\d+)?$'
             THEN (cp.m->>'player_b_utr')::numeric END AS player_b_utr
      FROM ctx_pre cp
      WHERE cp.rn = 1  -- latest submission per session
    )
    SELECT
      p.*,
      c.task_id,
      c.created_at,
      c.email,
      c.customer_name,
      c.match_date_meta,     -- keep this exact name for downstream expectations
      c.start_time,
      c.location,
      c.player_a_name,
      c.player_a_utr,
      c.player_b_name,
      c.player_b_utr
    FROM silver.vw_point_silver p
    LEFT JOIN ctx c
      ON c.session_id =
         CASE WHEN p.session_id::text ~ '^[0-9]+$' THEN p.session_id::bigint ELSE NULL END;
    """
