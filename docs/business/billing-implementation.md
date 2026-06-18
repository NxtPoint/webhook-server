# Billing — implementation reference

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)).
>
> Module-level reference for the billing subsystem. Files are scattered at repo root (not in a subdirectory), so this doc plays the role of a `billing/README.md`.

For the **business rules** (what's a credit, what gates upload, how the soft-delete contract works), read [`README.md`](README.md). For **pricing tiers and plan IDs**, read [`pricing-and-packages.md`](pricing-and-packages.md). This doc is the **file map + entry points + flow**.

## What this owns

- The `billing.*` Postgres schema (`account`, `member`, `entitlement_grant`, `entitlement_consumption`, `subscription_state`, `subscription_event_log`, `coaches_permission`, `monthly_refill_log`, `entitlements`, `vw_customer_usage`)
- The credit grant / consumption / entitlements logic
- The subscription webhook handling (PayPal — LIVE; Wix — rollback fallback) via the shared `apply_subscription_event` path + monthly refill cron driver
- Two ops endpoints (`/api/billing/summary`, `/api/billing/entitlement/check`, `/api/billing/entitlement/grant`) for backoffice use
- The `entitlements_api` UPSERT that derives all permission flags into `billing.entitlements`
- The capacity-sweep cron that detects stuck ingests / trims (auxiliary, not strictly billing)

## What this is NOT

- **Not the upload gate caller.** `upload_app.py` reads from `billing.entitlements` to decide if a request is allowed. This module *writes* the table; the consumer is `upload_app`.
- **Not the payment processor.** Since 2026-06-16 payment is **direct PayPal** (`paypal_billing/`, LIVE) — it owns checkout, the plan catalogue, and the signature-verified webhook. This module receives the *normalized* lifecycle event (from PayPal now; Wix only as the `PAYPAL_ENABLED=0` fallback) and translates it to credit grants via the shared `subscriptions_api.apply_subscription_event(payload, provider)`.
- **Not the AI Coach paywall.** That gate lives in `tennis_coach/coach_api.py::_check_ai_coach_entitled`. It reads `billing.subscription_state` and `billing.member.role` directly — bypasses `billing.entitlements` because it pre-dates that table.

## Files

| File | Purpose |
|---|---|
| `models_billing.py` | SQLAlchemy ORM declarations (`Account`, `Member`, `EntitlementGrant`, `EntitlementConsumption`) + **`billing_init()`** — the idempotent base-schema bootstrap (run on boot from `upload_app.py`). It creates the 4 ORM tables (`create_all`) plus the previously code-less raw objects (`subscription_state`, `subscription_event_log`, `monthly_refill_log`, `coaches_permission`, `entitlements`, `security_access`, `vw_customer_usage`, and **`payment`**) so a fresh DB reproduces prod. **`billing.payment`** is a record-only money-movement log (PayPal sale/capture = positive, refund/reversal = negative), written by `billing_service.record_payment()` from the PayPal webhook (`paypal_billing/webhook.py::_extract_payment`), idempotent on `(provider, provider_payment_id)`. Refunds are **recorded but do not revoke credits** (business decision 2026-06-18); revoking can be layered on later. Column *additions* still live in their `_ensure_*` owners (`client_api._ensure_member_profile_columns`, `billing_service._ensure_technique_columns`, `subscriptions_api._ensure_subscription_state_columns`, `entitlements_api._ensure_entitlements_schema`). |
| `billing_service.py` | The behavioural core. Account/member create-with-guard-rails, entitlement grant with three-way idempotency, consumption (match + technique), monthly no-rollover reset, coach cap gate, signup bonus. Direct callers from everywhere. |
| `billing_import_from_bronze.py` | Reconciliation: scans `bronze.submission_context` for completed tasks without consumption rows and writes them. Auto-creates accounts on the fly if needed. Used as a CLI tool for backfill, and called from ingest worker step 5. |
| `entitlements_api.py` | The big derived-flags UPSERT. One SQL statement that reads `account`, `member`, `subscription_state`, `entitlement_grant`, `entitlement_consumption`, `coaches_permission` and writes `billing.entitlements`. Single source of truth for `can_upload`, `can_view_dashboards`, `can_link_additional_player`, block reasons. |
| `subscriptions_api.py` | The **shared normalize→grant path** `apply_subscription_event(payload, provider)` + the Wix webhook endpoint (`/api/billing/subscription/event`, now the fallback) + monthly refill endpoint. Idempotent per event by sha256 of canonical fields. The PayPal receiver (`paypal_billing/webhook.py`) calls `apply_subscription_event(provider='paypal')` in-process — one grant path, two front doors. |
| `paypal_billing/` | **Direct PayPal payments (LIVE 2026-06-16) — replaces Wix Pricing Plans checkout.** `webhook.py` (signature-verified receiver → refetch from PayPal → `apply_subscription_event`; server-side create-subscription / create-order / capture-order / cancel-subscription; public `/config` probe), `client.py` (REST client), `plans.py` + committed `catalog.json` (plan catalogue + live Product/Billing-Plan ids), dark `register(app)` on `PAYPAL_ENABLED`. `billing.*` only (core mirror deferred). Full reference: `paypal_billing/README.md`. |
| `usage_api.py` | OPS_KEY ops endpoints: `summary`, `entitlement/check`, `entitlement/grant`. For backoffice / integration scripts / one-off corrections. |
| `cron_monthly_refill.py` | **Render cron.** Single HTTP POST to `/api/billing/cron/monthly_refill`. The endpoint owns the logic; this script is just the trigger. |
| `cron_capacity_sweep.py` | **Render cron.** Detects stuck ingests / trims by reading `bronze.submission_context` directly. Not strictly billing, but co-located with the other cron driver. |

## Entry points

### Behavioural API (Python — call from anywhere in the app)

| Function | What it does | Idempotency |
|---|---|---|
| `billing_service.create_account_with_primary_member(email, name, currency='USD', external_wix_id=None, role='player_parent')` | Idempotent account+member creation | By email |
| `billing_service.add_member_to_account(account_id, full_name, role)` | Add a non-primary member (child or coach) | None (always inserts) |
| `billing_service.grant_entitlement(account_id, source, plan_code, matches_granted, techniques_granted=0, external_wix_id=None, valid_from, valid_to, is_active=True)` | Add credits | Three-way: by `external_wix_id` if present; by `(account, source, plan_code)` for `signup_bonus`; by `(account, source, plan_code, valid_from)` otherwise |
| `billing_service.grant_signup_bonus(account_id)` | One-time free-trial grant: 1 match + 5 techniques, lifetime | One per account, ever |
| `billing_service.consume_match_for_task(account_id, task_id, source='sportai')` | Deduct 1 match credit | By `task_id` unique constraint |
| `billing_service.consume_matches_for_task(...)` | Same, parameterised count | By `task_id` |
| `billing_service.consume_technique_for_task(account_id, task_id, source='technique')` | Deduct 1 technique credit | By `task_id` |
| `billing_service.coach_accept_gate(coach_email)` | Phase 2 cap check | Pure read; fails open on DB error |
| `billing_service.get_remaining_matches(account_id)` | Compute current remaining | Read-only |
| `billing_service.get_usage_summary(account_id)` | Convenience UI helper | Read-only |
| `billing_import_from_bronze.sync_usage_for_task_id(task_id)` | Single-task reconciliation | Yes |
| `billing_import_from_bronze.sync_all_usage(dry_run=False)` | Batch reconciliation | Yes |

### HTTP endpoints

| Endpoint | Auth | Module | Purpose |
|---|---|---|---|
| `POST /api/billing/paypal/webhook` | PayPal signature | `paypal_billing/webhook.py` | **PayPal lifecycle webhook (LIVE)** → refetch → `apply_subscription_event(provider='paypal')` |
| `POST /api/billing/paypal/{create-subscription,create-order,capture-order,cancel-subscription}` | client-key **or** Clerk JWT (`_guard`) | `paypal_billing/webhook.py` | Server-side checkout + cancel (amounts/plan/custom_id set server-side) |
| `GET /api/billing/paypal/config` | none (public) | `paypal_billing/webhook.py` | Frontend probe: `enabled`/`env`/plan ids (drives `/pricing`; Wix fallback when off) |
| `POST /api/billing/subscription/event` | OPS_KEY | `subscriptions_api.py` | Wix subscription lifecycle webhook — **now the `PAYPAL_ENABLED=0` fallback** |
| `POST /api/billing/cron/monthly_refill` | OPS_KEY | `subscriptions_api.py` | Monthly refill (called by `cron_monthly_refill.py`) — Wix subs only |
| `GET /api/billing/summary?email=…` | OPS_KEY | `usage_api.py` | Account usage summary |
| `GET /api/billing/entitlement/check?email=…` | OPS_KEY | `usage_api.py` | Upload-gate check |
| `POST /api/billing/entitlement/grant` | OPS_KEY | `usage_api.py` | Manual credit grant |
| `GET /api/entitlements/summary?email=…` | OPS_KEY | `entitlements_api.py` | UPSERT-then-read derived flags |

### Crons (Render)

| Cron | Frequency | What it does |
|---|---|---|
| `cron_monthly_refill.py` | 1st of month | POST `/api/billing/cron/monthly_refill` |
| `cron_capacity_sweep.py` | Every few minutes | Scan for stuck ingests/trims |

## Data model

```
billing.account                          (1 per email)
  ├─ id, email (unique), primary_full_name, currency_code, active, external_wix_id
  └── billing.member  (n per account)
        ├─ account_id (FK), full_name, surname, is_primary, role, email (children only)
        ├─ profile fields (phone, utr, dominant_hand, country, area)
        └─ child fields (dob, skill_level, club_school, notes, profile_photo_url)

billing.entitlement_grant                (additive credit ledger — append-only)
  ├─ account_id (FK)
  ├─ source ∈ {wix_subscription, wix_payg, paypal_subscription, paypal_payg, manual_adjustment, signup_bonus}
  ├─ plan_code, external_wix_id (NULLABLE) — **reused by PayPal** (`purchase:{order_id}:{account_id}`); a misnomer pending the Wix-payment-deprecation rename to `external_id`
  ├─ matches_granted, techniques_granted
  ├─ valid_from, valid_to (NULLABLE = lifetime), is_active
  └─ idempotency: see grant_entitlement three-way rule above

billing.entitlement_consumption          (deduction ledger — append-only)
  ├─ account_id (FK)
  ├─ task_id (UNIQUE) — UUID-coerced via uuid5(NAMESPACE_URL, str)
  ├─ consumed_matches (default 1), consumed_techniques (default 0)
  ├─ source, consumed_at
  └─ NEVER deleted — soft-delete contract, see README.md §7

billing.subscription_state               (current state per account)
  ├─ account_id, plan_id (PayPal Billing-Plan id, or legacy Wix UUID), plan_code, status, period_end
  ├─ billing_provider ∈ {wix, paypal} — the monthly cron refills only 'wix'; PayPal subs are webhook-driven (grant per renewal payment)
  ├─ provider_subscription_id — PayPal subscription id (I-…) used to cancel; NULL for Wix
  └─ entitlements UPSERT picks the most recent by updated_at when multiple exist

billing.subscription_event_log           (Wix + PayPal webhook audit + idempotency)
  └─ unique on event_id (sha256 of canonical event fields)

billing.coaches_permission               (coach invite + access)
  ├─ owner_account_id, coach_account_id (NULLABLE), coach_email
  ├─ status ∈ {INVITED, ACCEPTED}, active, invite_token (NULLABLE, single-use)
  └─ unique partial index WHERE invite_token IS NOT NULL

billing.entitlements                     (DERIVED — written by entitlements UPSERT)
  ├─ account_id (PK), email, role, account_active
  ├─ subscription_status, current_period_end, paid_active
  ├─ matches_granted/consumed/remaining, techniques_*
  ├─ coach_linked_players, can_link_additional_player
  ├─ can_view_dashboards, dashboard_block_reason
  ├─ can_upload, block_reason
  └─ updated_at — refreshed every time UPSERT runs

billing.monthly_refill_log               (idempotency for monthly cron)
  └─ unique on (account_id, year_month)

billing.vw_customer_usage                (legacy view — pre-entitlements table)
  └─ matches_granted, matches_consumed, matches_remaining, last_processed_at
```

## Flow — match upload triggers a consumption row

```
user uploads → /api/submit_s3_task → ingest_worker_app step 5
        │
        ▼
billing_import_from_bronze.sync_usage_for_task_id(task_id)
        │
        ├─ SELECT email, customer_name, sport_type, last_status
        │     FROM bronze.submission_context WHERE task_id = ...
        │     (must be last_status='completed')
        │
        ├─ _find_or_create_account(email, customer_name)
        │     └─ creates billing.account + primary member if missing (USD, no Wix id)
        │
        ├─ if sport_type='technique_analysis':
        │       consume_technique_for_task(account_id, task_id, source='technique')
        │   else:
        │       consume_match_for_task(account_id, task_id, source='sportai')
        │
        └─ INSERT INTO billing.entitlement_consumption ...
              ON CONFLICT (task_id) DO NOTHING
              -- Idempotent. Re-running is a no-op.
```

## Flow — a subscription/payment event becomes credits

> Both providers feed the SAME `subscriptions_api.apply_subscription_event(payload, provider)`.
> **PayPal (LIVE):** `paypal_billing/webhook.py` verifies PayPal's signature, **refetches** the
> resource from PayPal, then calls it with `provider='paypal'` — recurring grants on
> `PAYMENT.SALE.COMPLETED` (`valid_to`=next billing → no rollover), PAYG on capture (never expires).
> **Wix (fallback):** the OPS_KEY `/api/billing/subscription/event` endpoint calls it with
> `provider='wix'`. The diagram below is the normalized shape both share.

```
subscription/payment lifecycle event (PayPal webhook | Wix webhook)
        │
        ▼
apply_subscription_event(payload, provider)   (via /api/billing/paypal/webhook | /api/billing/subscription/event)
        │
        ├─ event_id = sha256(event_type|email|order_id|plan_id|status|plan_start|plan_end)
        ├─ INSERT billing.subscription_event_log (skip if event_id exists — idempotent)
        │
        ├─ UPSERT billing.subscription_state
        │     ├─ PLAN_PURCHASED → status=ACTIVE, period_end=plan_end
        │     ├─ PLAN_CANCELLED / RECURRING_PAYMENT_CANCELLED → status=CANCELLED
        │     └─ ACTIVE with past period_end → status=EXPIRED
        │
        └─ if PLAN_PURCHASED + ACTIVE (matches_granted > 0):
              grant_entitlement(account_id, source=f'{provider}_subscription' | f'{provider}_payg',
                                plan_code, external_wix_id=f'purchase:{order_id}:{account_id}',
                                matches_granted=plan_allowance, valid_to=period_end)
              -- Immediate: user can upload right now, doesn't wait for monthly cron.
              -- billing_provider is stamped on subscription_state so the Wix-only cron skips PayPal subs.
```

## Flow — monthly refill (1st of month)

```
Render cron fires cron_monthly_refill.py
        │
        ▼
POST /api/billing/cron/monthly_refill   (OPS_KEY)
        │
        ├─ for each ACTIVE recurring WIX subscription (billing_provider='wix'):
        │     -- PayPal subs are excluded: they grant per renewal payment via the webhook, not here
        │     ├─ check billing.monthly_refill_log unique (account_id, YYYY-MM) — skip if done
        │     ├─ remaining = vw_customer_usage.matches_remaining
        │     ├─ allowance = subscription_state.plan_allowance
        │     │
        │     ├─ if remaining < allowance:
        │     │     grant_entitlement(matches_granted=allowance - remaining, ...)
        │     │
        │     └─ if remaining > allowance (no-rollover):
        │           expire excess by setting valid_to=now() on oldest active grants
        │
        └─ INSERT billing.monthly_refill_log (account_id, YYYY-MM)
```

## Gotchas

- **`grant_entitlement` has three idempotency strategies.** See [`README.md`](README.md) §3 for the table. Mixing them up will create duplicate grants or block legitimate ones.
- **Consumption is never deleted.** Soft-delete contract in [`README.md`](README.md) §7. The sweep at `cleanup/orphan_sweep.py` explicitly skips `billing.*`.
- **`task_id` is UUID-coerced.** Non-UUID strings become deterministic UUIDs via `uuid5(NAMESPACE_URL, str)`. Same string always produces the same UUID — that's the idempotency hook.
- **Subscription "ACTIVE" wins by recency.** When multiple `subscription_state` rows exist, `entitlements_api` UPSERT picks the most recent by `updated_at`. Stale CANCELLED rows can't block a renewed account.
- **`can_upload` does NOT require `paid_active`.** Free-trial credits are real credits — uploading on the signup bonus works. The gate is purely `account_active AND role <> 'coach' AND matches_remaining > 0`.
- **`can_view_dashboards` retains forever once any credit is consumed.** This is the conversion hook. Don't "fix" this — it's load-bearing. See `entitlements_api.py:196-203` and [`README.md`](README.md) §4.
- **Coach cap fires at accept time, not invite time.** Existing accepted coach links are grandfathered. The gate is in `coach_invite/accept_page.py`, not in the invite-creation flow.
- **Free Coach Access Wix plan does NOT count as paid.** Hard-coded plan ID `cd2b6772-1880-42ec-9049-4d9e4decc42b` is excluded from `is_coach_pro`. Free coach subscribers still hit the 1-player cap.
- **`OPS_KEY` env var: `BILLING_OPS_KEY` checked first, then `OPS_KEY`.** Both files (`subscriptions_api`, `usage_api`) follow this order. Set `BILLING_OPS_KEY` if you want a separate billing key; otherwise `OPS_KEY` works.
- **`billing_service._ensure_technique_columns()` runs on import** (`billing_service.py:75`). It widens the `source` CHECK constraint and adds `techniques_*` columns. This is part of why `billing_service` is import-time-side-effect-heavy.
- **Account auto-creation in reconciliation.** `billing_import_from_bronze` will create a fresh `billing.account` for any completed bronze task that lacks one (`_find_or_create_account`). This means uploads via legacy paths or manual bronze inserts still land in billing.

## Required environment variables

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection |
| `OPS_KEY` | Auth for all `/api/billing/*` ops endpoints (with optional `BILLING_OPS_KEY` override) |
| `CLIENT_API_KEY` | Not used here — but consumed by the entitlements *readers* in `client_api.py` |

## See also

- [`README.md`](README.md) §2–§8 — full business rules, entitlement contract, hidden invariants
- [`pricing-and-packages.md`](pricing-and-packages.md) — tier numerics, plan IDs (PayPal live in `paypal_billing/`; legacy Wix), AI Coach access matrix
- [`../paypal_billing/README.md`](../../paypal_billing/README.md) — direct PayPal payments (LIVE): checkout, webhook, grant model, rollback
- [`../coach_invite/README.md`](../../coach_invite/README.md) — coach invite flow (consumes `coach_accept_gate`)
- [`../tennis_coach/README.md`](../../tennis_coach/README.md) — AI Coach paywall (separate gate that reads `subscription_state` directly)
- [`../cleanup/README.md`](../../cleanup/README.md) — orphan sweep (the bright line: never touches `billing.*`)
- [`../CLAUDE.md`](../../CLAUDE.md) §Billing System — short overview that points here
