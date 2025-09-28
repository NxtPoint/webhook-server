CREATE SCHEMA IF NOT EXISTS ss_;

CREATE OR REPLACE VIEW ss_.serve_facts AS
WITH src AS (
  SELECT
    p.*,
    row_to_json(p) AS pj
  FROM ss_.vw_point_enriched p
),
parsed AS (
  SELECT
    -- robust IDs (prefer real columns; fall back to JSON if needed)
    COALESCE((src.pj ->> 'session_id')::bigint, src.session_id)                         AS session_id,
    (src.pj ->> 'player_id')::bigint                                                    AS player_id,
    (src.pj ->> 'player_name')                                                          AS player_name,
    NULLIF(src.pj ->> 'task_id', '')                                                    AS task_id_point_side,
    src.submission_task_id                                                              AS task_id_form_side,

    -- date/time for trending
    src.match_date_meta                                                                 AS match_date_meta,
    COALESCE(
      NULLIF(src.pj ->> 'start_ts','')::timestamptz,
      NULLIF(src.pj ->> 'point_start_ts','')::timestamptz,
      src.submission_created_at
    )                                                                                   AS start_ts,

    -- serve attempt (1/2) and booleans parsed safely from strings/bools/ints
    COALESCE(
      NULLIF(src.pj ->> 'serve_try','')::int,
      NULLIF(src.pj ->> 'serve_attempt','')::int
    )                                                                                   AS serve_try,

    CASE
      WHEN lower(coalesce(src.pj ->> 'is_serve','')) IN ('t','true','1','y','yes')  THEN TRUE
      WHEN lower(coalesce(src.pj ->> 'is_serve','')) IN ('f','false','0','n','no')  THEN FALSE
      ELSE NULL
    END                                                                                 AS is_serve,

    CASE
      WHEN lower(coalesce(src.pj ->> 'is_fault','')) IN ('t','true','1','y','yes')  THEN TRUE
      WHEN lower(coalesce(src.pj ->> 'is_fault','')) IN ('f','false','0','n','no')  THEN FALSE
      ELSE NULL
    END                                                                                 AS is_fault,

    CASE
      WHEN lower(coalesce(src.pj ->> 'is_double_fault','')) IN ('t','true','1','y','yes')  THEN TRUE
      WHEN lower(coalesce(src.pj ->> 'is_double_fault','')) IN ('f','false','0','n','no')  THEN FALSE
      ELSE NULL
    END                                                                                 AS is_double_fault,

    CASE
      WHEN lower(coalesce(src.pj ->> 'is_ace','')) IN ('t','true','1','y','yes')     THEN TRUE
      WHEN lower(coalesce(src.pj ->> 'is_ace','')) IN ('f','false','0','n','no')     THEN FALSE
      ELSE NULL
    END                                                                                 AS is_ace,

    -- convenience flags
    CASE
      WHEN lower(coalesce(src.pj ->> 'is_serve','')) IN ('t','true','1','y','yes')
           AND COALESCE(
                 CASE WHEN lower(coalesce(src.pj ->> 'is_fault','')) IN ('t','true','1','y','yes') THEN TRUE
                      WHEN lower(coalesce(src.pj ->> 'is_fault','')) IN ('f','false','0','n','no') THEN FALSE
                 END, FALSE)
           IS FALSE
           AND COALESCE(
                 CASE WHEN lower(coalesce(src.pj ->> 'is_double_fault','')) IN ('t','true','1','y','yes') THEN TRUE
                      WHEN lower(coalesce(src.pj ->> 'is_double_fault','')) IN ('f','false','0','n','no') THEN FALSE
                 END, FALSE)
           IS FALSE
      THEN TRUE ELSE FALSE END                                                           AS serve_in,

    -- speed and location (flexible field names)
    COALESCE(
      NULLIF(src.pj ->> 'serve_speed','')::numeric,
      NULLIF(src.pj ->> 'ball_speed','')::numeric
    )                                                                                   AS serve_speed,

    COALESCE(
      NULLIF(src.pj ->> 'ball_hit_x','')::numeric,
      NULLIF(src.pj ->> 'ball_hit_location_x','')::numeric
    )                                                                                   AS serve_loc_x,

    COALESCE(
      NULLIF(src.pj ->> 'ball_hit_y','')::numeric,
      NULLIF(src.pj ->> 'ball_hit_location_y','')::numeric
    )                                                                                   AS serve_loc_y

  FROM src
)
SELECT
  *
FROM parsed
-- keep only rows that look like serves; if your downstream expects all rows, remove this filter
WHERE is_serve IS TRUE;
