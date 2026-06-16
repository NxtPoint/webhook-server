# paypal_billing/client.py — thin PayPal REST v2/v1 client.
#
# Vanilla PayPal, server-side. No card data ever touches us. Credentials come from env:
#   PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_ENV (sandbox|live), PAYPAL_WEBHOOK_ID.
#
# Covers exactly what the integration needs:
#   - OAuth2 client-credentials token (short-lived, cached)
#   - Catalog: create_product / create_plan          (catalog.py, one-off setup)
#   - Subscriptions: get_subscription / cancel_subscription
#   - Orders (PAYG): create_order / capture_order / get_order   (server-side amounts)
#   - Webhooks: verify_webhook_signature              (PayPal's own verify API)
#
# Every call raises PayPalError on a non-2xx so callers fail loud.

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Optional

import requests

_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_LIVE_BASE = "https://api-m.paypal.com"

_TIMEOUT = 30


class PayPalError(RuntimeError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"PayPal API error {status}: {body}")


def _env() -> str:
    return (os.getenv("PAYPAL_ENV") or "sandbox").strip().lower()


def base_url() -> str:
    return _LIVE_BASE if _env() == "live" else _SANDBOX_BASE


def _creds() -> tuple[str, str]:
    cid = (os.getenv("PAYPAL_CLIENT_ID") or "").strip()
    secret = (os.getenv("PAYPAL_SECRET") or "").strip()
    if not cid or not secret:
        raise PayPalError(0, "PAYPAL_CLIENT_ID / PAYPAL_SECRET not configured")
    return cid, secret


# ── OAuth token (cached per env until ~60s before expiry) ────────────────────
_token_cache: dict[str, tuple[str, float]] = {}


def _access_token() -> str:
    env = _env()
    cached = _token_cache.get(env)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]

    cid, secret = _creds()
    resp = requests.post(
        f"{base_url()}/v1/oauth2/token",
        auth=(cid, secret),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise PayPalError(resp.status_code, _safe_json(resp))
    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 3000))
    _token_cache[env] = (token, now + max(0, expires_in - 60))
    return token


def _safe_json(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def _headers(extra: Optional[dict] = None) -> dict:
    h = {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _request(method: str, path: str, *, json_body: Optional[dict] = None,
             headers: Optional[dict] = None, ok=(200, 201, 204)) -> Any:
    url = f"{base_url()}{path}"
    resp = requests.request(method, url, json=json_body, headers=_headers(headers), timeout=_TIMEOUT)
    if resp.status_code not in ok:
        raise PayPalError(resp.status_code, _safe_json(resp))
    if resp.status_code == 204 or not resp.content:
        return {}
    return _safe_json(resp)


# ── Catalog (one-off setup via catalog.py) ───────────────────────────────────

def create_product(*, name: str, description: str = "") -> dict:
    """A PayPal Product groups Billing Plans. type=SERVICE for a SaaS analysis service."""
    return _request("POST", "/v1/catalogs/products", json_body={
        "name": name,
        "description": description or name,
        "type": "SERVICE",
        "category": "SOFTWARE",
    }, headers={"PayPal-Request-Id": f"prod-{uuid.uuid5(uuid.NAMESPACE_DNS, name)}"})


def create_plan(*, product_id: str, name: str, price: float, currency: str,
                interval_unit: str = "MONTH", interval_count: int = 1) -> dict:
    """Create a recurring Billing Plan. total_cycles=0 = bill until cancelled.
    The price is fixed server-side here, so the browser can never alter it."""
    body = {
        "product_id": product_id,
        "name": name,
        "status": "ACTIVE",
        "billing_cycles": [{
            "frequency": {"interval_unit": interval_unit, "interval_count": interval_count},
            "tenure_type": "REGULAR",
            "sequence": 1,
            "total_cycles": 0,
            "pricing_scheme": {"fixed_price": {"value": f"{price:.2f}", "currency_code": currency}},
        }],
        "payment_preferences": {
            "auto_bill_outstanding": True,
            "setup_fee_failure_action": "CANCEL",
            "payment_failure_threshold": 1,
        },
    }
    return _request("POST", "/v1/billing/plans", json_body=body,
                    headers={"PayPal-Request-Id": f"plan-{uuid.uuid5(uuid.NAMESPACE_DNS, product_id + name)}"})


# ── Subscriptions ────────────────────────────────────────────────────────────

def get_subscription(subscription_id: str) -> dict:
    return _request("GET", f"/v1/billing/subscriptions/{subscription_id}")


def cancel_subscription(subscription_id: str, *, reason: str = "Customer requested cancellation") -> None:
    _request("POST", f"/v1/billing/subscriptions/{subscription_id}/cancel",
             json_body={"reason": reason[:127]}, ok=(204,))


# ── Orders (PAYG one-off; amount set server-side for security) ────────────────

def create_order(*, amount: float, currency: str, custom_id: str, description: str = "") -> dict:
    """Create a CAPTURE-intent order. custom_id carries our account email so the
    webhook can resolve the buyer regardless of their PayPal email."""
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "custom_id": custom_id[:127],
            "description": description[:127] or "Match credits",
            "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
        }],
    }
    return _request("POST", "/v2/checkout/orders", json_body=body,
                    headers={"PayPal-Request-Id": str(uuid.uuid4())})


def capture_order(order_id: str) -> dict:
    return _request("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={})


def get_order(order_id: str) -> dict:
    return _request("GET", f"/v2/checkout/orders/{order_id}")


# ── Webhook signature verification (PayPal's own verify API) ──────────────────

def verify_webhook_signature(*, transmission_id: str, transmission_time: str,
                             cert_url: str, auth_algo: str, transmission_sig: str,
                             webhook_event: dict) -> bool:
    """Returns True only if PayPal confirms the signature. webhook_id from env."""
    webhook_id = (os.getenv("PAYPAL_WEBHOOK_ID") or "").strip()
    if not webhook_id:
        raise PayPalError(0, "PAYPAL_WEBHOOK_ID not configured")
    body = {
        "transmission_id": transmission_id,
        "transmission_time": transmission_time,
        "cert_url": cert_url,
        "auth_algo": auth_algo,
        "transmission_sig": transmission_sig,
        "webhook_id": webhook_id,
        "webhook_event": webhook_event,
    }
    out = _request("POST", "/v1/notifications/verify-webhook-signature", json_body=body)
    return (out or {}).get("verification_status") == "SUCCESS"
