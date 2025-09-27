-- ss_.serve_loc_distribution: distribution & effectiveness by 18-box grid
create or replace view ss_.serve_loc_distribution as
select
  f.session_id,
  f.player_id,
  f.side,
  f.serve_loc_18,

  count(*)                                         as attempts,
  sum(case when f.is_in=1 then 1 else 0 end)       as serves_in,
  sum(case when f.is_unreturned=1 then 1 else 0 end) as unreturned,
  sum(case when f.is_ace=1 then 1 else 0 end)      as aces,
  avg(nullif(f.serve_speed,0))                     as avg_speed,

  -- quality metrics
  (sum(case when f.is_in=1 then 1 else 0 end)::numeric / nullif(count(*),0))       as in_pct,
  (sum(case when f.point_won_by_server=1 and f.is_in=1 then 1 else 0 end)::numeric
      / nullif(sum(case when f.is_in=1 then 1 else 0 end),0))                      as win_pct_when_in,
  (sum(case when f.is_unreturned=1 then 1 else 0 end)::numeric / nullif(count(*),0)) as unret_pct
from ss_.serve_facts f
group by 1,2,3,4;
