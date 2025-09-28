CREATE SCHEMA IF NOT EXISTS ss_;

CREATE OR REPLACE VIEW ss_.serve_facts AS
WITH src AS (
  SELECT e.*, row_to_json(e) AS ej
  FROM ss_.vw_point_enriched e
)
SELECT
  -- IDs / meta (filterable in Superset)
  src.session_id::bigint                               AS session_id,
  (src.ej->>'player_id')::bigint                       AS player_id,
  (src.ej->>'player_name')                             AS player_name,
  src.submission_customer_name,
  src.submission_task_id,
  src.submission_match_date,
  src.submission_start_time,
  COALESCE(src.submission_first_point_ts, src.submission_created_at) AS start_ts,

  -- Serve fields parsed from the transactional row
  COALESCE(NULLIF(src.ej->>'serve_try','')::int,
           NULLIF(src.ej->>'serve_attempt','')::int)   AS serve_try,

  CASE WHEN lower(coalesce(src.ej->>'is_serve','')) IN ('t','true','1','y','yes')  THEN TRUE
       WHEN lower(coalesce(src.ej->>'is_serve','')) IN ('f','false','0','n','no')  THEN FALSE END AS is_serve,
  CASE WHEN lower(coalesce(src.ej->>'is_fault','')) IN ('t','true','1','y','yes')  THEN TRUE
       WHEN lower(coalesce(src.ej->>'is_fault','')) IN ('f','false','0','n','no')  THEN FALSE END AS is_fault,
  CASE WHEN lower(coalesce(src.ej->>'is_double_fault','')) IN ('t','true','1','y','yes') THEN TRUE
       WHEN lower(coalesce(src.ej->>'is_double_fault','')) IN ('f','false','0','n','no') THEN FALSE END AS is_double_fault,
  CASE WHEN lower(coalesce(src.ej->>'is_ace','')) IN ('t','true','1','y','yes')     THEN TRUE
       WHEN lower(coalesce(src.ej->>'is_ace','')) IN ('f','false','0','n','no')     THEN FALSE END AS is_ace,

  -- Convenience: served in
  CASE
    WHEN (CASE WHEN lower(coalesce(src.ej->>'is_serve','')) IN ('t','true','1','y','yes') THEN TRUE
               WHEN lower(coalesce(src.ej->>'is_serve','')) IN ('f','false','0','n','no') THEN FALSE END) IS TRUE
     AND COALESCE((CASE WHEN lower(coalesce(src.ej->>'is_fault','')) IN ('t','true','1','y','yes') THEN TRUE
                        WHEN lower(coalesce(src.ej->>'is_fault','')) IN ('f','false','0','n','no') THEN FALSE END), FALSE) IS FALSE
     AND COALESCE((CASE WHEN lower(coalesce(src.ej->>'is_double_fault','')) IN ('t','true','1','y','yes') THEN TRUE
                        WHEN lower(coalesce(src.ej->>'is_double_fault','')) IN ('f','false','0','n','no') THEN FALSE END), FALSE) IS FALSE
  THEN TRUE ELSE FALSE END                               AS serve_in,

  -- Speed & impact location
  COALESCE(NULLIF(src.ej->>'serve_speed','')::numeric,
           NULLIF(src.ej->>'ball_speed','')::numeric)    AS serve_speed,
  COALESCE(NULLIF(src.ej->>'ball_hit_x','')::numeric,
           NULLIF(src.ej->>'ball_hit_location_x','')::numeric) AS serve_loc_x,
  COALESCE(NULLIF(src.ej->>'ball_hit_y','')::numeric,
           NULLIF(src.ej->>'ball_hit_location_y','')::numeric) AS serve_loc_y,

  -- Side
  CASE
    WHEN lower(coalesce(src.ej->>'side',src.ej->>'serve_side',src.ej->>'court_side',src.ej->>'service_side',src.ej->>'service_box')) IN ('deuce','right','r','d') THEN 'deuce'
    WHEN lower(coalesce(src.ej->>'side',src.ej->>'serve_side',src.ej->>'court_side',src.ej->>'service_side',src.ej->>'service_box')) IN ('ad','left','l') THEN 'ad'
    ELSE NULL
  END                                                   AS side
FROM src
WHERE (CASE WHEN lower(coalesce(src.ej->>'is_serve','')) IN ('t','true','1','y','yes') THEN TRUE
            WHEN lower(coalesce(src.ej->>'is_serve','')) IN ('f','false','0','n','no') THEN FALSE END) IS TRUE;
