# 001: ss_.vw_point – BI-friendly point facts from vw_point_silver
# - denoise valid swings
# - normalize stroke names
# - add A/B labels and join keys for hitter, server, winner
# - keep only point facts (no player metadata)

def make_sql(cur):
    # Find schema containing vw_point_silver
    cur.execute("""
        with cte as (
          select table_schema from information_schema.views  where table_name='vw_point_silver'
          union all
          select table_schema from information_schema.tables where table_name='vw_point_silver'
        )
        select table_schema from cte limit 1;
    """)
    row = cur.fetchone()
    if not row:
        raise RuntimeError("vw_point_silver not found")
    src = f"{row[0]}.vw_point_silver"

    return f"""
    -- Recreate objects to allow column set changes
    DROP VIEW IF EXISTS ss_.vw_point CASCADE;
    DROP MATERIALIZED VIEW IF EXISTS ss_.mv_point CASCADE;

    CREATE MATERIALIZED VIEW ss_.mv_point AS
    WITH base AS (
      SELECT p.*
      FROM {src} p
      WHERE COALESCE(p.valid_swing_d, FALSE) = TRUE
    ),
    labeled AS (
      SELECT
        b.session_id,
        b.session_uid_d,
        b.player_id,

        -- Player A/B ids per session (numeric-order heuristic)
        MIN(b.player_id) OVER (PARTITION BY b.session_id) AS player_a_id,
        MAX(b.player_id) OVER (PARTITION BY b.session_id) AS player_b_id,

        -- Hitter (this row)
        CASE
          WHEN b.player_id = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
          ELSE 'Player B'
        END AS player_label,

        (b.session_id::text || '|' ||
          CASE
            WHEN b.player_id = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
            ELSE 'Player B'
          END
        ) AS session_player_key,

        -- Server / Winner IDs (raw)
        b.server_id,
        b.point_winner_player_id_d AS winner_id,

        -- Helper: server label and session key
        CASE
          WHEN b.server_id IS NULL THEN NULL
          WHEN b.server_id = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
          WHEN b.server_id = MAX(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player B'
          ELSE NULL
        END AS server_label,

        CASE
          WHEN b.server_id IS NULL THEN NULL
          WHEN b.server_id = MIN(b.player_id) OVER (PARTITION BY b.session_id)
            THEN (b.session_id::text || '|Player A')
          WHEN b.server_id = MAX(b.player_id) OVER (PARTITION BY b.session_id)
            THEN (b.session_id::text || '|Player B')
          ELSE NULL
        END AS server_session_player_key,

        -- Helper: winner label and session key
        CASE
          WHEN b.point_winner_player_id_d IS NULL THEN NULL
          WHEN b.point_winner_player_id_d = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
          WHEN b.point_winner_player_id_d = MAX(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player B'
          ELSE NULL
        END AS winner_label,

        CASE
          WHEN b.point_winner_player_id_d IS NULL THEN NULL
          WHEN b.point_winner_player_id_d = MIN(b.player_id) OVER (PARTITION BY b.session_id)
            THEN (b.session_id::text || '|Player A')
          WHEN b.point_winner_player_id_d = MAX(b.player_id) OVER (PARTITION BY b.session_id)
            THEN (b.session_id::text || '|Player B')
          ELSE NULL
        END AS winner_session_player_key,

        -- Stroke normalization (serve folded in; volley & overhead logic)
        CASE
          WHEN COALESCE(b.serve_d, FALSE) = TRUE
             OR b.start_serve_shot_ix = 1
             OR b.serve_try_ix_in_point IN (1,2)
            THEN 'serve'
          WHEN lower(COALESCE(b.swing_type_raw,'')) IN ('oh','overhead','fh_overhead','bh_overhead')
            THEN 'overhead'
          WHEN lower(COALESCE(b.play_d,'')) = 'net'
               AND lower(COALESCE(b.swing_type_raw,'')) NOT IN ('oh','overhead','fh_overhead','bh_overhead')
            THEN 'volley'
          WHEN lower(COALESCE(b.swing_type_raw,'')) IN ('fh','forehand')
            THEN 'forehand'
          WHEN lower(COALESCE(b.swing_type_raw,'')) IN ('bh','backhand','1hd_bh','1h_bh','2hd_bh','2h_bh')
            THEN 'backhand'
          WHEN lower(COALESCE(b.swing_type_raw,'')) = 'sv'
            THEN 'serve-volley'
          ELSE 'unknown'
        END AS swing_type,

        -- Keep useful tracking for visuals
        b.ball_speed,
        b.serve_loc_18_d           AS serve_loc_18,
        b.serving_side_d           AS serving_side,
        b.placement_ad_d           AS placement_ad,    -- A/B/C/D rally placement (requested)
        b.serve_try_ix_in_point,
        b.first_rally_shot_ix,
        b.start_serve_shot_ix,
        b.point_number_d           AS point_number,
        b.game_number_d            AS game_number,
        b.point_in_game_d          AS point_in_game,

        -- Optional scoring context
        b.point_score_text_d       AS point_score_text,
        b.game_score_text_after_d  AS game_score_text_after,

        -- Terminal/flags (kept minimal)
        b.is_serve_fault_d         AS is_serve_fault,
        b.is_last_in_point_d,
        b.is_last_valid_in_point_d,
        b.terminal_basis_d,
        b.play_d

      FROM base b
    )
    SELECT
      -- Select only clean fact columns (no player metadata here)
      session_id, session_uid_d, player_id,
      player_label, session_player_key,
      server_id, server_label, server_session_player_key,
      winner_id, winner_label, winner_session_player_key,
      swing_type, serve_try,
      ball_speed, serve_loc_18, serving_side, placement_ad,
      serve_try_ix_in_point, first_rally_shot_ix, start_serve_shot_ix,
      point_number, game_number, point_in_game,
      point_score_text, game_score_text_after,
      is_serve_fault, is_last_in_point_d, is_last_valid_in_point_d, terminal_basis_d, play_d
    FROM labeled;

    -- Indexes for joins & slicers
    CREATE INDEX IF NOT EXISTS mv_point_session_id_idx          ON ss_.mv_point (session_id);
    CREATE INDEX IF NOT EXISTS mv_point_sess_player_key_idx     ON ss_.mv_point (session_player_key);
    CREATE INDEX IF NOT EXISTS mv_point_player_label_idx        ON ss_.mv_point (player_label);
    CREATE INDEX IF NOT EXISTS mv_point_player_id_idx           ON ss_.mv_point (player_id);
    CREATE INDEX IF NOT EXISTS mv_point_server_id_idx           ON ss_.mv_point (server_id);
    CREATE INDEX IF NOT EXISTS mv_point_winner_id_idx           ON ss_.mv_point (winner_id);
    CREATE INDEX IF NOT EXISTS mv_point_server_key_idx          ON ss_.mv_point (server_session_player_key);
    CREATE INDEX IF NOT EXISTS mv_point_winner_key_idx          ON ss_.mv_point (winner_session_player_key);
    CREATE INDEX IF NOT EXISTS mv_point_swing_type_idx          ON ss_.mv_point (swing_type);
    CREATE INDEX IF NOT EXISTS mv_point_serve_loc_idx           ON ss_.mv_point (serve_loc_18);
    CREATE INDEX IF NOT EXISTS mv_point_placement_ad_idx        ON ss_.mv_point (placement_ad);

    -- Thin view for BI
    CREATE VIEW ss_.vw_point AS
    SELECT * FROM ss_.mv_point;
    """
