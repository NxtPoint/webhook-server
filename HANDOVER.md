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
