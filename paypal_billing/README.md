# paypal_billing — direct PayPal payments

Replaces the Wix Pricing Plans → PayPal checkout with **our own** PayPal integration
(recurring subscriptions + PAYG credit packs). Vanilla PayPal, server-side, security-first.
**Touches `billing.*` only** (the `core.*` mirror is deferred).

> **Status: LIVE since 2026-06-16** (`PAYPAL_ENABLED=1`, `PAYPAL_ENV=live`). Proven
> end-to-end on sandbox AND a real live purchase (PAYG + subscribe + cancel). The Wix
> checkout is retired — it remains only as the `PAYPAL_ENABLED=0` rollback fallback.

## Why it's a small build
The billing backend is provider-agnostic. `subscriptions_api.apply_subscription_event()`
already turns a normalized "a payment happened" event into idempotent credit grants
(`billing_service.grant_entitlement`). PayPal just **feeds that same path** — we did not
duplicate any billing logic.

## Architecture
```
pricing.html ──(GET /config)──► is PayPal on? ── no ─► Wix postMessage (unchanged fallback)
     │ yes
     ├─ recurring: paypal_sub.Buttons.createSubscription ─► POST /create-subscription
     │              (server sets plan_id + custom_id=email)         └► returns subscription id
     └─ PAYG:      paypal_order.Buttons.createOrder ──────► POST /create-order
                    (server sets amount + custom_id=email|code)     └► onApprove ─► POST /capture-order (grants now)

PayPal ──(webhook, signed)──► POST /webhook ─► verify signature ─► REFETCH resource from PayPal
                                              ─► map event ─► apply_subscription_event(provider='paypal')
                                                              ─► grant_entitlement (idempotent)
```

### Grant model (PayPal-native — credits granted when money is RECEIVED)
| Event | Action |
|---|---|
| `BILLING.SUBSCRIPTION.ACTIVATED` | state → ACTIVE, store sub id. **No grant.** |
| `PAYMENT.SALE.COMPLETED` (first + every renewal) | **grant** the period's matches; `valid_to` = next billing date → unused credits expire each cycle (no rollover) |
| `PAYMENT.CAPTURE.COMPLETED` (PAYG) | **grant** the pack (idempotent backstop to `/capture-order`); PAYG credits never expire |
| `BILLING.SUBSCRIPTION.CANCELLED` / `EXPIRED` / `SUSPENDED` | state → CANCELLED |

Idempotency: every grant keys on the PayPal resource id (`order_id` → `external_wix_id`),
and `apply_subscription_event` dedups on a sha256 of the event fields — PayPal retries and
the capture/webhook double-path are both safe.

PayPal subscriptions are **webhook-driven only**. The Wix monthly-refill cron is fenced off
(`subscription_state.billing_provider = 'wix'`), so PayPal subs are never cron-refilled.

## Security
- No card data touches us (PCI SAQ-A) — PayPal hosts the payment surface.
- Subscriptions + orders are **created server-side**, so plan/amount/`custom_id` can't be
  set by the browser.
- The webhook is authenticated by PayPal's `verify-webhook-signature` API, then **re-fetches**
  the resource from PayPal before granting — the webhook body is never trusted for money.
- `custom_id` carries our account email (PAYG: `email|plan_code`) so the buyer is resolved
  even if their PayPal email differs.

## Files
| File | What |
|---|---|
| `plans.py` | canonical plan catalogue (codes/matches mirror `frontend/pricing.html`); `PRICES` table; `catalog.json` bridge + `plan_id → {code,matches}` reverse map |
| `client.py` | thin PayPal REST client (oauth, products/plans, subscriptions, orders, verify-signature) |
| `catalog.py` | `python -m paypal_billing.catalog` — creates Products + Billing Plans, writes `catalog.json` |
| `webhook.py` | blueprint: webhook receiver + create/capture/cancel endpoints + public `/config` |
| `__init__.py` | dark `register(app)` gated on `PAYPAL_ENABLED` |
| `catalog.json` | created by catalog.py (committed) — maps plan `code` → PayPal product/plan ids |

## Env
| Var | Notes |
|---|---|
| `PAYPAL_ENABLED` | `0` (dark) / `1`. Default 0. |
| `PAYPAL_ENV` | `sandbox` (default) / `live` |
| `PAYPAL_CLIENT_ID` / `PAYPAL_SECRET` | REST app credentials (developer.paypal.com → Apps & Credentials) |
| `PAYPAL_WEBHOOK_ID` | id of the webhook you register in the dashboard (after the URL exists) |
| `PAYPAL_CURRENCY` | presentment currency (default `USD`) |

## Go-live runbook (DONE 2026-06-16 — kept for re-runs / reference)
Sandbox E2E (PAYG + subscribe + cancel) and a real live purchase are both proven; live
`catalog.json` + live webhook + live creds are in place. The sequence, for re-running the
catalog or standing this up again elsewhere:
1. **Set prices** in `plans.py` `PRICES` (all of them; catalog refuses to run with gaps).
2. **Creds** in env (`PAYPAL_ENV` + `PAYPAL_CLIENT_ID`/`SECRET`).
3. **Create the catalog:** `python -m paypal_billing.catalog` → commit `catalog.json`.
4. **Register the webhook** in the PayPal dashboard at
   `https://api.nextpointtennis.com/api/billing/paypal/webhook`, subscribe to:
   `BILLING.SUBSCRIPTION.ACTIVATED`, `BILLING.SUBSCRIPTION.CANCELLED`,
   `BILLING.SUBSCRIPTION.EXPIRED`, `PAYMENT.SALE.COMPLETED`, `PAYMENT.CAPTURE.COMPLETED`.
   Put its id in `PAYPAL_WEBHOOK_ID`.
5. **`PAYPAL_ENABLED=1`** + verify `/api/billing/paypal/config` reports `enabled:true` and
   the right `env` (see the gotcha below).

**Operational gotcha (bit the live cut):** a `value:` change in `render.yaml` (e.g.
`PAYPAL_ENV`) does NOT reliably auto-apply on push — set critical flips in the **Render
dashboard** too, then **verify via `/config`** rather than assuming the deploy applied it.
Secrets stay `sync:false` (dashboard-owned).

**Rollback:** `PAYPAL_ENABLED=0` — instantly reverts the frontend to Wix checkout. No deploy
needed beyond the env change.
