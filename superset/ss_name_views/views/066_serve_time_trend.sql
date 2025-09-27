-- ss_.serve_time_trend
-- Per-game serve KPIs across a match timeline, built on ss_.serve_facts (no boolean/int issues)

create or replace view ss_.serve_time_trend as
with s as (select * from ss_.serve_facts)
select
    s.session_id,
    s.player_id,
    min(s.match_date_meta)                                          as match_date_meta,
    s.game_number_d                                                 as game_no,

    count(*)                                          as total_serves,
    count(*) filter (where s.serve_try=1)             as first_serves,
    count(*) filter (where s.serve_try=2)             as second_serves,

    (sum(s.is_in))::numeric / nullif(count(*),0)                     as in_pct_total,
    (sum(s.is_in) filter (where s.serve_try=1))::numeric
        / nullif(count(*) filter (where s.serve_try=1),0)            as first_in_pct,
    (sum(s.is_in) filter (where s.serve_try=2))::numeric
        / nullif(count(*) filter (where s.serve_try=2),0)            as second_in_pct,

    (sum(s.point_won_by_server) filter (where s.serve_try=1 and s.is_in=1))::numeric
        / nullif(sum(s.is_in) filter (where s.serve_try=1),0)        as first_win_pct_when_in,
    (sum(s.point_won_by_server) filter (where s.serve_try=2 and s.is_in=1))::numeric
        / nullif(sum(s.is_in) filter (where s.serve_try=2),0)        as second_win_pct_when_in,

    (sum(s.is_double_fault))::numeric
        / nullif(count(*) filter (where s.serve_try=2),0)            as df_rate,
    (sum(s.is_ace))::numeric / nullif(count(*),0)                    as ace_rate,

    avg(s.serve_speed) filter (where s.serve_try=1)                  as avg_first_speed,
    avg(s.serve_speed) filter (where s.serve_try=2)                  as avg_second_speed

from s
group by s.session_id, s.player_id, s.game_number_d;
