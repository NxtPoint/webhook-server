# 001: ss_.vw_point – BI-friendly point rows built from vw_point_silver
# - auto-detect source schema
# - denoise (valid_swing only)
# - normalize A/B, names, swing types
# - add win/server flags & join key
# - materialized view + indexes, and a thin view for BI

def make_sql(cur):
    # Find which schema contains vw_point_silver
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

    # Build the SQL to (re)create objects
    return f"""
    -- Drop objects first so column set can change safely
    DROP VIEW IF EXISTS ss_.vw_point CASCADE;
    DROP MATERIALIZED VIEW IF EXISTS ss_.mv_point CASCADE;

    -- Materialized, denoised & normalized dataset
    CREATE MATERIALIZED VIEW ss_.mv_point AS
    WITH base AS (
      SELECT p.*
      FROM {src} p
      WHERE COALESCE(p.valid_swing_d, FALSE) = TRUE  -- keep only valid swings
    ),
    labeled AS (
      SELECT
        b.session_id,
        b.session_uid_d,
        b.player_id,

        -- Player A/B within session (numeric-id ordering)
        CASE
          WHEN b.player_id = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
          ELSE 'Player B'
        END AS player_label,

        -- Server / winner flags
        (b.server_id = b.player_id)                 AS is_server,
        (b.point_winner_player_id_d = b.player_id)  AS point_won,
        (b.game_winner_player_id_d  = b.player_id)  AS game_won,

        -- Human-friendly swing type
        CASE lower(COALESCE(b.swing_type_raw,''))
          WHEN 'fh'         THEN 'forehand'
          WHEN 'bh'         THEN 'backhand'
          WHEN 'serve'      THEN 'serve'
          WHEN 'sv'         THEN 'serve-volley'
          WHEN 'volley'     THEN 'volley'
          WHEN 'oh'         THEN 'overhead'
          WHEN 'fh_volley'  THEN 'forehand-volley'
          WHEN 'bh_volley'  THEN 'backhand-volley'
          ELSE 'unknown'
        END AS swing_type,

        -- Serve try (friendly)
        CASE b.serve_try_ix_in_point WHEN 1 THEN 'first' WHEN 2 THEN 'second' ELSE NULL END AS serve_try,

        -- Useful raw fields (renamed)
        b.serve_try_ix_in_point,
        b.first_rally_shot_ix,
        b.start_serve_shot_ix,
        b.point_number_d            AS point_number,
        b.game_number_d             AS game_number,
        b.point_in_game_d           AS point_in_game,
        b.serving_side_d            AS serving_side,
        b.is_serve_fault_d          AS is_serve_fault,
        b.is_last_in_point_d,
        b.is_last_valid_in_point_d,
        b.terminal_basis_d,
        b.play_d,
        b.point_score_text_d        AS point_score_text,
        b.game_score_text_after_d   AS game_score_text_after,

        -- Join key to ss_.vw_player
        (b.session_id::text || '|' ||
          CASE
            WHEN b.player_id = MIN(b.player_id) OVER (PARTITION BY b.session_id) THEN 'Player A'
            ELSE 'Player B'
          END
        ) AS session_player_key
      FROM base b
    ),
    with_names AS (
      SELECT
        l.*,
        pv.player_name,
        pv.player_utr
      FROM labeled l
      LEFT JOIN ss_.vw_player pv
        ON pv.session_player_key = l.session_player_key
    )
    SELECT * FROM with_names;

    -- Helpful indexes for BI filters/joins
    CREATE INDEX IF NOT EXISTS mv_point_session_id_idx        ON ss_.mv_point (session_id);
    CREATE INDEX IF NOT EXISTS mv_point_sess_player_key_idx   ON ss_.mv_point (session_player_key);
    CREATE INDEX IF NOT EXISTS mv_point_player_label_idx      ON ss_.mv_point (player_label);
    CREATE INDEX IF NOT EXISTS mv_point_player_id_idx         ON ss_.mv_point (player_id);
    CREATE INDEX IF NOT EXISTS mv_point_is_server_idx         ON ss_.mv_point (is_server);
    CREATE INDEX IF NOT EXISTS mv_point_swing_type_idx        ON ss_.mv_point (swing_type);

    -- Thin view for BI tools
    CREATE VIEW ss_.vw_point AS
    SELECT * FROM ss_.mv_point;
    """
