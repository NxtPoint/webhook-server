# paypal_billing/webhook.py — PayPal webhook receiver + secure checkout endpoints.
#
# Vanilla PayPal, server-side and security-first:
#   - Subscriptions + PAYG Orders are CREATED server-side so the plan/amount/custom_id
#     are set by us, never the browser.
#   - The webhook is authenticated by PayPal's own verify-webhook-signature API, then it
#     RE-FETCHES the subscription/order from PayPal (never trusts the webhook body for
#     money decisions) before mapping to the shared grant path.
#   - All grants go through subscriptions_api.apply_subscription_event(provider='paypal'),
#     idempotent by PayPal resource id. Touches billing.* only.
#
# Grant model (PayPal-native): credits are granted when money is RECEIVED —
#   recurring : on PAYMENT.SALE.COMPLETED (first + every renewal), valid_to = next
#               billing date (unused credits expire each cycle = no rollover).
#   PAYG      : on order capture (instant, via /capture-order) + PAYMENT.CAPTURE.COMPLETED
#               as an idempotent backstop. PAYG credits never expire.
#   ACTIVATED/CANCELLED/EXPIRED only move subscription_state (no grant).
#
# Routes (paypal_bp, registered only when PAYPAL_ENABLED=1):
#   POST /api/billing/paypal/create-subscription  (client-key auth) -> {id}
#   POST /api/billing/paypal/create-order         (client-key auth) -> {id}
#   POST /api/billing/paypal/capture-order        (client-key auth) -> grant now
#   POST /api/billing/paypal/cancel-subscription  (client-key auth) -> cancel at PayPal
#   POST /api/billing/paypal/webhook              (PayPal signature auth)
# Always-on (register_always, even when dark so the frontend can detect on/off):
#   GET  /api/billing/paypal/config

from __future__ import annotations

import os

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text

from paypal_billing import client, plans
from subscriptions_api import apply_subscription_event
# Reuse the client API's dual-mode auth so PayPal checkout works whether the portal
# forwards a per-user Clerk JWT (auth_v2) or the legacy shared key.
from client_api import _guard as _client_guard, _client_email

paypal_bp = Blueprint("paypal_billing", __name__)


# ── auth + small helpers ─────────────────────────────────────────────────────
# Checkout endpoints authenticate with the SAME dual-mode guard as the rest of the
# client API (client_api._guard): a per-user Clerk JWT (auth_v2) OR the legacy
# X-Client-Key. The buyer email comes from client_api._client_email — derived
# SERVER-SIDE from the verified token under JWT (a spoofed ?email is ignored), or
# ?email under the legacy key. So PayPal works regardless of which auth the portal
# forwards, and binds the purchase to the authenticated account either way.

def _currency() -> str:
    return (plans.load_catalog().get("currency") or plans.CURRENCY)


def _enabled() -> bool:
    return (os.getenv("PAYPAL_ENABLED") or "").strip() in ("1", "true", "True", "yes")


# ── GET /config (public probe) ───────────────────────────────────────────────

def _config_payload() -> dict:
    client_id = (os.getenv("PAYPAL_CLIENT_ID") or "").strip()
    plan_map = plans.load_catalog().get("plans") or {}
    recurring = [{
        "code": p["code"], "name": p["name"], "matches": p["matches"],
        "plan_id": (plan_map.get(p["code"]) or {}).get("plan_id"),
        "amount": (plan_map.get(p["code"]) or {}).get("price", plans.price_of(p["code"])),
    } for p in plans.recurring_plans()]
    payg = [{
        "code": p["code"], "name": p["name"], "matches": p["matches"],
        "amount": plans.price_of(p["code"]),
    } for p in plans.payg_packs()]
    return {
        "enabled": bool(_enabled() and client_id),
        "env": client._env(),
        "client_id": client_id if _enabled() else "",
        "currency": _currency(),
        "recurring": recurring,
        "payg": payg,
    }


def _config_view():
    return jsonify(_config_payload())


def register_always(app) -> None:
    """Register the public config probe even when PayPal is dark, so frontend can detect
    on/off and fall back to Wix. Idempotent."""
    if "paypal_config" in app.view_functions:
        return
    app.add_url_rule("/api/billing/paypal/config", "paypal_config", _config_view, methods=["GET"])


# ── checkout: server-side create (plan/amount/custom_id set by us) ───────────

@paypal_bp.post("/api/billing/paypal/create-subscription")
def create_subscription():
    if not _client_guard():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    email = _client_email()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    code = ((request.get_json(silent=True) or {}).get("plan_code") or "").strip()
    plan = plans.by_code(code)
    if not plan or plan["plan_type"] != "recurring":
        return jsonify({"ok": False, "error": "unknown recurring plan"}), 400
    plan_id = ((plans.load_catalog().get("plans") or {}).get(code) or {}).get("plan_id")
    if not plan_id:
        return jsonify({"ok": False, "error": "plan not in catalog — run catalog.py"}), 503
    try:
        sub = client.create_subscription(plan_id=plan_id, custom_id=email)
    except client.PayPalError as e:
        current_app.logger.error("paypal create-subscription failed: %s", e)
        return jsonify({"ok": False, "error": "paypal_error"}), 502
    return jsonify({"ok": True, "id": sub["id"]})


@paypal_bp.post("/api/billing/paypal/create-order")
def create_order():
    if not _client_guard():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    email = _client_email()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    code = ((request.get_json(silent=True) or {}).get("plan_code") or "").strip()
    plan = plans.by_code(code)
    if not plan or plan["plan_type"] != "payg":
        return jsonify({"ok": False, "error": "unknown PAYG pack"}), 400
    amount = plans.price_of(code)
    if amount is None:
        return jsonify({"ok": False, "error": "pack price not set"}), 503
    try:
        # custom_id = "email|plan_code" — set server-side, so the webhook can trust it.
        order = client.create_order(
            amount=amount, currency=_currency(), custom_id=f"{email}|{code}",
            description=f"{plan['name']} ({plan['matches']} match credits)",
        )
    except client.PayPalError as e:
        current_app.logger.error("paypal create-order failed: %s", e)
        return jsonify({"ok": False, "error": "paypal_error"}), 502
    return jsonify({"ok": True, "id": order["id"]})


@paypal_bp.post("/api/billing/paypal/capture-order")
def capture_order():
    if not _client_guard():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not _client_email():
        return jsonify({"ok": False, "error": "email required"}), 400
    order_id = ((request.get_json(silent=True) or {}).get("order_id") or "").strip()
    if not order_id:
        return jsonify({"ok": False, "error": "order_id required"}), 400
    try:
        result = client.capture_order(order_id)
    except client.PayPalError as e:
        current_app.logger.error("paypal capture-order failed: %s", e)
        return jsonify({"ok": False, "error": "paypal_error"}), 502
    # Grant immediately for snappy UX; the PAYMENT.CAPTURE.COMPLETED webhook is an
    # idempotent backstop (same order_id -> same grant, no double-credit).
    norm = _normalize_payg_from_order(result)
    if norm and norm.get("buyer_email"):
        out, status = apply_subscription_event(norm, provider="paypal")
        return jsonify({"ok": status == 200, "capture": result.get("status"), "grant": out}), \
            (200 if status == 200 else status)
    return jsonify({"ok": True, "capture": result.get("status"), "granted": False})


@paypal_bp.post("/api/billing/paypal/cancel-subscription")
def cancel_subscription_route():
    if not _client_guard():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    email = _client_email()
    if not email:
        return jsonify({"ok": False, "error": "email required"}), 400
    sub_id = _lookup_paypal_subscription_id(email)
    if not sub_id:
        return jsonify({"ok": False, "error": "no active PayPal subscription"}), 404
    try:
        client.cancel_subscription(sub_id)
    except client.PayPalError as e:
        current_app.logger.error("paypal cancel failed: %s", e)
        return jsonify({"ok": False, "error": "paypal_error"}), 502
    # BILLING.SUBSCRIPTION.CANCELLED webhook finalizes subscription_state.
    return jsonify({"ok": True, "subscription_id": sub_id})


def _lookup_paypal_subscription_id(email: str):
    from db_init import engine
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT s.provider_subscription_id
            FROM billing.subscription_state s
            JOIN billing.account a ON a.id = s.account_id
            WHERE lower(a.email) = :email
              AND s.billing_provider = 'paypal'
              AND s.provider_subscription_id IS NOT NULL
              AND s.status = 'ACTIVE'
            ORDER BY s.updated_at DESC NULLS LAST
            LIMIT 1
        """), {"email": email.lower()}).first()
    return row[0] if row else None


# ── POST /webhook (PayPal -> us; signature-verified, refetch, grant) ─────────

@paypal_bp.post("/api/billing/paypal/webhook")
def webhook():
    event = request.get_json(silent=True) or {}
    h = request.headers
    try:
        verified = client.verify_webhook_signature(
            transmission_id=h.get("Paypal-Transmission-Id", ""),
            transmission_time=h.get("Paypal-Transmission-Time", ""),
            cert_url=h.get("Paypal-Cert-Url", ""),
            auth_algo=h.get("Paypal-Auth-Algo", ""),
            transmission_sig=h.get("Paypal-Transmission-Sig", ""),
            webhook_event=event,
        )
    except Exception:
        current_app.logger.exception("paypal webhook verify error")
        return jsonify({"ok": False, "error": "verify_error"}), 500
    if not verified:
        return jsonify({"ok": False, "error": "invalid_signature"}), 400

    etype = (event.get("event_type") or "").upper()
    resource = event.get("resource") or {}
    try:
        norm = _normalize_event(etype, resource)
    except Exception:
        current_app.logger.exception("paypal webhook normalize error (%s)", etype)
        return jsonify({"ok": False, "error": "normalize_error"}), 500

    # Record the money movement (record-only, idempotent, never raises). Independent of
    # the grant path: sale/capture are recorded as positive, refund/reversal as negative,
    # and refunds DO NOT revoke credits (business decision 2026-06-18).
    try:
        _pay = _extract_payment(etype, resource, norm)
        if _pay and _pay.get("provider_payment_id"):
            from billing_service import record_payment
            # record_payment returns True only when a NEW row is inserted, so the ops
            # alert fires once even if PayPal retries the webhook.
            if record_payment(**_pay):
                _alert_money_event(_pay)
    except Exception:
        current_app.logger.exception("paypal payment record failed (%s)", etype)

    if not norm:
        return jsonify({"ok": True, "ignored": True, "event_type": etype})
    if not norm.get("buyer_email"):
        return jsonify({"ok": True, "skipped": "no_email", "event_type": etype})

    # Churn alert — a subscription was cancelled/expired/suspended.
    if norm.get("event_type") == "PLAN_CANCELLED":
        _alert_subscription_cancelled(norm)

    out, status = apply_subscription_event(norm, provider="paypal")
    # Account-not-found must not make PayPal retry forever — accept + skip.
    if status == 404:
        return jsonify({"ok": True, "skipped": "account_not_found"}), 200
    return jsonify(out), (200 if status == 200 else status)


# ── ops alerts (best-effort; never break the webhook) ────────────────────────

def _fmt_money(cents, currency) -> str:
    if cents is None:
        return "(unknown amount)"
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get((currency or "").upper(), (currency or "") + " ")
    return f"{sym}{abs(cents) / 100:.2f}"


def _alert_money_event(pay: dict) -> None:
    """Ops email on a newly-recorded money movement — payment in (the fun one) or refund out."""
    try:
        from coach_invite.video_complete_email import send_ops_email
        amt = _fmt_money(pay.get("amount_cents"), pay.get("currency"))
        kind = pay.get("kind")
        if kind in ("refund", "reversal"):
            send_ops_email(
                f"↩️ Refund processed: -{amt}",
                f"A {kind} was processed.\n\nAmount:  -{amt}\n"
                f"Payment: {pay.get('provider_payment_id')}\nEvent:   {pay.get('event_type')}",
            )
        else:
            label = "Subscription payment" if kind == "subscription_payment" else "PAYG top-up"
            send_ops_email(
                f"💰 Payment received: {amt} ({pay.get('plan_code') or '—'})",
                "Money just came in! 🎾💸\n\n"
                f"Amount:   {amt}\nType:     {label}\n"
                f"Customer: {pay.get('buyer_email') or '—'}\n"
                f"Plan:     {pay.get('plan_code') or '—'}\n"
                f"Payment:  {pay.get('provider_payment_id')}",
            )
    except Exception:
        current_app.logger.exception("paypal money alert failed")


def _alert_subscription_cancelled(norm: dict) -> None:
    """Ops email when a subscription is cancelled/expired/suspended (churn signal)."""
    try:
        from coach_invite.video_complete_email import send_ops_email
        send_ops_email(
            f"⚠️ Subscription cancelled: {norm.get('buyer_email') or '—'}",
            "A subscription was cancelled / expired.\n\n"
            f"Customer: {norm.get('buyer_email') or '—'}\n"
            f"Plan:     {norm.get('plan_code') or '—'}\n"
            f"Sub ID:   {norm.get('provider_subscription_id') or '—'}",
        )
    except Exception:
        current_app.logger.exception("paypal cancel alert failed")


# ── event normalization (always re-fetches authoritative state from PayPal) ──

def _email_from_custom(custom_id: str) -> str:
    cid = (custom_id or "").strip()
    if "|" in cid:          # PAYG custom_id is "email|plan_code"
        cid = cid.split("|", 1)[0]
    return cid.lower()


def _normalize_event(etype: str, resource: dict):
    if etype.startswith("BILLING.SUBSCRIPTION."):
        return _normalize_subscription(etype, resource)
    if etype == "PAYMENT.SALE.COMPLETED":
        return _normalize_sale(resource)
    if etype == "PAYMENT.CAPTURE.COMPLETED":
        return _normalize_capture(resource)
    return None


# ── payment recording (record-only; independent of the grant path) ───────────

def _money_cents(amount: dict):
    """Parse a PayPal amount object — v1 ({total,currency}) or v2 ({value,currency_code})
    — into (integer cents, currency). Returns (None, currency) if unparseable."""
    if not isinstance(amount, dict):
        return None, None
    val = amount.get("total") if amount.get("total") is not None else amount.get("value")
    cur = amount.get("currency") or amount.get("currency_code")
    try:
        return int(round(float(val) * 100)), cur
    except (TypeError, ValueError):
        return None, cur


def _extract_payment(etype: str, resource: dict, norm):
    """Build a billing_service.record_payment kwargs dict for a money event, or None.
    sale/capture -> positive amount (email/plan from the grant `norm`); refund/reversal ->
    negative amount (recorded for revenue/360 accuracy; credits unaffected)."""
    etype = (etype or "").upper()
    amt, cur = _money_cents(resource.get("amount") or {})
    occurred = resource.get("create_time") or resource.get("update_time")
    pid = resource.get("id")
    if etype == "PAYMENT.SALE.COMPLETED":
        if not (norm and norm.get("plan_type") == "recurring"):
            return None
        return dict(kind="subscription_payment", provider_payment_id=pid,
                    provider_subscription_id=norm.get("provider_subscription_id"),
                    order_id=norm.get("order_id"), buyer_email=norm.get("buyer_email"),
                    plan_code=norm.get("plan_code"), amount_cents=amt, currency=cur,
                    status=resource.get("state"), event_type=etype, occurred_at=occurred,
                    raw=resource)
    if etype == "PAYMENT.CAPTURE.COMPLETED":
        if not norm:
            return None
        return dict(kind="payg_capture", provider_payment_id=pid,
                    order_id=norm.get("order_id"), buyer_email=norm.get("buyer_email"),
                    plan_code=norm.get("plan_code"), amount_cents=amt, currency=cur,
                    status=resource.get("status"), event_type=etype, occurred_at=occurred,
                    raw=resource)
    if etype in ("PAYMENT.SALE.REFUNDED", "PAYMENT.CAPTURE.REFUNDED"):
        return dict(kind="refund", provider_payment_id=pid,
                    order_id=resource.get("sale_id"),
                    amount_cents=(-abs(amt) if amt is not None else None), currency=cur,
                    status=resource.get("state") or resource.get("status"),
                    event_type=etype, occurred_at=occurred, raw=resource)
    if etype == "PAYMENT.CAPTURE.REVERSED":
        return dict(kind="reversal", provider_payment_id=pid,
                    amount_cents=(-abs(amt) if amt is not None else None), currency=cur,
                    status=resource.get("status"), event_type=etype, occurred_at=occurred,
                    raw=resource)
    return None


def _normalize_subscription(etype: str, resource: dict):
    sub_id = resource.get("id")
    sub = client.get_subscription(sub_id) if sub_id else resource
    email = _email_from_custom(sub.get("custom_id")) or \
        (sub.get("subscriber") or {}).get("email_address", "").strip().lower()
    plan_id = sub.get("plan_id")
    meta = plans.paypal_plan_id_map().get(plan_id or "", {})
    base = {
        "buyer_email": email,
        "plan_id": plan_id,
        "plan_code": meta.get("code"),
        "plan_type": "recurring",
        "order_id": sub_id,
        "provider_subscription_id": sub_id,
    }
    if etype == "BILLING.SUBSCRIPTION.ACTIVATED":
        # State only — credits are granted on PAYMENT.SALE.COMPLETED (money received).
        billing_info = sub.get("billing_info") or {}
        return {**base, "event_type": "PLAN_PURCHASED", "status": "ACTIVE",
                "matches_granted": 0,
                "plan_start": sub.get("start_time"),
                "plan_end": billing_info.get("next_billing_time")}
    if etype in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED",
                 "BILLING.SUBSCRIPTION.SUSPENDED"):
        return {**base, "event_type": "PLAN_CANCELLED", "status": "CANCELLED"}
    return None


def _normalize_sale(resource: dict):
    # v1 sale resource for a recurring payment carries billing_agreement_id (= subscription).
    sub_id = resource.get("billing_agreement_id")
    if not sub_id:
        return None  # not a subscription payment — ignore
    sub = client.get_subscription(sub_id)
    email = _email_from_custom(sub.get("custom_id")) or \
        (sub.get("subscriber") or {}).get("email_address", "").strip().lower()
    plan_id = sub.get("plan_id")
    meta = plans.paypal_plan_id_map().get(plan_id or "", {})
    billing_info = sub.get("billing_info") or {}
    return {
        "event_type": "PLAN_PURCHASED",
        "status": "ACTIVE",
        "buyer_email": email,
        "plan_id": plan_id,
        "plan_code": meta.get("code"),
        "plan_type": "recurring",
        "matches_granted": meta.get("matches", 0),
        "order_id": resource.get("id"),                     # sale id — unique per payment
        "provider_subscription_id": sub_id,
        "plan_start": resource.get("create_time"),
        "plan_end": billing_info.get("next_billing_time"),  # valid_to -> expire at cycle end
    }


def _normalize_capture(resource: dict):
    order_id = (((resource.get("supplementary_data") or {}).get("related_ids") or {})
                .get("order_id"))
    if not order_id:
        return None
    return _normalize_payg_from_order(client.get_order(order_id))


def _normalize_payg_from_order(order: dict):
    units = order.get("purchase_units") or []
    if not units:
        return None
    pu = units[0]
    custom = pu.get("custom_id") or ""
    email, _, code = custom.partition("|")
    plan = plans.by_code(code)
    if not plan or plan["plan_type"] != "payg":
        return None
    return {
        "event_type": "PLAN_PURCHASED",
        "status": "ACTIVE",
        "buyer_email": (email or "").strip().lower(),
        "plan_code": code,
        "plan_type": "payg",
        "matches_granted": plan["matches"],
        "order_id": order.get("id"),
        "plan_start": None,
        "plan_end": None,   # PAYG credits never expire
    }
