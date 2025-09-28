# -*- coding: utf-8 -*-
# 010: transactional points (EXACT from vw_point_silver) + latest submission meta
# Join rule: session_uid_d  <->  task_id || '_statistics'

def make_sql(cur):
    # Find schema that contains vw_point_silver
    cur.execute("""
        with cte as (
          select table_schema from information_schema.tables where table_name='vw_point_silver'
          union all
          select table_schema from information_schema.views  where table_name='vw_point_silver'
        )
        select table_schema from cte limit 1;
    """)
    row = cur.fetchone()
    if not row:
        raise RuntimeError("vw_point_silver not found")
    point_schema = row[0]
    point_src = f"{point_schema}.vw_point_silver"

    return f"""
    CREATE SCHEMA IF NOT EXISTS ss_;
    DROP VIEW IF EXISTS ss_.vw_point_enriched CASCADE;

    CREATE VIEW ss_.vw_point_enriched AS
    WITH ctx_pre AS (
      SELECT
        sc.task_id,
        sc.created_at,
        sc.email,
        sc.raw_meta::jsonb AS m,
        ROW_NUMBER() OVER (
          PARTITION BY sc.task_id
          ORDER BY sc.created_at DESC NULLS LAST
        ) AS rn
      FROM public.submission_context sc
      WHERE sc.task_id IS NOT NULL
    ),
    ctx AS (
      SELECT
        cp.task_id::text                               AS submission_task_id,   -- join only (not projected)
        cp.created_at                                  AS submission_created_at,
        NULLIF(cp.email,'')                            AS submission_email,
        NULLIF(cp.m->>'customer_name','')              AS submission_customer_name,
        CASE
          WHEN COALESCE(cp.m->>'match_date','') ~ '^[0-9]{{4}}[-/][0-9]{{2}}[-/][0-9]{{2}}$'
            THEN REPLACE(cp.m->>'match_date','/','-')::date
          ELSE NULL
        END                                            AS submission_match_date,
        CASE
          WHEN COALESCE(cp.m->>'start_time','') ~ '^[0-9]{{2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
            THEN (cp.m->>'start_time')::time
          ELSE NULL
        END                                            AS submission_start_time,
        CASE
          WHEN COALESCE(cp.m->>'match_date','') ~ '^[0-9]{{4}}[-/][0-9]{{2}}[-/][0-9]{{2}}$'
           AND COALESCE(cp.m->>'start_time','') ~ '^[0-9]{{2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
            THEN (REPLACE(cp.m->>'match_date','/','-') || ' ' || (cp.m->>'start_time'))::timestamptz
          ELSE NULL
        END                                            AS submission_first_point_ts,
        NULLIF(cp.m->>'location','')                   AS submission_location,
        NULLIF(cp.m->>'player_a_name','')              AS submission_player_a_name,
        NULLIF(cp.m->>'player_b_name','')              AS submission_player_b_name,
        CASE WHEN (cp.m->>'player_a_utr') ~ '^[0-9]+(\\.[0-9]+)?$'
             THEN (cp.m->>'player_a_utr')::numeric END AS submission_player_a_utr,
        CASE WHEN (cp.m->>'player_b_utr') ~ '^[0-9]+(\\.[0-9]+)?$'
             THEN (cp.m->>'player_b_utr')::numeric END AS submission_player_b_utr
      FROM ctx_pre cp
      WHERE cp.rn = 1
    )
    SELECT
      p.*,                               -- EXACT transactional columns from {point_src}
      -- (do NOT project submission_task_id)
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
    FROM {point_src} p
    LEFT JOIN ctx c
      ON p.session_uid_d = (c.submission_task_id || '_statistics');
    """
