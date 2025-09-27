-- ss_.serve_facts: canonical per-serve dataset (robust to text/boolean/int flags)
create or replace view ss_.serve_facts as
with base as (
  select
      p.session_id,
      p.session_uid_d              as session_uid,
      p.match_date_meta,
      p.customer_name,
      p.location,

      -- who served this attempt
      p.server_id                  as player_id,

      -- ordering / bucketing within the match
      p.game_number_d,
      p.point_number_d,
      p.point_in_game_d,
      p.start_s,

      -- serve attributes
      p.serve_try_ix_in_point      as serve_try,          -- 1 or 2
      p.ball_speed                 as serve_speed,        -- units from source
      p.serve_loc_18_d             as serve_loc_18,

      -- SIDE: prefer serving_side_d; else placement_ad_d; else infer from score text
      case
        when lower(coalesce(p.serving_side_d::text, '')) in ('ad','adv','advantage') then 'AD'
        when lower(coalesce(p.serving_side_d::text, '')) = 'deuce' then 'DEUCE'
        when lower(coalesce(p.placement_ad_d::text, '')) in ('1','t','true','ad','adv','advantage') then 'AD'
        when lower(coalesce(p.point_score_text_d::text, '')) like '%ad%' then 'AD'
        else 'DEUCE'
      end                           as side,

      -- outcomes (normalize mixed types by casting to text first)
      case when coalesce(p.is_serve_fault_d::text,'0') in ('1','t','true','TRUE','True')
           then 1 else 0 end                                                   as is_fault,
      case when coalesce(p.is_serve_fault_d::text,'0') in ('1','t','true','TRUE','True')
           then 0 else 1 end                                                   as is_in,
      case when coalesce(p.is_serve_fault_d::text,'0') in ('1','t','true','TRUE','True')
                and p.serve_try_ix_in_point = 2
           then 1 else 0 end                                                   as is_double_fault,

      -- “ace” = serve-in, no rally shot, server wins point
      case when coalesce(p.is_serve_fault_d::text,'0') in ('0','f','false','FALSE','False','')
                and p.first_rally_shot_ix is null
                and p.point_winner_player_id_d = p.server_id
           then 1 else 0 end                                                   as is_ace,

      -- unreturned (includes aces): in, no rally shot
      case when coalesce(p.is_serve_fault_d::text,'0') in ('0','f','false','FALSE','False','')
                and p.first_rally_shot_ix is null
           then 1 else 0 end                                                   as is_unreturned,

      case when p.point_winner_player_id_d = p.server_id
           then 1 else 0 end                                                   as point_won_by_server

  from ss_.vw_point_enriched p
  where coalesce(p.serve_d::text,'0') in ('1','t','true','TRUE','True')   -- keep only serve swings
)
select * from base;
