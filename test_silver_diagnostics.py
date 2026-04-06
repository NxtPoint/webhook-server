"""
Silver diagnostics — run on Render shell:
    python test_silver_diagnostics.py 992fc5f2-fb7e-4ad6-8ac8-934b71592b2b
"""
import json, sys
from db_init import engine
from sqlalchemy import text

def run(tid):
    out = {}
    with engine.connect() as c:
        # 1. Counts
        r = c.execute(text("""
            SELECT COUNT(*) as total_rows,
                   COUNT(DISTINCT point_number) FILTER (WHERE point_number > 0) as distinct_points,
                   MIN(point_number) FILTER (WHERE point_number > 0) as min_point,
                   MAX(point_number) as max_point,
                   COUNT(DISTINCT game_number) FILTER (WHERE game_number > 0) as distinct_games,
                   MIN(game_number) FILTER (WHERE game_number > 0) as min_game,
                   MAX(game_number) as max_game,
                   COUNT(DISTINCT set_number) FILTER (WHERE set_number > 0) as distinct_sets
            FROM silver.point_detail WHERE task_id = :tid
        """), {"tid": tid}).mappings().first()
        out["1_counts"] = dict(r)

        # 2. NULL coverage per column
        r = c.execute(text("""
            SELECT COUNT(*) as total,
                   COUNT(serve_d) as serve_d,
                   COUNT(server_end_d) as server_end_d,
                   COUNT(serve_side_d) as serve_side_d,
                   COUNT(point_number) FILTER (WHERE point_number > 0) as point_number,
                   COUNT(game_number) FILTER (WHERE game_number > 0) as game_number,
                   COUNT(exclude_d) as exclude_d,
                   COUNT(point_winner_player_id) as point_winner,
                   COUNT(server_id) as server_id,
                   COUNT(shot_ix_in_point) as shot_ix,
                   COUNT(shot_phase_d) as shot_phase,
                   COUNT(shot_outcome_d) as shot_outcome,
                   COUNT(set_number) FILTER (WHERE set_number > 0) as set_number,
                   COUNT(serve_location) FILTER (WHERE serve_d IS TRUE) as serve_loc_on_serves,
                   COUNT(serve_try_ix_in_point) as serve_try,
                   COUNT(rally_length) as rally_length,
                   COUNT(stroke_d) as stroke,
                   COUNT(aggression_d) as aggression,
                   COUNT(depth_d) as depth,
                   COUNT(shot_q) as shot_q,
                   COUNT(ball_hit_x_norm) as hit_x_norm,
                   COUNT(ball_bounce_x_norm) as bounce_x_norm
            FROM silver.point_detail WHERE task_id = :tid
        """), {"tid": tid}).mappings().first()
        out["2_null_coverage"] = dict(r)

        # 3. Rally validation
        r = c.execute(text("""
            SELECT
              (SELECT COUNT(DISTINCT point_number) FROM silver.point_detail
               WHERE task_id = :tid AND point_number > 0) as silver_points,
              (SELECT COUNT(*) FROM bronze.rally WHERE task_id::text = :tid) as bronze_rallies
        """), {"tid": tid}).mappings().first()
        out["3_rally_validation"] = dict(r)

        # 4. Points 1-3 sample
        rows = c.execute(text("""
            SELECT point_number, game_number, set_number, serve_d,
                   server_end_d, serve_side_d, serve_try_ix_in_point,
                   shot_ix_in_point, shot_phase_d, shot_outcome_d,
                   point_winner_player_id, server_id, exclude_d, player_id
            FROM silver.point_detail
            WHERE task_id = :tid AND point_number BETWEEN 1 AND 3
            ORDER BY ball_hit_s, id LIMIT 20
        """), {"tid": tid}).mappings().fetchall()
        out["4_sample_points_1_3"] = [dict(r) for r in rows]

        # 5. Exclusion stats
        r = c.execute(text("""
            SELECT COUNT(*) FILTER (WHERE exclude_d IS TRUE) as excluded,
                   COUNT(*) FILTER (WHERE exclude_d IS FALSE OR exclude_d IS NULL) as included,
                   COUNT(*) FILTER (WHERE point_number IS NULL OR point_number <= 0) as no_point
            FROM silver.point_detail WHERE task_id = :tid
        """), {"tid": tid}).mappings().first()
        out["5_exclusions"] = dict(r)

        # 6. Serve stats
        r = c.execute(text("""
            SELECT COUNT(*) FILTER (WHERE serve_d IS TRUE) as total_serves,
                   COUNT(*) FILTER (WHERE serve_try_ix_in_point = '1st') as first_serves,
                   COUNT(*) FILTER (WHERE serve_try_ix_in_point = '2nd') as second_serves,
                   COUNT(*) FILTER (WHERE serve_try_ix_in_point = 'Double') as double_faults,
                   COUNT(*) FILTER (WHERE ace_d IS TRUE) as aces,
                   COUNT(*) FILTER (WHERE service_winner_d IS TRUE) as service_winners
            FROM silver.point_detail WHERE task_id = :tid
        """), {"tid": tid}).mappings().first()
        out["6_serve_stats"] = dict(r)

        # 7. Outcome distribution
        rows = c.execute(text("""
            SELECT shot_outcome_d, COUNT(*) as n
            FROM silver.point_detail
            WHERE task_id = :tid AND shot_outcome_d IS NOT NULL
            GROUP BY shot_outcome_d ORDER BY n DESC
        """), {"tid": tid}).mappings().fetchall()
        out["7_outcomes"] = [dict(r) for r in rows]

        # 8. Player split
        rows = c.execute(text("""
            SELECT player_id, COUNT(*) as shots,
                   COUNT(DISTINCT point_number) FILTER (WHERE point_number > 0) as points_involved
            FROM silver.point_detail WHERE task_id = :tid
            GROUP BY player_id ORDER BY shots DESC
        """), {"tid": tid}).mappings().fetchall()
        out["8_players"] = [dict(r) for r in rows]

    print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_silver_diagnostics.py <task_id>")
        sys.exit(1)
    run(sys.argv[1])
