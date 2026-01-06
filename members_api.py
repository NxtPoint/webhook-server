#==================================
# members_api.py  (UPDATED)
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


def _member_to_dict(m: Member) -> Dict[str, Any]:
    return {
        "id": int(m.id),
        "account_id": int(m.account_id),
        "full_name": m.full_name,
        "is_primary": bool(m.is_primary),
        "role": m.role,
        "active": bool(m.active),
        "email": (getattr(m, "email", None) or None),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _require_email_arg() -> str:
    # used for GET calls
    email = _norm_email(request.args.get("email"))
    if not email:
        raise ValueError("email required")
    return email


@members_bp.get("/api/billing/members")
def list_members():
    """
    Authoritative list for Member Profile Section 2.
    Returns all members (primary + children) for the account identified by email.
    """
    try:
        email = _require_email_arg()

        with Session(engine) as session:
            acct = _find_account(session, email=email, external_wix_id=None)
            if acct is None:
                return jsonify({"ok": False, "error": "account_not_found"}), 404

            rows = session.execute(
                select(Member).where(Member.account_id == acct.id).order_by(
                    Member.is_primary.desc(), Member.created_at.asc()
                )
            ).scalars().all()

            return jsonify({
                "ok": True,
                "data": {
                    "account_id": int(acct.id),
                    "email": acct.email,
                    "external_wix_id": acct.external_wix_id,
                    "members": [_member_to_dict(m) for m in rows],
                }
            })

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"list_members_failed: {str(e)}"}), 500


@members_bp.post("/api/billing/member/upsert")
def upsert_member():
    """
    Create/update a NON-primary member (child).
    Body:
      {
        "email": "owner@email.com",
        "member_id": 123 (optional, for update),
        "full_name": "Child Name",
        "child_email": "child@email.com" (optional),
        "active": true|false (optional; default true)
      }

    Rules:
    - Cannot create/update primary via this endpoint.
    - Role forced to player_parent for children.
    - Member must belong to the account for the given email.
    """
    payload = request.get_json(silent=True) or {}

    try:
        owner_email = _norm_email(payload.get("email"))
        if not owner_email:
            return jsonify({"ok": False, "error": "email required"}), 400

        member_id_in = payload.get("member_id")
        member_id = int(member_id_in) if member_id_in not in (None, "", 0) else None

        full_name = (payload.get("full_name") or "").strip()
        if not full_name:
            return jsonify({"ok": False, "error": "full_name required"}), 400

        child_email = _norm_email(payload.get("child_email")) or None
        active = payload.get("active")
        if active is None:
            active = True
        active = bool(active)

        with Session(engine) as session:
            acct = _find_account(session, email=owner_email, external_wix_id=None)
            if acct is None:
                return jsonify({"ok": False, "error": "account_not_found"}), 404

            if member_id is None:
                # create new child
                m = Member(
                    account_id=acct.id,
                    full_name=full_name,
                    is_primary=False,
                    role="player_parent",
                    active=active,
                )
                if hasattr(Member, "email"):
                    setattr(m, "email", child_email)

                session.add(m)
                session.commit()
                session.refresh(m)

                return jsonify({"ok": True, "data": _member_to_dict(m)})

            # update existing
            m = session.execute(
                select(Member).where(Member.id == member_id)
            ).scalar_one_or_none()

            if m is None:
                return jsonify({"ok": False, "error": "member_not_found"}), 404

            if int(m.account_id) != int(acct.id):
                return jsonify({"ok": False, "error": "member_not_in_account"}), 403

            if bool(m.is_primary):
                return jsonify({"ok": False, "error": "cannot_update_primary_member"}), 400

            m.full_name = full_name
            m.active = active
            m.role = "player_parent"  # force

            if hasattr(Member, "email"):
                setattr(m, "email", child_email)

            session.commit()
            session.refresh(m)

            return jsonify({"ok": True, "data": _member_to_dict(m)})

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"upsert_failed: {str(e)}"}), 500


@members_bp.post("/api/billing/member/deactivate")
def deactivate_member():
    """
    Soft delete a child member (active=false).
    Body: { "email": "owner@email.com", "member_id": 123 }
    """
    payload = request.get_json(silent=True) or {}

    try:
        owner_email = _norm_email(payload.get("email"))
        if not owner_email:
            return jsonify({"ok": False, "error": "email required"}), 400

        member_id_in = payload.get("member_id")
        if member_id_in in (None, "", 0):
            return jsonify({"ok": False, "error": "member_id required"}), 400
        member_id = int(member_id_in)

        with Session(engine) as session:
            acct = _find_account(session, email=owner_email, external_wix_id=None)
            if acct is None:
                return jsonify({"ok": False, "error": "account_not_found"}), 404

            m = session.execute(
                select(Member).where(Member.id == member_id)
            ).scalar_one_or_none()

            if m is None:
                return jsonify({"ok": False, "error": "member_not_found"}), 404

            if int(m.account_id) != int(acct.id):
                return jsonify({"ok": False, "error": "member_not_in_account"}), 403

            if bool(m.is_primary):
                return jsonify({"ok": False, "error": "cannot_deactivate_primary_member"}), 400

            m.active = False
            session.commit()
            session.refresh(m)

            return jsonify({"ok": True, "data": _member_to_dict(m)})

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"deactivate_failed: {str(e)}"}), 500


@members_bp.post("/api/billing/sync_account")
def sync_account():
    """
    Bulk sync used by onboarding/backfill.
    NOTE: This deletes all members for the account and recreates them.
    """
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

                # child email support (ignored for primary)
                child_email = None
                if not is_primary:
                    child_email = _norm_email(m.get("child_email") or m.get("email")) or None

                snapshot.append({
                    "full_name": full_name,
                    "is_primary": is_primary,
                    "role": role,
                    "active": True,
                    "email": child_email,
                })

            if primary_count == 0:
                snapshot.insert(
                    0,
                    {"full_name": primary_full_name, "is_primary": True, "role": primary_role, "active": True, "email": None},
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
                mem = Member(
                    account_id=account_db.id,
                    full_name=m["full_name"],
                    is_primary=m["is_primary"],
                    role=m["role"],
                    active=m["active"],
                )
                if hasattr(Member, "email"):
                    setattr(mem, "email", m.get("email"))

                session.add(mem)

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
