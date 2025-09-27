-- ss_.serve_match_summary: match-level KPIs per server
create or replace view ss_.serve_match_summary as
select
  f.session_id,
  f.player_id,

  count(*)                                   as serve_attempts,
  sum(case when f.serve_try=1 then 1 else 0 end)                     as first_attempts,
  sum(case when f.serve_try=1 and f.is_in=1 then 1 else 0 end)       as first_in,
  sum(case when f.serve_try=1 and f.is_in=1 and f.point_won_by_server=1 then 1 else 0 end) as first_points_won,

  sum(case when f.serve_try=2 then 1 else 0 end)                     as second_attempts,
  sum(case when f.serve_try=2 and f.is_in=1 then 1 else 0 end)       as second_in,
  sum(case when f.serve_try=2 and f.is_in=1 and f.point_won_by_server=1 then 1 else 0 end) as second_points_won,

  sum(f.is_ace)                      as aces,
  sum(f.is_double_fault)             as double_faults,

  -- games (distinct by game_number_d where this player served at least once)
  count(distinct f.game_number_d)    as service_games_played,
  count(distinct case when f.game_number_d is not null
                         and exists (
                           select 1
                           from ss_.vw_point_enriched p
                           where p.session_id = f.session_id
                             and p.game_number_d = f.game_number_d
                             and p.is_game_end_d = 1
                             and p.game_winner_player_id_d = f.player_id)
                      then f.game_number_d end)                       as service_games_won,

  -- headline rates
  (first_in::numeric / nullif(first_attempts,0))                      as first_in_pct,
  (second_in::numeric / nullif(second_attempts,0))                    as second_in_pct,
  (first_points_won::numeric / nullif(first_in,0))                    as first_win_pct_when_in,
  (second_points_won::numeric / nullif(second_in,0))                  as second_win_pct_when_in,
  (aces::numeric / nullif(serve_attempts,0))                          as ace_rate,
  (double_faults::numeric / nullif(serve_attempts,0))                 as df_rate
from ss_.serve_facts f
group by 1,2;
