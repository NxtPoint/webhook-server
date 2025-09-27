-- ss_.serve_match_summary
-- Match-level KPIs per (session_id, player_id), built on the canonical ss_.serve_facts
-- Also counts service games and service games won from ss_.vw_point_enriched (robust boolean handling)

create or replace view ss_.serve_match_summary as
with s as (
  select * from ss_.serve_facts
),
games as (
  select
      p.session_id,
      p.server_id as player_id,
      count(*)                                                    as serve_games,
      sum(case when p.game_winner_player_id_d = p.server_id
               then 1 else 0 end)                                 as serve_games_won
  from ss_.vw_point_enriched p
  where coalesce(p.is_game_end_d::text,'0') in ('1','t','true','TRUE','True')
  group by p.session_id, p.server_id
)
select
    s.session_id,
    s.player_id,
    min(s.match_date_meta)                                        as match_date_meta,

    -- volume
    count(*)                                          as total_serves,
    count(*) filter (where s.serve_try=1)             as first_serves,
    count(*) filter (where s.serve_try=2)             as second_serves,

    -- first-serve results
    sum(s.is_in)                                      as serves_in_total,
    sum(s.is_in)  filter (where s.serve_try=1)        as first_in,
    sum(s.is_in)  filter (where s.serve_try=2)        as second_in,
    sum(s.point_won_by_server) filter (where s.serve_try=1 and s.is_in=1)
                                                      as first_points_won_when_in,
    sum(s.point_won_by_server) filter (where s.serve_try=2 and s.is_in=1)
                                                      as second_points_won_when_in,

    -- faults / aces / unreturned
    sum(s.is_double_fault)                            as double_faults,
    sum(s.is_ace)                                     as aces,
    sum(s.is_unreturned)                              as unreturned,

    -- speeds
    avg(s.serve_speed) filter (where s.serve_try=1)   as avg_first_speed,
    avg(s.serve_speed) filter (where s.serve_try=2)   as avg_second_speed,

    -- percentages
    (sum(s.is_in))::numeric / nullif(count(*),0)                               as in_pct_total,
    (sum(s.is_in) filter (where s.serve_try=1))::numeric
        / nullif(count(*) filter (where s.serve_try=1),0)                      as first_in_pct,
    (sum(s.is_in) filter (where s.serve_try=2))::numeric
        / nullif(count(*) filter (where s.serve_try=2),0)                      as second_in_pct,

    (sum(s.point_won_by_server) filter (where s.serve_try=1 and s.is_in=1))::numeric
        / nullif(sum(s.is_in) filter (where s.serve_try=1),0)                  as first_win_pct_when_in,
    (sum(s.point_won_by_server) filter (where s.serve_try=2 and s.is_in=1))::numeric
        / nullif(sum(s.is_in) filter (where s.serve_try=2),0)                  as second_win_pct_when_in,

    (sum(s.is_ace))::numeric / nullif(count(*),0)                              as ace_rate,
    (sum(s.is_double_fault))::numeric / nullif(count(*) filter (where s.serve_try=2),0)
                                                                              as df_rate,
    (sum(s.is_unreturned))::numeric / nullif(sum(s.is_in),0)                   as unret_pct_when_in,

    -- service games
    coalesce(g.serve_games, 0)                                                 as serve_games,
    coalesce(g.serve_games_won, 0)                                             as serve_games_won,
    coalesce(g.serve_games_won,0)::numeric / nullif(coalesce(g.serve_games,0),0)
                                                                              as serve_games_win_pct

from s
left join games g
  on g.session_id = s.session_id
 and g.player_id  = s.player_id
group by s.session_id, s.player_id, g.serve_games, g.serve_games_won;
