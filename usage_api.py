#==================================
# usage_api.py  (NEW - FINAL)
#==================================

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account
from billing_service import grant_entitlement


usage_bp = Blueprint("usage_api", __name__)


# ----------------------------
# Helpers
# ----------------------------

def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _ops_key_ok() -> bool:
    ops_key = (os.getenv("BILLING_OPS_KEY") or os.getenv("OPS_KEY") or "").strip()
    if not ops_key:
        return False

    h = request.headers
    provided = (
        h.get("X-Ops-Key")
        or h.get("X-OPS-Key")
        or h.get("X-OPS-KEY")
        or h.get("x-ops-key")
        or h.get("x-OPS-key")
        or ""
    ).strip()

    return provided == ops_key


def _find_account(session: Session, *, email: str, external_wix_id: Optional[str]) -> Optional[Account]:
    if external_wix_id:
        acct = session.execute(
            select(Account).where(Account.external_wix_id == external_wix_id)
        ).scalar_one_or_none()
        if acct is not None:
            return acct

    return session.execute(
        select(Account).where(Account.email == email)
    ).scalar_one_or_none()


def _parse_dt(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ----------------------------
# Read endpoints
# ----------------------------

@usage_bp.get("/api/billing/summary")
def billing_summary():
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.begin() as conn:
        row = conn.execute(
            text("""
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
                WHERE a.email = :email
                LIMIT 1
            """),
            {"email": email},
        ).mappings().first()

    return jsonify({"ok": True, "data": dict(row) if row else None})


@usage_bp.get("/api/billing/entitlement/check")
def entitlement_check():
    """
    Read-only entitlement check for frontend.

    Rules:
      - Coaches can never upload
      - Must have remaining credits > 0
      - Must have ACTIVE subscription status (from billing.subscription_state)

    Safety:
      - If billing.subscription_state table is missing/unavailable, DENY (fail-closed).
    """
    email = _norm_email(request.args.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    with engine.begin() as conn:
        row = conn.execute(
            text("""
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
                LIMIT 1
            """),
            {"email": email},
        ).mappings().first()

        if not row:
            return jsonify({"ok": True, "allowed": False, "reason": "account_not_found", "data": None})

        role = str(row.get("role") or "player_parent").strip().lower()
        remaining = int(row.get("matches_remaining") or 0)
        account_id = int(row.get("account_id"))

        try:
            sub_row = conn.execute(
                text("""
                    SELECT COALESCE(status, 'NONE') AS subscription_status
                    FROM billing.subscription_state
                    WHERE account_id = :account_id
                    LIMIT 1
                """),
                {"account_id": account_id},
            ).mappings().first()
        except Exception as e:
            data = dict(row)
            data["subscription_status"] = None
            return jsonify({
                "ok": True,
                "allowed": False,
                "reason": "subscription_state_unavailable",
                "data": data,
                "detail": str(e),
            })

        subscription_status = (sub_row.get("subscription_status") if sub_row else "NONE")
        subscription_status = str(subscription_status or "NONE").strip().upper()

    data = dict(row)
    data["subscription_status"] = subscription_status

    if role == "coach":
        return jsonify({"ok": True, "allowed": False, "reason": "coach_cannot_upload", "data": data})

    if subscription_status != "ACTIVE":
        return jsonify({"ok": True, "allowed": False, "reason": "subscription_inactive", "data": data})

    if remaining <= 0:
        return jsonify({"ok": True, "allowed": False, "reason": "insufficient_credits", "data": data})

    return jsonify({"ok": True, "allowed": True, "reason": None, "data": data})


# ----------------------------
# Ops write endpoint
# ----------------------------

@usage_bp.post("/api/billing/entitlement/grant")
def entitlement_grant_endpoint():
    """
    Ops-protected credit grant. Called from Wix webhook handler.
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

    valid_from_dt = _parse_dt(payload.get("valid_from"))
    valid_to_dt = _parse_dt(payload.get("valid_to"))

    try:
        if matches_granted is None:
            return jsonify({"ok": False, "error": "matches_granted required"}), 400
        matches_granted = int(matches_granted)

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
