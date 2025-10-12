# 001: ss_.vw_point – BI-friendly point rows built from vw_point_silver
# - auto-detect source schema
# - denoise (valid_swing only)
# - normalize A/B + join key
# - fold serve into swing_type
# - keep only point facts (no player metadata)

def make_sql(cur):
    # Locate vw_point_silver
    cur.execute("""
        with cte as (
          select table_schema from information_schema.views  where table_name = 'vw_point_silver'
          union all
          select table_schema from information_schema.tables where table_name = 'vw_point_silver'
        )
        select table_schema from cte limit 1;
    """)
    row = cur.fetchone()
    if not row:
        raise RuntimeError("vw_point_silver not found")
    src = f"{row[0]}.vw_point_silver"

    return f"""
    -- Rebuild objects so column set can change safely
    DROP VIEW IF EXISTS ss_.vw_point CASCADE;
    DROP MATERIALIZED VIEW IF EXISTS ss_.mv_point CASCADE;

    CREATE MATERIALIZED VIEW ss_.mv_point AS
    WITH base AS (
      SELECT p.*
      FROM {src} p
      WHERE COALESCE(p.valid_swing_d, FALSE) = TRUE   -- keep only valid swings (remove SportAI noise)
    ),
    labeled AS (
      SELECT
        /* identity */
        b.session_id,
        b.session_uid_d,
        b.player_id,

        /* Player A/B per session (numeric-id ordering heuristic) */
        CASE
          WHEN b.player_id = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
          ELSE 'Player B'
        END AS player_label,

        /* join key to ss_.vw_player */
        (b.session_id::text || '|' ||
          CASE
            WHEN b.player_id = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
            ELSE 'Player B'
          END
        ) AS session_player_key,

        /* server/winner ids (to link back to player tab) */
        b.server_id,
        b.point_winner_player_id_d AS winner_id,

        /* fold serve into swing_type */
        CASE
          WHEN COALESCE(b.serve_d, FALSE) = TRUE OR b.start_serve_shot_ix = 1 THEN 'serve'
          ELSE CASE lower(COALESCE(b.swing_type_raw,''))
                 WHEN 'fh'         THEN 'forehand'
                 WHEN 'bh'         THEN 'backhand'
                 WHEN 'sv'         THEN 'serve-volley'
                 WHEN 'volley'     THEN 'volley'
                 WHEN 'oh'         THEN 'overhead'
                 WHEN 'fh_volley'  THEN 'forehand-volley'
                 WHEN 'bh_volley'  THEN 'backhand-volley'
                 ELSE 'unknown'
               END
        END AS swing_type,

        /* friendly serve try (optional but handy) */
        CASE b.serve_try_ix_in_point WHEN 1 THEN 'first' WHEN 2 THEN 'second' ELSE NULL END AS serve_try,

        /* tracking fields useful for visuals */
        b.ball_speed,                 -- numeric (can be NULL)
        b.serve_loc_18_d   AS serve_loc_18,  -- 1..8 if present
        b.serving_side_d   AS serving_side,  -- 'ad'/'deuce' (keep for visuals)
        b.point_number_d   AS point_number,
        b.game_number_d    AS game_number,
        b.point_in_game_d  AS point_in_game,
        b.first_rally_shot_ix,
        b.start_serve_shot_ix,

        /* optional scoring context (texts are handy in tooltips) */
        b.point_score_text_d        AS point_score_text,
        b.game_score_text_after_d   AS game_score_text_after,

        /* keep a few terminal flags for analysis (but no player metadata) */
        b.is_serve_fault_d  AS is_serve_fault,
        b.is_last_in_point_d,
        b.is_last_valid_in_point_d,
        b.terminal_basis_d,
        b.play_d

      FROM base b
    )
    SELECT * FROM labeled;

    -- Indexes for joins & slicers
    CREATE INDEX IF NOT EXISTS mv_point_session_id_idx        ON ss_.mv_point (session_id);
    CREATE INDEX IF NOT EXISTS mv_point_sess_player_key_idx   ON ss_.mv_point (session_player_key);
    CREATE INDEX IF NOT EXISTS mv_point_player_label_idx      ON ss_.mv_point (player_label);
    CREATE INDEX IF NOT EXISTS mv_point_player_id_idx         ON ss_.mv_point (player_id);
    CREATE INDEX IF NOT EXISTS mv_point_server_id_idx         ON ss_.mv_point (server_id);
    CREATE INDEX IF NOT EXISTS mv_point_winner_id_idx         ON ss_.mv_point (winner_id);
    CREATE INDEX IF NOT EXISTS mv_point_swing_type_idx        ON ss_.mv_point (swing_type);
    CREATE INDEX IF NOT EXISTS mv_point_serve_loc_idx         ON ss_.mv_point (serve_loc_18);

    -- Thin view for BI
    CREATE VIEW ss_.vw_point AS
    SELECT * FROM ss_.mv_point;
    """
