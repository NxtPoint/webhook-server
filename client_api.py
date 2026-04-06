# client_api.py — Client-facing API for Locker Room dashboard
# Auth: X-Client-Key header (CLIENT_API_KEY env var, separate from OPS_KEY)

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db_init import engine

client_bp = Blueprint("client_api", __name__)

CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "").strip()

log = logging.getLogger(__name__)


# ----------------------------
# Auth
# ----------------------------

def _guard() -> bool:
    hk = request.headers.get("X-Client-Key") or ""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    return bool(CLIENT_API_KEY) and hk.strip() == CLIENT_API_KEY


def _forbid():
    return jsonify({"ok": False, "error": "forbidden"}), 403


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


# ----------------------------
# GET /api/client/matches
# ----------------------------

@client_bp.get("/api/client/matches")
def list_matches():
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    task_id, match_date, location,
                    player_a_name, player_b_name, sport_type,
                    video_url, share_url, email, last_status, created_at,
                    total_points, total_games, total_sets,
                    player_a_points_won, player_b_points_won,
                    player_a_games_won, player_b_games_won,
                    total_aces, total_double_faults,
                    avg_rally_length, max_rally_length,
                    player_a_first_serve_pct, player_b_first_serve_pct,
                    player_a_winners, player_b_winners,
                    player_a_set1_games, player_b_set1_games,
                    player_a_set2_games, player_b_set2_games,
                    player_a_set3_games, player_b_set3_games
                FROM gold.vw_client_match_summary
                WHERE email = :email
                ORDER BY match_date DESC NULLS LAST, created_at DESC
            """),
            {"email": email},
        ).mappings().all()

    matches = []
    for r in rows:
        matches.append({
            "task_id": r["task_id"],
            "match_date": str(r["match_date"]) if r["match_date"] else None,
            "location": r["location"],
            "player_a_name": r["player_a_name"],
            "player_b_name": r["player_b_name"],
            "sport_type": r["sport_type"],
            "video_url": r["video_url"],
            "share_url": r["share_url"],
            "last_status": r["last_status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "total_points": int(r["total_points"] or 0),
            "total_games": int(r["total_games"] or 0),
            "total_sets": int(r["total_sets"] or 0),
            "player_a_points_won": int(r["player_a_points_won"] or 0),
            "player_b_points_won": int(r["player_b_points_won"] or 0),
            "player_a_games_won": int(r["player_a_games_won"] or 0),
            "player_b_games_won": int(r["player_b_games_won"] or 0),
            "total_aces": int(r["total_aces"] or 0),
            "total_double_faults": int(r["total_double_faults"] or 0),
            "avg_rally_length": float(r["avg_rally_length"] or 0),
            "max_rally_length": int(r["max_rally_length"] or 0),
            "player_a_first_serve_pct": float(r["player_a_first_serve_pct"] or 0),
            "player_b_first_serve_pct": float(r["player_b_first_serve_pct"] or 0),
            "player_a_winners": int(r["player_a_winners"] or 0),
            "player_b_winners": int(r["player_b_winners"] or 0),
            "score": _format_score(r),
        })

    return jsonify({"ok": True, "matches": matches})


def _format_score(r) -> str:
    sets = []
    for i in (1, 2, 3):
        a = r.get(f"player_a_set{i}_games")
        b = r.get(f"player_b_set{i}_games")
        if a is not None and b is not None:
            sets.append(f"{a}-{b}")
    return "  ".join(sets) if sets else ""


# ----------------------------
# GET /api/client/matches/<task_id>
# ----------------------------

@client_bp.get("/api/client/matches/<task_id>")
def match_detail(task_id: str):
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        # Verify the match belongs to this email
        owner = conn.execute(
            text("SELECT email FROM bronze.submission_context WHERE task_id = :tid"),
            {"tid": task_id},
        ).scalar_one_or_none()

        if not owner or _norm_email(owner) != email:
            return jsonify({"ok": False, "error": "not_found"}), 404

        # Point-level detail from silver
        rows = conn.execute(
            text("""
                SELECT
                    point_number, player_id, serve_d, swing_type, volley,
                    ball_speed, shot_ix_in_point, shot_phase_d, shot_outcome_d,
                    point_winner_player_id, game_number, game_winner_player_id,
                    server_id, set_number, set_game_number, ace_d,
                    rally_length_point, stroke_d, aggression_d, depth_d,
                    serve_bucket_d, rally_location_hit, rally_location_bounce,
                    serve_try_ix_in_point, service_winner_d, exclude_d
                FROM silver.point_detail
                WHERE task_id = :tid::uuid
                  AND COALESCE(exclude_d, FALSE) = FALSE
                ORDER BY point_number, shot_ix_in_point
            """),
            {"tid": task_id},
        ).mappings().all()

    points = []
    for r in rows:
        points.append({k: _serialize(v) for k, v in r.items()})

    return jsonify({"ok": True, "task_id": task_id, "points": points})


def _serialize(v):
    if v is None:
        return None
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, bool):
        return v
    return v


# ----------------------------
# GET /api/client/usage
# ----------------------------

@client_bp.get("/api/client/usage")
def client_usage():
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    a.id AS account_id,
                    a.primary_full_name,
                    COALESCE(v.matches_granted, 0)   AS matches_granted,
                    COALESCE(v.matches_consumed, 0)   AS matches_consumed,
                    COALESCE(v.matches_remaining, 0)  AS matches_remaining
                FROM billing.account a
                LEFT JOIN billing.vw_customer_usage v ON v.account_id = a.id
                WHERE a.email = :email
            """),
            {"email": email},
        ).mappings().first()

    if not row:
        return jsonify({
            "ok": True,
            "usage": {
                "matches_granted": 0,
                "matches_consumed": 0,
                "matches_remaining": 0,
                "player_name": None,
            },
        })

    return jsonify({
        "ok": True,
        "usage": {
            "matches_granted": int(row["matches_granted"]),
            "matches_consumed": int(row["matches_consumed"]),
            "matches_remaining": int(row["matches_remaining"]),
            "player_name": row["primary_full_name"],
        },
    })


# ----------------------------
# PATCH /api/client/matches/<task_id>
# ----------------------------

EDITABLE_FIELDS = {"player_a_name", "player_b_name", "location", "match_date"}

@client_bp.patch("/api/client/matches/<task_id>")
def update_match(task_id: str):
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    payload = request.get_json(silent=True) or {}
    updates = {k: v for k, v in payload.items() if k in EDITABLE_FIELDS}
    if not updates:
        return jsonify({"ok": False, "error": "no editable fields provided"}), 400

    with Session(engine) as session:
        owner = session.execute(
            text("SELECT email FROM bronze.submission_context WHERE task_id = :tid"),
            {"tid": task_id},
        ).scalar_one_or_none()

        if not owner or _norm_email(owner) != email:
            return jsonify({"ok": False, "error": "not_found"}), 404

        # Build SET clause dynamically — only whitelisted fields
        set_parts = []
        params = {"tid": task_id}
        for k, v in updates.items():
            set_parts.append(f"{k} = :{k}")
            params[k] = v

        sql = f"UPDATE bronze.submission_context SET {', '.join(set_parts)} WHERE task_id = :tid"
        session.execute(text(sql), params)
        session.commit()

    return jsonify({"ok": True, "updated": list(updates.keys())})


# ----------------------------
# POST /api/client/matches/<task_id>/reprocess
# ----------------------------

@client_bp.post("/api/client/matches/<task_id>/reprocess")
def reprocess_match(task_id: str):
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT email FROM bronze.submission_context WHERE task_id = :tid"),
            {"tid": task_id},
        ).scalar_one_or_none()

        if not owner or _norm_email(owner) != email:
            return jsonify({"ok": False, "error": "not_found"}), 404

    try:
        from build_silver_v2 import build_silver_v2
        result = build_silver_v2(task_id=task_id, replace=True)
        return jsonify({"ok": True, "result": result or "rebuilt"})
    except Exception:
        log.exception("reprocess failed task_id=%s", task_id)
        return jsonify({"ok": False, "error": "reprocess_failed"}), 500
