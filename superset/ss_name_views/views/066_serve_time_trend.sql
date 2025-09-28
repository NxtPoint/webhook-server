CREATE SCHEMA IF NOT EXISTS ss_;

DROP VIEW IF EXISTS ss_.serve_time_trend CASCADE;

-- Time trend of serves (per day), sourced purely from 010 via ss_.serve_facts.
-- We parse match_date if present (YYYY-MM-DD or YYYY/MM/DD). Rows without a valid
-- match_date are excluded to keep the trend clean.

CREATE VIEW ss_.serve_time_trend AS
WITH base AS (
  SELECT
    s.*,
    /* normalize match_date text -> date when it matches YYYY[-|/]MM[-|/]DD */
    CASE
      WHEN COALESCE(s.match_date, '') ~ '^[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}$'
        THEN REPLACE(s.match_date, '/', '-')::date
      ELSE NULL::date
    END AS match_date_d
  FROM ss_.serve_facts s
)
SELECT
  b.match_date_d                          AS d,
  b.customer_name,
  b.email,
  COUNT(*)                                 AS n_serves,
  SUM(CASE WHEN b.is_in THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0) AS in_pct
FROM base b
WHERE b.match_date_d IS NOT NULL
GROUP BY b.match_date_d, b.customer_name, b.email
ORDER BY b.match_date_d, b.customer_name, b.email;
