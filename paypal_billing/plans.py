# paypal_billing/plans.py — canonical plan catalogue for the PayPal-direct integration.
#
# SINGLE SOURCE OF TRUTH for what we sell. Mirrors the plan defs in frontend/pricing.html
# (the `code` / `matches` / `type` values MUST match — `code` is the plan_code that flows
# into billing_service.grant_entitlement and that pricing.html matches on to highlight the
# current plan). Prices live HERE (not Wix any more) because PayPal billing plans need a
# fixed price at creation time.
#
# Flow:
#   - catalog.py reads PLANS, creates a PayPal Product + Billing Plan per RECURRING plan,
#     and writes the resulting PayPal ids to catalog.json.
#   - The webhook (webhook.py) loads catalog.json and resolves an authoritative PayPal
#     plan_id back to {code, matches, plan_type} so a payment grants the right credits.
#   - PAYG packs are NOT PayPal Billing Plans — they are one-off Orders priced server-side
#     from this table, so the amount can never be set by the browser.
#
# To change a price/plan: edit PRICES below, re-run `python -m paypal_billing.catalog`
# (sandbox first), commit the updated catalog.json.

from __future__ import annotations

import json
import os
from typing import Optional

# Currency PayPal charges in. Settlement to FNB (SA) is independent of the presentment
# currency; the marketing copy quotes USD ("$40/mo"), so we present in USD.
CURRENCY = os.getenv("PAYPAL_CURRENCY", "USD")

# ── PRICE TABLE — CONFIRM BEFORE RUNNING catalog.py ──────────────────────────
# Keyed by plan `code`. Value = price in MAJOR units (e.g. 40.00 = $40.00).
# Only `player standard` is known from repo copy (nudge banner: "$40/mo for 5 matches").
# The rest are None and catalog.py will REFUSE to run until they are real numbers.
PRICES = {
    # Recurring (per month) — from the live Wix Pricing Plans (2026-06-16)
    "player starter":   25.00,   # 3 matches / mo   (Wix "Player – Starter")
    "player standard":  40.00,   # 5 matches / mo   (Wix "Player – Standard")
    "player advances":  70.00,   # 10 matches / mo  (Wix "Player – Advanced")
    # PAYG one-off packs
    "once off":         25.00,   # 1 match   (Wix "Once off")
    "payg 3 matches":   50.00,   # 3 matches (Wix "Pay as you go - 3 matches")
    "payg 5 matches":  100.00,   # 5 matches (Wix "Pay as you go - 5 matches")
}

# ── PLAN CATALOGUE (codes + matches mirror frontend/pricing.html exactly) ─────
PLANS = [
    # Recurring monthly subscriptions
    {"code": "player starter",  "name": "Starter",  "plan_type": "recurring", "matches": 3,  "interval": "MONTH"},
    {"code": "player standard", "name": "Standard", "plan_type": "recurring", "matches": 5,  "interval": "MONTH"},
    {"code": "player advances", "name": "Advanced", "plan_type": "recurring", "matches": 10, "interval": "MONTH"},
    # Pay-as-you-go credit packs (one-off Orders, not Billing Plans)
    {"code": "once off",        "name": "1 Match Credit",  "plan_type": "payg", "matches": 1, "interval": None},
    {"code": "payg 3 matches",  "name": "3 Match Credits", "plan_type": "payg", "matches": 3, "interval": None},
    {"code": "payg 5 matches",  "name": "5 Match Credits", "plan_type": "payg", "matches": 5, "interval": None},
]

_BY_CODE = {p["code"]: p for p in PLANS}

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")


def by_code(code: str) -> Optional[dict]:
    return _BY_CODE.get((code or "").strip())


def recurring_plans() -> list[dict]:
    return [p for p in PLANS if p["plan_type"] == "recurring"]


def payg_packs() -> list[dict]:
    return [p for p in PLANS if p["plan_type"] == "payg"]


def price_of(code: str) -> Optional[float]:
    return PRICES.get((code or "").strip())


def missing_prices() -> list[str]:
    """Plan codes that still need a real price before catalog.py can run."""
    return [c for c, v in PRICES.items() if v is None]


# ── catalog.json bridge (PayPal ids) ─────────────────────────────────────────
# Shape written by catalog.py:
#   {
#     "env": "sandbox",
#     "currency": "USD",
#     "plans": {                       # recurring only — PayPal Billing Plan ids
#       "player standard": {"product_id": "PROD-xxx", "plan_id": "P-xxx", "price": 40.0, "matches": 5}
#     }
#   }

def load_catalog() -> dict:
    """Load catalog.json (PayPal ids). Returns {} if not yet created."""
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_catalog(data: dict) -> None:
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def paypal_plan_id_map() -> dict[str, dict]:
    """Reverse map for the webhook: PayPal plan_id -> {code, matches, plan_type, price}.
    Authoritative resolution of a recurring payment to the credits it grants."""
    cat = load_catalog()
    out: dict[str, dict] = {}
    for code, rec in (cat.get("plans") or {}).items():
        plan_id = rec.get("plan_id")
        meta = by_code(code) or {}
        if plan_id:
            out[plan_id] = {
                "code": code,
                "matches": rec.get("matches", meta.get("matches", 0)),
                "plan_type": "recurring",
                "price": rec.get("price"),
            }
    return out
