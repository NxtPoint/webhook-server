# tennis_coach/data_fetcher.py — Assemble match data payload for Claude.
#
# Reads from existing gold views (no new views needed for KPIs or serve):
#   - gold.match_kpi              → summary + serve overview KPIs
#   - gold.match_serve_breakdown  → serve direction × side × win rate
#   - gold.match_rally_breakdown  → rally shot counts by aggression/depth/stroke
#   - gold.match_return_breakdown → return stats
#   - gold.coach_rally_patterns   → per stroke×depth×aggression error/winner rates
#
# Anti-hallucination: any dimension with shot_count < MIN_SAMPLE is dropped
# from the payload so Claude cannot cite unreliable stats.
#
# Output format matches the design spec section 3 ("Data format passed to Claude").

import logging
from typing import Optional

from sqlalchemy import text

from db_init import engine

log = logging.getLogger(__name__)

MIN_SAMPLE = 5  # suppress dimensions with fewer than this many shots


def _pct(numerator, denominator) -> Optional[float]:
    """Safe percentage rounded to 1 dp, or None if denominator is 0/None."""
    try:
        n = float(numerator or 0)
        d = float(denominator or 0)
        if d < 1:
            return None
        return round(100.0 * n / d, 1)
    except (TypeError, ValueError):
        return None


def _val(v, cast=None):
    """Return None for falsy-zero safely; cast if provided."""
    if v is None:
        return None
    if cast:
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None
    return v


def fetch_match_data(task_id: str) -> dict:
    """
    Assemble and return the structured match data dict for a given task_id.

    Raises ValueError if no match found for that task_id.
    """
    with engine.connect() as conn:
        # --- 1. match_kpi: one row with all top-level KPIs -----------------
        kpi = conn.execute(
            text("""
                SELECT
                    task_id, match_date, location,
                    player_a_name, player_b_name,
                    player_a_utr, player_b_utr,
                    total_points, pa_points_won, pb_points_won,
                    pa_aces, pb_aces,
                    pa_double_faults, pb_double_faults,
                    pa_winners, pb_winners,
                    pa_errors, pb_errors,
                    pa_first_serve_pct, pb_first_serve_pct,
                    pa_first_serves_total, pa_first_serves_in,
                    pb_first_serves_total, pb_first_serves_in,
                    pa_service_points, pa_svc_pts_won,
                    pa_svc_pts_won_pct,
                    pb_service_points, pb_svc_pts_won,
                    pb_svc_pts_won_pct,
                    pa_ret_pts_won, pb_ret_pts_won,
                    pa_ret_pts_won_pct, pb_ret_pts_won_pct,
                    pa_rally_pts_won, pb_rally_pts_won,
                    pa_rally_pts_won_pct, pb_rally_pts_won_pct,
                    avg_rally_length, max_rally_length,
                    pa_serve_speed_avg, pa_serve_speed_max,
                    pb_serve_speed_avg, pb_serve_speed_max
                FROM gold.match_kpi
                WHERE task_id = :tid
                LIMIT 1
            """),
            {"tid": task_id},
        ).mappings().fetchone()

        if kpi is None:
            raise ValueError(f"No match found for task_id={task_id}")

        # --- 2. serve breakdown: per player × side × direction --------------
        serve_rows = conn.execute(
            text("""
                SELECT player_role, serve_side_d, serve_bucket_d,
                       serve_count, serves_in, points_played, points_won
                FROM gold.match_serve_breakdown
                WHERE task_id = :tid
                  AND serve_count >= :min_sample
                ORDER BY player_role, serve_side_d, serve_bucket_d
            """),
            {"tid": task_id, "min_sample": MIN_SAMPLE},
        ).mappings().all()

        # --- 3. rally breakdown: per player aggregate -----------------------
        rally_rows = conn.execute(
            text("""
                SELECT player_role,
                       rally_shots,
                       aggression_attack, aggression_neutral, aggression_defence,
                       depth_deep, depth_middle, depth_short,
                       stroke_forehand, stroke_backhand,
                       winners, errors
                FROM gold.match_rally_breakdown
                WHERE task_id = :tid
                ORDER BY player_role
            """),
            {"tid": task_id},
        ).mappings().all()

        # --- 4. rally patterns: per player × stroke × depth × aggression ---
        pattern_rows = conn.execute(
            text("""
                SELECT player_role, stroke_d, depth_d, aggression_d,
                       shot_count, error_count, winner_count, error_pct, winner_pct
                FROM gold.coach_rally_patterns
                WHERE task_id = :tid
                  AND shot_count >= :min_sample
                ORDER BY player_role, stroke_d, depth_d, aggression_d
            """),
            {"tid": task_id, "min_sample": MIN_SAMPLE},
        ).mappings().all()

        # --- 5. return breakdown: per player --------------------------------
        return_rows = conn.execute(
            text("""
                SELECT player_role,
                       returns_played, return_pts_won,
                       vs_first_serve_played, vs_first_serve_won,
                       vs_second_serve_played, vs_second_serve_won,
                       return_winners, return_errors,
                       returns_deep, returns_middle, returns_short,
                       returns_forehand, returns_backhand
                FROM gold.match_return_breakdown
                WHERE task_id = :tid
                ORDER BY player_role
            """),
            {"tid": task_id},
        ).mappings().all()

    # -----------------------------------------------------------------------
    # Build payload
    # -----------------------------------------------------------------------
    k = kpi  # shorthand

    # --- match meta ---------------------------------------------------------
    match_meta: dict = {
        "player_a": str(k["player_a_name"] or "Player A"),
        "player_b": str(k["player_b_name"] or "Player B"),
    }
    if k["match_date"]:
        match_meta["date"] = str(k["match_date"])
    if k["location"]:
        match_meta["location"] = str(k["location"])

    # --- summary ------------------------------------------------------------
    total_pts = int(k["total_points"] or 0)
    pa_won    = int(k["pa_points_won"] or 0)
    pb_won    = int(k["pb_points_won"] or 0)

    summary: dict = {}
    if total_pts:
        summary["total_points"] = total_pts
    if pa_won or pb_won:
        summary["points_won_pct"] = {
            "a": round(100.0 * pa_won / total_pts, 1) if total_pts else None,
            "b": round(100.0 * pb_won / total_pts, 1) if total_pts else None,
        }
    for field, pa_col, pb_col in [
        ("aces",             "pa_aces",          "pb_aces"),
        ("double_faults",    "pa_double_faults",  "pb_double_faults"),
        ("winners",          "pa_winners",        "pb_winners"),
        ("unforced_errors",  "pa_errors",         "pb_errors"),
    ]:
        pa_v = _val(k[pa_col], int)
        pb_v = _val(k[pb_col], int)
        if pa_v is not None or pb_v is not None:
            summary[field] = {"a": pa_v, "b": pb_v}

    if k["avg_rally_length"] is not None:
        summary["avg_rally_length"] = float(k["avg_rally_length"])
    if k["max_rally_length"] is not None:
        summary["max_rally"] = int(k["max_rally_length"])

    # --- serve overview -----------------------------------------------------
    serve: dict = {}
    for label, pa_col, pb_col in [
        ("first_serve_pct",     "pa_first_serve_pct",  "pb_first_serve_pct"),
        ("service_pts_won_pct", "pa_svc_pts_won_pct",  "pb_svc_pts_won_pct"),
        ("return_pts_won_pct",  "pa_ret_pts_won_pct",  "pb_ret_pts_won_pct"),
    ]:
        _pa = _val(k[pa_col], float)
        _pb = _val(k[pb_col], float)
        if _pa is not None or _pb is not None:
            serve[label] = {"a": _pa, "b": _pb}

    if k["pa_serve_speed_avg"] or k["pb_serve_speed_avg"]:
        serve["serve_speed_avg_kmh"] = {
            "a": _val(k["pa_serve_speed_avg"], float),
            "b": _val(k["pb_serve_speed_avg"], float),
        }

    # Serve direction breakdown from match_serve_breakdown
    direction_pct:     dict = {"a": {}, "b": {}}
    direction_win_pct: dict = {"a": {}, "b": {}}

    _role_letter = {"player_a": "a", "player_b": "b"}
    for row in serve_rows:
        role   = row["player_role"]
        letter = _role_letter.get(role)
        if letter is None:
            continue
        bucket = (row["serve_bucket_d"] or "").lower()  # 'wide' / 'body' / 't'
        if not bucket:
            continue
        sc = int(row["serve_count"] or 0)
        # Sum all buckets per role to compute direction %
        direction_pct[letter][bucket] = direction_pct[letter].get(bucket, 0) + sc
        pts_played = int(row["points_played"] or 0)
        pts_won    = int(row["points_won"] or 0)
        direction_win_pct[letter][bucket] = {
            "pts_played": direction_win_pct[letter].get(bucket, {}).get("pts_played", 0) + pts_played,
            "pts_won":    direction_win_pct[letter].get(bucket, {}).get("pts_won", 0)    + pts_won,
        }

    # Convert counts to percentages, suppress buckets < MIN_SAMPLE
    for letter in ("a", "b"):
        total_dir = sum(direction_pct[letter].values())
        if total_dir >= MIN_SAMPLE:
            serve[f"direction_pct_{letter}"] = {
                b: round(100.0 * c / total_dir, 1)
                for b, c in direction_pct[letter].items()
                if c >= MIN_SAMPLE
            }
        direction_win_pct[letter] = {
            b: round(100.0 * v["pts_won"] / v["pts_played"], 1)
            for b, v in direction_win_pct[letter].items()
            if v["pts_played"] >= MIN_SAMPLE
        }
        if direction_win_pct[letter]:
            serve[f"direction_win_pct_{letter}"] = direction_win_pct[letter]

    # --- rally summary ------------------------------------------------------
    rally: dict = {}
    rally_by_role: dict = {}
    for row in rally_rows:
        role   = row["player_role"]
        letter = _role_letter.get(role)
        if letter is None:
            continue
        total_shots = int(row["rally_shots"] or 0)
        if total_shots < MIN_SAMPLE:
            continue
        rally_by_role[letter] = {
            "total_shots":    total_shots,
            "attack":         int(row["aggression_attack"] or 0),
            "neutral":        int(row["aggression_neutral"] or 0),
            "defence":        int(row["aggression_defence"] or 0),
            "deep":           int(row["depth_deep"] or 0),
            "middle":         int(row["depth_middle"] or 0),
            "short":          int(row["depth_short"] or 0),
            "forehand":       int(row["stroke_forehand"] or 0),
            "backhand":       int(row["stroke_backhand"] or 0),
            "winners":        int(row["winners"] or 0),
            "errors":         int(row["errors"] or 0),
        }

    if k["pa_rally_pts_won_pct"] is not None or k["pb_rally_pts_won_pct"] is not None:
        rally["rally_pts_won_pct"] = {
            "a": _val(k["pa_rally_pts_won_pct"], float),
            "b": _val(k["pb_rally_pts_won_pct"], float),
        }

    for letter, rd in rally_by_role.items():
        ts = rd["total_shots"]
        if rd["forehand"] + rd["backhand"] >= MIN_SAMPLE:
            rally[f"stroke_split_{letter}"] = {
                "forehand": round(100.0 * rd["forehand"] / ts, 1),
                "backhand":  round(100.0 * rd["backhand"]  / ts, 1),
            }
        if rd["attack"] + rd["neutral"] + rd["defence"] >= MIN_SAMPLE:
            rally[f"aggression_pct_{letter}"] = {
                "attack":  round(100.0 * rd["attack"]  / ts, 1),
                "neutral": round(100.0 * rd["neutral"] / ts, 1),
                "defence": round(100.0 * rd["defence"] / ts, 1),
            }
        if rd["deep"] + rd["middle"] + rd["short"] >= MIN_SAMPLE:
            rally[f"depth_pct_{letter}"] = {
                "deep":   round(100.0 * rd["deep"]   / ts, 1),
                "middle": round(100.0 * rd["middle"] / ts, 1),
                "short":  round(100.0 * rd["short"]  / ts, 1),
            }
        # Aggregate error count (not per-stroke — use stroke_patterns_* for per-stroke breakdown)
        if rd["errors"]:
            rally[f"total_errors_{letter}"] = rd["errors"]
        if rd["winners"]:
            rally[f"total_winners_{letter}"] = rd["winners"]

    # More granular stroke error rates from coach_rally_patterns
    pattern_errors: dict = {"a": {}, "b": {}}
    for row in pattern_rows:
        letter = _role_letter.get(row["player_role"])
        if letter is None:
            continue
        stroke = (row["stroke_d"] or "").lower()
        if not stroke or int(row["shot_count"] or 0) < MIN_SAMPLE:
            continue
        pattern_errors[letter][stroke] = {
            "shot_count":  int(row["shot_count"]),
            "error_pct":   float(row["error_pct"]) if row["error_pct"] is not None else None,
            "winner_pct":  float(row["winner_pct"]) if row["winner_pct"] is not None else None,
        }
    if pattern_errors["a"]:
        rally["stroke_patterns_a"] = pattern_errors["a"]
    if pattern_errors["b"]:
        rally["stroke_patterns_b"] = pattern_errors["b"]

    # --- return summary -----------------------------------------------------
    return_data: dict = {}
    for row in return_rows:
        letter = _role_letter.get(row["player_role"])
        if letter is None:
            continue
        rp = int(row["returns_played"] or 0)
        if rp < MIN_SAMPLE:
            continue
        rw = int(row["return_pts_won"] or 0)
        return_data[f"return_pts_won_pct_{letter}"] = _pct(rw, rp)
        vs1_p = int(row["vs_first_serve_played"] or 0)
        vs1_w = int(row["vs_first_serve_won"] or 0)
        vs2_p = int(row["vs_second_serve_played"] or 0)
        vs2_w = int(row["vs_second_serve_won"] or 0)
        if vs1_p >= MIN_SAMPLE:
            return_data[f"vs_first_serve_won_pct_{letter}"] = _pct(vs1_w, vs1_p)
        if vs2_p >= MIN_SAMPLE:
            return_data[f"vs_second_serve_won_pct_{letter}"] = _pct(vs2_w, vs2_p)

    # --- assemble final payload --------------------------------------------
    payload: dict = {"match": match_meta}
    if summary:
        payload["summary"] = summary
    if serve:
        payload["serve"] = serve
    if rally:
        payload["rally"] = rally
    if return_data:
        payload["return"] = return_data
    # pressure block always empty for now (stub view returns 0 rows)
    # will be populated once coach_pressure_points view is implemented

    return payload
