# client_api.py — Client-facing REST API for all Locker Room and Players' Enclosure SPAs.
#
# Auth: X-Client-Key header checked against CLIENT_API_KEY env var (separate from OPS_KEY).
# Admin endpoints additionally require email in ADMIN_EMAILS whitelist.
#
# Endpoints:
#   GET    /api/client/matches              — list matches with stats, scores, trim status
#   GET    /api/client/matches/<task_id>    — point-level detail from silver.point_detail
#   GET    /api/client/match-analysis/<task_id> — full silver.point_detail with coordinates [LEGACY]
#
#   GOLD presentation endpoints (thin passthrough to gold.* views):
#   GET    /api/client/match/kpi/<task_id>              — gold.match_kpi (1 row, H2H card)
#   GET    /api/client/match/serve-breakdown/<task_id>  — gold.match_serve_breakdown (strategy table)
#   GET    /api/client/match/return-breakdown/<task_id> — gold.match_return_breakdown (return stats)
#   GET    /api/client/match/rally-breakdown/<task_id>  — gold.match_rally_breakdown (rally stats)
#   GET    /api/client/match/rally-length/<task_id>     — gold.match_rally_length (length distribution)
#   GET    /api/client/match/shot-placement/<task_id>   — gold.match_shot_placement (heatmap data)
#   PATCH  /api/client/matches/<task_id>    — update match metadata (whitelisted fields only)
#   POST   /api/client/matches/<task_id>/reprocess — rebuild silver via build_silver_v2
#   GET    /api/client/players              — distinct player names for autocomplete
#   GET    /api/client/usage                — account credit usage summary
#   GET    /api/client/profile              — primary member profile from billing.member
#   PATCH  /api/client/profile              — update profile fields (whitelisted set)
#   GET    /api/client/footage-url/<task_id>— presigned S3 URL for trimmed video
#   GET    /api/client/entitlements         — role, plan, credits, account_status for UI gating
#   POST   /api/client/register             — onboarding registration (creates account + member)
#   POST   /api/client/children             — add child member profiles during onboarding
#   GET    /api/client/profile-photo-upload-url — presigned S3 PUT URL for profile photo
#   GET    /api/client/members              — all active members on the account
#   POST   /api/client/members              — add a linked player/coach member
#   PATCH  /api/client/members/<id>         — update a linked member's profile
#   DELETE /api/client/members/<id>         — soft-delete (sets active=false, cannot delete primary)
#   GET    /api/client/coaches              — list coach permissions for the account
#   POST   /api/client/coach-invite         — invite a coach (creates permission + token + SES email)
#   POST   /api/client/coach-revoke         — revoke a coach permission
#   GET    /api/client/pbi-embed            — Power BI embed token (proxies to PBI service)
#   POST   /api/client/pbi-heartbeat        — keep PBI capacity session alive
#   POST   /api/client/pbi-session-end      — end PBI capacity session on page unload
#   GET    /api/client/backoffice/pipeline   — admin: pipeline status table
#   GET    /api/client/backoffice/customers  — admin: customer list with usage stats
#   GET    /api/client/backoffice/kpis       — admin: KPI cards
#
# Business rules:
#   - All endpoints require email parameter for tenant isolation
#   - Match ownership verified before returning data or allowing edits
#   - Entitlements check merges billing.subscription_state (if table exists) with usage view
#   - Coach invite reuses existing permission rows on re-invite (idempotent)
#   - Profile columns are added idempotently on module import via _ensure_member_profile_columns()
#   - Only EDITABLE_FIELDS / PROFILE_FIELDS / MEMBER_EDITABLE can be written (whitelist pattern)

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime
from typing import Optional

import requests as http_requests
from flask import Blueprint, jsonify, request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Member

client_bp = Blueprint("client_api", __name__)

CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY", "").strip()
PLANS_PAGE_URL = os.environ.get("PLANS_PAGE_URL", "https://www.ten-fifty5.com/plans").strip()

ADMIN_EMAILS = {"info@ten-fifty5.com", "tomo.stojakovic@gmail.com"}

COACH_ACCEPT_BASE_URL = os.environ.get("COACH_ACCEPT_BASE_URL", "https://api.nextpointtennis.com").strip()

log = logging.getLogger(__name__)

# Profile fields editable from Locker Room
PROFILE_FIELDS = {
    "full_name", "surname", "phone", "utr",
    "dominant_hand", "country", "area",
    "profile_photo_url",
}


# Handle OPTIONS preflight for all client API routes
@client_bp.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return "", 204


# ----------------------------
# Auth
# ----------------------------

def _guard() -> bool:
    hk = request.headers.get("X-Client-Key") or ""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        hk = auth.split(" ", 1)[1].strip()
    return bool(CLIENT_API_KEY) and hmac.compare_digest(hk.strip(), CLIENT_API_KEY)


def _forbid():
    return jsonify({"ok": False, "error": "forbidden"}), 403


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


# ----------------------------
# GET /api/client/matches
# ----------------------------

@client_bp.route("/api/client/matches", methods=["GET", "OPTIONS"])
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
                    g.task_id, g.match_date, g.location,
                    g.player_a_name, g.player_b_name, g.sport_type,
                    g.video_url, g.share_url, g.email, g.last_status, g.created_at,
                    g.total_points, g.total_games, g.total_sets,
                    g.player_a_points_won, g.player_b_points_won,
                    g.player_a_games_won, g.player_b_games_won,
                    g.total_aces, g.total_double_faults,
                    g.avg_rally_length, g.max_rally_length,
                    g.player_a_first_serve_pct, g.player_b_first_serve_pct,
                    g.player_a_winners, g.player_b_winners,
                    g.player_a_set1_games, g.player_b_set1_games,
                    g.player_a_set2_games, g.player_b_set2_games,
                    g.player_a_set3_games, g.player_b_set3_games,
                    sc.player_a_utr, sc.player_b_utr,
                    sc.first_server, sc.start_time,
                    sc.trim_status, sc.trim_output_s3_key,
                    sc.trim_duration_s
                FROM gold.vw_client_match_summary g
                JOIN bronze.submission_context sc ON sc.task_id = g.task_id
                WHERE g.email = :email
                ORDER BY g.match_date ASC NULLS LAST, g.created_at ASC
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
            "player_a_utr": r["player_a_utr"],
            "player_b_utr": r["player_b_utr"],
            "first_server": r["first_server"],
            "start_time": r["start_time"],
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
            "player_a_set1_games": r["player_a_set1_games"],
            "player_b_set1_games": r["player_b_set1_games"],
            "player_a_set2_games": r["player_a_set2_games"],
            "player_b_set2_games": r["player_b_set2_games"],
            "player_a_set3_games": r["player_a_set3_games"],
            "player_b_set3_games": r["player_b_set3_games"],
            "trim_status": r["trim_status"],
            "trim_output_s3_key": r["trim_output_s3_key"],
            "trim_duration_s": float(r["trim_duration_s"]) if r["trim_duration_s"] else None,
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
# GET /api/client/players
# ----------------------------

@client_bp.route("/api/client/players", methods=["GET", "OPTIONS"])
def list_players():
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT player_a_name AS name
                FROM bronze.submission_context
                WHERE email = :email AND player_a_name IS NOT NULL AND player_a_name != ''
                UNION
                SELECT DISTINCT player_b_name AS name
                FROM bronze.submission_context
                WHERE email = :email AND player_b_name IS NOT NULL AND player_b_name != ''
                ORDER BY name
            """),
            {"email": email},
        ).scalars().all()

    return jsonify({"ok": True, "players": list(rows)})


# ----------------------------
# GET /api/client/matches/<task_id>
# ----------------------------

@client_bp.route("/api/client/matches/<task_id>", methods=["GET", "OPTIONS"])
def match_detail(task_id: str):
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
    if isinstance(v, bool):
        return v
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_serialize(x) for x in v]
    # Decimal, UUID, etc.
    if hasattr(v, '__float__'):
        return float(v)
    return str(v)


# ----------------------------
# GET /api/client/usage
# ----------------------------

@client_bp.route("/api/client/usage", methods=["GET", "OPTIONS"])
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

EDITABLE_FIELDS = {
    "player_a_name", "player_a_utr", "player_b_name", "player_b_utr",
    "match_date", "location", "first_server", "start_time",
    "player_a_set1_games", "player_b_set1_games",
    "player_a_set2_games", "player_b_set2_games",
    "player_a_set3_games", "player_b_set3_games",
}

@client_bp.route("/api/client/matches/<task_id>", methods=["PATCH"], endpoint="update_match")
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

@client_bp.route("/api/client/matches/<task_id>/reprocess", methods=["POST", "OPTIONS"])
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


# ----------------------------
# GET /api/client/profile
# ----------------------------

@client_bp.route("/api/client/profile", methods=["GET", "OPTIONS"])
def get_profile():
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT
                    m.id, m.full_name, m.surname, m.phone, m.utr,
                    m.dominant_hand, m.country, m.area,
                    m.role, a.email,
                    a.id AS account_id,
                    a.created_at AS account_created_at,
                    a.active AS account_active,
                    m.created_at AS member_created_at
                FROM billing.account a
                JOIN billing.member m ON m.account_id = a.id AND m.is_primary = true
                WHERE a.email = :email
            """),
            {"email": email},
        ).mappings().first()

    if not row:
        return jsonify({"ok": True, "profile": None})

    return jsonify({
        "ok": True,
        "profile": {
            "member_id": int(row["id"]),
            "account_id": int(row["account_id"]),
            "full_name": row["full_name"],
            "surname": row["surname"],
            "email": row["email"],
            "phone": row["phone"],
            "utr": row["utr"],
            "dominant_hand": row["dominant_hand"],
            "country": row["country"],
            "area": row["area"],
            "role": row["role"],
            "account_created_at": row["account_created_at"].isoformat() if row["account_created_at"] else None,
            "account_active": row["account_active"],
            "member_created_at": row["member_created_at"].isoformat() if row["member_created_at"] else None,
        },
    })


# ----------------------------
# PATCH /api/client/profile
# ----------------------------

@client_bp.route("/api/client/profile", methods=["PATCH"], endpoint="update_profile")
def update_profile():
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    payload = request.get_json(silent=True) or {}
    updates = {k: v for k, v in payload.items() if k in PROFILE_FIELDS}
    if not updates:
        return jsonify({"ok": False, "error": "no editable fields provided"}), 400

    # Convert empty strings to None for nullable DB columns
    for k in updates:
        if k != "full_name" and updates[k] == "":
            updates[k] = None

    with Session(engine) as session:
        row = session.execute(
            text("""
                SELECT m.id
                FROM billing.account a
                JOIN billing.member m ON m.account_id = a.id AND m.is_primary = true
                WHERE a.email = :email
            """),
            {"email": email},
        ).mappings().first()

        if not row:
            return jsonify({"ok": False, "error": "profile_not_found"}), 404

        member_id = row["id"]
        set_parts = []
        params = {"mid": member_id}
        for k, v in updates.items():
            set_parts.append(f"{k} = :{k}")
            params[k] = v

        sql = f"UPDATE billing.member SET {', '.join(set_parts)} WHERE id = :mid"
        session.execute(text(sql), params)
        session.commit()

    return jsonify({"ok": True, "updated": list(updates.keys())})


# ----------------------------
# GET /api/client/footage-url/<task_id>
# ----------------------------

@client_bp.route("/api/client/footage-url/<task_id>", methods=["GET", "OPTIONS"])
def footage_url(task_id: str):
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT email, trim_status, trim_output_s3_key
                FROM bronze.submission_context
                WHERE task_id = :tid
            """),
            {"tid": task_id},
        ).mappings().first()

    if not row or _norm_email(row["email"]) != email:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if row["trim_status"] != "completed" or not row["trim_output_s3_key"]:
        return jsonify({"ok": False, "error": "footage_not_ready"}), 404

    try:
        from upload_app import _s3_presigned_get_url
        url = _s3_presigned_get_url(row["trim_output_s3_key"], expires=3600)
        return jsonify({"ok": True, "url": url})
    except Exception:
        log.exception("presigned url failed task_id=%s", task_id)
        return jsonify({"ok": False, "error": "url_generation_failed"}), 500


# ----------------------------
# GET /api/client/entitlements
# ----------------------------

@client_bp.route("/api/client/entitlements", methods=["GET", "OPTIONS"])
def client_entitlements():
    """Authoritative entitlement check for all client-facing pages."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        # Check if subscription_state table exists (created lazily by subscriptions_api)
        has_sub_table = conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'billing' AND table_name = 'subscription_state'
                )
            """)
        ).scalar()

        if has_sub_table:
            row = conn.execute(
                text("""
                    SELECT
                        a.id AS account_id,
                        a.active AS account_active,
                        COALESCE(m.role, 'player_parent') AS role,
                        s.status AS subscription_status,
                        s.plan_code,
                        s.plan_type,
                        s.current_period_end,
                        COALESCE(v.matches_remaining, 0) AS credits_remaining,
                        COALESCE(v.matches_granted, 0)   AS matches_granted,
                        COALESCE(v.matches_consumed, 0)   AS matches_consumed
                    FROM billing.account a
                    LEFT JOIN billing.member m
                        ON m.account_id = a.id AND m.is_primary = true
                    LEFT JOIN billing.subscription_state s
                        ON s.account_id = a.id
                    LEFT JOIN billing.vw_customer_usage v
                        ON v.account_id = a.id
                    WHERE a.email = :email
                """),
                {"email": email},
            ).mappings().first()
        else:
            row = conn.execute(
                text("""
                    SELECT
                        a.id AS account_id,
                        a.active AS account_active,
                        COALESCE(m.role, 'player_parent') AS role,
                        NULL AS subscription_status,
                        NULL AS plan_code,
                        NULL AS plan_type,
                        NULL AS current_period_end,
                        COALESCE(v.matches_remaining, 0) AS credits_remaining,
                        COALESCE(v.matches_granted, 0)   AS matches_granted,
                        COALESCE(v.matches_consumed, 0)   AS matches_consumed
                    FROM billing.account a
                    LEFT JOIN billing.member m
                        ON m.account_id = a.id AND m.is_primary = true
                    LEFT JOIN billing.vw_customer_usage v
                        ON v.account_id = a.id
                    WHERE a.email = :email
                """),
                {"email": email},
            ).mappings().first()

    if not row:
        return jsonify({"ok": True, "entitlements": None})

    account_active = bool(row["account_active"])
    sub_status = (row["subscription_status"] or "").upper()
    plan_active = sub_status == "ACTIVE"

    if not account_active:
        account_status = "terminated"
    else:
        account_status = "active"

    period_end = row["current_period_end"]
    period_end_iso = period_end.isoformat() if period_end else None

    return jsonify({
        "ok": True,
        "entitlements": {
            "role": row["role"],
            "plan_active": plan_active,
            "credits_remaining": int(row["credits_remaining"]),
            "matches_granted": int(row["matches_granted"]),
            "matches_consumed": int(row["matches_consumed"]),
            "account_status": account_status,
            "subscription_status": sub_status or None,
            "plan_code": row["plan_code"],
            "plan_type": row["plan_type"],
            "current_period_end": period_end_iso,
            "plans_page_url": PLANS_PAGE_URL,
        },
    })


# ----------------------------
# POST /api/client/register
# ----------------------------

@client_bp.route("/api/client/register", methods=["POST", "OPTIONS"])
def register_member():
    """Onboarding registration from Players' Enclosure."""
    if not _guard():
        return _forbid()

    payload = request.get_json(silent=True) or {}
    email = _norm_email(payload.get("email"))
    first_name = (payload.get("first_name") or "").strip()
    surname = (payload.get("surname") or "").strip()
    wix_member_id = (payload.get("wix_member_id") or "").strip() or None
    role = (payload.get("role") or "player_parent").strip().lower()

    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    if not first_name:
        return jsonify({"ok": False, "error": "first_name required"}), 400

    if role not in ("player_parent", "coach"):
        role = "player_parent"

    from billing_service import create_account_with_primary_member

    acct = create_account_with_primary_member(
        email=email,
        primary_full_name=first_name,
        external_wix_id=wix_member_id,
        role=role,
    )

    with Session(engine) as session:
        member_id = session.execute(
            text("""
                SELECT id FROM billing.member
                WHERE account_id = :aid AND is_primary = true
                LIMIT 1
            """),
            {"aid": acct.id},
        ).scalar_one_or_none()

        if member_id:
            session.execute(
                text("""
                    UPDATE billing.member
                    SET surname = :surname, role = :role, full_name = :full_name
                    WHERE id = :mid
                """),
                {"surname": surname or None, "role": role, "full_name": first_name, "mid": member_id},
            )
            session.commit()

    return jsonify({"ok": True, "account_id": int(acct.id)})


# ----------------------------
# POST /api/client/children
# ----------------------------

@client_bp.route("/api/client/children", methods=["POST", "OPTIONS"])
def add_children():
    """Add child profiles from Players' Enclosure onboarding."""
    if not _guard():
        return _forbid()

    payload = request.get_json(silent=True) or {}
    email = _norm_email(payload.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    children = payload.get("children", [])
    if not isinstance(children, list) or not children:
        return jsonify({"ok": False, "error": "children list required"}), 400

    with Session(engine) as session:
        acct_id = session.execute(
            text("SELECT id FROM billing.account WHERE email = :email"),
            {"email": email},
        ).scalar_one_or_none()

        if not acct_id:
            return jsonify({"ok": False, "error": "account_not_found"}), 404

        created = []
        for c in children:
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            if not name:
                continue

            row = session.execute(
                text("""
                    INSERT INTO billing.member
                        (account_id, full_name, is_primary, role, active,
                         dominant_hand, dob, skill_level, club_school, notes)
                    VALUES
                        (:aid, :name, false, 'player_parent', true,
                         :hand, :dob::date, :skill, :club, :notes)
                    RETURNING id
                """),
                {
                    "aid": acct_id,
                    "name": name,
                    "hand": (c.get("dominant_hand") or "").strip() or None,
                    "dob": (c.get("dob") or "").strip() or None,
                    "skill": (c.get("skill_level") or "").strip() or None,
                    "club": (c.get("club_school") or "").strip() or None,
                    "notes": (c.get("notes") or "").strip() or None,
                },
            )
            child_id = row.scalar_one()
            created.append({"id": int(child_id), "name": name})

        session.commit()

    return jsonify({"ok": True, "children": created})


# ----------------------------
# GET /api/client/profile-photo-upload-url
# ----------------------------

@client_bp.route("/api/client/profile-photo-upload-url", methods=["GET", "OPTIONS"])
def profile_photo_upload_url():
    """Returns a presigned S3 PUT URL for profile photo upload."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    import uuid as _uuid
    bucket = os.environ.get("S3_BUCKET", "")
    region = os.environ.get("AWS_REGION", "us-east-1")
    if not bucket:
        return jsonify({"ok": False, "error": "s3_not_configured"}), 500

    photo_key = f"profile-photos/{email}/{_uuid.uuid4().hex}.jpg"

    try:
        import boto3
        s3 = boto3.client("s3", region_name=region)
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": photo_key,
                "ContentType": "image/jpeg",
            },
            ExpiresIn=300,
        )
        return jsonify({"ok": True, "upload_url": url, "photo_key": photo_key})
    except Exception:
        log.exception("presigned photo upload url failed")
        return jsonify({"ok": False, "error": "url_generation_failed"}), 500


# ----------------------------
# DDL: ensure profile columns exist on billing.member
# ----------------------------

def _ensure_member_profile_columns():
    """Idempotent ALTER TABLE to add profile columns. Called on import."""
    try:
        with engine.begin() as conn:
            for ddl in (
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS surname TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS phone TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS utr TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS dominant_hand TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS country TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS area TEXT",
                # Child profile fields (Players' Enclosure)
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS dob DATE",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS skill_level TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS club_school TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS notes TEXT",
                "ALTER TABLE billing.member ADD COLUMN IF NOT EXISTS profile_photo_url TEXT",
            ):
                conn.execute(text(ddl))
    except Exception:
        log.warning("member profile columns DDL skipped (DB not available)")


_ensure_member_profile_columns()


# ----------------------------
# GET /api/client/members
# ----------------------------

MEMBER_FIELDS = [
    "id", "full_name", "surname", "is_primary", "role", "email",
    "phone", "utr", "dominant_hand", "country", "area",
    "dob", "skill_level", "club_school", "notes", "profile_photo_url",
]

MEMBER_EDITABLE = {
    "full_name", "surname", "phone", "utr", "dominant_hand",
    "country", "area", "dob", "skill_level", "club_school", "notes",
    "profile_photo_url", "email",
}


def _member_row_to_dict(r) -> dict:
    d = {}
    for f in MEMBER_FIELDS:
        v = r.get(f)
        if f == "id":
            d[f] = int(v)
        elif f == "is_primary":
            d[f] = bool(v)
        elif f == "dob":
            d[f] = str(v) if v else None
        else:
            d[f] = v
    return d


@client_bp.route("/api/client/members", methods=["GET", "OPTIONS"])
def list_account_members():
    """Return all active members on the account (primary + children/coaches)."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT m.id, m.full_name, m.surname, m.is_primary, m.role, m.email,
                       m.phone, m.utr, m.dominant_hand, m.country, m.area,
                       m.dob, m.skill_level, m.club_school, m.notes, m.profile_photo_url
                FROM billing.account a
                JOIN billing.member m ON m.account_id = a.id AND m.active = true
                WHERE a.email = :email
                ORDER BY m.is_primary DESC, m.full_name ASC
            """),
            {"email": email},
        ).mappings().all()

    return jsonify({"ok": True, "members": [_member_row_to_dict(r) for r in rows]})


# ----------------------------
# POST /api/client/members  (add a linked player)
# ----------------------------

@client_bp.route("/api/client/members", methods=["POST"], endpoint="add_member")
def add_member():
    """Add a child or coach member to the account."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    payload = request.get_json(silent=True) or {}
    full_name = (payload.get("full_name") or "").strip()
    if not full_name:
        return jsonify({"ok": False, "error": "full_name required"}), 400

    with Session(engine) as session:
        acct_id = session.execute(
            text("SELECT id FROM billing.account WHERE email = :email"),
            {"email": email},
        ).scalar_one_or_none()
        if not acct_id:
            return jsonify({"ok": False, "error": "account_not_found"}), 404

        params = {"aid": acct_id, "name": full_name}
        cols = ["account_id", "full_name", "is_primary", "active"]
        vals = [":aid", ":name", "false", "true"]

        for f in MEMBER_EDITABLE - {"full_name"}:
            v = payload.get(f)
            if v is not None:
                v_str = str(v).strip()
                if v_str:
                    cols.append(f)
                    vals.append(f":{f}")
                    params[f] = v_str if f != "dob" else v_str

        # Force role to valid value
        role = (payload.get("role") or "player_parent").strip().lower()
        if role not in ("player_parent", "coach"):
            role = "player_parent"
        cols.append("role")
        vals.append(":role")
        params["role"] = role

        row = session.execute(
            text(f"INSERT INTO billing.member ({', '.join(cols)}) VALUES ({', '.join(vals)}) RETURNING id"),
            params,
        )
        member_id = int(row.scalar_one())
        session.commit()

    return jsonify({"ok": True, "member_id": member_id})


# ----------------------------
# PATCH /api/client/members/<member_id>
# ----------------------------

@client_bp.route("/api/client/members/<int:member_id>", methods=["PATCH", "OPTIONS"], endpoint="update_member")
def update_member(member_id: int):
    """Update a linked member's profile fields."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    payload = request.get_json(silent=True) or {}
    updates = {k: v for k, v in payload.items() if k in MEMBER_EDITABLE}
    if not updates:
        return jsonify({"ok": False, "error": "no editable fields provided"}), 400

    # Convert empty strings to None for nullable DB columns
    NULLABLE_COLS = {"dob", "phone", "utr", "dominant_hand", "country", "area",
                     "skill_level", "club_school", "notes", "profile_photo_url", "email"}
    for k in updates:
        if k in NULLABLE_COLS and updates[k] == "":
            updates[k] = None

    with Session(engine) as session:
        row = session.execute(
            text("""
                SELECT m.id, m.account_id
                FROM billing.member m
                JOIN billing.account a ON a.id = m.account_id
                WHERE m.id = :mid AND a.email = :email AND m.active = true
            """),
            {"mid": member_id, "email": email},
        ).mappings().first()

        if not row:
            return jsonify({"ok": False, "error": "member_not_found"}), 404

        set_parts = []
        params = {"mid": member_id}
        for k, v in updates.items():
            set_parts.append(f"{k} = :{k}")
            params[k] = v

        try:
            session.execute(
                text(f"UPDATE billing.member SET {', '.join(set_parts)} WHERE id = :mid"),
                params,
            )
            session.commit()
        except Exception as e:
            session.rollback()
            log.exception("Member update failed mid=%s", member_id)
            return jsonify({"ok": False, "error": "update_failed"}), 500

    return jsonify({"ok": True, "updated": list(updates.keys())})


# ----------------------------
# DELETE /api/client/members/<member_id>  (soft-delete)
# ----------------------------

@client_bp.route("/api/client/members/<int:member_id>", methods=["DELETE", "OPTIONS"], endpoint="delete_member")
def delete_member(member_id: int):
    """Soft-delete a linked member (set active=false). Cannot delete primary."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with Session(engine) as session:
        row = session.execute(
            text("""
                SELECT m.id, m.is_primary
                FROM billing.member m
                JOIN billing.account a ON a.id = m.account_id
                WHERE m.id = :mid AND a.email = :email AND m.active = true
            """),
            {"mid": member_id, "email": email},
        ).mappings().first()

        if not row:
            return jsonify({"ok": False, "error": "member_not_found"}), 404

        if row["is_primary"]:
            return jsonify({"ok": False, "error": "cannot_delete_primary_member"}), 400

        session.execute(
            text("UPDATE billing.member SET active = false WHERE id = :mid"),
            {"mid": member_id},
        )
        session.commit()

    return jsonify({"ok": True})


# ============================================================
# BACKOFFICE — admin-only endpoints
# ============================================================

def _admin_guard() -> bool:
    """Guard + admin email whitelist."""
    if not _guard():
        return False
    email = _norm_email(request.args.get("email"))
    return email in ADMIN_EMAILS


# ----------------------------
# GET /api/client/practice-sessions
# ----------------------------

@client_bp.route("/api/client/practice-sessions", methods=["GET", "OPTIONS"])
def practice_sessions():
    """List all practice sessions with aggregate stats."""
    if not _guard():
        return _forbid()
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT sc.task_id, sc.sport_type, sc.match_date, sc.location,
                       sc.player_a_name, sc.last_status, sc.created_at,
                       sc.trim_status, sc.trim_output_s3_key,
                       a.bounce_count, a.bounces_in, a.bounces_out,
                       a.max_speed_kmh, a.avg_speed_kmh,
                       a.rally_count, a.avg_rally_length,
                       a.serve_count, a.first_serve_pct,
                       j.job_id, j.ball_heatmap_s3_key, j.player_heatmap_s3_keys,
                       j.processing_time_sec
                FROM bronze.submission_context sc
                LEFT JOIN ml_analysis.video_analysis_jobs j ON j.task_id = sc.task_id
                LEFT JOIN ml_analysis.match_analytics a ON a.job_id = j.job_id
                WHERE sc.email = :email
                  AND sc.sport_type IN ('serve_practice', 'rally_practice')
                ORDER BY sc.match_date DESC NULLS LAST, sc.created_at DESC
            """),
            {"email": email},
        ).mappings().all()

    sessions = []
    for r in rows:
        sessions.append({
            "task_id": r["task_id"],
            "sport_type": r["sport_type"],
            "match_date": str(r["match_date"]) if r["match_date"] else None,
            "location": r["location"],
            "player_name": r["player_a_name"],
            "last_status": r["last_status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "trim_status": r["trim_status"],
            "trim_output_s3_key": r["trim_output_s3_key"],
            "bounce_count": int(r["bounce_count"] or 0),
            "bounces_in": int(r["bounces_in"] or 0),
            "bounces_out": int(r["bounces_out"] or 0),
            "max_speed_kmh": float(r["max_speed_kmh"] or 0),
            "avg_speed_kmh": float(r["avg_speed_kmh"] or 0),
            "rally_count": int(r["rally_count"] or 0),
            "avg_rally_length": float(r["avg_rally_length"] or 0),
            "serve_count": int(r["serve_count"] or 0),
            "first_serve_pct": float(r["first_serve_pct"] or 0),
            "job_id": r["job_id"],
            "has_heatmaps": bool(r["ball_heatmap_s3_key"]),
            "processing_time_sec": float(r["processing_time_sec"] or 0),
        })

    return jsonify({"ok": True, "sessions": sessions})


# ----------------------------
# GET /api/client/practice-detail/<task_id>
# ----------------------------

@client_bp.route("/api/client/practice-detail/<task_id>", methods=["GET", "OPTIONS"])
def practice_detail(task_id):
    """Return practice detail rows + computed summary for a session."""
    if not _guard():
        return _forbid()
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    # Ownership check
    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT email, sport_type FROM bronze.submission_context WHERE task_id = :tid"),
            {"tid": task_id},
        ).mappings().first()
    if not owner or _norm_email(owner["email"]) != email:
        return jsonify({"ok": False, "error": "not_found"}), 404

    practice_type = owner["sport_type"]

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT sequence_num, shot_ix, ball_x, ball_y, ball_speed_kmh,
                       is_in, serve_zone, serve_side, serve_result,
                       serve_location, serve_bucket_d,
                       placement_zone, depth_d, stroke_d, aggression_d, timestamp_s,
                       rally_length, rally_duration_s, rally_length_bucket_d,
                       player_court_x, player_court_y
                FROM silver.practice_detail
                WHERE task_id = :tid
                ORDER BY sequence_num, shot_ix
            """),
            {"tid": task_id},
        ).mappings().all()

    details = []
    speeds = []
    zone_counts = {}
    depth_counts = {}
    stroke_counts = {}
    aggression_counts = {}
    serve_zone_counts = {}
    side_counts = {}
    results_in = 0
    results_fault = 0
    rally_lengths = []
    rally_durations = []

    for r in rows:
        d = {
            "sequence_num": r["sequence_num"],
            "shot_ix": r["shot_ix"],
            "ball_x": r["ball_x"],
            "ball_y": r["ball_y"],
            "ball_speed_kmh": r["ball_speed_kmh"],
            "is_in": r["is_in"],
            "serve_zone": r["serve_zone"],
            "serve_side": r["serve_side"],
            "serve_result": r["serve_result"],
            "serve_location": r["serve_location"],
            "serve_bucket_d": r["serve_bucket_d"],
            "placement_zone": r["placement_zone"],
            "depth_d": r["depth_d"],
            "stroke_d": r["stroke_d"],
            "aggression_d": r["aggression_d"],
            "timestamp_s": r["timestamp_s"],
            "rally_length": r["rally_length"],
            "rally_duration_s": r["rally_duration_s"],
            "rally_length_bucket_d": r["rally_length_bucket_d"],
        }
        details.append(d)

        if r["ball_speed_kmh"]:
            speeds.append(float(r["ball_speed_kmh"]))
        if r["placement_zone"]:
            zone_counts[r["placement_zone"]] = zone_counts.get(r["placement_zone"], 0) + 1
        if r["depth_d"]:
            depth_counts[r["depth_d"]] = depth_counts.get(r["depth_d"], 0) + 1
        if r["stroke_d"]:
            stroke_counts[r["stroke_d"]] = stroke_counts.get(r["stroke_d"], 0) + 1
        if r["aggression_d"]:
            aggression_counts[r["aggression_d"]] = aggression_counts.get(r["aggression_d"], 0) + 1
        if r["serve_zone"]:
            serve_zone_counts[r["serve_zone"]] = serve_zone_counts.get(r["serve_zone"], 0) + 1
        if r["serve_side"]:
            side_counts[r["serve_side"]] = side_counts.get(r["serve_side"], 0) + 1
        if r["serve_result"] == "in":
            results_in += 1
        elif r["serve_result"] == "fault":
            results_fault += 1
        if r["rally_length"] and r["shot_ix"] == 1:
            rally_lengths.append(int(r["rally_length"]))
        if r["rally_duration_s"] and r["shot_ix"] == 1:
            rally_durations.append(float(r["rally_duration_s"]))

    total_serves = results_in + results_fault
    summary = {
        "practice_type": practice_type,
        "total_shots": len(details),
        "total_serves": total_serves,
        "serves_in": results_in,
        "serves_fault": results_fault,
        "first_serve_pct": round(100.0 * results_in / total_serves, 1) if total_serves else 0,
        "max_speed_kmh": round(max(speeds), 1) if speeds else 0,
        "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else 0,
        "zone_counts": zone_counts,
        "depth_counts": depth_counts,
        "stroke_counts": stroke_counts,
        "aggression_counts": aggression_counts,
        "serve_zone_counts": serve_zone_counts,
        "side_counts": side_counts,
        "total_rallies": len(rally_lengths),
        "avg_rally_length": round(sum(rally_lengths) / len(rally_lengths), 1) if rally_lengths else 0,
        "max_rally_length": max(rally_lengths) if rally_lengths else 0,
        "avg_duration_s": round(sum(rally_durations) / len(rally_durations), 1) if rally_durations else 0,
        "max_duration_s": round(max(rally_durations), 1) if rally_durations else 0,
    }

    return jsonify({"ok": True, "task_id": task_id, "summary": summary, "details": details})


# ----------------------------
# GET /api/client/practice-heatmap/<task_id>/<heatmap_type>
# ----------------------------

@client_bp.route("/api/client/practice-heatmap/<task_id>/<heatmap_type>", methods=["GET", "OPTIONS"])
def practice_heatmap(task_id, heatmap_type):
    """Return a presigned S3 URL for a practice heatmap image."""
    if not _guard():
        return _forbid()
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    # Ownership check
    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT email FROM bronze.submission_context WHERE task_id = :tid"),
            {"tid": task_id},
        ).mappings().first()
    if not owner or _norm_email(owner["email"]) != email:
        return jsonify({"ok": False, "error": "not_found"}), 404

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT ball_heatmap_s3_key, player_heatmap_s3_keys
                FROM ml_analysis.video_analysis_jobs
                WHERE task_id = :tid
                ORDER BY created_at DESC LIMIT 1
            """),
            {"tid": task_id},
        ).mappings().first()

    if not row:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if heatmap_type == "ball":
        s3_key = row["ball_heatmap_s3_key"]
    else:
        keys = row["player_heatmap_s3_keys"] or {}
        s3_key = keys.get(f"player_heatmap_{heatmap_type}.png") or keys.get(heatmap_type)

    if not s3_key:
        return jsonify({"ok": False, "error": "heatmap_not_available"}), 404

    try:
        from upload_app import _s3_presigned_get_url
        url = _s3_presigned_get_url(s3_key, expires=3600)
        return jsonify({"ok": True, "url": url})
    except Exception:
        log.exception("heatmap presigned url failed task_id=%s type=%s", task_id, heatmap_type)
        return jsonify({"ok": False, "error": "url_generation_failed"}), 500


# ============================================================================
# GOLD PRESENTATION ENDPOINTS
# Thin passthrough to gold.* views. No aggregation in Python — the view does it.
# Used by match_analysis.html and the LLM coach (both read the same numbers).
# ============================================================================

def _owns_task(conn, task_id, email):
    """Return True if the given email owns the given task_id."""
    owner = conn.execute(
        text("SELECT email FROM bronze.submission_context WHERE task_id = :tid"),
        {"tid": task_id},
    ).scalar_one_or_none()
    return owner is not None and _norm_email(owner) == email


def _gold_one(view_name, task_id):
    """Fetch all rows from a gold view filtered by task_id. Serializes Decimal/UUID."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT * FROM gold.{view_name} WHERE task_id = CAST(:tid AS uuid)"),
            {"tid": task_id},
        ).mappings().all()
    return [{k: _serialize(v) for k, v in r.items()} for r in rows]


def _gold_guard_and_fetch(view_name, task_id):
    """Shared guard + fetch pattern. Returns (status_code, payload_dict)."""
    if not _guard():
        return 403, {"ok": False, "error": "forbidden"}
    email = _norm_email(request.args.get("email"))
    if not email:
        return 400, {"ok": False, "error": "email required"}
    try:
        with engine.connect() as conn:
            if not _owns_task(conn, task_id, email):
                return 404, {"ok": False, "error": "not_found"}
        rows = _gold_one(view_name, task_id)
        return 200, {"ok": True, "task_id": task_id, "rows": rows}
    except Exception:
        log.exception("gold endpoint failed view=%s task_id=%s", view_name, task_id)
        return 500, {"ok": False, "error": "internal_error"}


@client_bp.route("/api/client/match/kpi/<task_id>", methods=["GET", "OPTIONS"])
def gold_match_kpi(task_id):
    """Single-row match KPIs for both players (Summary tab head-to-head)."""
    code, payload = _gold_guard_and_fetch("match_kpi", task_id)
    return jsonify(payload), code


@client_bp.route("/api/client/match/serve-breakdown/<task_id>", methods=["GET", "OPTIONS"])
def gold_match_serve_breakdown(task_id):
    """Serve direction × side × win rate (Serve Detail strategy table)."""
    code, payload = _gold_guard_and_fetch("match_serve_breakdown", task_id)
    return jsonify(payload), code


@client_bp.route("/api/client/match/return-breakdown/<task_id>", methods=["GET", "OPTIONS"])
def gold_match_return_breakdown(task_id):
    """Return stats per player with vs-1st/vs-2nd split (Return Detail tab)."""
    code, payload = _gold_guard_and_fetch("match_return_breakdown", task_id)
    return jsonify(payload), code


@client_bp.route("/api/client/match/rally-breakdown/<task_id>", methods=["GET", "OPTIONS"])
def gold_match_rally_breakdown(task_id):
    """Per-player rally stats: aggression/depth/stroke + speeds (Rally Detail tab)."""
    code, payload = _gold_guard_and_fetch("match_rally_breakdown", task_id)
    return jsonify(payload), code


@client_bp.route("/api/client/match/rally-length/<task_id>", methods=["GET", "OPTIONS"])
def gold_match_rally_length(task_id):
    """Rally length distribution with per-player wins (Rally Detail length chart)."""
    code, payload = _gold_guard_and_fetch("match_rally_length", task_id)
    return jsonify(payload), code


@client_bp.route("/api/client/match/shot-placement/<task_id>", methods=["GET", "OPTIONS"])
def gold_match_shot_placement(task_id):
    """Thin shot-level data for heatmaps (Placement Heatmaps module)."""
    code, payload = _gold_guard_and_fetch("match_shot_placement", task_id)
    return jsonify(payload), code


@client_bp.route("/api/client/player/performance", methods=["GET", "OPTIONS"])
def gold_player_performance():
    """Cross-match KPI scorecard for Player A (the customer). Email-scoped."""
    if not _guard():
        return _forbid()
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM gold.player_performance WHERE email = :email ORDER BY category, kpi_name"),
                {"email": email},
            ).mappings().all()
        serialized = [{k: _serialize(v) for k, v in r.items()} for r in rows]
        return jsonify({"ok": True, "rows": serialized})
    except Exception as e:
        err_msg = str(e)
        if "does not exist" in err_msg:
            log.warning("player_performance view not yet created email=%s", email)
            return jsonify({"ok": True, "rows": []})
        log.exception("player_performance endpoint failed email=%s", email)
        return jsonify({"ok": False, "error": "internal_error"}), 500


# ----------------------------
# GET /api/client/match-analysis/<task_id>  [LEGACY — to be retired]
# ----------------------------

@client_bp.route("/api/client/match-analysis/<task_id>", methods=["GET", "OPTIONS"])
def match_analysis(task_id):
    """Return full silver.point_detail with coordinates + match metadata for analysis dashboards."""
    if not _guard():
        return _forbid()
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    try:
        with engine.connect() as conn:
            # Ownership + metadata from submission_context
            meta = conn.execute(
                text("""
                    SELECT email, player_a_name, player_b_name,
                           player_a_utr, player_b_utr,
                           match_date, location, first_server, sport_type,
                           trim_status, trim_output_s3_key,
                           player_a_set1_games, player_b_set1_games,
                           player_a_set2_games, player_b_set2_games,
                           player_a_set3_games, player_b_set3_games
                    FROM bronze.submission_context WHERE task_id = :tid
                """),
                {"tid": task_id},
            ).mappings().first()

            if not meta or _norm_email(meta["email"]) != email:
                return jsonify({"ok": False, "error": "not_found"}), 404

            # Use the same proven column list as the existing match_detail endpoint,
            # plus coordinate columns that are known to exist from build_silver_v2
            rows = conn.execute(
                text("""
                    SELECT
                        point_number, player_id, serve_d, swing_type, volley,
                        ball_speed, shot_ix_in_point, shot_phase_d, shot_outcome_d,
                        point_winner_player_id, game_number, game_winner_player_id,
                        server_id, set_number, set_game_number, ace_d,
                        rally_length_point, stroke_d, aggression_d, depth_d,
                        serve_bucket_d, rally_location_hit, rally_location_bounce,
                        serve_try_ix_in_point, service_winner_d, exclude_d,
                        serve_side_d, serve_location, rally_length, rally_length_bucket_d,
                        ball_hit_location_x, ball_hit_location_y,
                        ball_hit_x_norm, ball_hit_y_norm,
                        ball_bounce_x_norm, ball_bounce_y_norm,
                        court_x, court_y, point_key
                    FROM silver.point_detail
                    WHERE task_id = CAST(:tid AS uuid)
                      AND COALESCE(exclude_d, FALSE) = FALSE
                    ORDER BY point_number, shot_ix_in_point
                """),
                {"tid": task_id},
            ).mappings().all()

        points = []
        for r in rows:
            points.append({k: _serialize(v) for k, v in r.items()})

        match_meta = {
            "task_id": task_id,
            "player_a_name": meta["player_a_name"],
            "player_b_name": meta["player_b_name"],
            "player_a_utr": _serialize(meta["player_a_utr"]),
            "player_b_utr": _serialize(meta["player_b_utr"]),
            "match_date": str(meta["match_date"]) if meta["match_date"] else None,
            "location": meta["location"],
            "first_server": meta["first_server"],
            "sport_type": meta["sport_type"],
            "trim_status": meta["trim_status"],
            "trim_output_s3_key": meta["trim_output_s3_key"],
            "player_a_set1_games": _serialize(meta["player_a_set1_games"]),
            "player_b_set1_games": _serialize(meta["player_b_set1_games"]),
            "player_a_set2_games": _serialize(meta["player_a_set2_games"]),
            "player_b_set2_games": _serialize(meta["player_b_set2_games"]),
            "player_a_set3_games": _serialize(meta["player_a_set3_games"]),
            "player_b_set3_games": _serialize(meta["player_b_set3_games"]),
        }

        return jsonify({"ok": True, "match": match_meta, "points": points})

    except Exception:
        log.exception("match_analysis failed task_id=%s", task_id)
        return jsonify({"ok": False, "error": "internal_error"}), 500


# ----------------------------
# GET /api/client/backoffice/pipeline
# ----------------------------

@client_bp.route("/api/client/backoffice/pipeline", methods=["GET", "OPTIONS"])
def backoffice_pipeline():
    if not _admin_guard():
        return _forbid()

    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT
                    sc.task_id,
                    sc.email,
                    sc.customer_name,
                    sc.created_at,
                    sc.match_date,
                    sc.location,
                    sc.player_a_name,
                    sc.player_b_name,
                    sc.s3_key,
                    -- SportAI stage
                    sc.last_status,
                    sc.last_status_at,
                    -- Bronze ingest stage
                    sc.ingest_started_at,
                    sc.ingest_finished_at,
                    sc.ingest_error,
                    sc.session_id,
                    -- Silver (session_id not null = silver built)
                    -- Video trim stage
                    sc.trim_status,
                    sc.trim_requested_at,
                    sc.trim_finished_at,
                    sc.trim_error,
                    sc.trim_output_s3_key,
                    sc.trim_duration_s,
                    sc.trim_source_duration_s,
                    sc.trim_segment_count,
                    -- PBI refresh stage
                    sc.pbi_refresh_status,
                    sc.pbi_refresh_started_at,
                    sc.pbi_refresh_finished_at,
                    sc.pbi_refresh_error,
                    -- SES notify stage
                    sc.ses_notified_at,
                    sc.ses_notify_error,
                    -- Score
                    sc.player_a_set1_games, sc.player_b_set1_games,
                    sc.player_a_set2_games, sc.player_b_set2_games,
                    sc.player_a_set3_games, sc.player_b_set3_games
                FROM bronze.submission_context sc
                WHERE sc.created_at >= COALESCE(:d_from, CURRENT_DATE)::timestamptz
                  AND sc.created_at < (COALESCE(:d_to, CURRENT_DATE)::date + 1)::timestamptz
                ORDER BY sc.created_at DESC
            """),
            {"d_from": date_from, "d_to": date_to},
        ).mappings().all()

    def _ts(v):
        return v.isoformat() if v else None

    tasks = []
    for r in rows:
        tasks.append({
            "task_id": r["task_id"],
            "email": r["email"],
            "customer_name": r["customer_name"],
            "created_at": _ts(r["created_at"]),
            "match_date": str(r["match_date"]) if r["match_date"] else None,
            "location": r["location"],
            "player_a_name": r["player_a_name"],
            "player_b_name": r["player_b_name"],
            "s3_key": r["s3_key"],
            "last_status": r["last_status"],
            "last_status_at": _ts(r["last_status_at"]),
            "ingest_started_at": _ts(r["ingest_started_at"]),
            "ingest_finished_at": _ts(r["ingest_finished_at"]),
            "ingest_error": r["ingest_error"],
            "session_id": r["session_id"],
            "silver_built": r["session_id"] is not None,
            "trim_status": r["trim_status"],
            "trim_requested_at": _ts(r["trim_requested_at"]),
            "trim_finished_at": _ts(r["trim_finished_at"]),
            "trim_error": r["trim_error"],
            "trim_output_s3_key": r["trim_output_s3_key"],
            "trim_duration_s": float(r["trim_duration_s"]) if r["trim_duration_s"] else None,
            "trim_source_duration_s": float(r["trim_source_duration_s"]) if r["trim_source_duration_s"] else None,
            "trim_segment_count": int(r["trim_segment_count"]) if r["trim_segment_count"] else None,
            "pbi_refresh_status": r["pbi_refresh_status"],
            "pbi_refresh_started_at": _ts(r["pbi_refresh_started_at"]),
            "pbi_refresh_finished_at": _ts(r["pbi_refresh_finished_at"]),
            "pbi_refresh_error": r["pbi_refresh_error"],
            "ses_notified_at": _ts(r["ses_notified_at"]),
            "ses_notify_error": r["ses_notify_error"],
            "score": _format_score(r),
        })

    return jsonify({"ok": True, "tasks": tasks})


# ----------------------------
# GET /api/client/backoffice/customers
# ----------------------------

@client_bp.route("/api/client/backoffice/customers", methods=["GET", "OPTIONS"])
def backoffice_customers():
    if not _admin_guard():
        return _forbid()

    with engine.connect() as conn:
        # subscription_state is created lazily — check before joining
        has_sub = conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'billing' AND table_name = 'subscription_state'
                )
            """)
        ).scalar()

        sub_join = "LEFT JOIN billing.subscription_state s ON s.account_id = a.id" if has_sub else ""
        sub_cols = """
                    s.plan_code,
                    s.plan_type,
                    s.status AS subscription_status,
                    s.matches_granted AS plan_allowance,
                    s.current_period_start,
                    s.current_period_end,
                    s.cancelled_at,""" if has_sub else """
                    NULL AS plan_code,
                    NULL AS plan_type,
                    NULL AS subscription_status,
                    NULL AS plan_allowance,
                    NULL AS current_period_start,
                    NULL AS current_period_end,
                    NULL AS cancelled_at,"""

        rows = conn.execute(
            text(f"""
                SELECT
                    a.id AS account_id,
                    a.email,
                    a.primary_full_name,
                    a.active AS account_active,
                    a.created_at AS account_created_at,
                    -- Usage
                    COALESCE(v.matches_granted, 0)    AS matches_granted,
                    COALESCE(v.matches_consumed, 0)    AS matches_consumed,
                    COALESCE(v.matches_remaining, 0)   AS matches_remaining,
                    -- Subscription
                    {sub_cols}
                    -- Members
                    (SELECT count(*) FROM billing.member m
                     WHERE m.account_id = a.id AND m.active = true) AS member_count,
                    -- Match stats
                    (SELECT count(*) FROM bronze.submission_context sc
                     WHERE sc.email = a.email) AS total_tasks,
                    (SELECT count(*) FROM bronze.submission_context sc
                     WHERE sc.email = a.email AND sc.last_status = 'completed') AS completed_tasks,
                    (SELECT count(*) FROM bronze.submission_context sc
                     WHERE sc.email = a.email AND sc.last_status = 'failed') AS failed_tasks,
                    (SELECT max(sc.created_at) FROM bronze.submission_context sc
                     WHERE sc.email = a.email) AS last_upload_at
                FROM billing.account a
                LEFT JOIN billing.vw_customer_usage v ON v.account_id = a.id
                {sub_join}
                ORDER BY a.created_at DESC
            """)
        ).mappings().all()

    def _ts(v):
        return v.isoformat() if v else None

    customers = []
    for r in rows:
        customers.append({
            "account_id": int(r["account_id"]),
            "email": r["email"],
            "name": r["primary_full_name"],
            "account_active": r["account_active"],
            "account_created_at": _ts(r["account_created_at"]),
            "matches_granted": int(r["matches_granted"]),
            "matches_consumed": int(r["matches_consumed"]),
            "matches_remaining": int(r["matches_remaining"]),
            "plan_code": r["plan_code"],
            "plan_type": r["plan_type"],
            "subscription_status": r["subscription_status"],
            "plan_allowance": int(r["plan_allowance"]) if r["plan_allowance"] else None,
            "current_period_start": _ts(r["current_period_start"]),
            "current_period_end": _ts(r["current_period_end"]),
            "cancelled_at": _ts(r["cancelled_at"]),
            "member_count": int(r["member_count"]),
            "total_tasks": int(r["total_tasks"]),
            "completed_tasks": int(r["completed_tasks"]),
            "failed_tasks": int(r["failed_tasks"]),
            "last_upload_at": _ts(r["last_upload_at"]),
        })

    return jsonify({"ok": True, "customers": customers})


# ----------------------------
# GET /api/client/backoffice/kpis
# ----------------------------

@client_bp.route("/api/client/backoffice/kpis", methods=["GET", "OPTIONS"])
def backoffice_kpis():
    if not _admin_guard():
        return _forbid()

    with engine.connect() as conn:
        # subscription_state is created lazily — check before querying
        has_sub = conn.execute(
            text("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'billing' AND table_name = 'subscription_state'
                )
            """)
        ).scalar()

        sub_count = "(SELECT count(*) FROM billing.subscription_state WHERE status = 'ACTIVE')" if has_sub else "0"

        kpi = conn.execute(
            text(f"""
                SELECT
                  (SELECT count(*) FROM billing.account WHERE active = true)
                      AS active_accounts,
                  (SELECT count(*) FROM billing.member WHERE active = true)
                      AS active_members,
                  -- Today
                  (SELECT count(*) FROM bronze.submission_context
                   WHERE created_at >= CURRENT_DATE)
                      AS tasks_today,
                  (SELECT count(*) FROM bronze.submission_context
                   WHERE created_at >= CURRENT_DATE AND last_status = 'completed')
                      AS completed_today,
                  (SELECT count(*) FROM bronze.submission_context
                   WHERE created_at >= CURRENT_DATE AND last_status = 'failed')
                      AS failed_today,
                  -- This month
                  (SELECT count(*) FROM bronze.submission_context
                   WHERE created_at >= date_trunc('month', CURRENT_DATE))
                      AS tasks_month,
                  (SELECT count(*) FROM bronze.submission_context
                   WHERE created_at >= date_trunc('month', CURRENT_DATE) AND last_status = 'completed')
                      AS completed_month,
                  -- All time
                  (SELECT count(*) FROM bronze.submission_context)
                      AS tasks_all_time,
                  (SELECT count(*) FROM bronze.submission_context
                   WHERE last_status = 'completed')
                      AS completed_all_time,
                  -- Credits
                  (SELECT COALESCE(sum(matches_granted), 0)
                   FROM billing.entitlement_grant WHERE is_active = true)
                      AS total_credits_granted,
                  (SELECT COALESCE(sum(consumed_matches), 0)
                   FROM billing.entitlement_consumption)
                      AS total_credits_consumed,
                  -- Active subscriptions
                  {sub_count}
                      AS active_subscriptions,
                  -- Monthly tasks (last 12 months for chart)
                  (SELECT json_agg(row_to_json(m))
                   FROM (
                     SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS month,
                            count(*) AS total,
                            count(*) FILTER (WHERE last_status = 'completed') AS completed,
                            count(*) FILTER (WHERE last_status = 'failed') AS failed
                     FROM bronze.submission_context
                     WHERE created_at >= (CURRENT_DATE - interval '12 months')
                     GROUP BY 1 ORDER BY 1
                   ) m)
                      AS monthly_trend
            """)
        ).mappings().first()

    return jsonify({
        "ok": True,
        "kpis": {
            "active_accounts": int(kpi["active_accounts"]),
            "active_members": int(kpi["active_members"]),
            "tasks_today": int(kpi["tasks_today"]),
            "completed_today": int(kpi["completed_today"]),
            "failed_today": int(kpi["failed_today"]),
            "tasks_month": int(kpi["tasks_month"]),
            "completed_month": int(kpi["completed_month"]),
            "tasks_all_time": int(kpi["tasks_all_time"]),
            "completed_all_time": int(kpi["completed_all_time"]),
            "total_credits_granted": int(kpi["total_credits_granted"]),
            "total_credits_consumed": int(kpi["total_credits_consumed"]),
            "active_subscriptions": int(kpi["active_subscriptions"]),
            "monthly_trend": kpi["monthly_trend"] or [],
        },
    })


# ============================================================
# ANALYTICS — Power BI embed (proxies to PBI service)
# ============================================================

PBI_SERVICE_BASE = os.environ.get("POWERBI_SERVICE_BASE_URL", "").strip().rstrip("/")
PBI_SERVICE_OPS_KEY = (os.environ.get("POWERBI_SERVICE_OPS_KEY") or os.environ.get("OPS_KEY", "")).strip()


@client_bp.route("/api/client/pbi-embed", methods=["GET", "OPTIONS"])
def pbi_embed():
    """Get PowerBI embed config + token for the authenticated user."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    if not PBI_SERVICE_BASE or not PBI_SERVICE_OPS_KEY:
        return jsonify({"ok": False, "error": "pbi_service_not_configured"}), 503

    pbi_headers = {"x-ops-key": PBI_SERVICE_OPS_KEY, "Content-Type": "application/json"}

    try:
        # 1. Start a session (warms up capacity)
        sess_resp = http_requests.post(
            f"{PBI_SERVICE_BASE}/session/start",
            json={"username": email},
            headers=pbi_headers,
            timeout=30,
        )
        if sess_resp.status_code >= 400:
            log.error("PBI session/start failed: %s %s", sess_resp.status_code, sess_resp.text[:200])
            return jsonify({"ok": False, "error": "pbi_session_failed"}), 502

        sess = sess_resp.json()

        # 2. Get embed config
        cfg_resp = http_requests.get(
            f"{PBI_SERVICE_BASE}/embed/config",
            headers=pbi_headers,
            timeout=15,
        )
        if cfg_resp.status_code >= 400:
            return jsonify({"ok": False, "error": "pbi_config_failed"}), 502

        cfg = cfg_resp.json()

        # 3. Generate embed token (RLS by email)
        tok_resp = http_requests.post(
            f"{PBI_SERVICE_BASE}/embed/token",
            json={"username": email},
            headers=pbi_headers,
            timeout=30,
        )
        if tok_resp.status_code >= 400:
            log.error("PBI embed/token failed: %s %s", tok_resp.status_code, tok_resp.text[:200])
            return jsonify({"ok": False, "error": "pbi_token_failed"}), 502

        tok = tok_resp.json()

        return jsonify({
            "ok": True,
            "embedUrl": cfg.get("embedUrl"),
            "reportId": cfg.get("reportId"),
            "token": tok.get("token"),
            "tokenExpiry": tok.get("expiration"),
            "sessionId": sess.get("session_id"),
        })

    except http_requests.Timeout:
        return jsonify({"ok": False, "error": "pbi_timeout"}), 504
    except Exception as e:
        log.exception("PBI embed proxy error")
        return jsonify({"ok": False, "error": "pbi_error"}), 502


@client_bp.route("/api/client/pbi-heartbeat", methods=["POST", "OPTIONS"])
def pbi_heartbeat():
    """Keep PBI capacity session alive."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    body = request.get_json(silent=True) or {}
    session_id = (body.get("session_id") or "").strip()

    if not email or not session_id or not PBI_SERVICE_BASE:
        return jsonify({"ok": False}), 400

    try:
        http_requests.post(
            f"{PBI_SERVICE_BASE}/session/heartbeat",
            json={"username": email, "session_id": session_id},
            headers={"x-ops-key": PBI_SERVICE_OPS_KEY, "Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        pass  # non-critical

    return jsonify({"ok": True})


@client_bp.route("/api/client/pbi-session-end", methods=["POST", "OPTIONS"])
def pbi_session_end():
    """End PBI capacity session (called on page unload)."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    body = request.get_json(silent=True) or {}
    session_id = (body.get("session_id") or "").strip()

    if not email or not session_id or not PBI_SERVICE_BASE:
        return jsonify({"ok": False}), 400

    try:
        http_requests.post(
            f"{PBI_SERVICE_BASE}/session/end",
            json={"username": email, "session_id": session_id, "reason": "client_navigate_away"},
            headers={"x-ops-key": PBI_SERVICE_OPS_KEY, "Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        pass  # best effort

    return jsonify({"ok": True})


# ----------------------------
# GET /api/client/coaches
# ----------------------------

@client_bp.route("/api/client/coaches", methods=["GET", "OPTIONS"])
def list_coaches():
    """List coach permissions for the account (INVITED/ACCEPTED/REVOKED)."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT cp.id, cp.coach_email, cp.status, cp.active,
                       cp.created_at, cp.updated_at
                FROM billing.coaches_permission cp
                JOIN billing.account a ON a.id = cp.owner_account_id
                WHERE a.email = :email
                ORDER BY cp.created_at DESC
            """),
            {"email": email},
        ).mappings().all()

    coaches = []
    for r in rows:
        coaches.append({
            "id": int(r["id"]),
            "coach_email": r["coach_email"],
            "status": r["status"],
            "active": bool(r["active"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })

    return jsonify({"ok": True, "coaches": coaches})


# ----------------------------
# POST /api/client/coach-invite
# ----------------------------

@client_bp.route("/api/client/coach-invite", methods=["POST", "OPTIONS"])
def coach_invite():
    """Invite a coach — creates permission row, generates token, sends SES email."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    payload = request.get_json(silent=True) or {}
    coach_email = _norm_email(payload.get("coach_email"))
    if not coach_email:
        return jsonify({"ok": False, "error": "coach_email required"}), 400

    from coaches_api import STATUS_INVITED, SCHEMA, TABLE

    with Session(engine) as session:
        acct_row = session.execute(
            text("""
                SELECT a.id, m.full_name, m.surname
                FROM billing.account a
                LEFT JOIN billing.member m ON m.account_id = a.id AND m.is_primary = true AND m.active = true
                WHERE a.email = :email
                LIMIT 1
            """),
            {"email": email},
        ).mappings().first()
        if not acct_row:
            return jsonify({"ok": False, "error": "account_not_found"}), 404

        acct_id = int(acct_row["id"])
        owner_name = " ".join(filter(None, [acct_row["full_name"], acct_row["surname"]])) or email

        from datetime import timezone
        now = datetime.now(tz=timezone.utc)

        existing = session.execute(
            text(f"""
                SELECT id FROM {SCHEMA}.{TABLE}
                WHERE owner_account_id = :aid AND coach_email = :ce
                LIMIT 1
            """),
            {"aid": acct_id, "ce": coach_email},
        ).mappings().first()

        if existing:
            session.execute(
                text(f"""
                    UPDATE {SCHEMA}.{TABLE}
                    SET status = :status, active = true,
                        coach_account_id = NULL, invite_token = NULL, updated_at = :now
                    WHERE id = :id
                """),
                {"id": int(existing["id"]), "status": STATUS_INVITED, "now": now},
            )
            session.commit()
            permission_id = int(existing["id"])
            reused = True
        else:
            row = session.execute(
                text(f"""
                    INSERT INTO {SCHEMA}.{TABLE}
                      (owner_account_id, coach_account_id, coach_email, status, active, created_at, updated_at)
                    VALUES (:aid, NULL, :ce, :status, true, :now, :now)
                    RETURNING id
                """),
                {"aid": acct_id, "ce": coach_email, "status": STATUS_INVITED, "now": now},
            ).mappings().first()
            session.commit()
            permission_id = int(row["id"])
            reused = False

    # Generate token and send invite email via SES
    from coach_invite.db import generate_token, set_token
    from coach_invite.email_sender import send_invite_email

    token = generate_token()
    set_token(permission_id, token)

    accept_url = f"{COACH_ACCEPT_BASE_URL}/coach-accept?token={token}"
    coach_name = (payload.get("coach_name") or "").strip()
    email_result = send_invite_email(coach_email, coach_name, owner_name, accept_url)

    return jsonify({
        "ok": True,
        "permission_id": permission_id,
        "status": STATUS_INVITED,
        "reused": reused,
        "email_sent": email_result.get("ok", False),
        "email_error": email_result.get("error"),
    })


# ----------------------------
# POST /api/client/coach-revoke
# ----------------------------

@client_bp.route("/api/client/coach-revoke", methods=["POST", "OPTIONS"])
def coach_revoke():
    """Revoke a coach permission."""
    if not _guard():
        return _forbid()

    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    payload = request.get_json(silent=True) or {}
    permission_id = payload.get("permission_id")
    if not permission_id:
        return jsonify({"ok": False, "error": "permission_id required"}), 400

    from coaches_api import STATUS_REVOKED, SCHEMA, TABLE

    with Session(engine) as session:
        acct_id = session.execute(
            text("SELECT id FROM billing.account WHERE email = :email"),
            {"email": email},
        ).scalar_one_or_none()
        if not acct_id:
            return jsonify({"ok": False, "error": "account_not_found"}), 404

        perm = session.execute(
            text(f"""
                SELECT id FROM {SCHEMA}.{TABLE}
                WHERE id = :id AND owner_account_id = :aid
                LIMIT 1
            """),
            {"id": int(permission_id), "aid": acct_id},
        ).mappings().first()

        if not perm:
            return jsonify({"ok": False, "error": "permission_not_found"}), 404

        from datetime import timezone
        now = datetime.now(tz=timezone.utc)

        session.execute(
            text(f"""
                UPDATE {SCHEMA}.{TABLE}
                SET status = :status, active = false,
                    coach_account_id = NULL, invite_token = NULL, updated_at = :now
                WHERE id = :id
            """),
            {"id": int(permission_id), "status": STATUS_REVOKED, "now": now},
        )
        session.commit()
        return jsonify({"ok": True, "permission_id": int(permission_id),
                        "status": STATUS_REVOKED})
