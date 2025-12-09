#======================================================================= 
# billing_api.py
#=======================================================================


import os
from billing_import_from_bronze import sync_usage_from_submission_context

from flask import Blueprint, request, jsonify

from sqlalchemy.orm import Session, selectinload
from db_init import engine
from models_billing import PricingComponent, Account, Invoice, Member

from billing_service import (
    create_account_with_primary_member,
    add_member_to_account,
    record_video_usage,
    generate_invoice_for_period,
    get_month_period,
)

from datetime import datetime

OPS_KEY = os.environ.get("OPS_KEY")

billing_bp = Blueprint("billing", __name__, url_prefix="/api/billing")


def _error(message: str, status: int = 400):
    resp = jsonify({"ok": False, "error": message})
    resp.status_code = status
    return resp


@billing_bp.get("/debug/pricing")
def api_debug_pricing():
    """Debug endpoint: list all pricing components."""
    with Session(engine) as session:
        rows = session.query(PricingComponent).all()
        pricing = []
        for pc in rows:
            pricing.append(
                {
                    "code": pc.code,
                    "description": pc.description,
                    "billing_metric": pc.billing_metric,
                    "unit": pc.unit,
                    "currency_code": pc.currency_code,
                    "unit_amount": float(pc.unit_amount),
                    "active": pc.active,
                }
            )
    return jsonify({"ok": True, "pricing": pricing})


@billing_bp.post("/account")
def api_create_account():
    data = request.get_json(force=True) or {}
    email = data.get("email")
    name = data.get("primary_full_name")
    currency_code = data.get("currency_code", "USD")
    external_wix_id = data.get("external_wix_id")

    if not email or not name:
        return _error("email and primary_full_name are required", 400)

    account = create_account_with_primary_member(
        email=email,
        primary_full_name=name,
        currency_code=currency_code,
        external_wix_id=external_wix_id,
    )

    return jsonify(
        {
            "ok": True,
            "account": {
                "id": account.id,
                "email": account.email,
                "primary_full_name": account.primary_full_name,
                "currency_code": account.currency_code,
            },
        }
    )


@billing_bp.post("/account/<int:account_id>/members")
def api_add_member(account_id: int):
    data = request.get_json(force=True) or {}
    name = data.get("full_name")
    if not name:
        return _error("full_name is required", 400)

    member = add_member_to_account(account_id=account_id, full_name=name)

    return jsonify(
        {
            "ok": True,
            "member": {
                "id": member.id,
                "account_id": member.account_id,
                "full_name": member.full_name,
                "is_primary": member.is_primary,
            },
        }
    )


@billing_bp.get("/account/lookup")
def api_account_lookup():
    """
    Look up an account by email.

    GET /api/billing/account/lookup?email=someone@example.com
    """
    email = request.args.get("email")
    if not email:
        return _error("email query param is required", 400)

    with Session(engine) as session:
        acct = (
            session.query(Account)
            .filter(Account.email == email)
            .one_or_none()
        )

        if acct is None:
            return _error("account not found", 404)

        return jsonify(
            {
                "ok": True,
                "account": {
                    "id": acct.id,
                    "email": acct.email,
                    "primary_full_name": acct.primary_full_name,
                    "currency_code": acct.currency_code,
                },
            }
        )


@billing_bp.get("/account/members")
def api_account_members():
    """
    Debug: list members for an account by email.

    GET /api/billing/account/members?email=someone@example.com
    """
    email = request.args.get("email")
    if not email:
        return _error("email query param is required", 400)

    with Session(engine) as session:
        acct = (
            session.query(Account)
            .filter(Account.email == email)
            .one_or_none()
        )
        if acct is None:
            return _error("account not found", 404)

        members = (
            session.query(Member)
            .filter(Member.account_id == acct.id)
            .order_by(Member.id)
            .all()
        )

        members_out = [
            {
                "id": m.id,
                "full_name": m.full_name,
                "is_primary": m.is_primary,
            }
            for m in members
        ]

        return jsonify(
            {
                "ok": True,
                "account_id": acct.id,
                "email": acct.email,
                "members": members_out,
            }
        )


@billing_bp.post("/sync_account")
def api_sync_account():
    """
    Upsert an account + members from Wix.

    POST /api/billing/sync_account
    Payload:
    {
      "external_wix_id": "abc123",              # optional
      "email": "user@example.com",              # required
      "primary_full_name": "John Smith",        # required (fallback to email)
      "currency_code": "USD",                   # optional, default USD
      "members": [
        {"full_name": "John Smith", "is_primary": true},
        {"full_name": "Child A", "is_primary": false}
      ]
    }
    """
    data = request.get_json(force=True) or {}

    external_wix_id = data.get("external_wix_id")
    email = data.get("email")
    primary_full_name = data.get("primary_full_name")
    currency_code = data.get("currency_code", "USD")
    members_payload = data.get("members") or []

    if not email:
        return _error("email is required", 400)

    if not primary_full_name:
        primary_full_name = email  # fallback if missing

    with Session(engine) as session:
        try:
            # 1) Find existing account by external_wix_id or email
            acct = None
            if external_wix_id:
                acct = (
                    session.query(Account)
                    .filter(Account.external_wix_id == external_wix_id)
                    .one_or_none()
                )

            if acct is None:
                acct = (
                    session.query(Account)
                    .filter(Account.email == email)
                    .one_or_none()
                )

            # 2) Create if not found, else update
            if acct is None:
                acct = Account(
                    email=email,
                    primary_full_name=primary_full_name,
                    currency_code=currency_code,
                    external_wix_id=external_wix_id,
                    active=True,
                    created_at=datetime.utcnow(),
                )
                session.add(acct)
                session.flush()
            else:
                acct.email = email
                acct.primary_full_name = primary_full_name
                if external_wix_id:
                    acct.external_wix_id = external_wix_id
                if not acct.currency_code:
                    acct.currency_code = currency_code
                acct.active = True
                session.flush()

            # 3) Replace members snapshot for this account
            session.query(Member).filter(Member.account_id == acct.id).delete()

            created_members = []
            for m in members_payload:
                full_name = (m.get("full_name") or "").strip()
                if not full_name:
                    continue
                is_primary = bool(m.get("is_primary"))

                member = Member(
                    account_id=acct.id,
                    full_name=full_name,
                    is_primary=is_primary,
                    active=True,
                    created_at=datetime.utcnow(),
                )
                session.add(member)
                created_members.append(member)

            session.commit()

            members_out = [
                {
                    "id": mem.id,
                    "full_name": mem.full_name,
                    "is_primary": mem.is_primary,
                }
                for mem in created_members
            ]

            return jsonify(
                {
                    "ok": True,
                    "account": {
                        "id": acct.id,
                        "email": acct.email,
                        "primary_full_name": acct.primary_full_name,
                        "currency_code": acct.currency_code,
                        "external_wix_id": acct.external_wix_id,
                    },
                    "members": members_out,
                }
            )

        except Exception as e:
            session.rollback()
            return _error(f"{type(e).__name__}: {e}", 500)


@billing_bp.post("/usage/video")
def api_record_video_usage():
    data = request.get_json(force=True) or {}
    try:
        account_id = int(data["account_id"])
        member_id = data.get("member_id")
        if member_id is not None:
            member_id = int(member_id)
        video_minutes = float(data["video_minutes"])
        task_id = str(data["task_id"])
    except (KeyError, ValueError) as e:
        return _error(f"invalid payload: {e}", 400)

    usage = record_video_usage(
        account_id=account_id,
        member_id=member_id,
        video_minutes=video_minutes,
        task_id=task_id,
    )

    return jsonify(
        {
            "ok": True,
            "usage": {
                "id": usage.id,
                "account_id": usage.account_id,
                "member_id": usage.member_id,
                "task_id": usage.task_id,
                "video_minutes": float(usage.video_minutes),
                "billable_minutes": float(usage.billable_minutes),
            },
        }
    )


@billing_bp.post("/invoice/generate")
def api_generate_invoice():
    data = request.get_json(force=True) or {}
    try:
        account_id = int(data["account_id"])
        year = int(data["year"])
        month = int(data["month"])
    except (KeyError, ValueError) as e:
        return _error(f"{type(e).__name__}: {e}", 400)

    period_start, period_end = get_month_period(year, month)

    # First: generate or regenerate the invoice (writes rows and lines)
    try:
        generate_invoice_for_period(
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
        )
    except Exception as e:
        return _error(f"{type(e).__name__}: {e}", 400)

    # Second: reload invoice + lines in a fresh session
    with Session(engine) as session:
        invoice = (
            session.query(Invoice)
            .options(selectinload(Invoice.lines))
            .filter(
                Invoice.account_id == account_id,
                Invoice.period_start == period_start,
                Invoice.period_end == period_end,
            )
            .one()
        )

        lines_payload = []
        for line in invoice.lines:
            lines_payload.append(
                {
                    "id": line.id,
                    "pricing_component_code": line.pricing_component_code,
                    "description": line.description,
                    "quantity": float(line.quantity),
                    "unit_amount": float(line.unit_amount),
                    "line_amount": float(line.line_amount),
                }
            )

        return jsonify(
            {
                "ok": True,
                "invoice": {
                    "id": invoice.id,
                    "account_id": invoice.account_id,
                    "period_start": invoice.period_start.isoformat(),
                    "period_end": invoice.period_end.isoformat(),
                    "currency_code": invoice.currency_code,
                    "total_amount": float(invoice.total_amount),
                    "status": invoice.status,
                    "lines": lines_payload,
                },
            }
        )


@billing_bp.get("/invoices/monthly")
def api_list_invoices_monthly():
    """
    Export view: all invoices for a given month, per account.
    Example:
      GET /api/billing/invoices/monthly?year=2025&month=12
    """
    try:
        year = int(request.args.get("year", ""))
        month = int(request.args.get("month", ""))
    except ValueError:
        return _error("year and month query params are required and must be integers", 400)

    if not (1 <= month <= 12):
        return _error("month must be between 1 and 12", 400)

    period_start, period_end = get_month_period(year, month)

    with Session(engine) as session:
        rows = (
            session.query(Invoice, Account)
            .join(Account, Invoice.account_id == Account.id)
            .filter(
                Invoice.period_start == period_start,
                Invoice.period_end == period_end,
            )
            .all()
        )

        invoices_payload = []
        for inv, acc in rows:
            invoices_payload.append(
                {
                    "account_id": inv.account_id,
                    "email": acc.email,
                    "currency_code": inv.currency_code,
                    "period_start": inv.period_start.isoformat(),
                    "period_end": inv.period_end.isoformat(),
                    "total_amount": float(inv.total_amount),
                    "status": inv.status,
                }
            )

    return jsonify({"ok": True, "invoices": invoices_payload})


@billing_bp.post("/sync-usage-from-bronze")
def api_sync_usage_from_bronze():
    """
    Admin endpoint to pull completed SportAI submissions from bronze.submission_context
    into billing.usage_video.

    Protected by OPS_KEY via X-Ops-Key header.
    """
    header_key = request.headers.get("X-Ops-Key")
    if not OPS_KEY or header_key != OPS_KEY:
        return _error("unauthorized", 401)

    dry_run_param = request.args.get("dry_run", "true").lower()
    dry_run = dry_run_param in ("1", "true", "yes", "y")

    try:
        result = sync_usage_from_submission_context(dry_run=dry_run)
    except Exception as e:
        return _error(f"{type(e).__name__}: {e}", 400)

    return jsonify({"ok": True, "result": result})

from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy.orm import Session
from models_billing import Account, Member, PricingComponent

# reuse get_price from billing_service if you like; duplicated here for clarity
def _get_price(session: Session, code: str) -> PricingComponent:
    pc = (
        session.query(PricingComponent)
        .filter_by(code=code, active=True)
        .one_or_none()
    )
    if pc is None:
        raise ValueError(f"Pricing component '{code}' not found or inactive")
    return pc


@billing_bp.post("/summary")
def api_billing_summary():
    """
    POST /api/billing/summary
    Body: { "external_wix_id": "..." }

    Returns membership + pay-per-match pricing summary for this account.
    """
    data = request.get_json(force=True) or {}
    external_wix_id = (data.get("external_wix_id") or "").strip()

    if not external_wix_id:
        return _error("external_wix_id is required", 400)

    with Session(engine) as session:
        acct = (
            session.query(Account)
            .filter(Account.external_wix_id == external_wix_id)
            .one_or_none()
        )
        if acct is None:
            return _error("account not found for this external_wix_id", 404)

        # Active members count
        total_members = (
            session.query(Member)
            .filter(Member.account_id == acct.id, Member.active == True)
            .count()
        )
        extra_members = max(total_members - 1, 0)

        # Pricing components (USD codes for now)
        base_pc = _get_price(session, "MEMBERSHIP_BASE_USD")
        addon_pc = _get_price(session, "MEMBER_ADDON_USD")
        video_pc = _get_price(session, "VIDEO_ANALYSIS_USD")

        currency_code = base_pc.currency_code  # assume all aligned

        base_fee = Decimal(base_pc.unit_amount)
        extra_player_fee = Decimal(addon_pc.unit_amount)
        analysis_hour_fee = Decimal(video_pc.unit_amount)

        membership_monthly_total = base_fee + extra_player_fee * Decimal(extra_members)

        # Simple 15% discount on annual; adjust as needed
        annual_factor = Decimal("0.85")
        membership_annual_total = (
            membership_monthly_total * Decimal(12) * annual_factor
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Pay-per-match price: hardcode for now (can move to PricingComponent)
        payg_price_per_match = Decimal("20.00")

        summary = {
            "account_id": acct.id,
            "currency_code": currency_code,
            "base_fee": float(base_fee),
            "extra_player_fee": float(extra_player_fee),
            "extra_player_count": int(extra_members),
            "analysis_hour_fee": float(analysis_hour_fee),
            "membership_monthly_total": float(membership_monthly_total),
            "membership_annual_total": float(membership_annual_total),
            "payg_price_per_match": float(payg_price_per_match),
        }

        return jsonify({"ok": True, "summary": summary})

@billing_bp.post("/select-plan")
def api_select_plan():
    """
    POST /api/billing/select-plan

    Body:
    {
      "external_wix_id": "...",
      "billing_model": "MEMBERSHIP" | "PAYG_MATCH",
      "billing_period": "MONTHLY" | "ANNUAL" | null
    }

    For now, just validate and return ok=true, checkout_required=false.
    Later we can add PayPal integration and real plan persistence.
    """
    data = request.get_json(force=True) or {}

    external_wix_id = (data.get("external_wix_id") or "").strip()
    billing_model = (data.get("billing_model") or "").strip().upper()
    billing_period = (data.get("billing_period") or None)
    if isinstance(billing_period, str):
        billing_period = billing_period.upper()

    if not external_wix_id:
        return _error("external_wix_id is required", 400)
    if billing_model not in ("MEMBERSHIP", "PAYG_MATCH"):
        return _error("billing_model must be MEMBERSHIP or PAYG_MATCH", 400)
    if billing_model == "MEMBERSHIP" and billing_period not in ("MONTHLY", "ANNUAL"):
        return _error("billing_period must be MONTHLY or ANNUAL for MEMBERSHIP", 400)

    with Session(engine) as session:
        acct = (
            session.query(Account)
            .filter(Account.external_wix_id == external_wix_id)
            .one_or_none()
        )
        if acct is None:
            return _error("account not found for this external_wix_id", 404)

        # TODO: later add columns to Account for billing_model/billing_period
        # and persist them here, plus integrate with PayPal.

        # For now, no upfront checkout. We just confirm the choice.
        return jsonify(
            {
                "ok": True,
                "billing_model": billing_model,
                "billing_period": billing_period,
                "checkout_required": False,
            }
        )
