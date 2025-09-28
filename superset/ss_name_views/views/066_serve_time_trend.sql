CREATE SCHEMA IF NOT EXISTS ss_;

CREATE OR REPLACE VIEW ss_.serve_time_trend AS
WITH base AS (
  SELECT
    COALESCE(s.submission_match_date, s.start_ts::date) AS day,
    s.submission_customer_name,
    s.submission_task_id,
    s.player_id,
    s.serve_try,
    s.side,
    s.serve_in,
    s.is_ace,
    s.is_fault,
    s.is_double_fault
  FROM ss_.serve_facts s
)
SELECT
  day,
  submission_customer_name,
  submission_task_id,
  player_id,
  serve_try,
  side,
  COUNT(*)::bigint                                           AS attempts,
  (COUNT(*) FILTER (WHERE serve_in))::numeric / NULLIF(COUNT(*),0)          AS in_rate,
  (COUNT(*) FILTER (WHERE is_ace))::numeric / NULLIF(COUNT(*),0)            AS ace_rate,
  (COUNT(*) FILTER (WHERE is_fault))::numeric / NULLIF(COUNT(*),0)          AS fault_rate,
  (COUNT(*) FILTER (WHERE is_double_fault))::numeric / NULLIF(COUNT(*),0)   AS double_fault_rate
FROM base
GROUP BY 1,2,3,4,5,6
ORDER BY day DESC, submission_customer_name, submission_task_id, player_id, serve_try, side;
