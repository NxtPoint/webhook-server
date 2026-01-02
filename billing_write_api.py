#==================================
# billing_write_api.py  (FINAL BASELINE)
#==================================

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import select, text
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


def _parse_dt(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _ym_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _event_id(payload: Dict[str, Any]) -> str:
    """
    Deterministic id for idempotency. Do NOT rely on Wix providing an id.
    """
    key = "|".join([
        str(payload.get("event_type") or ""),
        str(payload.get("buyer_email") or ""),
        str(payload.get("order_id") or ""),
        str(payload.get("plan_id") or ""),
        str(payload.get("status") or ""),
        str(payload.get("plan_start") or ""),
        str(payload.get("plan_end") or ""),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


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
        # -------- Query 1: account + role + remaining credits --------
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
        account_id = int(row.get("account_id"))

        # -------- Query 2: subscription status (split query; no joins) --------
        try:
            sub_row = conn.execute(
                text(
                    """
                    SELECT COALESCE(status, 'NONE') AS subscription_status
                    FROM billing.subscription_state
                    WHERE account_id = :account_id
                    """
                ),
                {"account_id": account_id},
            ).mappings().first()
        except Exception as e:
            data = dict(row)
            data["subscription_status"] = None
            return jsonify(
                {
                    "ok": True,
                    "allowed": False,
                    "reason": "subscription_state_unavailable",
                    "data": data,
                    "detail": str(e),
                }
            )

        subscription_status = (sub_row.get("subscription_status") if sub_row else "NONE")
        subscription_status = str(subscription_status or "NONE").strip().upper()

    data = dict(row)
    data["subscription_status"] = subscription_status

    # -------- Policy checks (Python-only logic) --------
    if role == "coach":
        return jsonify({"ok": True, "allowed": False, "reason": "coach_cannot_upload", "data": data})

    if subscription_status != "ACTIVE":
        return jsonify({"ok": True, "allowed": False, "reason": "subscription_inactive", "data": data})

    if remaining <= 0:
        return jsonify({"ok": True, "allowed": False, "reason": "insufficient_credits", "data": data})

    return jsonify({"ok": True, "allowed": True, "reason": None, "data": data})


@billing_write_bp.post("/api/billing/subscription/event")
def subscription_event():
    """
    Ops-protected subscription lifecycle update from Wix.
    Stores subscription status and plan window.

    Expected payload (canonical keys; null allowed):
      event_type, buyer_email, status, order_id, plan_id, plan_name,
      plan_start, plan_end, plan_code, plan_type, matches_granted
    """
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    event_type = (payload.get("event_type") or "").strip().upper()
    buyer_email = _norm_email(payload.get("buyer_email"))
    status = (payload.get("status") or "").strip().upper()

    order_id = (payload.get("order_id") or "").strip() or None
    plan_id = (payload.get("plan_id") or "").strip() or None
    plan_name = (payload.get("plan_name") or "").strip() or None  # stored only in event log
    plan_code = (payload.get("plan_code") or "").strip() or None
    plan_type = (payload.get("plan_type") or "").strip().lower() or None
    matches_granted = payload.get("matches_granted")

    plan_start_dt = _parse_dt(payload.get("plan_start"))
    plan_end_dt = _parse_dt(payload.get("plan_end"))

    if not buyer_email:
        return jsonify({"ok": False, "error": "buyer_email required"}), 400
    if not event_type:
        return jsonify({"ok": False, "error": "event_type required"}), 400

    # Normalize plan_type
    if plan_type is not None and plan_type not in ("recurring", "payg"):
        return jsonify({"ok": False, "error": "invalid plan_type"}), 400

    if matches_granted is not None:
        try:
            matches_granted = int(matches_granted)
        except Exception:
            return jsonify({"ok": False, "error": "matches_granted must be int"}), 400

    ev_id = _event_id(payload)

    with Session(engine) as session:
        acct = _find_account(session, email=buyer_email, external_wix_id=None)
        if acct is None:
            return jsonify({"ok": False, "error": "account not found"}), 404

        account_id = int(acct.id)

        # Idempotency: if event_id exists, ignore
        exists = session.execute(
            text("SELECT 1 FROM billing.subscription_event_log WHERE event_id = :event_id"),
            {"event_id": ev_id},
        ).first()
        if exists:
            return jsonify({"ok": True, "ignored": True, "reason": "duplicate_event", "event_id": ev_id})

        # Log event (audit)
        session.execute(
            text("""
                INSERT INTO billing.subscription_event_log (event_id, account_id, event_type, payload)
                VALUES (:event_id, :account_id, :event_type, CAST(:payload AS jsonb))
            """),
            {
                "event_id": ev_id,
                "account_id": account_id,
                "event_type": event_type,
                "payload": json.dumps({**payload, "plan_name": plan_name}),
            },
        )

        # Ensure subscription_state exists
        session.execute(
            text("""
                INSERT INTO billing.subscription_state (account_id)
                VALUES (:account_id)
                ON CONFLICT (account_id) DO NOTHING
            """),
            {"account_id": account_id},
        )

        # Locked transitions
        new_status: Optional[str] = None
        cancelled_at = None
        payment_cancelled_at = None

        if event_type == "PLAN_PURCHASED" and status == "ACTIVE":
            new_status = "ACTIVE"
        elif event_type == "PLAN_CANCELLED":
            new_status = "CANCELLED"
            cancelled_at = datetime.now(timezone.utc)
        elif event_type == "RECURRING_PAYMENT_CANCELLED":
            new_status = "CANCELLED"
            payment_cancelled_at = datetime.now(timezone.utc)

        # If active but end is in the past, mark EXPIRED
        now_utc = datetime.now(timezone.utc)
        if new_status == "ACTIVE" and plan_end_dt and plan_end_dt < now_utc:
            new_status = "EXPIRED"

        if new_status is None:
            session.commit()
            return jsonify({"ok": True, "stored": True, "state_changed": False, "event_id": ev_id})

        # Update state (matches_granted is stored for deterministic monthly refill)
        session.execute(
            text("""
                UPDATE billing.subscription_state
                SET
                  plan_id = COALESCE(:plan_id, plan_id),
                  plan_code = COALESCE(:plan_code, plan_code),
                  plan_type = COALESCE(:plan_type, plan_type),
                  matches_granted = COALESCE(:matches_granted, matches_granted),
                  status = :status,
                  current_period_start = COALESCE(:start_dt, current_period_start),
                  current_period_end = COALESCE(:end_dt, current_period_end),
                  cancelled_at = COALESCE(:cancelled_at, cancelled_at),
                  payment_cancelled_at = COALESCE(:payment_cancelled_at, payment_cancelled_at),
                  updated_at = now()
                WHERE account_id = :account_id
            """),
            {
                "account_id": account_id,
                "plan_id": plan_id,
                "plan_code": plan_code,
                "plan_type": plan_type,
                "matches_granted": matches_granted,
                "status": new_status,
                "start_dt": plan_start_dt,
                "end_dt": plan_end_dt,
                "cancelled_at": cancelled_at,
                "payment_cancelled_at": payment_cancelled_at,
            },
        )

        session.commit()
        return jsonify({
            "ok": True,
            "stored": True,
            "state_changed": True,
            "event_id": ev_id,
            "account_id": account_id,
            "status": new_status,
        })


@billing_write_bp.post("/api/billing/cron/monthly_refill")
def monthly_refill():
    """
    Ops-protected cron. Grants recurring plan credits on the 1st.
    Idempotent per account per YYYY-MM.
    """
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force", False))

    now_utc = datetime.now(timezone.utc)
    if not force and now_utc.day != 1:
        return jsonify({"ok": True, "skipped": True, "reason": "not_first_of_month", "today": now_utc.isoformat()})

    ym = _ym_key(now_utc)

    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT
                  s.account_id,
                  s.plan_code,
                  s.plan_type,
                  s.matches_granted,
                  s.current_period_start,
                  s.current_period_end
                FROM billing.subscription_state s
                WHERE s.status = 'ACTIVE'
                  AND s.plan_type = 'recurring'
                  AND (s.current_period_end IS NULL OR s.current_period_end >= now())
            """)
        ).mappings().all()

    granted = 0
    already = 0
    missing_plan = 0
    errors = 0
    details = []

    for r in rows:
        account_id = int(r["account_id"])
        plan_code = (r.get("plan_code") or "").strip()
        matches_granted = int(r.get("matches_granted") or 0)

        if not plan_code:
            missing_plan += 1
            details.append({"account_id": account_id, "result": "skipped", "reason": "missing_plan_code"})
            continue

        if matches_granted <= 0:
            missing_plan += 1
            details.append({"account_id": account_id, "result": "skipped", "reason": "missing_matches_granted"})
            continue

        try:
            with engine.begin() as conn:
                # Idempotency lock for this account + year_month
                exists = conn.execute(
                    text("""
                        SELECT 1 FROM billing.monthly_refill_log
                        WHERE account_id = :account_id AND year_month = :ym
                    """),
                    {"account_id": account_id, "ym": ym},
                ).first()

                if exists:
                    already += 1
                    continue

                # Grant
                grant_id = grant_entitlement(
                    account_id=account_id,
                    source="wix_subscription",
                    plan_code=plan_code,
                    matches_granted=matches_granted,
                    external_wix_id=f"monthly_refill:{ym}",
                    valid_from=now_utc,
                    valid_to=None,
                    is_active=True,
                )

                # Log refill (idempotent key)
                conn.execute(
                    text("""
                        INSERT INTO billing.monthly_refill_log (account_id, year_month, grant_id)
                        VALUES (:account_id, :ym, :grant_id)
                    """),
                    {"account_id": account_id, "ym": ym, "grant_id": grant_id},
                )

            granted += 1

        except Exception as e:
            errors += 1
            details.append({"account_id": account_id, "result": "error", "error": str(e)})

    return jsonify({
        "ok": True,
        "year_month": ym,
        "eligible": len(rows),
        "granted": granted,
        "already": already,
        "missing_plan": missing_plan,
        "errors": errors,
        "details": details[:50],
    })
