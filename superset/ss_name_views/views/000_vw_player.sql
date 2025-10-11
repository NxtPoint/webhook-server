-- ss_/views/000_vw_player.sql
CREATE SCHEMA IF NOT EXISTS ss_;

CREATE OR REPLACE VIEW ss_.vw_player AS
WITH base AS (
  SELECT
    sc.session_id,
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
    sc.raw_meta
  FROM public.submission_context sc
  WHERE sc.session_id IS NOT NULL
)
-- Player A row
SELECT
  b.session_id,
  'Player A'::text AS player_label,
  (b.session_id::text || '|Player A') AS session_player_key,
  b.player_a_name  AS player_name,
  b.player_a_utr   AS player_utr,
  b.task_id,
  b.created_at,
  b.email,
  b.customer_name,
  b.match_date,
  b.start_time,
  b.location,
  b.share_url,
  b.video_url,
  b.raw_meta
FROM base b
UNION ALL
-- Player B row
SELECT
  b.session_id,
  'Player B'::text AS player_label,
  (b.session_id::text || '|Player B') AS session_player_key,
  b.player_b_name  AS player_name,
  b.player_b_utr   AS player_utr,
  b.task_id,
  b.created_at,
  b.email,
  b.customer_name,
  b.match_date,
  b.start_time,
  b.location,
  b.share_url,
  b.video_url,
  b.raw_meta
FROM base b;
