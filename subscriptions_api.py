#==================================
# subscriptions_api.py  (NEW - FINAL)
#==================================

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db_init import engine
from models_billing import Account
from billing_service import grant_entitlement, consume_matches_for_task


subscriptions_bp = Blueprint("subscriptions_api", __name__)


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


def _ym_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _event_id(payload: Dict[str, Any]) -> str:
    key = "|".join([
        str((payload.get("event_type") or "")).strip().upper(),
        str((payload.get("buyer_email") or "")).strip().lower(),
        str((payload.get("order_id") or "")).strip(),
        str((payload.get("plan_id") or "")).strip(),
        str((payload.get("status") or "")).strip().upper(),
        str(payload.get("plan_start") or ""),
        str(payload.get("plan_end") or ""),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _find_account(session: Session, *, email: str) -> Optional[Account]:
    return session.execute(select(Account).where(Account.email == email)).scalar_one_or_none()


# ----------------------------
# Endpoints
# ----------------------------

@subscriptions_bp.post("/api/billing/subscription/event")
def subscription_event():
    """
    Ops-protected subscription lifecycle update from Wix.
    Writes:
      - billing.subscription_event_log (idempotent via event_id)
      - billing.subscription_state (upsert + update)
    """
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    event_type = (payload.get("event_type") or "").strip().upper()
    buyer_email = _norm_email(payload.get("buyer_email"))
    status = (payload.get("status") or "").strip().upper()

    order_id = (payload.get("order_id") or "").strip() or None
    plan_id = (payload.get("plan_id") or "").strip() or None
    plan_name = (payload.get("plan_name") or "").strip() or None
    plan_code = (payload.get("plan_code") or "").strip() or None
    plan_type = (payload.get("plan_type") or "").strip().lower() or None
    matches_granted = payload.get("matches_granted")

    plan_start_dt = _parse_dt(payload.get("plan_start"))
    plan_end_dt = _parse_dt(payload.get("plan_end"))

    if not buyer_email:
        return jsonify({"ok": False, "error": "buyer_email required"}), 400
    if not event_type:
        return jsonify({"ok": False, "error": "event_type required"}), 400

    if plan_type is not None and plan_type not in ("recurring", "payg"):
        return jsonify({"ok": False, "error": "invalid plan_type"}), 400

    if matches_granted is not None:
        try:
            matches_granted = int(matches_granted)
        except Exception:
            return jsonify({"ok": False, "error": "matches_granted must be int"}), 400

    ev_id = _event_id(payload)

    with Session(engine) as session:
        acct = _find_account(session, email=buyer_email)
        if acct is None:
            return jsonify({"ok": False, "error": "account not found"}), 404

        account_id = int(acct.id)

        exists = session.execute(
            text("SELECT 1 FROM billing.subscription_event_log WHERE event_id = :event_id"),
            {"event_id": ev_id},
        ).first()
        if exists:
            return jsonify({"ok": True, "ignored": True, "reason": "duplicate_event", "event_id": ev_id})

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

        session.execute(
            text("""
                INSERT INTO billing.subscription_state (account_id)
                VALUES (:account_id)
                ON CONFLICT (account_id) DO NOTHING
            """),
            {"account_id": account_id},
        )

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

        now_utc = datetime.now(timezone.utc)
        if new_status == "ACTIVE" and plan_end_dt and plan_end_dt < now_utc:
            new_status = "EXPIRED"

        if new_status is None:
            session.commit()
            return jsonify({"ok": True, "stored": True, "state_changed": False, "event_id": ev_id})

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
                  cancelled_at = CASE
                    WHEN :status = 'ACTIVE' THEN NULL
                    ELSE COALESCE(:cancelled_at, cancelled_at)
                    END,
                  payment_cancelled_at = CASE
                    WHEN :status = 'ACTIVE' THEN NULL
                    ELSE COALESCE(:payment_cancelled_at, payment_cancelled_at)
                  END,
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


@subscriptions_bp.post("/api/billing/cron/monthly_refill")
def monthly_refill():
    """
    Ops-protected cron. Runs on the 1st.

    NO ROLLOVER RULE (locked):
      - allowance = subscription_state.matches_granted
      - after refill, matches_remaining == allowance

    Implementation:
      - if remaining < allowance: grant delta
      - if remaining > allowance: consume (expire) excess with deterministic task_id
      - log monthly_refill_log per account per YYYY-MM (idempotent)
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
                  s.matches_granted AS allowance,
                  s.current_period_end,
                  s.cancelled_at,
                  s.payment_cancelled_at
                FROM billing.subscription_state s
                WHERE s.status = 'ACTIVE'
                  AND s.plan_type = 'recurring'
                  AND s.cancelled_at IS NULL
                  AND s.payment_cancelled_at IS NULL
                  AND (s.current_period_end IS NULL OR s.current_period_end >= now())
            """)
        ).mappings().all()

    eligible = len(rows)
    processed = 0
    already = 0
    errors = 0
    missing = 0

    granted_delta_total = 0
    expired_total = 0
    details = []

    for r in rows:
        account_id = int(r["account_id"])
        plan_code = (r.get("plan_code") or "").strip()
        allowance = int(r.get("allowance") or 0)

        if not plan_code:
            missing += 1
            details.append({"account_id": account_id, "result": "skipped", "reason": "missing_plan_code"})
            continue
        if allowance <= 0:
            missing += 1
            details.append({"account_id": account_id, "result": "skipped", "reason": "missing_allowance"})
            continue

        try:
            with engine.begin() as conn:
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

                usage = conn.execute(
                    text("""
                        SELECT COALESCE(matches_remaining, 0) AS remaining
                        FROM billing.vw_customer_usage
                        WHERE account_id = :account_id
                    """),
                    {"account_id": account_id},
                ).mappings().first()

                remaining = int((usage or {}).get("remaining") or 0)

            delta_grant = 0
            delta_expire = 0
            if remaining < allowance:
                delta_grant = allowance - remaining
            elif remaining > allowance:
                delta_expire = remaining - allowance

            grant_id = None

            if delta_grant > 0:
                grant_id = grant_entitlement(
                    account_id=account_id,
                    source="wix_subscription",
                    plan_code=plan_code,
                    matches_granted=delta_grant,
                    external_wix_id=f"monthly_refill:{ym}:{account_id}",
                    valid_from=now_utc,
                    valid_to=None,
                    is_active=True,
                )
                granted_delta_total += delta_grant

            if delta_expire > 0:
                task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"monthly_expire:{ym}:{account_id}"))
                inserted = consume_matches_for_task(
                    account_id=account_id,
                    task_id=task_id,
                    consumed_matches=delta_expire,
                    source="monthly_expire",
                )
                if inserted:
                    expired_total += delta_expire

            with engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO billing.monthly_refill_log
                        (account_id, year_month, grant_id)
                        VALUES
                        (:account_id, :ym, :grant_id)
                    """),
                    {"account_id": account_id, "ym": ym, "grant_id": grant_id},
                )

            processed += 1
            details.append({
                "account_id": account_id,
                "result": "ok",
                "remaining_before": remaining,
                "allowance": allowance,
                "delta_grant": delta_grant,
                "delta_expire": delta_expire,
            })

        except Exception as e:
            errors += 1
            details.append({"account_id": account_id, "result": "error", "error": str(e)})

    return jsonify({
        "ok": True,
        "year_month": ym,
        "eligible": eligible,
        "processed": processed,
        "already": already,
        "missing": missing,
        "errors": errors,
        "granted_delta_total": granted_delta_total,
        "expired_total": expired_total,
        "details": details[:50],
    })
