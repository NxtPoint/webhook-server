CREATE SCHEMA IF NOT EXISTS ss_;

CREATE OR REPLACE VIEW ss_.serve_facts AS
WITH src AS (
  SELECT p.*, row_to_json(p) AS pjson
  FROM ss_.vw_point_enriched p
),
parsed AS (
  SELECT
    -- Stable IDs
    COALESCE((src.pjson->>'session_id')::bigint, src.session_id) AS session_id,
    (src.pjson->>'player_id')::bigint                            AS player_id,
    (src.pjson->>'player_name')                                  AS player_name,

    -- Task ids (point vs form)
    NULLIF(src.pjson->>'task_id','')                             AS task_id_point_side,
    src.submission_task_id                                       AS task_id_form_side,

    -- Robust match_date_meta
    CASE
      WHEN src.submission_match_date_meta IS NOT NULL
        THEN src.submission_match_date_meta
      WHEN COALESCE(src.pjson->>'match_date_meta','') ~ '^\d{4}-\d{2}-\d{2}$'
        THEN (src.pjson->>'match_date_meta')::date
      WHEN COALESCE(src.pjson->>'match_date','') ~ '^\d{4}[-/]\d{2}[-/]\d{2}$'
        THEN REPLACE(src.pjson->>'match_date','/','-')::date
      ELSE NULL
    END                                                           AS match_date_meta,

    -- Start timestamp with fallbacks
    COALESCE(
      NULLIF(src.pjson->>'start_ts','')::timestamptz,
      NULLIF(src.pjson->>'point_start_ts','')::timestamptz,
      src.submission_created_at
    )                                                             AS start_ts,

    -- 🔹 New: set/game numbers (many possible field names)
    COALESCE(
      NULLIF(src.pjson->>'set_number','')::int,
      NULLIF(src.pjson->>'set_no','')::int,
      NULLIF(src.pjson->>'set','')::int,
      NULLIF(src.pjson->>'setNumber','')::int
    )                                                             AS set_number_d,
    COALESCE(
      NULLIF(src.pjson->>'game_number','')::int,
      NULLIF(src.pjson->>'game_no','')::int,
      NULLIF(src.pjson->>'game','')::int,
      NULLIF(src.pjson->>'gameNumber','')::int,
      NULLIF(src.pjson->>'point_game_number','')::int
    )                                                             AS game_number_d,

    -- Serve attempt number (1/2)
    COALESCE(
      NULLIF(src.pjson->>'serve_try','')::int,
      NULLIF(src.pjson->>'serve_attempt','')::int
    )                                                             AS serve_try,

    -- Booleans
    CASE WHEN lower(coalesce(src.pjson->>'is_serve','')) IN ('t','true','1','y','yes')  THEN TRUE
         WHEN lower(coalesce(src.pjson->>'is_serve','')) IN ('f','false','0','n','no')  THEN FALSE END AS is_serve,
    CASE WHEN lower(coalesce(src.pjson->>'is_fault','')) IN ('t','true','1','y','yes')  THEN TRUE
         WHEN lower(coalesce(src.pjson->>'is_fault','')) IN ('f','false','0','n','no')  THEN FALSE END AS is_fault,
    CASE WHEN lower(coalesce(src.pjson->>'is_double_fault','')) IN ('t','true','1','y','yes') THEN TRUE
         WHEN lower(coalesce(src.pjson->>'is_double_fault','')) IN ('f','false','0','n','no') THEN FALSE END AS is_double_fault,
    CASE WHEN lower(coalesce(src.pjson->>'is_ace','')) IN ('t','true','1','y','yes')     THEN TRUE
         WHEN lower(coalesce(src.pjson->>'is_ace','')) IN ('f','false','0','n','no')     THEN FALSE END AS is_ace,

    -- Convenience: served in (serve & not fault & not double-fault)
    CASE
      WHEN (CASE WHEN lower(coalesce(src.pjson->>'is_serve','')) IN ('t','true','1','y','yes') THEN TRUE
                 WHEN lower(coalesce(src.pjson->>'is_serve','')) IN ('f','false','0','n','no') THEN FALSE END) IS TRUE
       AND COALESCE((CASE WHEN lower(coalesce(src.pjson->>'is_fault','')) IN ('t','true','1','y','yes') THEN TRUE
                          WHEN lower(coalesce(src.pjson->>'is_fault','')) IN ('f','false','0','n','no') THEN FALSE END), FALSE) IS FALSE
       AND COALESCE((CASE WHEN lower(coalesce(src.pjson->>'is_double_fault','')) IN ('t','true','1','y','yes') THEN TRUE
                          WHEN lower(coalesce(src.pjson->>'is_double_fault','')) IN ('f','false','0','n','no') THEN FALSE END), FALSE) IS FALSE
    THEN TRUE ELSE FALSE END                                         AS serve_in,

    -- Speed & impact location
    COALESCE(NULLIF(src.pjson->>'serve_speed','')::numeric,
             NULLIF(src.pjson->>'ball_speed','')::numeric)           AS serve_speed,
    COALESCE(NULLIF(src.pjson->>'ball_hit_x','')::numeric,
             NULLIF(src.pjson->>'ball_hit_location_x','')::numeric)  AS serve_loc_x,
    COALESCE(NULLIF(src.pjson->>'ball_hit_y','')::numeric,
             NULLIF(src.pjson->>'ball_hit_location_y','')::numeric)  AS serve_loc_y,

    -- Side (deuce/ad)
    CASE
      WHEN lower(coalesce(src.pjson->>'side',src.pjson->>'serve_side',src.pjson->>'court_side',src.pjson->>'service_side',src.pjson->>'service_box')) IN ('deuce','right','r','d') THEN 'deuce'
      WHEN lower(coalesce(src.pjson->>'side',src.pjson->>'serve_side',src.pjson->>'court_side',src.pjson->>'service_side',src.pjson->>'service_box')) IN ('ad','left','l') THEN 'ad'
      ELSE NULL
    END                                                             AS side
  FROM src
)
SELECT *
FROM parsed
WHERE is_serve IS TRUE;
