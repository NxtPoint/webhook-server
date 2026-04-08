# coach_invite/accept_page.py — Accept page + public token-based accept endpoint

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
        "coach_linked": bool(coach_account_id),
    })
