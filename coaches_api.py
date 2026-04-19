# coaches_api.py — Server-to-server coach permission management (OPS_KEY auth).
#
# Manages the billing.coaches_permission table: creating, accepting, and revoking
# coach access grants on behalf of player/parent account owners. These are the
# server-side endpoints called by client_api.py and the coach invite flow.
# The token-based public accept endpoint lives in coach_invite/accept_page.py.
#
# Endpoints (all OPS_KEY auth via X-Ops-Key header):
#   POST /api/coaches/invite
#     — Creates a new coaches_permission row (status=INVITED) for the given owner + coach email.
#     — Idempotent: re-inviting a previously invited/revoked coach reuses the existing row
#       (resets status to INVITED, clears coach_account_id).
#     — Owner resolved by owner_external_wix_id (preferred) or owner_email.
#
#   POST /api/coaches/accept
#     — Sets status=ACCEPTED and links coach_account_id (if the coach has a billing account).
#     — Accepts by permission_id (preferred) or by coach_email if exactly one INVITED row exists.
#     — Requires invite to be in status=INVITED and active=true.
#
#   POST /api/coaches/revoke
#     — Sets status=REVOKED, active=false, clears coach_account_id and invite_token.
#     — Accepts by permission_id (preferred) or by owner + coach_email pair.
#
#   GET /api/coaches/health  — liveness probe, no auth required.
#
# Auth: OPS_KEY via X-Ops-Key header (checks BILLING_OPS_KEY then OPS_KEY env vars)
#
# Business rules:
#   - coaches_permission is keyed by (owner_account_id, coach_email) — one row per pair
#   - coach_account_id is nullable: set when/if the coach registers their own billing account
#   - invite_token column is managed by coach_invite/db.py (not set here; cleared on revoke)
#   - This module uses raw SQL only (no ORM) to stay self-contained and avoid FK issues

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db_init import engine

# ----------------------------
# Constants
# ----------------------------

SCHEMA = "billing"
TABLE = "coaches_permission"

STATUS_INVITED = "INVITED"
STATUS_ACCEPTED = "ACCEPTED"
STATUS_REVOKED = "REVOKED"

# ----------------------------
# Blueprint
# ----------------------------

bp = Blueprint("coaches", __name__, url_prefix="/api/coaches")

# ----------------------------
# Helpers
# ----------------------------

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _ops_key_ok() -> bool:
    ops_key = os.getenv("BILLING_OPS_KEY") or os.getenv("OPS_KEY") or ""
    h = request.headers
    provided = (
        h.get("X-Ops-Key")
        or h.get("X-OPS-KEY")
        or h.get("x-ops-key")
        or h.get("x-OPS-key")
        or ""
    )
    import hmac
    return bool(ops_key) and hmac.compare_digest(provided, ops_key)


def _require_ops_key():
    if not _ops_key_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return None

def _norm_str(v: Optional[str]) -> str:
    return (v or "").strip()

def _get_owner_account_id(session: Session, owner_external_wix_id: str, owner_email: str) -> Optional[int]:
    if owner_external_wix_id:
        row = session.execute(
            text("SELECT id FROM billing.account WHERE external_wix_id = :x LIMIT 1"),
            {"x": owner_external_wix_id},
        ).mappings().first()
        if row:
            return int(row["id"])

    if owner_email:
        row = session.execute(
            text("SELECT id FROM billing.account WHERE email = :email LIMIT 1"),
            {"email": owner_email},
        ).mappings().first()
        if row:
            return int(row["id"])

    return None

# ----------------------------
# API: Invite
# ----------------------------

@bp.post("/invite")
def api_invite():
    unauth = _require_ops_key()
    if unauth:
        return unauth

    body: Dict[str, Any] = request.get_json(silent=True) or {}

    owner_external_wix_id = _norm_str(body.get("owner_external_wix_id"))
    owner_email = _norm_email(body.get("owner_email"))
    coach_email = _norm_email(body.get("coach_email"))

    if not coach_email:
        return jsonify(ok=False, error="coach_email_required"), 400

    if not owner_external_wix_id and not owner_email:
        return jsonify(ok=False, error="owner_external_wix_id_or_owner_email_required"), 400

    with Session(engine) as session:
        try:
            owner_account_id = _get_owner_account_id(session, owner_external_wix_id, owner_email)
            if not owner_account_id:
                return jsonify(ok=False, error="owner_not_found"), 404

            now = _now_utc()

            existing = session.execute(
                text(f"""
                    SELECT id
                    FROM {SCHEMA}.{TABLE}
                    WHERE owner_account_id = :owner_account_id
                      AND coach_email = :coach_email
                    LIMIT 1
                """),
                {"owner_account_id": owner_account_id, "coach_email": coach_email},
            ).mappings().first()

            if existing:
                session.execute(
                    text(f"""
                        UPDATE {SCHEMA}.{TABLE}
                        SET
                          status = :status,
                          active = true,
                          coach_account_id = NULL,
                          updated_at = :now
                        WHERE id = :id
                    """),
                    {"id": int(existing["id"]), "status": STATUS_INVITED, "now": now},
                )
                session.commit()
                return jsonify(ok=True, permission_id=int(existing["id"]), status=STATUS_INVITED, reused=True)

            row = session.execute(
                text(f"""
                    INSERT INTO {SCHEMA}.{TABLE}
                      (owner_account_id, coach_account_id, coach_email, status, active, created_at, updated_at)
                    VALUES
                      (:owner_account_id, NULL, :coach_email, :status, true, :now, :now)
                    RETURNING id
                """),
                {"owner_account_id": owner_account_id, "coach_email": coach_email, "status": STATUS_INVITED, "now": now},
            ).mappings().first()

            session.commit()
            return jsonify(ok=True, permission_id=int(row["id"]), status=STATUS_INVITED, reused=False)

        except Exception as e:
            session.rollback()
            return jsonify(ok=False, error="invite_failed"), 500



# ----------------------------
# API: Accept
# ----------------------------

@bp.post("/accept")
def api_accept():
    unauth = _require_ops_key()
    if unauth:
        return unauth

    body: Dict[str, Any] = request.get_json(silent=True) or {}

    coach_email = _norm_email(body.get("coach_email"))
    permission_id = body.get("permission_id")  # preferred

    if not coach_email:
        return jsonify(ok=False, error="coach_email_required"), 400

    # Phase 2 cap — first linked player free, Coach Pro required for more.
    # Gate before any UPDATE so we don't half-transition state on a blocked
    # accept. See docs/pricing_strategy.md §6.
    from billing_service import (
        coach_accept_gate,
        count_accepted_coach_links,
        COACH_PRO_UPGRADE_URL,
        FREE_COACH_LINK_LIMIT,
    )
    allowed, reason = coach_accept_gate(coach_email)
    if not allowed:
        return jsonify(
            ok=False,
            error=reason or "COACH_UPGRADE_REQUIRED",
            message="Coach has reached free limit of 1 linked player. Coach Pro required.",
            upgrade_url=COACH_PRO_UPGRADE_URL,
            coach_email=coach_email,
            current_links=count_accepted_coach_links(coach_email),
            free_limit=FREE_COACH_LINK_LIMIT,
        ), 402

    with Session(engine) as session:
        try:
            coach = session.execute(
                text("SELECT id FROM billing.account WHERE email = :email LIMIT 1"),
                {"email": coach_email},
            ).mappings().first()

            coach_account_id = int(coach["id"]) if coach else None
            now = _now_utc()

            if permission_id is not None:
                perm = session.execute(
                    text(f"""
                        SELECT id, status, active
                        FROM {SCHEMA}.{TABLE}
                        WHERE id = :id
                          AND coach_email = :coach_email
                        LIMIT 1
                    """),
                    {"id": int(permission_id), "coach_email": coach_email},
                ).mappings().first()

                if not perm:
                    return jsonify(ok=False, error="invite_not_found"), 400

                if str(perm["status"]).upper() != STATUS_INVITED or not bool(perm["active"]):
                    return jsonify(ok=False, error="invite_not_eligible"), 400

                session.execute(
                    text(f"""
                        UPDATE {SCHEMA}.{TABLE}
                        SET
                          status = :status,
                          coach_account_id = :coach_account_id,
                          active = true,
                          updated_at = :now
                        WHERE id = :id
                    """),
                    {
                        "id": int(permission_id),
                        "status": STATUS_ACCEPTED,
                        "coach_account_id": coach_account_id,
                        "now": now,
                    },
                )
                session.commit()
                return jsonify(ok=True, permission_id=int(permission_id), status=STATUS_ACCEPTED, coach_linked=bool(coach_account_id))

            rows = session.execute(
                text(f"""
                    SELECT id
                    FROM {SCHEMA}.{TABLE}
                    WHERE coach_email = :coach_email
                      AND status = :status
                      AND active = true
                    ORDER BY id DESC
                """),
                {"coach_email": coach_email, "status": STATUS_INVITED},
            ).mappings().all()

            if not rows:
                return jsonify(ok=False, error="invite_not_found"), 400

            if len(rows) > 1:
                return jsonify(ok=False, error="multiple_invites_require_permission_id"), 400

            pid = int(rows[0]["id"])
            session.execute(
                text(f"""
                    UPDATE {SCHEMA}.{TABLE}
                    SET
                      status = :status,
                      coach_account_id = :coach_account_id,
                      active = true,
                      updated_at = :now
                    WHERE id = :id
                """),
                {"id": pid, "status": STATUS_ACCEPTED, "coach_account_id": coach_account_id, "now": now},
            )
            session.commit()
            return jsonify(ok=True, permission_id=pid, status=STATUS_ACCEPTED, coach_linked=bool(coach_account_id))

        except Exception as e:
            session.rollback()
            return jsonify(ok=False, error="accept_failed"), 500



# ----------------------------
# API: Revoke
# ----------------------------

@bp.post("/revoke")
def api_revoke():
    unauth = _require_ops_key()
    if unauth:
        return unauth

    body: Dict[str, Any] = request.get_json(silent=True) or {}
    permission_id = body.get("permission_id")

    owner_external_wix_id = _norm_str(body.get("owner_external_wix_id"))
    owner_email = _norm_email(body.get("owner_email"))
    coach_email = _norm_email(body.get("coach_email"))

    with Session(engine) as session:
        try:
            now = _now_utc()

            if permission_id:
                perm = session.execute(
                    text(f"SELECT id FROM {SCHEMA}.{TABLE} WHERE id = :id LIMIT 1"),
                    {"id": int(permission_id)},
                ).mappings().first()

                if not perm:
                    return jsonify(ok=False, error="permission_not_found"), 404

                session.execute(
                    text(f"""
                        UPDATE {SCHEMA}.{TABLE}
                        SET
                        status = :status,
                        active = false,
                        coach_account_id = NULL,
                        invite_token = NULL,
                        updated_at = :now
                        WHERE id = :id
                    """),
                    {"id": int(permission_id), "status": STATUS_REVOKED, "now": now},
                )

                session.commit()
                return jsonify(ok=True, permission_id=int(permission_id), status=STATUS_REVOKED)

            # email-based revoke (requires owner + coach_email)
            if not coach_email:
                return jsonify(ok=False, error="permission_id_or_coach_email_required"), 400

            if not owner_external_wix_id and not owner_email:
                return jsonify(ok=False, error="owner_external_wix_id_or_owner_email_required"), 400

            owner_account_id = _get_owner_account_id(session, owner_external_wix_id, owner_email)
            if not owner_account_id:
                return jsonify(ok=False, error="owner_not_found"), 404

            perm = session.execute(
                text(f"""
                    SELECT id
                    FROM {SCHEMA}.{TABLE}
                    WHERE owner_account_id = :owner_account_id
                      AND coach_email = :coach_email
                    LIMIT 1
                """),
                {"owner_account_id": owner_account_id, "coach_email": coach_email},
            ).mappings().first()

            if not perm:
                return jsonify(ok=False, error="permission_not_found"), 404

            pid = int(perm["id"])
            session.execute(
                text(f"""
                    UPDATE {SCHEMA}.{TABLE}
                    SET
                    status = :status,
                    active = false,
                    coach_account_id = NULL,
                    invite_token = NULL,
                    updated_at = :now
                    WHERE id = :id
                """),
                {"id": pid, "status": STATUS_REVOKED, "now": now},
            )

            session.commit()
            return jsonify(ok=True, permission_id=pid, status=STATUS_REVOKED)

        except Exception as e:
            session.rollback()
            return jsonify(ok=False, error="revoke_failed"), 500


# ----------------------------
# Optional: quick health check (useful for routing debug)
# ----------------------------

@bp.get("/health")
def api_health():
    return jsonify(ok=True, service="coaches")
