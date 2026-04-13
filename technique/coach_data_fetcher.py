# technique/coach_data_fetcher.py — Assemble technique analysis data for the AI Coach.
#
# Reads from gold technique views to build a compact dict that the coach
# prompt builder can pass to Claude. Pattern mirrors tennis_coach/data_fetcher.py.

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from db_init import engine

log = logging.getLogger(__name__)


def fetch_technique_data(task_id: str) -> dict:
    """
    Assemble technique analysis data for the AI Coach.
    Reads from gold.technique_report and gold.technique_kinetic_chain_summary.

    Raises ValueError if no analysis found.
    Returns a compact nested dict safe to pass to Claude.
    """
    with engine.connect() as conn:
        # 1. Technique report (summary + features)
        report = conn.execute(text("""
            SELECT task_id, email, sport, swing_type, dominant_hand,
                   player_height_mm, overall_score, level,
                   feature_count, category_count,
                   top_strength, top_improvement,
                   category_scores, top_strengths, top_improvements,
                   all_features, player_a_name, analysis_date, location
            FROM gold.technique_report
            WHERE task_id = :tid
            LIMIT 1
        """), {"tid": task_id}).mappings().first()

        if report is None:
            raise ValueError(f"No technique analysis found for task_id={task_id}")

        # 2. Kinetic chain summary
        kc = conn.execute(text("""
            SELECT segment_count, chain_is_sequential,
                   max_peak_speed, min_peak_speed,
                   chain_sequence, fastest_segment, slowest_segment,
                   chain_duration_s, segments_json
            FROM gold.technique_kinetic_chain_summary
            WHERE task_id = :tid
            LIMIT 1
        """), {"tid": task_id}).mappings().first()

        # 3. Feature comparison data (level benchmarks)
        comparisons = conn.execute(text("""
            SELECT feature_name, feature_human_readable, score, value, level,
                   category_name, observation, suggestion,
                   beginner_range, intermediate_range, advanced_range, professional_range
            FROM gold.technique_comparison
            WHERE task_id = :tid
            ORDER BY category_name, score DESC
        """), {"tid": task_id}).mappings().all()

    # Build output payload
    payload = {
        "analysis": {
            "player_name": report.get("player_a_name") or "",
            "sport": report.get("sport") or "tennis",
            "swing_type": report.get("swing_type") or "",
            "dominant_hand": report.get("dominant_hand") or "",
            "player_height_mm": report.get("player_height_mm"),
            "date": str(report.get("analysis_date") or ""),
            "location": report.get("location") or "",
        },
        "summary": {
            "overall_score": _val(report.get("overall_score"), float),
            "level": report.get("level"),
            "feature_count": _val(report.get("feature_count"), int),
            "category_count": _val(report.get("category_count"), int),
            "top_strength": report.get("top_strength"),
            "top_improvement": report.get("top_improvement"),
            "category_scores": report.get("category_scores"),
        },
        "top_strengths": report.get("top_strengths") or [],
        "top_improvements": report.get("top_improvements") or [],
        "features": [],
        "kinetic_chain": None,
    }

    # Features with benchmark comparisons
    for comp in comparisons:
        payload["features"].append({
            "name": comp.get("feature_human_readable") or comp.get("feature_name"),
            "score": _val(comp.get("score"), float),
            "value": _val(comp.get("value"), float),
            "level": comp.get("level"),
            "category": comp.get("category_name"),
            "observation": comp.get("observation"),
            "suggestion": comp.get("suggestion"),
            "benchmarks": {
                "beginner": comp.get("beginner_range"),
                "intermediate": comp.get("intermediate_range"),
                "advanced": comp.get("advanced_range"),
                "professional": comp.get("professional_range"),
            },
        })

    # Kinetic chain
    if kc:
        payload["kinetic_chain"] = {
            "segment_count": _val(kc.get("segment_count"), int),
            "is_sequential": kc.get("chain_is_sequential"),
            "chain_sequence": kc.get("chain_sequence"),
            "fastest_segment": kc.get("fastest_segment"),
            "slowest_segment": kc.get("slowest_segment"),
            "max_peak_speed": _val(kc.get("max_peak_speed"), float),
            "min_peak_speed": _val(kc.get("min_peak_speed"), float),
            "duration_s": _val(kc.get("chain_duration_s"), float),
            "segments": kc.get("segments_json"),
        }

    return payload


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
