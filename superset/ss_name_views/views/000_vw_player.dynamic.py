# 000: ss_.vw_player – normalized A/B submission data (dynamic, drop+create)
def make_sql(cur):
    return """
    DROP VIEW IF EXISTS ss_.vw_player CASCADE;

    CREATE OR REPLACE VIEW ss_.vw_player AS
    WITH ctx_pre AS (
      SELECT
        sc.task_id,
        sc.created_at,
        sc.email,
        sc.customer_name,
        sc.match_date,
        sc.start_time,
        sc.location,
        sc.player_a_name,
        sc.player_b_name,
        sc.player_a_utr,
        sc.player_b_utr,
        sc.share_url,
        sc.video_url,
        sc.session_id AS session_id_typed,
        sc.raw_meta::jsonb AS m,
        ROW_NUMBER() OVER (
          PARTITION BY sc.task_id
          ORDER BY sc.created_at DESC NULLS LAST
        ) AS rn
      FROM public.submission_context sc
      WHERE sc.task_id IS NOT NULL
    ),
    ctx_norm AS (
      SELECT
        cp.task_id,
        cp.created_at,
        NULLIF(cp.email,'') AS email,
        COALESCE(NULLIF(cp.customer_name,''), NULLIF(cp.m->>'customer_name','')) AS customer_name,
        COALESCE(
          cp.match_date,
          CASE
            WHEN COALESCE(cp.m->>'match_date','') ~ '^[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}$'
              THEN REPLACE(cp.m->>'match_date','/','-')::date
          END
        ) AS match_date,
        CASE
          WHEN COALESCE(NULLIF(cp.start_time::text,''),'') ~ '^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$'
            THEN (cp.start_time::text)::time
          WHEN COALESCE(cp.m->>'start_time','') ~ '^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$'
            THEN (cp.m->>'start_time')::time
          ELSE NULL
        END AS start_time,
        COALESCE(NULLIF(cp.location,''), NULLIF(cp.m->>'location','')) AS location,
        COALESCE(NULLIF(cp.player_a_name,''), NULLIF(cp.m->>'player_a_name','')) AS player_a_name,
        COALESCE(NULLIF(cp.player_b_name,''), NULLIF(cp.m->>'player_b_name','')) AS player_b_name,
        COALESCE(
          NULLIF(cp.player_a_utr::text, '')::numeric,
          CASE
            WHEN COALESCE(cp.m->>'player_a_utr','') ~ '^[0-9]+(\\.[0-9]+)?$'
              THEN (cp.m->>'player_a_utr')::numeric
          END
        ) AS player_a_utr,
        COALESCE(
          NULLIF(cp.player_b_utr::text, '')::numeric,
          CASE
            WHEN COALESCE(cp.m->>'player_b_utr','') ~ '^[0-9]+(\\.[0-9]+)?$'
              THEN (cp.m->>'player_b_utr')::numeric
          END
        ) AS player_b_utr,
        cp.share_url,
        cp.video_url,
        cp.session_id_typed,
        cp.m
      FROM ctx_pre cp
      WHERE cp.rn = 1
    ),
    ctx_with_session AS (
      SELECT
        c.task_id,
        COALESCE(c.session_id_typed, ds.session_id) AS session_id_resolved,
        c.created_at,
        c.email,
        c.customer_name,
        c.match_date,
        c.start_time,
        c.location,
        c.player_a_name,
        c.player_b_name,
        c.player_a_utr,
        c.player_b_utr,
        c.share_url,
        c.video_url,
        c.m
      FROM ctx_norm c
      LEFT JOIN public.dim_session ds
        ON ds.session_uid = (c.task_id || '_statistics')
    )
    SELECT
      cw.session_id_resolved AS session_id,
      'Player A'::text AS player_label,
      (cw.session_id_resolved::text || '|Player A') AS session_player_key,
      cw.player_a_name AS player_name,
      cw.player_a_utr AS player_utr,
      cw.task_id,
      cw.created_at,
      cw.email,
      cw.customer_name,
      cw.match_date,
      cw.start_time,
      cw.location,
      cw.share_url,
      cw.video_url,
      'v5'::text AS _vw_version
    FROM ctx_with_session cw
    WHERE cw.session_id_resolved IS NOT NULL
    UNION ALL
    SELECT
      cw.session_id_resolved AS session_id,
      'Player B'::text AS player_label,
      (cw.session_id_resolved::text || '|Player B') AS session_player_key,
      cw.player_b_name AS player_name,
      cw.player_b_utr AS player_utr,
      cw.task_id,
      cw.created_at,
      cw.email,
      cw.customer_name,
      cw.match_date,
      cw.start_time,
      cw.location,
      cw.share_url,
      cw.video_url,
      'v5'::text AS _vw_version
    FROM ctx_with_session cw
    WHERE cw.session_id_resolved IS NOT NULL;
    """
