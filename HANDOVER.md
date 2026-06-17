# HANDOVER.md — picking up the growth stack / auth / payment in a fresh session

> For a new Claude Code chat continuing this work. Paste the relevant **kickoff prompt** (below) to
> start with full context. Built 2026-06-16; pre-launch, no real customers (free to deploy to prod).

## Read-order (5 minutes)
1. `marketing_crm/STATUS.md` — the living hymn sheet: what's built, every enable switch, lanes/ownership, events, open items.
2. `ARCHITECTURE.md` + `DATA-INVENTORY.md` + `WIX-DEPENDENCY.md` — system map, where data lives, what Wix still owns.
3. `DB-SCHEMA-PROPOSAL.md` + `core_db/README.md` — the canonical `core.*` model (account/user/person, subscriptions, credit ledger, matches, usage, consent).
4. `AUTH-MIGRATION-PLAN.md` — de-Wix auth plan + payment (PayPal-direct) sizing appendix.

## What already exists (REUSE — do not rebuild)
- **`core.*` schema** (live on prod, empty bar tomo's backfilled account): identity, billing, matches, usage, consent. DAL in `core_db/repositories/`, write-path via `core_db.repositories.accounts.ensure_identity`.
- **Billing backend (provider-agnostic):** `billing_service.py` (`grant_entitlement`), `subscriptions_api.py` (`POST /api/billing/subscription/event` → grants credits idempotently), `models_billing.py`. **A new payment processor only needs to feed this normalized events.**
- **`marketing_crm/`**: `tracking/` (events + page-view beacon), `crm_sync/` (HubSpot/Klaviyo), `backoffice/` (cockpit), `feedback/`, `consent/`. All **dark-by-default**, gated by env flags.
- **Patterns to follow:** dark-by-default `register(app)` gated on an env flag; fire-and-forget never blocks requests; one-way mirror of `core.*`; aggregation in SQL views (rule #2).

## Working rules (non-negotiable)
- **Lane guard:** `.githooks/pre-commit` blocks commits touching code unless `CLAUDE_CODE=1`. Commit code with `CLAUDE_CODE=1 git commit … && CLAUDE_CODE=1 git push`. Docs (`.md`) commit without it.
- **Commit + push to `main`** every time (Render deploys from `origin/main`). Small reviewable commits.
- **Verify against prod safely:** the dev box reaches prod Postgres. Use `core_db.seed` (`--force --allow-remote`) → assert → **purge**; never leave test rows. Update `marketing_crm/STATUS.md` when state changes.
- **Don't do auth and payment in the same chat/session** (independent tracks; keep them decoupled). Run them sequentially to avoid two agents racing on `main`.

---

## KICKOFF PROMPT — AUTH (de-Wix authentication)

> **✅ DONE 2026-06-16/17 — this kickoff is historical.** De-Wix auth shipped: Clerk PRODUCTION live
> (`clerk.ten-fifty5.com`, `pk_live`, own Google OAuth), `auth_v2/` verifies the JWT, all client surfaces
> dual-mode, marketing CTAs → `/login`, Wix `postMessage` handoff removed from code. Only Phase 4 remains
> (delete the legacy `CLIENT_API_KEY` — now a pure fallback). Current state: `marketing_crm/STATUS.md`;
> executed plan: `AUTH-MIGRATION-PLAN.md`. The prompt below is kept as the original kickoff record.

```
We're starting the de-Wix AUTHENTICATION migration. Read HANDOVER.md, AUTH-MIGRATION-PLAN.md,
WIX-DEPENDENCY.md, and marketing_crm/STATUS.md first.

Context: pre-launch, no real customers, so the "preserve logins / no downtime" problem is near-zero
now — this is the cheap window. Provider decision: I'm going with <Clerk | Auth0 | Cognito> and have
created the account/tenant: <paste publishable key / tenant details or say "not yet">.

Reuse 100% of what's built: core.user (auth_provider + auth_provider_uid is purpose-built for the IdP
id), the consent write-path (signup already creates core.* — see marketing_crm/consent), the
dark-by-default + env-gated pattern, the lane-guard (CLAUDE_CODE=1 for code commits), commit+push to
main, and the seed->assert->purge verification approach.

Do Phase 0 + Phase 1 from AUTH-MIGRATION-PLAN.md, gated behind AUTH_V2_ENABLED (default off), without
breaking the current Wix login:
  - Phase 0: a JWT-verify middleware that accepts the IdP session token (resolve core.user by
    auth_provider_uid, derive account/role server-side) AND still accepts the legacy
    CLIENT_API_KEY + ?email during the transition. This replaces the shared-key model (ARCHITECTURE.md
    §6.1) — the client must no longer assert its own account.
  - Phase 1: our own login/signup UI using the IdP; new signups flow into core.* via the existing
    consent write-path; the API accepts both auth methods.
Propose the exact file plan for approval BEFORE writing code. Keep Wix payment untouched.
```

## KICKOFF PROMPT — PAYMENT (PayPal-direct, NEW chat)

> **✅ DONE 2026-06-16 — this kickoff is historical.** Direct PayPal is LIVE (`PAYPAL_ENABLED=1`,
> `PAYPAL_ENV=live`) in `paypal_billing/`: vanilla PayPal Subscriptions + Orders, signature-verified
> webhook → refetch → the shared `apply_subscription_event` grant path, native buttons + cancel in
> `pricing.html`. Proven on sandbox AND a real live purchase. **`billing.*` only — the core.* mirror in
> step 4 below was DEFERRED** (own ticket: `STATUS.md` "Not built yet" + the CORE.* kickoff below). Wix
> checkout retained only as the `PAYPAL_ENABLED=0` rollback (its deprecation is the WIX PAYMENT
> DEPRECATION kickoff below). Runbook: `paypal_billing/README.md`. Prompt kept as the original record.

```
We're building DIRECT PayPal payments to replace Wix Pricing Plans checkout. Read HANDOVER.md, the
"Payment off Wix" appendix in AUTH-MIGRATION-PLAN.md, WIX-DEPENDENCY.md, and marketing_crm/STATUS.md
first.

Decision (locked): PayPal-direct — my PayPal settles to FNB (SA bank) and is wired to QuickBooks; tax
is an accountant-owned, scales-with-traction concern, NOT a processor problem (per the appendix).
Pre-launch, no live subscriptions to migrate.

REUSE 100% of what's built — do NOT duplicate billing logic:
  - billing_service.py grant_entitlement(...) and the entitlement/subscription model already turn
    "a purchase happened" into granted credits, idempotently.
  - subscriptions_api.py POST /api/billing/subscription/event is the existing normalize->grant path;
    the PayPal webhook receiver should map PayPal events into THIS same flow (reuse, don't reinvent).
  - core_db (core.subscription / core.credit_ledger) for the canonical mirror.
  - Follow the marketing_crm dark-by-default + env-gated pattern (PAYPAL_ENABLED, default off), the
    lane-guard (CLAUDE_CODE=1 on code commits), commit+push to main, and seed->assert->purge
    verification (PayPal sandbox for the live API).

Scope (propose the file plan + sequence for approval BEFORE coding):
  1. PayPal catalog: create Products + Billing Plans mirroring frontend/pricing.html plan defs
     (recurring) — script it, store plan_id mapping.
  2. Checkout: replace the wix-checkout postMessage in frontend/pricing.html with PayPal JS SDK
     buttons — Subscriptions API for recurring, Orders API for PAYG top-ups.
  3. Webhook receiver: a new endpoint that verifies PayPal webhook signatures and maps
     BILLING.SUBSCRIPTION.ACTIVATED/CANCELLED/EXPIRED + PAYMENT.SALE.COMPLETED -> the existing
     grant/subscription logic. Idempotent.
  4. Keep Wix payment working as a fallback behind the flag until PayPal is proven; sandbox-test
     everything first; then flip PAYPAL_ENABLED=1.
  5. Update marketing_crm/STATUS.md + AUTH-MIGRATION-PLAN.md.

Env to expect: PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_WEBHOOK_ID, PAYPAL_ENV (sandbox|live).
Start by confirming the plan; don't write code until I approve.
```

## KICKOFF PROMPT — CORE.* BILLING/USAGE STRATEGY (due-diligence session, NEW chat)

> Added 2026-06-16 after the PayPal launch deferred the `core.*` payment mirror. This is a
> **think-first** session: produce a recommendation, not code. Background: payments (PayPal),
> auth (Clerk prod), and marketing (Render) are all LIVE — fully off Wix.

```
Focused DUE-DILIGENCE + RECOMMENDATION session on the core.* data layer — specifically whether
(and how) to make core.* the canonical home for BILLING + USAGE data, or to keep billing.* as the
system of record. This is THINK-FIRST: deliver a recommendation doc; do NOT implement until I approve
a direction.

Context: we're fully live (marketing→Render, auth→Clerk prod, payments→direct PayPal; off Wix).
billing.* is the live system of record for entitlements/credits and is correct. The newer core.*
schema (core_db/) is the canonical model (account/user/person, subscription, credit_ledger, matches,
usage_event, consent). IDENTITY already fills going forward (signup/consent + auth_v2 new signups
write core.account/user/person), but the BILLING slice (core.subscription, core.credit_ledger) and
matches/usage are NOT fed from the billing/payment/upload paths — so cockpit MRR/credit views read
empty tables. The PayPal launch deliberately deferred the payment→core mirror (marketing_crm/STATUS.md
"Not built yet").

Read-order:
1. marketing_crm/STATUS.md — current state + the deferred core.* mirror note.
2. core_db/README.md + DB-SCHEMA-PROPOSAL.md — the canonical model + rationale.
3. core_db/repositories/ (accounts/subscriptions/matches) + core_db/schema.py (vw_account_credits,
   vw_subscription_current, vw_mrr).
4. subscriptions_api.py::apply_subscription_event (grant path) + billing_service.py (grant/consume)
   + the consumption path (match upload → billing.entitlement_consumption).
5. marketing_crm/backoffice/ — what the cockpit actually reads from core.*.
6. ARCHITECTURE.md / DATA-INVENTORY.md — medallion + where data lives.

Answer (the DD):
- Trace exactly what is fed into core.* today vs not (identity vs billing vs matches vs usage).
- Who CONSUMES core.* (cockpit, CRM sync, customer-facing)? What under-reports because billing/usage
  are empty?
- Intended end-state options, each with effort/risk: (A) billing.* stays SoR + core.* mirror for
  analytics; (B) core.* becomes the true SoR, billing.* derived/retired; (C) skip the second
  write-path — feed the cockpit with SQL views over billing.* instead. Recommend one.
- The ledger trap: core.credit_ledger balance = grants − consumption; both sides must be wired
  together (grants in payment path, consumption in upload path) or balances are wrong.
- Identity reconciliation: billing.account vs core.account mapping (ensure_identity) — drift/dupes?
- Is the value (MRR/credit/churn analytics, referrals, unified identity) worth the dual-write cost
  now, or is a leaner path better?

Deliverable: docs/_investigation/core_db_billing_strategy.md — what core.* is for, the options, a
clear recommendation with effort/risk, and (only if "proceed") a phased plan
(identity → subscriptions → credit ledger both-sides → matches/usage → cockpit cutover). Propose for
approval BEFORE any code.

Constraints: billing.* stays SoR until an explicit cutover; additive + dark-by-default + env-gated;
lane guard (CLAUDE_CODE=1 on code commits); commit+push to main; seed→assert→purge for any prod write.
```

## KICKOFF PROMPT — WIX PAYMENT DEPRECATION (migration, NEW chat)

> Added 2026-06-17. Direct PayPal is LIVE; the Wix payment path remains ONLY as the
> `PAYPAL_ENABLED=0` rollback. **SOAK GATE: do not start until PayPal has processed real
> customer traffic cleanly (first paying customers / ~2 weeks).** This OVERLAPS the auth lane's
> `external_wix_id` work — coordinate; it's a migration, not a delete. Full scope in
> `marketing_crm/STATUS.md` "Not built yet" → Wix PAYMENT deprecation.

```
We're deprecating the Wix PAYMENT path now that direct PayPal is live and soaked. This is a
MIGRATION, not a delete — propose the plan + migration steps for approval BEFORE writing code.

Read first: marketing_crm/STATUS.md ("Not built yet" → Wix PAYMENT deprecation), WIX-DEPENDENCY.md,
paypal_billing/README.md, billing_service.py, subscriptions_api.py, frontend/pricing.html, render.yaml.

Hard constraint — `external_wix_id` is NOT dead: live PayPal grants reuse
billing.entitlement_grant.external_wix_id as their idempotency key (`purchase:{order_id}:{account_id}`).
So it CANNOT be dropped — it must be RENAMED `external_wix_id -> external_id`, migrating in lockstep:
the unique index (account_id, source, plan_code, external_wix_id), billing_service.grant_entitlement,
subscriptions_api.apply_subscription_event, monthly_refill, models_billing.py. Keep `wix_subscription`/
`wix_payg` grant sources + CHECK (historical rows carry them). This overlaps the auth lane's
external_wix_id work — confirm ownership/coordination before touching shared schema.

Two buckets:
1. Load-bearing — migrate, don't delete: the external_wix_id rename above; the Wix webhook endpoint
   `/api/billing/subscription/event` + the `provider='wix'` path in apply_subscription_event; the
   pricing.html Wix `postMessage`/`wixPlanId` fallback. These stay until the soak gate clears, then retire.
2. Dead now — safe to remove: WIX_NOTIFY_UPLOAD_COMPLETE_URL + RENDER_TO_WIX_OPS_KEY (render.yaml both
   services + notify code in coach_invite/video_complete_email.py + ingest paths — already inactive);
   account.external_wix_id (stored wixMemberId) IF verified unused.

Deliverable: a migration plan (idempotent schema rename + code cutover + the dead-code removals + a
verification step that live PayPal idempotency still holds), proposed for approval first. Confirm PayPal
has soaked (real purchases processed cleanly) before executing. No automated tests — validate against the
live DB with seed->assert->purge; keep PAYPAL_ENABLED=0 rollback intact until the very last step.
Constraints: lane guard (CLAUDE_CODE=1), commit+push to main, coordinate the shared external_wix_id with
the auth lane.
```
