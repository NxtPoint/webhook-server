# views_point_variants.py — point view variants (baseline v1; AF mirrors v1)

def get_point_view_sql(variant: str) -> str:
    """
    Returns the full SQL for the requested point view variant.
    Builds the common core up to swing_bounce_primary, then plugs in
    variant-specific validity CTEs, then the shared serve/outcome/final select.

    NOTE: For now, the 'af' variant is IDENTICAL to 'v1' (no experimental logic).
    """
    variant = (variant or "v1").lower()
    if variant not in ("v1", "af"):
        variant = "v1"

    CORE = r'''
CREATE OR REPLACE VIEW vw_point_silver_{TAG} AS
WITH
const AS (
  SELECT
    8.23::numeric       AS court_w_m,
    23.77::numeric      AS court_l_m,
    8.23::numeric/2     AS half_w_m,
    23.77::numeric/2    AS mid_y_m,
    6.40::numeric       AS service_box_depth_m,
    0.50::numeric       AS serve_eps_m,
    0.00001::numeric    AS eps_m
),
swing_players AS (
  SELECT fs.session_id, fs.player_id, COUNT(*) AS n_sw
  FROM fact_swing fs
  GROUP BY fs.session_id, fs.player_id
),
swing_players_ranked AS (
  SELECT sp.*,
         ROW_NUMBER() OVER (PARTITION BY sp.session_id
                            ORDER BY sp.n_sw DESC, sp.player_id) AS rn
  FROM swing_players sp
),
players_pair AS (
  SELECT
    r.session_id,
    MAX(CASE WHEN r.rn=1 THEN r.player_id END) AS p1,
    MAX(CASE WHEN r.rn=2 THEN r.player_id END) AS p2
  FROM swing_players_ranked r
  WHERE r.rn <= 2
  GROUP BY r.session_id
),
swings AS (
  SELECT
    v.*,
    COALESCE(
      v.ball_hit_ts,
      v.start_ts,
      (TIMESTAMP 'epoch' + COALESCE(v.ball_hit_s, v.start_s, 0) * INTERVAL '1 second')
    ) AS ord_ts
  FROM vw_swing_silver v
),
player_orientation AS (
  SELECT
    s.session_id, s.player_id,
    AVG(s.ball_hit_y) AS avg_hit_y,
    COALESCE(BOOL_OR(s.player_side_far_d), AVG(s.ball_hit_y) < 0) AS is_far_side_d
  FROM swings s
  GROUP BY s.session_id, s.player_id
),
serve_candidates AS (
  SELECT
    s.session_id, s.swing_id, s.player_id, s.ord_ts,
    s.ball_hit_x AS x_ref, s.ball_hit_y AS y_ref,
    (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
    CASE
      WHEN s.ball_hit_y IS NULL THEN NULL
      ELSE (s.ball_hit_y <= (SELECT serve_eps_m FROM const)
        OR  s.ball_hit_y >= (SELECT court_l_m FROM const) - (SELECT serve_eps_m FROM const))
    END AS inside_serve_band
  FROM swings s
),
serve_centerline AS (
  SELECT
    sc.session_id,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY sc.x_ref) AS center_x
  FROM serve_candidates sc
  WHERE sc.is_fh_overhead AND COALESCE(sc.inside_serve_band, FALSE)
  GROUP BY sc.session_id
),
serve_flags AS (
  SELECT
    s.session_id, s.swing_id, s.player_id, s.ord_ts,
    s.ball_hit_x AS x_ref, s.ball_hit_y AS y_ref,
    (lower(s.swing_type) IN ('fh_overhead','fh-overhead')) AS is_fh_overhead,
    CASE
      WHEN s.ball_hit_y IS NULL OR s.ball_hit_x IS NULL THEN NULL
      WHEN s.ball_hit_y < (SELECT mid_y_m FROM const)
        THEN CASE WHEN s.ball_hit_x < (SELECT center_x FROM serve_centerline sc WHERE sc.session_id = s.session_id)
                  THEN 'deuce' ELSE 'ad' END
      ELSE CASE WHEN s.ball_hit_x > (SELECT center_x FROM serve_centerline sc WHERE sc.session_id = s.session_id)
                THEN 'deuce' ELSE 'ad' END
    END AS serving_side_d,
    CASE
      WHEN s.ball_hit_y IS NULL THEN NULL
      ELSE (s.ball_hit_y <= (SELECT serve_eps_m FROM const)
        OR  s.ball_hit_y >= (SELECT court_l_m FROM const) - (SELECT serve_eps_m FROM const))
    END AS inside_serve_band
  FROM swings s
),
serve_events AS (
  SELECT
    sf.session_id,
    sf.swing_id           AS srv_swing_id,
    sf.player_id          AS server_id,
    sf.ord_ts,
    sf.serving_side_d
  FROM serve_flags sf
  WHERE sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
),
serve_events_numbered AS (
  SELECT
    se.*,
    LAG(se.serving_side_d) OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_side,
    LAG(se.server_id)      OVER (PARTITION BY se.session_id ORDER BY se.ord_ts, se.srv_swing_id) AS prev_server
  FROM serve_events se
),
serve_points AS (
  SELECT
    sen.*,
    SUM(CASE WHEN sen.prev_side IS NULL THEN 1
             WHEN sen.serving_side_d IS DISTINCT FROM sen.prev_side THEN 1
             ELSE 0 END)
      OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
            ROWS UNBOUNDED PRECEDING) AS point_number_d,
    SUM(CASE WHEN sen.prev_server IS NULL THEN 1
             WHEN sen.server_id IS DISTINCT FROM sen.prev_server THEN 1
             ELSE 0 END)
      OVER (PARTITION BY sen.session_id ORDER BY sen.ord_ts, sen.srv_swing_id
            ROWS UNBOUNDED PRECEDING) AS game_number_d
  FROM serve_events_numbered sen
),
serve_points_ix AS (
  SELECT
    sp.*,
    sp.point_number_d
      - MIN(sp.point_number_d) OVER (PARTITION BY sp.session_id, sp.game_number_d)
      + 1 AS point_in_game_d
  FROM serve_points sp
),
game_last_point AS (
  SELECT session_id, game_number_d, MAX(point_in_game_d) AS last_point_in_game_d
  FROM serve_points_ix
  GROUP BY session_id, game_number_d
),
bounces_norm AS (
  SELECT
    b.session_id, b.bounce_id, b.bounce_ts, b.bounce_s, b.bounce_type,
    b.x AS bounce_x_center_m,
    b.y AS bounce_y_center_m,
    ((SELECT mid_y_m FROM const) + b.y) AS bounce_y_norm_m,
    COALESCE(b.bounce_ts, (TIMESTAMP 'epoch' + b.bounce_s * INTERVAL '1 second')) AS bounce_ts_pref
  FROM vw_bounce_silver b
),
swings_in_point AS (
  SELECT
    s.*,
    sp.point_number_d,
    sp.game_number_d,
    sp.point_in_game_d,
    sp.server_id,
    sp.serving_side_d
  FROM swings s
  LEFT JOIN LATERAL (
    SELECT sp.* FROM serve_points_ix sp
    WHERE sp.session_id = s.session_id AND sp.ord_ts <= s.ord_ts
    ORDER BY sp.ord_ts DESC
    LIMIT 1
  ) sp ON TRUE
),
swings_with_serve AS (
  SELECT
    sip.*,
    EXISTS (
      SELECT 1 FROM serve_flags sf
      WHERE sf.session_id = sip.session_id
        AND sf.swing_id   = sip.swing_id
        AND sf.is_fh_overhead AND COALESCE(sf.inside_serve_band, FALSE)
    ) AS serve_d
  FROM swings_in_point sip
),
swings_numbered AS (
  SELECT
    sps.*,
    ROW_NUMBER() OVER (PARTITION BY sps.session_id, sps.point_number_d
                       ORDER BY sps.ord_ts, sps.swing_id) AS shot_ix,
    COUNT(*) OVER (PARTITION BY sps.session_id, sps.point_number_d) AS last_shot_ix,

    LAG(sps.player_id) OVER (
      PARTITION BY sps.session_id, sps.point_number_d
      ORDER BY sps.ord_ts, sps.swing_id) AS prev_player_id,

    LAG(sps.ball_hit_ts) OVER (
      PARTITION BY sps.session_id, sps.point_number_d
      ORDER BY sps.ord_ts, sps.swing_id) AS prev_ball_hit_ts,
    LAG(sps.ball_hit_s)  OVER (
      PARTITION BY sps.session_id, sps.point_number_d
      ORDER BY sps.ord_ts, sps.swing_id) AS prev_ball_hit_s,

    LEAD(sps.ball_hit_ts) OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_ball_hit_ts,
    LEAD(sps.ball_hit_s)  OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_ball_hit_s,
    LEAD(sps.ball_hit_x)  OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_ball_hit_x,
    LEAD(sps.ball_hit_y)  OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_ball_hit_y,
    LEAD(sps.player_id)   OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_player_id,
    LEAD(sps.swing_id)    OVER (PARTITION BY sps.session_id ORDER BY sps.ord_ts, sps.swing_id) AS next_swing_id
  FROM swings_with_serve sps
),
/* Serve bounds from serves in the point */
serve_bounds_simple AS (
  SELECT
    sn.session_id,
    sn.point_number_d,
    MIN(sn.shot_ix) FILTER (WHERE sn.serve_d) AS first_serve_ix,
    MAX(sn.shot_ix) FILTER (WHERE sn.serve_d) AS last_serve_ix_any
  FROM swings_numbered sn
  GROUP BY sn.session_id, sn.point_number_d
),
point_first_rally AS (
  SELECT sn.session_id, sn.point_number_d, MIN(sn.shot_ix) AS first_rally_shot_ix
  FROM swings_numbered sn
  JOIN serve_bounds_simple sb
    ON sb.session_id = sn.session_id AND sb.point_number_d = sn.point_number_d
  WHERE NOT sn.serve_d
    AND sn.player_id IS DISTINCT FROM sn.server_id
    AND (sb.last_serve_ix_any IS NULL OR sn.shot_ix > sb.last_serve_ix_any)
  GROUP BY sn.session_id, sn.point_number_d
),
point_starting_serve AS (
  SELECT
    sb.session_id,
    sb.point_number_d,
    sb.last_serve_ix_any AS start_serve_shot_ix
  FROM serve_bounds_simple sb
),

swings_enriched AS (
  SELECT
    sn.*,
    pfr.first_rally_shot_ix,
    pss.start_serve_shot_ix
  FROM swings_numbered sn
  LEFT JOIN point_first_rally    pfr ON pfr.session_id = sn.session_id AND pfr.point_number_d = sn.point_number_d
  LEFT JOIN point_starting_serve pss ON pss.session_id = sn.session_id AND pss.point_number_d = sn.point_number_d
),


swing_windows AS (
  SELECT
    se.*,
    COALESCE(se.ball_hit_ts, (TIMESTAMP 'epoch' + se.ball_hit_s * INTERVAL '1 second')) AS start_ts_pref
  FROM swings_enriched se
),
swing_windows_cap AS (
  SELECT
    sw.*,
    LEAST(
      sw.start_ts_pref + INTERVAL '2.5 seconds',
      COALESCE(sw.next_ball_hit_ts, sw.start_ts_pref + INTERVAL '2.5 seconds')
    ) AS end_ts_pref_raw,
    LEAST(
      sw.start_ts_pref + INTERVAL '2.5 seconds',
      COALESCE(sw.next_ball_hit_ts, sw.start_ts_pref + INTERVAL '2.5 seconds')
    ) + INTERVAL '20 milliseconds' AS end_ts_pref,
    sw.start_ts_pref + INTERVAL '5 milliseconds' AS start_ts_guard
  FROM swing_windows sw
),
swing_bounce_floor AS (
  SELECT
    swc.swing_id, swc.session_id, swc.point_number_d, swc.shot_ix,
    b.bounce_id, b.bounce_ts, b.bounce_s,
    b.bounce_x_center_m, b.bounce_y_center_m, b.bounce_y_norm_m,
    b.bounce_type AS bounce_type_raw
  FROM swing_windows_cap swc
  LEFT JOIN LATERAL (
    SELECT b.* FROM bounces_norm b
    WHERE b.session_id = swc.session_id
      AND b.bounce_type = 'floor'
      AND b.bounce_ts_pref >  swc.start_ts_guard
      AND b.bounce_ts_pref <= swc.end_ts_pref
    ORDER BY b.bounce_ts_pref, b.bounce_id
    LIMIT 1
  ) b ON TRUE
),
swing_bounce_any AS (
  SELECT
    swc.swing_id, swc.session_id, swc.point_number_d, swc.shot_ix,
    b.bounce_id   AS any_bounce_id,
    b.bounce_ts   AS any_bounce_ts,
    b.bounce_s    AS any_bounce_s,
    b.bounce_x_center_m AS any_bounce_x_center_m,
    b.bounce_y_center_m AS any_bounce_y_center_m,
    b.bounce_y_norm_m   AS any_bounce_y_norm_m,
    b.bounce_type       AS any_bounce_type
  FROM swing_windows_cap swc
  LEFT JOIN LATERAL (
    SELECT b.* FROM bounces_norm b
    WHERE b.session_id = swc.session_id
      AND b.bounce_ts_pref >  swc.start_ts_guard
      AND b.bounce_ts_pref <= swc.end_ts_pref
    ORDER BY b.bounce_ts_pref, b.bounce_id
    LIMIT 1
  ) b ON TRUE
),
swing_bounce_primary AS (
  SELECT DISTINCT ON (se.session_id, se.swing_id)
    se.session_id, se.swing_id, se.point_number_d, se.shot_ix, se.last_shot_ix,
    COALESCE(f.bounce_id,         a.any_bounce_id)          AS bounce_id,
    COALESCE(f.bounce_ts,         a.any_bounce_ts)          AS bounce_ts,
    COALESCE(f.bounce_s,          a.any_bounce_s)           AS bounce_s,
    COALESCE(f.bounce_x_center_m, a.any_bounce_x_center_m)  AS bounce_x_center_m,
    COALESCE(f.bounce_y_center_m, a.any_bounce_y_center_m)  AS bounce_y_center_m,
    COALESCE(f.bounce_y_norm_m,   a.any_bounce_y_norm_m)    AS bounce_y_norm_m,
    COALESCE(f.bounce_type_raw,   a.any_bounce_type)        AS bounce_type_raw,
    CASE WHEN f.bounce_id IS NOT NULL THEN 'floor'::text
         WHEN a.any_bounce_id IS NOT NULL THEN 'any'::text
         ELSE NULL::text
    END AS primary_source_d,

    se.serve_d, se.first_rally_shot_ix, se.start_serve_shot_ix,
    se.player_id, se.prev_player_id, se.server_id, se.game_number_d, se.point_in_game_d, se.serving_side_d,

    se.start_s, se.end_s, se.ball_hit_s, se.start_ts, se.end_ts, se.ball_hit_ts,
    se.ball_hit_x, se.ball_hit_y, se.ball_speed, se.swing_type AS swing_type_raw,

    /* kept for future use (not required by baseline) */
    se.next_ball_hit_ts,
    se.next_ball_hit_s,

    se.next_ball_hit_x, se.next_ball_hit_y, se.next_player_id, se.next_swing_id,
    se.prev_ball_hit_ts, se.prev_ball_hit_s,
    se.player_side_far_d,
    se.ord_ts
  FROM swings_enriched se
  LEFT JOIN swing_bounce_floor f
    ON f.session_id = se.session_id AND f.swing_id = se.swing_id
  LEFT JOIN swing_bounce_any a
    ON a.session_id = se.session_id AND a.swing_id = se.swing_id
  ORDER BY se.session_id, se.swing_id,
           (f.bounce_id IS NULL) ASC,
           se.shot_ix DESC
)
'''

    # ------------ Variant blocks (AF mirrors V1; no experimental heuristics) ---------

    V1 = r'''
    ,/* score + cluster de-dupe (keep only the better of same-player hits within 120ms) */
    sbp_scored AS (
    SELECT
        sbp.*,
        (CASE WHEN sbp.primary_source_d = 'floor' THEN 2 ELSE 0 END) +
        (CASE WHEN sbp.bounce_id IS NOT NULL     THEN 1 ELSE 0 END) +
        (CASE WHEN sbp.ball_speed IS NOT NULL    THEN 1 ELSE 0 END) +
        (CASE WHEN sbp.next_ball_hit_ts IS NOT NULL THEN 1 ELSE 0 END) AS evidence_score,
        COALESCE(sbp.ball_hit_ts, (TIMESTAMP 'epoch' + sbp.ball_hit_s * INTERVAL '1 second')) AS this_ts
    FROM swing_bounce_primary sbp
    ),
    cluster_flags AS (
    SELECT
        s.*,
        /* same player hit within 2s BEFORE this swing (non-serve) */
        (s.prev_player_id = s.player_id
        AND s.prev_ball_hit_ts IS NOT NULL
        AND (s.this_ts - s.prev_ball_hit_ts) <= INTERVAL '2 seconds'
        AND NOT s.serve_d) AS cluster_prev,
        /* same player hit within 2s AFTER this swing (non-serve) */
        (s.next_player_id = s.player_id
        AND s.next_ball_hit_ts IS NOT NULL
        AND (s.next_ball_hit_ts - s.this_ts) <= INTERVAL '2 seconds'
        AND NOT s.serve_d) AS cluster_next,
        LAG(s.evidence_score) OVER (PARTITION BY s.session_id, s.point_number_d
                                    ORDER BY s.ord_ts, s.swing_id) AS prev_score,
        LEAD(s.evidence_score) OVER (PARTITION BY s.session_id, s.point_number_d
                                    ORDER BY s.ord_ts, s.swing_id) AS next_score
    FROM sbp_scored s
    ),


    /* soft kill 1: 2s same-player cluster — later wins on tie */
    kills_soft AS (
    SELECT
        c.*,
        ((c.cluster_prev AND c.prev_score IS NOT NULL AND c.evidence_score <  c.prev_score) OR
        (c.cluster_next AND c.next_score IS NOT NULL AND c.evidence_score <= c.next_score)) AS cluster_kill_d,
        FALSE::boolean AS alt_kill_d
    FROM cluster_flags c
    ),
    /* soft kill 3: duplicate bounce_id — keep exactly one winner per (session_id, bounce_id) */
    bounce_dupe_rank AS (
    SELECT
        k.session_id,
        k.swing_id,
        k.bounce_id,
        ROW_NUMBER() OVER (
        PARTITION BY k.session_id, k.bounce_id
        ORDER BY
            k.evidence_score DESC,
            CASE WHEN k.primary_source_d='floor' THEN 0 ELSE 1 END,
            k.shot_ix ASC,
            k.swing_id ASC
        ) AS rn
    FROM kills_soft k
    WHERE k.bounce_id IS NOT NULL
    ),
    kills_all AS (
    SELECT
        k.*,
        (br.rn IS NOT NULL AND br.rn > 1) AS bounce_kill_d
    FROM kills_soft k
    LEFT JOIN bounce_dupe_rank br
        ON br.session_id = k.session_id AND br.swing_id = k.swing_id
    ),

    /* serve bounds for between-serves (first serve → last serve before rally) */
    serve_bounds AS (
    SELECT
        se.session_id, se.point_number_d,
        MIN(se.shot_ix) FILTER (WHERE se.serve_d) AS first_serve_ix,
        MAX(se.start_serve_shot_ix)               AS last_serve_before_rally_ix
    FROM kills_all se
    GROUP BY se.session_id, se.point_number_d
    ),

    between_serves AS (
    SELECT
        k.session_id,
        k.swing_id,
        k.point_number_d,
        k.shot_ix,
        k.serve_d,
        k.ord_ts,
        k.first_rally_shot_ix,

        /* expose soft-kill bits so TAIL can show them */
        COALESCE(k.cluster_kill_d, FALSE) AS cluster_kill_d,
        FALSE::boolean                    AS alt_kill_d,   -- keep disabled
        COALESCE(k.bounce_kill_d, FALSE)  AS bounce_kill_d,

        /* strictly between first serve and last serve-before-rally */
        (NOT k.serve_d
        AND sb.first_serve_ix IS NOT NULL
        AND sb.last_serve_before_rally_ix IS NOT NULL
        AND k.shot_ix > sb.first_serve_ix
        AND k.shot_ix < sb.last_serve_before_rally_ix) AS between_serves_d,

        /* timestamps needed downstream */
        k.this_ts,
        k.prev_ball_hit_ts,
        k.prev_ball_hit_s
    FROM kills_all k
    LEFT JOIN serve_bounds sb
        ON sb.session_id = k.session_id
    AND sb.point_number_d = k.point_number_d
    ),


    sn_ts AS (
    SELECT
        b.session_id, b.swing_id, b.point_number_d, b.shot_ix,
        b.serve_d, b.ord_ts, b.first_rally_shot_ix,
        b.cluster_kill_d, b.alt_kill_d, b.bounce_kill_d, b.between_serves_d,
        b.this_ts,
        COALESCE(b.prev_ball_hit_ts, (TIMESTAMP 'epoch' + b.prev_ball_hit_s * INTERVAL '1 second')) AS prev_ts
    FROM between_serves b
    ),


    swing_validity_base AS (
    SELECT
        s.*,
        /* base timing rule (4s) */
        CASE
        WHEN s.point_number_d IS NULL THEN FALSE
        WHEN s.serve_d THEN TRUE
        WHEN s.prev_ts IS NULL THEN FALSE
        WHEN (s.this_ts - s.prev_ts) <= INTERVAL '4 seconds' THEN TRUE
        ELSE FALSE
        END AS valid_time_rule_d,

        /* row-level "hard invalid" reasons */
        CASE
        WHEN s.point_number_d IS NULL THEN TRUE
        WHEN s.serve_d THEN FALSE
        WHEN s.between_serves_d THEN TRUE                 -- in-between serves
        WHEN s.prev_ts IS NULL THEN TRUE
        WHEN (s.this_ts - s.prev_ts) > INTERVAL '4 seconds' THEN TRUE   -- long gap
        ELSE FALSE
        END AS hard_invalid_d,

        /* soft invalid = any soft kills (cluster/bounce) */
        (s.cluster_kill_d OR s.bounce_kill_d OR s.alt_kill_d) AS soft_invalid_d,

        /* only count hard invalids that occur at/after rally start for cascades */
        CASE
        WHEN s.serve_d THEN FALSE
        WHEN s.first_rally_shot_ix IS NULL THEN FALSE
        WHEN s.shot_ix < s.first_rally_shot_ix THEN FALSE
        ELSE
            CASE
            WHEN s.between_serves_d THEN FALSE
            WHEN s.prev_ts IS NULL THEN TRUE
            WHEN (s.this_ts - s.prev_ts) > INTERVAL '4 seconds' THEN TRUE
            ELSE FALSE
            END
        END AS hard_invalid_post_rally_d
    FROM sn_ts s
    ),

    valid_cascade AS (
    SELECT
        v.*,
        SUM(CASE WHEN v.hard_invalid_post_rally_d THEN 1 ELSE 0 END)
        OVER (PARTITION BY v.session_id, v.point_number_d
                ORDER BY v.ord_ts, v.swing_id
                ROWS UNBOUNDED PRECEDING) AS hard_invalid_seen
    FROM swing_validity_base v
    ),

    valid_final AS (
        SELECT
            vc.*,
            CASE
            WHEN vc.serve_d THEN TRUE

            /* NEW: anything strictly between first serve and last serve-before-rally is invalid */
            WHEN vc.between_serves_d THEN FALSE

            /* First rally swing (receiver's first non-serve) must be ≤ 2s after the last serve */
            WHEN vc.first_rally_shot_ix IS NOT NULL AND vc.shot_ix = vc.first_rally_shot_ix
                THEN (vc.prev_ts IS NOT NULL AND (vc.this_ts - vc.prev_ts) <= INTERVAL '2 seconds')

            /* After rally start, any hard invalid ends the rest of the point */
            WHEN vc.hard_invalid_seen > 0 THEN FALSE

            /* Soft kills (same-player ≤2s cluster winner-only, and bounce-id de-dupe) */
            WHEN vc.soft_invalid_d THEN FALSE

            /* Otherwise, timing rule decides (currently 4s) */
            ELSE vc.valid_time_rule_d
            END AS valid_swing_final_d
        FROM valid_cascade vc
        ),


    valid_numbered AS (
    SELECT
        vf.*,
        SUM(CASE WHEN vf.valid_swing_final_d THEN 1 ELSE 0 END)
        OVER (PARTITION BY vf.session_id, vf.point_number_d
                ORDER BY vf.ord_ts, vf.swing_id
                ROWS UNBOUNDED PRECEDING) AS valid_shot_ix,
        CASE WHEN vf.serve_d THEN
        SUM(CASE WHEN vf.serve_d THEN 1 ELSE 0 END)
            OVER (PARTITION BY vf.session_id, vf.point_number_d
                ORDER BY vf.ord_ts, vf.swing_id
                ROWS UNBOUNDED PRECEDING)
        END AS serve_try_ix_in_point
    FROM valid_final vf
    ),
    valid_numbered_last AS (
    SELECT
        vn.*,
        MAX(vn.valid_shot_ix) FILTER (WHERE vn.valid_swing_final_d)
        OVER (PARTITION BY vn.session_id, vn.point_number_d) AS last_valid_shot_ix,
        MAX(vn.valid_shot_ix) FILTER (WHERE vn.valid_swing_final_d AND NOT vn.serve_d)
        OVER (PARTITION BY vn.session_id, vn.point_number_d) AS last_valid_shot_ix_ns
    FROM valid_numbered vn
    )
    '''

    # AF is currently identical to V1 (no enhancements)
    AF = V1

    TAIL = r'''
,point_ends AS (
  SELECT
    ssr.session_id,
    ssr.point_number_d,
    ssr.server_id,
    CASE
      WHEN ssr.start_srv_y IS NOT NULL THEN (ssr.start_srv_y >= 20.0)
      ELSE pdir_s.is_far_side_d
    END AS server_is_far_end_d,
    CASE
      WHEN ssr.start_srv_y IS NOT NULL THEN NOT (ssr.start_srv_y >= 20.0)
      ELSE NOT pdir_s.is_far_side_d
    END AS receiver_is_far_end_d
  FROM (
    SELECT
      se.session_id, se.point_number_d, se.server_id,
      se.ball_hit_y AS start_srv_y
    FROM swing_bounce_primary se
    WHERE se.shot_ix = se.start_serve_shot_ix
  ) ssr
  LEFT JOIN player_orientation pdir_s
         ON pdir_s.session_id = ssr.session_id
        AND pdir_s.player_id  = ssr.server_id
),
serve_place_core AS (
  SELECT
    sbp.session_id, sbp.swing_id,
    sbp.serving_side_d, sbp.serve_d, sbp.start_serve_shot_ix, sbp.shot_ix,
    pe.server_is_far_end_d AS is_far_end,
    CASE WHEN sbp.bounce_type_raw = 'floor' THEN sbp.bounce_x_center_m END AS floor_x,
    sbp.ball_hit_x AS srv_x0,
    CASE WHEN sbp.next_player_id IS DISTINCT FROM sbp.player_id THEN sbp.next_ball_hit_x END AS rcv_x1
  FROM swing_bounce_primary sbp
  JOIN point_ends pe
    ON pe.session_id     = sbp.session_id
   AND pe.point_number_d = sbp.point_number_d
),
serve_place_x AS (
  SELECT
    c.*,
    CASE
      WHEN c.floor_x IS NOT NULL THEN c.floor_x
      WHEN c.rcv_x1  IS NOT NULL THEN c.rcv_x1
      ELSE c.srv_x0
    END AS srv_x_resolved
  FROM serve_place_core c
),
serve_place_final AS (
  SELECT
    x.session_id,
    x.swing_id,
    CASE
      WHEN x.serve_d IS TRUE
       AND x.start_serve_shot_ix IS NOT NULL
       AND x.shot_ix = x.start_serve_shot_ix
      THEN (
        WITH params AS (
          SELECT (SELECT court_w_m FROM const) AS cw,
                 (SELECT eps_m    FROM const) AS eps
        ),
        norm AS (
          SELECT
            CASE WHEN x.is_far_end THEN x.srv_x_resolved
                 ELSE (SELECT cw FROM params) - x.srv_x_resolved
            END AS x_eff,
            (SELECT (cw / 8.0) FROM params) AS w8
        ),
        idx8 AS (
          SELECT
            GREATEST(1,
              LEAST(8,
                (1 + FLOOR(
                  LEAST(GREATEST(x_eff, 0::numeric),
                        (SELECT cw FROM params) - (SELECT eps FROM params)
                  ) / w8
                ))::int
              )
            ) AS lane_1_8
          FROM norm
        ),
        sided AS (
          SELECT
            CASE
              WHEN x.serving_side_d = 'deuce'
                THEN CASE WHEN lane_1_8 > 4 THEN lane_1_8 - 4 ELSE lane_1_8 END
              ELSE CASE WHEN lane_1_8 < 5 THEN lane_1_8 + 4 ELSE lane_1_8 END
            END AS lane_1_8_sided
          FROM idx8
        )
        SELECT lane_1_8_sided FROM sided
      )
      ELSE NULL
    END AS serve_bucket_1_8
  FROM serve_place_x x
),
point_outcome AS (
  SELECT
    sbp.session_id, sbp.point_number_d, sbp.game_number_d, sbp.point_in_game_d,
    sbp.server_id,
    sbp.player_id AS hitter_id,
    sbp.shot_ix, sbp.last_shot_ix,
    sbp.ball_speed, sbp.bounce_id,
    sbp.bounce_x_center_m, sbp.bounce_y_norm_m,
    CASE
      WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN TRUE
      WHEN sbp.bounce_id IS NULL THEN TRUE
      WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
            AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN FALSE
      ELSE TRUE
    END AS is_error_last,
    CASE
      WHEN (
        CASE
          WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN TRUE
          WHEN sbp.bounce_id IS NULL THEN TRUE
          WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
                AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN FALSE
          ELSE TRUE
        END
      ) IS TRUE
      THEN NULL
      ELSE sbp.player_id
    END AS point_winner_if_in_d
  FROM swing_bounce_primary sbp
  WHERE sbp.shot_ix = sbp.last_shot_ix
),
point_outcome_winner AS (
  SELECT
    po.*,
    CASE
      WHEN po.point_winner_if_in_d IS NOT NULL THEN po.point_winner_if_in_d
      ELSE CASE WHEN po.hitter_id = pp.p1 THEN pp.p2 ELSE pp.p1 END
    END AS point_winner_player_id_d
  FROM point_outcome po
  JOIN players_pair pp ON pp.session_id = po.session_id
),
bounce_explain AS (
  SELECT
    se.session_id, se.swing_id,
    CASE WHEN sbp.bounce_id IS NOT NULL THEN NULL ELSE 'no_bounce_in_window' END AS why_null
  FROM swings_enriched se
  LEFT JOIN swing_bounce_primary sbp
    ON sbp.session_id=se.session_id AND sbp.swing_id=se.swing_id
),
points_accum AS (
  SELECT
    pow.*,
    SUM(CASE WHEN pow.point_winner_player_id_d = pow.server_id THEN 1 ELSE 0 END)
      OVER (PARTITION BY pow.session_id, pow.game_number_d
            ORDER BY pow.point_in_game_d
            ROWS UNBOUNDED PRECEDING) AS server_pts_cum,
    SUM(CASE WHEN pow.point_winner_player_id_d <> pow.server_id THEN 1 ELSE 0 END)
      OVER (PARTITION BY pow.session_id, pow.game_number_d
            ORDER BY pow.point_in_game_d
            ROWS UNBOUNDED PRECEDING) AS recv_pts_cum
  FROM point_outcome_winner pow
),
points_scored AS (
  SELECT
    pa.*,
    glp.last_point_in_game_d,
    (pa.point_in_game_d = glp.last_point_in_game_d) AS is_last_point_by_serve,
    CASE
      WHEN pa.server_pts_cum >= 4 OR pa.recv_pts_cum >= 4 THEN
          CASE WHEN ABS(pa.server_pts_cum - pa.recv_pts_cum) >= 2 THEN TRUE ELSE FALSE END
      ELSE FALSE
    END AS is_game_end_scoring,
    CASE
      WHEN pa.server_pts_cum >= 3 AND pa.recv_pts_cum >= 3 THEN
        CASE
          WHEN pa.server_pts_cum = pa.recv_pts_cum     THEN '40-40'
          WHEN pa.server_pts_cum = pa.recv_pts_cum + 1 THEN 'Ad-40'
          WHEN pa.recv_pts_cum  = pa.server_pts_cum + 1 THEN '40-Ad'
          ELSE '40-40'
        END
      ELSE
        (CASE pa.server_pts_cum WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END)
        || '-' ||
        (CASE pa.recv_pts_cum   WHEN 0 THEN '0' WHEN 1 THEN '15' WHEN 2 THEN '30' ELSE '40' END)
    END AS point_score_text_d
  FROM points_accum pa
  JOIN game_last_point glp
    ON glp.session_id = pa.session_id AND glp.game_number_d = pa.game_number_d
),
points_scored_winner AS (
  SELECT
    ps.*,
    (ps.is_game_end_scoring AND ps.is_last_point_by_serve) AS is_game_end_d,
    CASE
      WHEN (ps.is_game_end_scoring AND ps.is_last_point_by_serve) THEN
        CASE
          WHEN ps.server_pts_cum > ps.recv_pts_cum THEN ps.server_id
          ELSE CASE WHEN ps.server_id = pp.p1 THEN pp.p2 ELSE pp.p1 END
        END
      ELSE NULL
    END AS game_winner_player_id_d
  FROM points_scored ps
  JOIN players_pair pp ON pp.session_id = ps.session_id
),
games_running AS (
  SELECT
    psw.*,
    SUM(CASE WHEN psw.is_game_end_d AND psw.game_winner_player_id_d = psw.server_id THEN 1 ELSE 0 END)
      OVER (PARTITION BY psw.session_id
            ORDER BY psw.game_number_d, psw.point_in_game_d
            ROWS UNBOUNDED PRECEDING) AS games_server_after_d,
    SUM(CASE WHEN psw.is_game_end_d AND psw.game_winner_player_id_d <> psw.server_id THEN 1 ELSE 0 END)
      OVER (PARTITION BY psw.session_id
            ORDER BY psw.game_number_d, psw.point_in_game_d
            ROWS UNBOUNDED PRECEDING) AS games_receiver_after_d
  FROM points_scored_winner psw
)
SELECT
  sbp.session_id,
  vss.session_uid_d,
  sbp.swing_id,
  sbp.player_id,
  sbp.start_s, sbp.end_s, sbp.ball_hit_s,
  sbp.start_ts, sbp.end_ts, sbp.ball_hit_ts,
  sbp.ball_hit_x, sbp.ball_hit_y,
  sbp.ball_speed,
  sbp.swing_type_raw,

  sbp.bounce_id,
  sbp.bounce_ts             AS bounce_ts_d,
  sbp.bounce_type_raw,
  sbp.bounce_s              AS bounce_s_d,
  sbp.bounce_x_center_m     AS bounce_x_center_m,
  sbp.bounce_y_center_m     AS bounce_y_center_m,
  sbp.bounce_y_norm_m       AS bounce_y_norm_m,
  sbp.primary_source_d,

  vbs.bounce_hitter_id,

  sbp.serve_d,
  vnl.serve_try_ix_in_point,
  sbp.first_rally_shot_ix,
  sbp.start_serve_shot_ix,

  sbp.point_number_d,
  sbp.game_number_d,
  sbp.point_in_game_d,
  sbp.serving_side_d,
  sbp.server_id,

  vb.between_serves_d,
  vb.cluster_kill_d,
  vb.alt_kill_d,

  (vnl.valid_swing_final_d AND vnl.valid_shot_ix = vnl.last_valid_shot_ix_ns) AS is_last_in_point_d,

  vnl.valid_swing_final_d AS valid_swing_d,
  (vnl.valid_swing_final_d AND vnl.valid_shot_ix = vnl.last_valid_shot_ix) AS is_last_valid_in_point_d,

  CASE
    WHEN sbp.bounce_id IS NULL THEN NULL
    ELSE (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
      AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const))
  END AS bounce_in_court_any_d,

  CASE
    WHEN sbp.serve_d IS NOT TRUE THEN NULL
    WHEN sbp.first_rally_shot_ix IS NULL THEN TRUE
    WHEN sbp.start_serve_shot_ix IS NULL THEN TRUE
    WHEN sbp.shot_ix < sbp.start_serve_shot_ix THEN TRUE
    WHEN sbp.shot_ix = sbp.start_serve_shot_ix THEN FALSE
    ELSE NULL
  END AS is_serve_fault_d,

  CASE
    WHEN sbp.shot_ix <> sbp.last_shot_ix THEN NULL
    ELSE CASE
      WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN 'no_speed'
      WHEN sbp.bounce_id IS NULL THEN 'no_bounce'
      WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
            AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN 'in'
      ELSE 'out'
    END
  END AS terminal_basis_d,

  CASE
    WHEN sbp.shot_ix = sbp.last_shot_ix THEN
      CASE
        WHEN (
          CASE
            WHEN COALESCE(sbp.ball_speed, 0) <= 0 THEN TRUE
            WHEN sbp.bounce_id IS NULL THEN TRUE
            WHEN (sbp.bounce_x_center_m BETWEEN 0 AND (SELECT court_w_m FROM const)
                  AND sbp.bounce_y_norm_m BETWEEN 0 AND (SELECT court_l_m FROM const)) THEN FALSE
            ELSE TRUE
          END
        ) IS TRUE
        THEN (CASE WHEN sbp.player_id = pp.p1 THEN pp.p2 ELSE pp.p1 END)
        ELSE sbp.player_id
      END
    ELSE NULL
  END AS point_winner_player_id_d,

  pdir.is_far_side_d AS player_is_far_side_d,

  CASE
    WHEN sbp.shot_ix <> sbp.last_shot_ix OR sbp.bounce_id IS NULL THEN NULL
    ELSE (sbp.bounce_x_center_m < 0 OR sbp.bounce_x_center_m > (SELECT court_w_m FROM const))
  END AS is_wide_last_d,

  CASE
    WHEN sbp.shot_ix <> sbp.last_shot_ix OR sbp.bounce_id IS NULL THEN NULL
    ELSE CASE
      WHEN pdir.is_far_side_d THEN (sbp.bounce_y_norm_m < 0)
      ELSE (sbp.bounce_y_norm_m > (SELECT court_l_m FROM const))
    END
  END AS is_long_last_d,

  CASE
    WHEN sbp.shot_ix <> sbp.last_shot_ix OR sbp.bounce_id IS NULL THEN NULL
    ELSE CASE
      WHEN (sbp.bounce_x_center_m < 0 OR sbp.bounce_x_center_m > (SELECT court_w_m FROM const))
          AND (CASE WHEN pdir.is_far_side_d THEN sbp.bounce_y_norm_m < 0 ELSE sbp.bounce_y_norm_m > (SELECT court_l_m FROM const) END)
        THEN 'both'
      WHEN (sbp.bounce_x_center_m < 0 OR sbp.bounce_x_center_m > (SELECT court_w_m FROM const))
        THEN 'wide'
      WHEN (CASE WHEN pdir.is_far_side_d THEN sbp.bounce_y_norm_m < 0 ELSE sbp.bounce_y_norm_m > (SELECT court_l_m FROM const) END)
        THEN 'long'
      ELSE NULL
    END
  END AS out_axis_last_d,

  spf.serve_bucket_1_8 AS serve_loc_18_d,

  CASE
    WHEN sbp.serve_d THEN NULL
    ELSE
      CASE
        WHEN (
          CASE
            WHEN sbp.shot_ix = sbp.last_shot_ix
              THEN sbp.bounce_x_center_m
            ELSE COALESCE(
                   CASE WHEN sbp.bounce_type_raw = 'floor' THEN sbp.bounce_x_center_m END,
                   sbp.next_ball_hit_x,
                   sbp.ball_hit_x
                 )
          END
        ) IS NULL
        THEN NULL
        ELSE placement_ad(
               (
                 CASE
                   WHEN sbp.shot_ix = sbp.last_shot_ix
                     THEN sbp.bounce_x_center_m
                   ELSE COALESCE(
                          CASE WHEN sbp.bounce_type_raw = 'floor' THEN sbp.bounce_x_center_m END,
                          sbp.next_ball_hit_x,
                          sbp.ball_hit_x
                        )
                 END
               )::numeric,
               NOT COALESCE(sbp.player_side_far_d, sbp.ball_hit_y < 0),
               (SELECT court_w_m FROM const),
               (SELECT eps_m    FROM const)
             )
      END
  END AS placement_ad_d,

  CASE
    WHEN sbp.serve_d THEN 'serve'
    WHEN sbp.shot_ix = sbp.first_rally_shot_ix THEN 'return'
    WHEN ABS(sbp.ball_hit_y) <= (SELECT service_box_depth_m FROM const) THEN 'net'
    ELSE 'baseline'
  END AS play_d,

  gr.point_score_text_d,
  gr.is_game_end_d,
  gr.game_winner_player_id_d,
  gr.games_server_after_d,
  gr.games_receiver_after_d,
  CASE
    WHEN gr.point_score_text_d IS NULL THEN NULL
    ELSE (gr.games_server_after_d::text || '-' || gr.games_receiver_after_d::text)
  END AS game_score_text_after_d,

  be.why_null
FROM swing_bounce_primary sbp
JOIN vw_swing_silver vss USING (session_id, swing_id)
JOIN players_pair pp       ON pp.session_id = sbp.session_id
LEFT JOIN player_orientation pdir
      ON pdir.session_id = sbp.session_id AND pdir.player_id = sbp.player_id
LEFT JOIN games_running gr
      ON gr.session_id = sbp.session_id
     AND gr.point_number_d = sbp.point_number_d
LEFT JOIN bounce_explain be
      ON be.session_id = sbp.session_id AND be.swing_id = sbp.swing_id
LEFT JOIN serve_place_final spf
      ON spf.session_id = sbp.session_id AND spf.swing_id = sbp.swing_id
LEFT JOIN valid_numbered_last vnl
      ON vnl.session_id = sbp.session_id AND vnl.swing_id = sbp.swing_id
LEFT JOIN between_serves vb
      ON vb.session_id = sbp.session_id AND vb.swing_id = sbp.swing_id
LEFT JOIN vw_bounce_silver vbs
      ON vbs.session_id = sbp.session_id AND vbs.bounce_id = sbp.bounce_id
ORDER BY sbp.session_id, sbp.point_number_d, sbp.shot_ix, sbp.swing_id
;
'''

    block = V1 if variant == "v1" else AF # AF == V1 for now
    sql = CORE.replace("{TAG}", variant) + block + TAIL
    return sql
