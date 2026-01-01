#==================================
# billing_write_api.py  (FINAL BASELINE)
#==================================

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import text, select
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account, Member
from billing_service import (
    create_account_with_primary_member,
    grant_entitlement,
)

billing_write_bp = Blueprint("billing_write", __name__)


# ----------------------------
# Helpers
# ----------------------------

def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _ops_key_ok() -> bool:
    # Keep compatibility with existing prod patterns
    ops_key = os.getenv("BILLING_OPS_KEY") or os.getenv("OPS_KEY") or ""
    provided = request.headers.get("X-Ops-Key") or request.headers.get("X-OPS-KEY") or ""
    return bool(ops_key) and (provided == ops_key)


def _validate_role(role: str) -> str:
    """
    Render roles are only: 'player_parent' | 'coach'
    Wix/UI may send: 'player' for child rows -> normalize to 'player_parent'
    """
    role = (role or "").strip().lower()

    if role == "player":
        role = "player_parent"

    if role not in ("player_parent", "coach"):
        raise ValueError("invalid role")
    return role


def _find_account(session: Session, *, email: str, external_wix_id: Optional[str]) -> Optional[Account]:
    # Prefer external_wix_id when supplied (stable Wix identity),
    # fall back to email uniqueness.
    if external_wix_id:
        acct = session.execute(
            select(Account).where(Account.external_wix_id == external_wix_id)
        ).scalar_one_or_none()
        if acct is not None:
            return acct

    return session.execute(
        select(Account).where(Account.email == email)
    ).scalar_one_or_none()


# ----------------------------
# Write endpoints
# ----------------------------

@billing_write_bp.post("/api/billing/sync_account")
def sync_account():
    """
    Upsert account + replace members snapshot.
    Intended to be called from Wix backend during onboarding (or later profile updates).

    SAFETY / GUARD:
    - We only delete+replace members if we have a valid snapshot ready to write.
    - Prevents a bad/empty payload from wiping members.
    """
    payload = request.get_json(silent=True) or {}

    email = _norm_email(payload.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    primary_full_name = (payload.get("primary_full_name") or "").strip() or email
    currency_code = (payload.get("currency_code") or "USD").strip().upper() or "USD"
    external_wix_id = (payload.get("external_wix_id") or "").strip() or None

    # Members snapshot from Wix
    members_in = payload.get("members") or []
    if not isinstance(members_in, list):
        return jsonify({"ok": False, "error": "members must be a list"}), 400

    try:
        # Determine the primary role from input, else default player_parent
        primary_role = None
        for m in members_in:
            if isinstance(m, dict) and bool(m.get("is_primary")):
                primary_role = _validate_role(m.get("role") or "player_parent")
                break
        if primary_role is None:
            primary_role = _validate_role(payload.get("role") or "player_parent")

        # Ensure account exists (stable logic)
        _ = create_account_with_primary_member(
            email=email,
            primary_full_name=primary_full_name,
            currency_code=currency_code,
            external_wix_id=external_wix_id,
            role=primary_role,
        )

        # Replace members snapshot (atomic) WITH GUARD
        with Session(engine) as session:
            # Re-load account inside this session
            account_db = _find_account(session, email=email, external_wix_id=external_wix_id)
            if account_db is None:
                return jsonify({"ok": False, "error": "account not found after create"}), 500

            # If Wix id is newly available, persist it (safe, non-destructive)
            if external_wix_id and not account_db.external_wix_id:
                account_db.external_wix_id = external_wix_id

            # -------- Build snapshot FIRST (GUARD) --------
            snapshot: List[Dict[str, Any]] = []
            primary_count = 0

            for m in members_in:
                if not isinstance(m, dict):
                    continue

                full_name = (m.get("full_name") or "").strip()
                if not full_name:
                    continue

                is_primary = bool(m.get("is_primary"))

                # Normalize/validate role:
                # - primary uses primary_role default
                # - children default to player_parent (and accept 'player' -> player_parent)
                role_in = m.get("role") or ("player_parent" if not is_primary else primary_role)
                role = _validate_role(role_in)

                if is_primary:
                    primary_count += 1

                snapshot.append(
                    {
                        "full_name": full_name,
                        "is_primary": is_primary,
                        "role": role,
                        "active": True,
                    }
                )

            # Enforce exactly one primary
            if primary_count == 0:
                snapshot.insert(
                    0,
                    {
                        "full_name": primary_full_name,
                        "is_primary": True,
                        "role": primary_role,
                        "active": True,
                    },
                )
            elif primary_count > 1:
                return jsonify({"ok": False, "error": "only one primary member allowed"}), 400

            # -------- GUARD: do not wipe DB if snapshot is empty --------
            # (Can happen if Wix sends blanks / fields missing)
            if not snapshot:
                return jsonify({"ok": False, "error": "no valid members in payload"}), 400

            # -------- Apply snapshot (delete+insert) --------
            session.execute(
                text("DELETE FROM billing.member WHERE account_id = :account_id"),
                {"account_id": account_db.id},
            )

            for m in snapshot:
                session.add(
                    Member(
                        account_id=account_db.id,
                        full_name=m["full_name"],
                        is_primary=m["is_primary"],
                        role=m["role"],
                        active=m["active"],
                    )
                )

            session.commit()

            # Return fresh summary-compatible data
            row = session.execute(
                text(
                    """
                    SELECT
                      a.email,
                      m.role,
                      COALESCE(v.matches_granted, 0)   AS matches_granted,
                      COALESCE(v.matches_consumed, 0)  AS matches_consumed,
                      COALESCE(v.matches_remaining, 0) AS matches_remaining,
                      v.last_processed_at
                    FROM billing.account a
                    LEFT JOIN billing.member m
                      ON m.account_id = a.id AND m.is_primary = true
                    LEFT JOIN billing.vw_customer_usage v
                      ON v.account_id = a.id
                    WHERE a.id = :account_id
                    """
                ),
                {"account_id": account_db.id},
            ).mappings().first()

            return jsonify({"ok": True, "data": dict(row) if row else None})

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"sync failed: {str(e)}"}), 500


@billing_write_bp.post("/api/billing/entitlement/grant")
def entitlement_grant():
    """
    Ops-protected credit grant. Intended for Wix subscription webhook handler to call.
    """
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    email = _norm_email(payload.get("email"))
    account_id = payload.get("account_id")
    external_wix_id = (payload.get("external_wix_id") or "").strip() or None

    source = (payload.get("source") or "").strip()
    plan_code = (payload.get("plan_code") or "").strip()
    matches_granted = payload.get("matches_granted")
    is_active = bool(payload.get("is_active", True))

    valid_from = payload.get("valid_from")
    valid_to = payload.get("valid_to")

    try:
        if matches_granted is None:
            return jsonify({"ok": False, "error": "matches_granted required"}), 400
        matches_granted = int(matches_granted)

        # Parse ISO timestamps if supplied
        def _parse_dt(v):
            if not v:
                return None
            if isinstance(v, datetime):
                return v
            return datetime.fromisoformat(str(v))

        valid_from_dt = _parse_dt(valid_from)
        valid_to_dt = _parse_dt(valid_to)

        with Session(engine) as session:
            acct: Optional[Account] = None

            if account_id is not None:
                acct = session.execute(
                    select(Account).where(Account.id == int(account_id))
                ).scalar_one_or_none()

            if acct is None and (external_wix_id or email):
                acct = _find_account(session, email=email, external_wix_id=external_wix_id)

            if acct is None:
                return jsonify({"ok": False, "error": "account not found"}), 404

            grant_id = grant_entitlement(
                account_id=int(acct.id),
                source=source,
                plan_code=plan_code,
                matches_granted=matches_granted,
                external_wix_id=external_wix_id,
                valid_from=valid_from_dt,
                valid_to=valid_to_dt,
                is_active=is_active,
            )

            return jsonify({"ok": True, "grant_id": grant_id, "account_id": int(acct.id)})

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"grant failed: {str(e)}"}), 500


@billing_write_bp.get("/api/billing/entitlement/check")
def entitlement_check():
    """
    Read-only entitlement check for frontend.
    Uses the same source-of-truth tables/views as summary + upload gate.
    """
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                  a.id AS account_id,
                  a.email,
                  m.role,
                  COALESCE(v.matches_remaining, 0) AS matches_remaining
                FROM billing.account a
                LEFT JOIN billing.member m
                  ON m.account_id = a.id AND m.is_primary = true
                LEFT JOIN billing.vw_customer_usage v
                  ON v.account_id = a.id
                WHERE a.email = :email
                """
            ),
            {"email": email},
        ).mappings().first()

    if not row:
        return jsonify({"ok": True, "allowed": False, "reason": "account_not_found", "data": None})

    role = (row.get("role") or "player_parent").strip()
    remaining = int(row.get("matches_remaining") or 0)

    if role == "coach":
        return jsonify({"ok": True, "allowed": False, "reason": "coach_cannot_upload", "data": dict(row)})

    if remaining <= 0:
        return jsonify({"ok": True, "allowed": False, "reason": "insufficient_credits", "data": dict(row)})

    return jsonify({"ok": True, "allowed": True, "reason": None, "data": dict(row)})
