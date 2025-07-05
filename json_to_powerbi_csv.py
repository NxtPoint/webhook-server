import os
import json
import pandas as pd

def export_csv_from_json(filepath):
    with open(filepath, "r") as f:
        data = json.load(f)

    os.makedirs("powerbi_exports", exist_ok=True)
    base = os.path.splitext(os.path.basename(filepath))[0]

    # Swings
    swings_records = []
    for player in data.get("players", []):
        pid = player.get("player_id")
        for swing in player.get("swings", []):
            loc = swing.get("ball_hit_location") or [None, None]
            swings_records.append({
                "player_id": pid,
                "swing_type": swing.get("swing_type"),
                "serve": swing.get("serve"),
                "in_rally": swing.get("is_in_rally"),
                "start_time": swing.get("start", {}).get("timestamp"),
                "end_time": swing.get("end", {}).get("timestamp"),
                "ball_hit_time": swing.get("ball_hit", {}).get("timestamp"),
                "ball_hit_x": loc[0],
                "ball_hit_y": loc[1],
                "ball_speed": swing.get("ball_speed"),
                "ball_player_distance": swing.get("ball_player_distance"),
                "confidence": swing.get("confidence"),
                "confidence_swing_type": swing.get("confidence_swing_type")
            })
    pd.DataFrame(swings_records).to_csv(f"powerbi_exports/{base}_swings.csv", index=False)

    # Rallies
    rallies = data.get("rallies", [])
    pd.DataFrame([
        {"rally_id": i+1, "start_time": r[0], "end_time": r[1], "duration": r[1]-r[0]}
        for i, r in enumerate(rallies)
    ]).to_csv(f"powerbi_exports/{base}_rallies.csv", index=False)

    # Ball bounces
    pd.DataFrame([
        {
            "timestamp": b["timestamp"],
            "court_x": b["court_pos"][0],
            "court_y": b["court_pos"][1],
            "player_id": b["player_id"]
        }
        for b in data.get("ball_bounces", [])
    ]).to_csv(f"powerbi_exports/{base}_bounces.csv", index=False)

    # Ball positions
    pd.DataFrame(data.get("ball_positions", [])).to_csv(f"powerbi_exports/{base}_ball_positions.csv", index=False)

    # Player positions
    player_records = []
    for pid, positions in data.get("player_positions", {}).items():
        for p in positions:
            player_records.append({
                "player_id": int(pid),
                "timestamp": p["timestamp"],
                "x": p["X"],
                "y": p["Y"]
            })
    pd.DataFrame(player_records).to_csv(f"powerbi_exports/{base}_player_positions.csv", index=False)

    print(f"✅ CSV export complete for {base}")
