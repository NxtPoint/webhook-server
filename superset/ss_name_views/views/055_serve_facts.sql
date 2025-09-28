@'
CREATE SCHEMA IF NOT EXISTS ss_;
DROP VIEW IF EXISTS ss_.vw_point_enriched CASCADE;

CREATE VIEW ss_.vw_point_enriched AS
WITH sc_base AS (
  SELECT
    COALESCE(NULLIF(sc.session_id::text,''), NULLIF(sc.raw_meta->>'session_id',''))           AS session_id_txt,
    COALESCE(NULLIF(sc.raw_meta->>'session_uid',''), NULLIF(sc.raw_meta->>'sessionUid',''))   AS session_uid_txt,
    COALESCE(NULLIF(sc.task_id::text,''),    NULLIF(sc.raw_meta->>'task_id',''))              AS task_id_txt,
    COALESCE(sc.created_at, NULLIF(sc.raw_meta->>'created_at','')::timestamptz)               AS created_at_ts,
    sc.raw_meta::jsonb AS m
  FROM public.submission_context sc
),
sc_latest AS (
  SELECT
    b.*,
    COALESCE(NULLIF(b.session_id_txt,''), NULLIF(b.session_uid_txt,''), NULLIF(b.task_id_txt,'')) AS group_key,
    ROW_NUMBER() OVER (
      PARTITION BY COALESCE(NULLIF(b.session_id_txt,''), NULLIF(b.session_uid_txt,''), NULLIF(b.task_id_txt,''))
      ORDER BY b.created_at_ts DESC NULLS LAST
    ) AS rn
  FROM sc_base b
  WHERE COALESCE(NULLIF(b.session_id_txt,''), NULLIF(b.session_uid_txt,''), NULLIF(b.task_id_txt,'')) IS NOT NULL
),
ctx AS (
  SELECT
    sl.session_id_txt,
    sl.session_uid_txt,
    sl.task_id_txt                                  AS submission_task_id,
    sl.created_at_ts                                AS submission_created_at,
    NULLIF(sl.m->>'email','')                       AS submission_email,
    NULLIF(sl.m->>'customer_name','')               AS submission_customer_name,
    CASE
      WHEN COALESCE(sl.m->>'match_date','') ~ '^[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}$'
      THEN REPLACE(sl.m->>'match_date','/','-')::date
      ELSE NULL
    END                                             AS submission_match_date_meta,
    CASE
      WHEN COALESCE(sl.m->>'start_time','') ~ '^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$'
      THEN (sl.m->>'start_time')::time
      ELSE NULL
    END                                             AS submission_start_time,
    NULLIF(sl.m->>'location','')                    AS submission_location,
    NULLIF(sl.m->>'player_a_name','')               AS submission_player_a_name,
    NULLIF(sl.m->>'player_b_name','')               AS submission_player_b_name,
    CASE WHEN (sl.m->>'player_a_utr') ~ '^[0-9]+(\.[0-9]+)?$'
         THEN (sl.m->>'player_a_utr')::numeric END  AS submission_player_a_utr,
    CASE WHEN (sl.m->>'player_b_utr') ~ '^[0-9]+(\.[0-9]+)?$'
         THEN (sl.m->>'player_b_utr')::numeric END  AS submission_player_b_utr
  FROM sc_latest sl
  WHERE sl.rn = 1
),
p0 AS (
  SELECT p.*, row_to_json(p) AS pj
  FROM silver.vw_point_silver p
)
SELECT
  p0.*,

  -- submission_* fields (stable, collision-free)
  c.submission_task_id,
  c.submission_created_at,
  c.submission_email,
  c.submission_customer_name,
  c.submission_match_date_meta,
  c.submission_start_time,
  c.submission_location,
  c.submission_player_a_name,
  c.submission_player_a_utr,
  c.submission_player_b_name,
  c.submission_player_b_utr,

  -- 🔁 back-compat aliases for downstream SQLs like 055:
  c.submission_match_date_meta AS match_date_meta,
  c.submission_start_time      AS start_time

FROM p0
LEFT JOIN ctx c
  ON (
       (c.session_id_txt  IS NOT NULL AND c.session_id_txt  = p0.session_id::text)
    OR (c.session_uid_txt IS NOT NULL AND c.session_uid_txt = COALESCE(p0.pj->>'session_uid', p0.pj->>'sessionUid'))
    OR (c.submission_task_id IS NOT NULL AND c.submission_task_id = COALESCE(p0.pj->>'task_id', p0.pj->>'sportai_task_id', p0.pj->>'job_id'))
  );
'@ | Set-Content C:\tmp\010_view.sql -Encoding UTF8

# Apply the patch
curl.exe -X POST "https://api.nextpointtennis.com/ops/sql?key=<OPS_KEY>" --data-binary "@C:\tmp\010_view.sql"
