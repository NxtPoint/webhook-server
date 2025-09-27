-- ss_.serve_time_trend: in-match trend per game (fast & stable)
create or replace view ss_.serve_time_trend as
select
  f.session_id,
  f.player_id,
  f.game_number_d                               as game_no,

  sum(case when f.serve_try=1 then 1 else 0 end)                                   as first_attempts,
  sum(case when f.serve_try=1 and f.is_in=1 then 1 else 0 end)                     as first_in,
  sum(case when f.serve_try=1 and f.is_in=1 and f.point_won_by_server=1 then 1 else 0 end) as first_points_won,

  sum(case when f.serve_try=2 then 1 else 0 end)                                   as second_attempts,
  sum(case when f.serve_try=2 and f.is_in=1 then 1 else 0 end)                     as second_in,
  sum(case when f.serve_try=2 and f.is_in=1 and f.point_won_by_server=1 then 1 else 0 end) as second_points_won,

  sum(f.is_ace)              as aces,
  sum(f.is_double_fault)     as double_faults,

  -- rates per game (safe divide)
  (first_in::numeric / nullif(first_attempts,0))                     as first_in_pct,
  (second_in::numeric / nullif(second_attempts,0))                   as second_in_pct,
  (first_points_won::numeric / nullif(first_in,0))                   as first_win_pct_when_in,
  (second_points_won::numeric / nullif(second_in,0))                 as second_win_pct_when_in
from ss_.serve_facts f
group by 1,2,3
order by 1,2,3;
