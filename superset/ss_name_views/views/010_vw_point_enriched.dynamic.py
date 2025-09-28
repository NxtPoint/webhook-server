# 010_vw_point_enriched.dynamic.py
# transactional points (vw_point_silver) + latest submission meta by task_id

def make_sql(cur):
    return """
    CREATE SCHEMA IF NOT EXISTS ss_;
    DROP VIEW IF EXISTS ss_.vw_point_enriched CASCADE;

    CREATE VIEW ss_.vw_point_enriched AS
    WITH ctx_pre AS (
      SELECT
        sc.task_id::text                       AS submission_task_id,
        sc.created_at                          AS submission_created_at,
        sc.raw_meta::jsonb                     AS m,
        ROW_NUMBER() OVER (
          PARTITION BY sc.task_id
          ORDER BY sc.created_at DESC NULLS LAST
        ) AS rn
      FROM public.submission_context sc
      WHERE sc.task_id IS NOT NULL
    ),
    ctx AS (
      SELECT
        submission_task_id,
        submission_created_at,
        NULLIF(m->>'email','')                 AS submission_email,
        NULLIF(m->>'customer_name','')         AS submission_customer_name,
        CASE
          WHEN COALESCE(m->>'match_date','') ~ '^[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}$'
            THEN REPLACE(m->>'match_date','/','-')::date
        END                                    AS submission_match_date,
        CASE
          WHEN COALESCE(m->>'start_time','') ~ '^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$'
            THEN (m->>'start_time')::time
        END                                    AS submission_start_time,
        CASE
          WHEN COALESCE(m->>'match_date','') ~ '^[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}$'
           AND COALESCE(m->>'start_time','') ~ '^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$'
            THEN (REPLACE(m->>'match_date','/','-') || ' ' || (m->>'start_time'))::timestamptz
        END                                    AS submission_first_point_ts,
        NULLIF(m->>'location','')              AS submission_location,
        NULLIF(m->>'player_a_name','')         AS submission_player_a_name,
        NULLIF(m->>'player_b_name','')         AS submission_player_b_name,
        CASE WHEN (m->>'player_a_utr') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (m->>'player_a_utr')::numeric END AS submission_player_a_utr,
        CASE WHEN (m->>'player_b_utr') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (m->>'player_b_utr')::numeric END AS submission_player_b_utr
      FROM ctx_pre
      WHERE rn = 1
    )
    SELECT
      p.*,
      c.submission_task_id,
      c.submission_created_at,
      c.submission_email,
      c.submission_customer_name,
      c.submission_match_date,
      c.submission_start_time,
      c.submission_first_point_ts,
      c.submission_location,
      c.submission_player_a_name,
      c.submission_player_a_utr,
      c.submission_player_b_name,
      c.submission_player_b_utr
    FROM public.vw_point_silver AS p
    LEFT JOIN ctx AS c
      ON c.submission_task_id = p.task_id::text;
    """
