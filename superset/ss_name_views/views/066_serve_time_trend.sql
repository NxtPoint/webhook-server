CREATE SCHEMA IF NOT EXISTS ss_;

-- Time trend of serve outcomes (by day / player / attempt / side)
CREATE OR REPLACE VIEW ss_.serve_time_trend AS
WITH base AS (
  SELECT
    COALESCE(s.match_date_meta, s.start_ts::date) AS day,
    s.player_id,
    s.serve_try,
    s.side,
    s.is_in,
    s.is_ace,
    s.is_fault,
    s.is_double_fault
  FROM ss_.serve_facts s
)
SELECT
  day,
  player_id,
  serve_try,
  side,
  COUNT(*)::bigint                                                AS attempts,
  (COUNT(*) FILTER (WHERE is_in))::numeric          / NULLIF(COUNT(*),0) AS in_rate,
  (COUNT(*) FILTER (WHERE is_ace))::numeric         / NULLIF(COUNT(*),0) AS ace_rate,
  (COUNT(*) FILTER (WHERE is_fault))::numeric       / NULLIF(COUNT(*),0) AS fault_rate,
  (COUNT(*) FILTER (WHERE is_double_fault))::numeric/ NULLIF(COUNT(*),0) AS double_fault_rate
FROM base
GROUP BY 1,2,3,4
ORDER BY day DESC, player_id, serve_try, side;
