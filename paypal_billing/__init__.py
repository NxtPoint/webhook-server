# paypal_billing — direct PayPal payments (replaces Wix Pricing Plans checkout).
#
# DARK by default: register(app) is a no-op unless PAYPAL_ENABLED=1. Vanilla PayPal —
# SDK Buttons → Subscriptions (recurring) / Orders (PAYG) → signature-verified webhook →
# refetch resource → the shared billing grant path (subscriptions_api.apply_subscription_event).
# Touches billing.* only (core.* mirror deferred). Env:
#   PAYPAL_ENABLED, PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_WEBHOOK_ID, PAYPAL_ENV(sandbox|live)
#
# Catalog/plan model lives in plans.py + catalog.json; the REST client in client.py.

from __future__ import annotations

import os


def enabled() -> bool:
    return (os.getenv("PAYPAL_ENABLED") or "").strip() in ("1", "true", "True", "yes")


def register(app) -> bool:
    """Register the PayPal blueprint when PAYPAL_ENABLED=1. Returns True if registered.
    The config + checkout config route is registered even when dark so the frontend can
    detect that PayPal is off and fall back to the Wix path — but it reports enabled=false."""
    try:
        from paypal_billing.webhook import paypal_bp, register_always
        # Always-on, side-effect-free routes (config probe). Reports enabled=false when dark.
        register_always(app)
        if not enabled():
            return False
        app.register_blueprint(paypal_bp)
        return True
    except Exception:  # pragma: no cover - webhook module arrives in step 3
        app.logger.exception("paypal_billing register failed")
        return False
