#==================================
# members_api.py  (NEW)
#==================================

from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account, Member
from billing_service import create_account_with_primary_member


members_bp = Blueprint("members_api", __name__)


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _validate_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role == "player":
        role = "player_parent"
    if role not in ("player_parent", "coach"):
        raise ValueError("invalid role")
    return role


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


@members_bp.post("/api/billing/sync_account")
def sync_account():
    payload = request.get_json(silent=True) or {}

    email = _norm_email(payload.get("email"))
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400

    primary_full_name = (payload.get("primary_full_name") or "").strip() or email
    currency_code = (payload.get("currency_code") or "USD").strip().upper() or "USD"
    external_wix_id = (payload.get("external_wix_id") or "").strip() or None

    members_in = payload.get("members") or []
    if not isinstance(members_in, list):
        return jsonify({"ok": False, "error": "members must be a list"}), 400

    try:
        primary_role = None
        for m in members_in:
            if isinstance(m, dict) and bool(m.get("is_primary")):
                primary_role = _validate_role(m.get("role") or "player_parent")
                break
        if primary_role is None:
            primary_role = _validate_role(payload.get("role") or "player_parent")

        _ = create_account_with_primary_member(
            email=email,
            primary_full_name=primary_full_name,
            currency_code=currency_code,
            external_wix_id=external_wix_id,
            role=primary_role,
        )

        with Session(engine) as session:
            account_db = _find_account(session, email=email, external_wix_id=external_wix_id)
            if account_db is None:
                return jsonify({"ok": False, "error": "account not found after create"}), 500

            if external_wix_id and not account_db.external_wix_id:
                account_db.external_wix_id = external_wix_id

            snapshot: List[Dict[str, Any]] = []
            primary_count = 0

            for m in members_in:
                if not isinstance(m, dict):
                    continue

                full_name = (m.get("full_name") or "").strip()
                if not full_name:
                    continue

                is_primary = bool(m.get("is_primary"))
                role_in = m.get("role") or ("player_parent" if not is_primary else primary_role)
                role = _validate_role(role_in)

                if is_primary:
                    primary_count += 1

                snapshot.append({"full_name": full_name, "is_primary": is_primary, "role": role, "active": True})

            if primary_count == 0:
                snapshot.insert(
                    0,
                    {"full_name": primary_full_name, "is_primary": True, "role": primary_role, "active": True},
                )
            elif primary_count > 1:
                return jsonify({"ok": False, "error": "only one primary member allowed"}), 400

            if not snapshot:
                return jsonify({"ok": False, "error": "no valid members in payload"}), 400

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
