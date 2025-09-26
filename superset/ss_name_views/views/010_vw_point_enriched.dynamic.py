def make_sql(cur):
    return """
    create schema if not exists ss_;
    create or replace view ss_.vw_point_enriched as
    select
      p.*,
      null::text   as customer_name,
      null::text   as email,
      null::text   as first_point_time,
      null::date   as match_date_meta,
      null::text   as location,
      null::text   as player_a_name,
      null::numeric as player_a_utr,
      null::text   as player_b_name,
      null::numeric as player_b_utr,
      null::boolean as accept_terms
    from silver.vw_point_silver p;
    """
