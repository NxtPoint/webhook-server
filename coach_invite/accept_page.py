# coach_invite/accept_page.py
# ============================================================
# Flask blueprint for the coach invitation acceptance flow.
#
# Endpoints:
#   GET  /coach-accept
#       Serves coach_accept.html (standalone SPA, no auth required).
#
#   POST /api/coaches/accept-token
#       PUBLIC endpoint — the invite token IS the authentication.
#       Validates the token against billing.coaches_permission
#       (must be status=INVITED and active=true). On success:
#         - Sets status = ACCEPTED
#         - Clears invite_token (token is single-use)
#         - Returns { coach_email } so the SPA can show login guidance
#       Returns 400 if token is missing, 404 if not found / already used.
#
# Business rule: no OPS_KEY or CLIENT_API_KEY is required for the accept
# endpoint — possession of a valid token is sufficient proof of identity.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, send_file
from sqlalchemy import text
from sqlalchemy.orm import Session

from db_init import engine
from coach_invite.db import get_permission_by_token, clear_token

accept_bp = Blueprint("coach_accept", __name__)


@accept_bp.get("/coach-accept")
def coach_accept_page():
    """Serve the coach acceptance HTML page."""
    return send_file("coach_accept.html")


@accept_bp.route("/api/coaches/accept-token", methods=["POST", "OPTIONS"])
def accept_by_token():
    """Public endpoint — token IS the auth. No API key needed."""
    if request.method == "OPTIONS":
        return "", 204

    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "token required"}), 400

    perm = get_permission_by_token(token)
    if not perm:
        return jsonify({"ok": False, "error": "invalid_or_expired_token"}), 400

    # Look up coach account (if they have one) — same logic as coaches_api.api_accept
    with Session(engine) as session:
        coach_row = session.execute(
            text("SELECT id FROM billing.account WHERE email = :email LIMIT 1"),
            {"email": perm["coach_email"]},
        ).mappings().first()
        coach_account_id = int(coach_row["id"]) if coach_row else None

        now = datetime.now(tz=timezone.utc)
        session.execute(
            text("""
                UPDATE billing.coaches_permission
                SET status = 'ACCEPTED',
                    coach_account_id = :caid,
                    invite_token = NULL,
                    active = true,
                    updated_at = :now
                WHERE id = :id
            """),
            {"id": perm["id"], "caid": coach_account_id, "now": now},
        )
        session.commit()

    return jsonify({
        "ok": True,
        "status": "ACCEPTED",
        "owner_name": perm["owner_name"],
        "coach_email": perm["coach_email"],
        "coach_linked": bool(coach_account_id),
    })
