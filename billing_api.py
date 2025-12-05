# billing_api.py
from flask import Blueprint, request, jsonify

from billing_service import (
    create_account_with_primary_member,
    add_member_to_account,
    record_video_usage,
    generate_invoice_for_period,
    get_month_period,
)

billing_bp = Blueprint("billing", __name__, url_prefix="/api/billing")


def _error(message: str, status: int = 400):
    resp = jsonify({"ok": False, "error": message})
    resp.status_code = status
    return resp


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
        return _error(f"invalid payload: {e}", 400)

    period_start, period_end = get_month_period(year, month)

    try:
        invoice = generate_invoice_for_period(
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
        )
    except ValueError as e:
        return _error(str(e), 400)

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
