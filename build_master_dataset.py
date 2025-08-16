import os 
import json
import pandas as pd
from datetime import datetime

json_dir = "./data"
output_dir = "./powerbi_exports"
os.makedirs(output_dir, exist_ok=True)

# Master collectors
players, swings, team_sessions, rallies = [], [], [], []
highlights, bounce_heatmap, ball_bounces = [], [], []
ball_positions, player_positions = [], []

# Fixed analysis date for this export
analysis_date = datetime.now().strftime("%Y-%m-%d")

# Loop through all SportAI JSONs
for filename in os.listdir(json_dir):
    if not filename.endswith(".json"):
        continue

    filepath = os.path.join(json_dir, filename)
    match_id = os.path.splitext(filename)[0]

    try:
        with open(filepath, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print(f"❌ Skipping bad JSON: {filename}")
        continue

    # Players & Swings
    for player in data.get("players", []):
        pid = player.get("player_id")
        player_row = {
            "match_id": match_id,
            "player_id": pid,
            "covered_distance": player.get("covered_distance", None),
            "fastest_sprint": player.get("fastest_sprint", None),
            "fastest_sprint_timestamp": player.get("fastest_sprint_timestamp", None),
            "activity_score": player.get("activity_score", None)
        }
        player_row["analysis_date"] = analysis_date
        players.append(player_row)

        for swing in player.get("swings", []):
            rally = swing.get("rally") or [None, None]
            loc = swing.get("ball_hit_location") or [None, None]
            swing_row = {
                "match_id": match_id,
                "player_id": pid,
                "swing_type": swing.get("swing_type", None),
                "start_time": swing.get("start", {}).get("timestamp", None),
                "end_time": swing.get("end", {}).get("timestamp", None),
                "serve": swing.get("serve", None),
                "valid": swing.get("valid", None),
                "is_in_rally": swing.get("is_in_rally", None),
                "rally_start": rally[0],
                "rally_end": rally[1],
                "ball_hit_time": swing.get("ball_hit", {}).get("timestamp", None),
                "ball_hit_location_x": loc[0],
                "ball_hit_location_y": loc[1],
                "ball_speed": swing.get("ball_speed", None),
                "ball_player_distance": swing.get("ball_player_distance", None)
            }
            swing_row["analysis_date"] = analysis_date
            swings.append(swing_row)

    # Team Sessions
    for session in data.get("team_sessions", []):
        team_sessions.append({
            "match_id": match_id,
            "start_time": session.get("start_time", None),
            "end_time": session.get("end_time", None),
            "front_team": session.get("front_team", None),
            "back_team": session.get("back_team", None),
            "analysis_date": analysis_date
        })

    # Rallies
    for i, rally in enumerate(data.get("rallies", [])):
        rallies.append({
            "match_id": match_id,
            "rally_id": i + 1,
            "start_time": rally[0],
            "end_time": rally[1],
            "duration": round(rally[1] - rally[0], 3),
            "analysis_date": analysis_date
        })

    # Highlights
    for h in data.get("highlights", []):
        highlights.append({
            "match_id": match_id,
            "type": h.get("type", None),
            "start_time": h.get("start", {}).get("timestamp", None),
            "end_time": h.get("end", {}).get("timestamp", None),
            "duration": h.get("duration", None),
            "swing_count": h.get("swing_count", None),
            "ball_speed": h.get("ball_speed", None),
            "ball_distance": h.get("ball_distance", None),
            "players_distance": h.get("players_distance", None),
            "players_speed": h.get("players_speed", None),
            "dynamic_score": h.get("dynamic_score", None),
            "analysis_date": analysis_date
        })

    # Bounce Heatmap
    for row_idx, row in enumerate(data.get("bounce_heatmap", [])):
        for col_idx, count in enumerate(row):
            bounce_heatmap.append({
                "match_id": match_id,
                "row": row_idx,
                "col": col_idx,
                "count": count if count is not None else 0,
                "analysis_date": analysis_date
            })

    # Ball Bounces
    for b in data.get("ball_bounces", []):
        pos = b.get("court_pos") or [None, None]
        ball_bounces.append({
            "match_id": match_id,
            "timestamp": b.get("timestamp", None),
            "court_x": pos[0],
            "court_y": pos[1],
            "player_id": b.get("player_id", None),
            "analysis_date": analysis_date
        })

    # Ball Positions
    for pos in data.get("ball_positions", []):
        ball_positions.append({
            "match_id": match_id,
            "timestamp": pos.get("timestamp", None),
            "X": pos.get("X", None),
            "Y": pos.get("Y", None),
            "analysis_date": analysis_date
        })

    # Player Positions
    for pid, pos_list in data.get("player_positions", {}).items():
        for pos in pos_list:
            player_positions.append({
                "match_id": match_id,
                "player_id": pid,
                "timestamp": pos.get("timestamp", None),
                "X": pos.get("X", None),
                "Y": pos.get("Y", None),
                "analysis_date": analysis_date
            })

# Save all master files
pd.DataFrame(players).to_csv(os.path.join(output_dir, "Master_Players.csv"), index=False)
pd.DataFrame(swings).to_csv(os.path.join(output_dir, "Master_Swings.csv"), index=False)
pd.DataFrame(team_sessions).to_csv(os.path.join(output_dir, "Master_TeamSessions.csv"), index=False)
pd.DataFrame(rallies).to_csv(os.path.join(output_dir, "Master_Rallies.csv"), index=False)
pd.DataFrame(highlights).to_csv(os.path.join(output_dir, "Master_Highlights.csv"), index=False)
pd.DataFrame(bounce_heatmap).to_csv(os.path.join(output_dir, "Master_BounceHeatmap.csv"), index=False)
pd.DataFrame(ball_bounces).to_csv(os.path.join(output_dir, "Master_BallBounces.csv"), index=False)
pd.DataFrame(ball_positions).to_csv(os.path.join(output_dir, "Master_BallPositions.csv"), index=False)
pd.DataFrame(player_positions).to_csv(os.path.join(output_dir, "Master_PlayerPositions.csv"), index=False)

print("✅ All SportAI master files exported successfully with guaranteed structure and analysis_date.")
