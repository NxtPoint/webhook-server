-- Strict serve reconciliation: SportAI ground-truth (task 4a194ff3) vs
-- T5 detected serves (task 8a5e0b5e) on the same video.
-- For each SportAI serve, find the closest T5 serve_event within ±2s,
-- print side-by-side with time delta + bounce delta + verdict.
--
-- Tighter than harness eval-serve (which uses 3s greedy matching).
-- Shows whether our 14 matched TP are really the SAME physical serves.

WITH sa AS (
  SELECT
    ball_hit_s AS ts,
    serve_side_d AS side,
    CASE
      WHEN ball_hit_location_y > 22 THEN 'NEAR'
      WHEN ball_hit_location_y < 2 THEN 'FAR'
      ELSE '?'
    END AS role,
    ROUND(ball_hit_location_x::numeric, 1) AS hx,
    ROUND(ball_hit_location_y::numeric, 1) AS hy,
    ROUND(court_x::numeric, 1) AS bx,
    ROUND(court_y::numeric, 1) AS by
  FROM silver.point_detail
  WHERE task_id = CAST('4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb' AS uuid)
    AND model = 'sportai'
    AND serve_d = TRUE
),
paired AS (
  SELECT
    sa.*,
    t5.ts AS t5_ts,
    t5.player_id AS t5_pid,
    t5.source AS t5_source,
    ROUND(t5.confidence::numeric, 2) AS t5_conf,
    ROUND(t5.hitter_court_y::numeric, 1) AS t5_hy,
    ROUND(t5.bounce_court_x::numeric, 1) AS t5_bx,
    ROUND(t5.bounce_court_y::numeric, 1) AS t5_by,
    ROUND(ABS(sa.ts - t5.ts)::numeric, 2) AS dt,
    CASE
      WHEN t5.bounce_court_x IS NOT NULL AND sa.bx IS NOT NULL
      THEN ROUND(SQRT(POWER(sa.bx - t5.bounce_court_x, 2)
                    + POWER(sa.by - t5.bounce_court_y, 2))::numeric, 1)
      ELSE NULL
    END AS bounce_dist_m
  FROM sa
  LEFT JOIN LATERAL (
    SELECT ts, player_id, source, confidence,
           hitter_court_x, hitter_court_y,
           bounce_court_x, bounce_court_y
    FROM ml_analysis.serve_events
    WHERE task_id = CAST('8a5e0b5e-58a5-4236-a491-0fb7b3a25088' AS uuid)
      AND ABS(ts - sa.ts) <= 2.0
    ORDER BY ABS(ts - sa.ts)
    LIMIT 1
  ) t5 ON TRUE
)
SELECT
  ts AS sa_ts,
  role AS sa_role,
  side AS sa_side,
  hy AS sa_hy,
  bx AS sa_bx, by AS sa_by,
  t5_ts, t5_pid, t5_source, t5_conf,
  t5_hy, t5_bx, t5_by,
  dt,
  bounce_dist_m,
  CASE
    WHEN t5_ts IS NULL THEN 'NO_MATCH'
    WHEN dt > 1.0 THEN 'FAR_IN_TIME'
    WHEN dt > 0.5 THEN 'WEAK_TIME'
    WHEN bounce_dist_m IS NOT NULL AND bounce_dist_m > 4.0 THEN 'SUSPECT_BOUNCE'
    ELSE 'MATCH'
  END AS verdict
FROM paired
ORDER BY ts;
