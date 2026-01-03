# ============================================================
# coaches_api.py  (PRODUCTION BASELINE - SELF CONTAINED)
#
# PURPOSE
# -------
# Manage coach permissions (invite / accept / revoke)
# View-only access for dashboards.
#
# DESIGN PRINCIPLES
# -----------------
# - Separate from billing logic (no billing code touched)
# - Self-contained module (raw SQL; no ORM/FK metadata issues)
# - Idempotent where practical
# - Ops-key protected endpoints (server-to-server)
#
# TABLE
# -----
# schema: billing
# table : coaches_permission
#
# REQUIRED COLUMNS
# ----------------
# id BIGSERIAL PK
# owner_account_id BIGINT NOT NULL  (billing.account.id)
# coach_account_id BIGINT NULL      (billing.account.id)
# coach_email TEXT NOT NULL
# status TEXT NOT NULL              ('INVITED'|'ACCEPTED'|'REVOKED')
# active BOOLEAN NOT NULL
# created_at TIMESTAMPTZ NOT NULL
# updated_at TIMESTAMPTZ NOT NULL
#
# Recommended unique index:
#   (owner_account_id, coach_email)
# ============================================================

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
    return bool(ops_key) and (provided == ops_key)


def _require_ops_key():
    if not _ops_key_ok():
        return jsonify(ok=False, error="unauthorized"), 401
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

    owner_email = _norm_email(body.get("owner_email"))
    coach_email = _norm_email(body.get("coach_email"))

    if not owner_email or not coach_email:
        return jsonify(ok=False, error="owner_email_and_coach_email_required"), 400

    with Session(engine) as session:
        try:
            owner = session.execute(
                text("SELECT id FROM billing.account WHERE email = :email"),
                {"email": owner_email},
            ).mappings().first()

            if not owner:
                return jsonify(ok=False, error="owner_not_found"), 404

            owner_account_id = int(owner["id"])
            now = _now_utc()

            # Upsert-by-hand (keeps self-contained + avoids relying on unique constraint behavior)
            existing = session.execute(
                text(f"""
                    SELECT id, status, active
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
                return jsonify(
                    ok=True,
                    permission_id=int(existing["id"]),
                    status=STATUS_INVITED,
                    reused=True,
                )

            row = session.execute(
                text(f"""
                    INSERT INTO {SCHEMA}.{TABLE}
                      (owner_account_id, coach_account_id, coach_email, status, active, created_at, updated_at)
                    VALUES
                      (:owner_account_id, NULL, :coach_email, :status, true, :now, :now)
                    RETURNING id
                """),
                {
                    "owner_account_id": owner_account_id,
                    "coach_email": coach_email,
                    "status": STATUS_INVITED,
                    "now": now,
                },
            ).mappings().first()

            session.commit()
            return jsonify(ok=True, permission_id=int(row["id"]), status=STATUS_INVITED, reused=False)

        except Exception as e:
            session.rollback()
            return jsonify(ok=False, error="invite_failed", detail=str(e)), 500


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

    with Session(engine) as session:
        try:
            coach = session.execute(
                text("SELECT id FROM billing.account WHERE email = :email"),
                {"email": coach_email},
            ).mappings().first()

            if not coach:
                return jsonify(ok=False, error="coach_account_not_found"), 404

            coach_account_id = int(coach["id"])
            now = _now_utc()

            # Preferred: accept a specific invite (permission_id)
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
                return jsonify(ok=True, permission_id=int(permission_id), status=STATUS_ACCEPTED)

            # Back-compat: accept by email ONLY (allowed only if exactly one eligible invite exists)
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
            return jsonify(ok=True, permission_id=pid, status=STATUS_ACCEPTED)

        except Exception as e:
            session.rollback()
            return jsonify(ok=False, error="accept_failed", detail=str(e)), 500


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

    if not permission_id:
        return jsonify(ok=False, error="permission_id_required"), 400

    with Session(engine) as session:
        try:
            now = _now_utc()

            perm = session.execute(
                text(f"""
                    SELECT id
                    FROM {SCHEMA}.{TABLE}
                    WHERE id = :id
                    LIMIT 1
                """),
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
                      updated_at = :now
                    WHERE id = :id
                """),
                {"id": int(permission_id), "status": STATUS_REVOKED, "now": now},
            )
            session.commit()
            return jsonify(ok=True, permission_id=int(permission_id), status=STATUS_REVOKED)

        except Exception as e:
            session.rollback()
            return jsonify(ok=False, error="revoke_failed", detail=str(e)), 500


# ----------------------------
# Optional: quick health check (useful for routing debug)
# ----------------------------

@bp.get("/health")
def api_health():
    return jsonify(ok=True, service="coaches", ts=_now_utc().isoformat())
