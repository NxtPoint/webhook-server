# -*- coding: utf-8 -*-
# 010: transactional points (exact) + latest submission meta by task_id
# Output: all vw_point_silver columns EXCEPT task_id, plus raw meta fields verbatim.

def make_sql(cur):
    # Locate schema of vw_point_silver
    cur.execute("""
        WITH c AS (
          SELECT table_schema FROM information_schema.tables WHERE table_name='vw_point_silver'
          UNION ALL
          SELECT table_schema FROM information_schema.views  WHERE table_name='vw_point_silver'
        )
        SELECT table_schema FROM c LIMIT 1;
    """)
    row = cur.fetchone()
    if not row:
        raise RuntimeError("vw_point_silver not found")
    point_schema = row[0]

    # Fetch ordered column list, drop task_id
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name='vw_point_silver'
        ORDER BY ordinal_position;
    """, (point_schema,))
    cols = [r[0] for r in cur.fetchall()]
    keep_cols = [c for c in cols if c.lower() != 'task_id']
    if not keep_cols:
        raise RuntimeError("vw_point_silver has no columns after excluding task_id.")

    select_point_cols = ",\n      ".join([f'p."{c}"' for c in keep_cols])

    return f"""
    CREATE SCHEMA IF NOT EXISTS ss_;
    DROP VIEW IF EXISTS ss_.vw_point_enriched CASCADE;

    CREATE VIEW ss_.vw_point_enriched AS
    WITH ctx_pre AS (
      SELECT
        (sc.task_id::text || '_statistics') AS submission_session_uid,
        sc.raw_meta::jsonb                  AS m,
        sc.created_at,
        ROW_NUMBER() OVER (
          PARTITION BY sc.task_id
          ORDER BY sc.created_at DESC NULLS LAST
        ) AS rn
      FROM public.submission_context sc
      WHERE sc.task_id IS NOT NULL
    ),
    ctx AS (
      SELECT submission_session_uid, m
      FROM ctx_pre
      WHERE rn = 1
    )
    SELECT
      {select_point_cols},
      -- form fields verbatim (no casts, no prefixes)
      NULLIF(c.m->>'email','')           AS email,
      NULLIF(c.m->>'customer_name','')   AS customer_name,
      NULLIF(c.m->>'match_date','')      AS match_date,
      NULLIF(c.m->>'start_time','')      AS start_time,
      NULLIF(c.m->>'location','')        AS location,
      NULLIF(c.m->>'player_a_name','')   AS player_a_name,
      NULLIF(c.m->>'player_b_name','')   AS player_b_name,
      NULLIF(c.m->>'player_a_utr','')    AS player_a_utr,
      NULLIF(c.m->>'player_b_utr','')    AS player_b_utr
    FROM {point_schema}.vw_point_silver AS p
    LEFT JOIN ctx AS c
      ON btrim(p.session_uid_d) = btrim(c.submission_session_uid);
    """
