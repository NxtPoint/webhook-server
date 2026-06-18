# Wix Migration Record (historical / archive)

> **Archived reference.** The off-Wix migration is COMPLETE (auth → Clerk, payment → direct PayPal, marketing → native Render; Wix retained only as the `PAYPAL_ENABLED=0` rollback). This document preserves the pre-/during-migration maps and handover prompts. For current state see [`../growth-and-crm.md`](../growth-and-crm.md), [`../architecture.md`](../architecture.md), and [`../billing-implementation.md`](../billing-implementation.md).

Sources merged (verbatim): `WIX-DEPENDENCY.md`, `AUTH-MIGRATION-PLAN.md`, `HANDOVER.md`.
---

# WIX-DEPENDENCY.md (Wix coupling map)

# WIX-DEPENDENCY.md

> **Purpose.** Exactly what Wix is responsible for today, how tightly we're coupled, and what it would take to migrate auth + the portal off Wix later — with a difficulty rating and risk list. Audience: Tomo + future Claude sessions. Companions: [`../architecture.md`](../architecture.md), [`../architecture.md`](../architecture.md).
>
> **Freshness.** 2026-06-16 from code. Front-end line numbers (`frontend/*.html`) drift — file is the anchor. Wix-side internals (Velo code, Secrets Manager, Pricing Plans) are invisible to this repo and noted as such.

---

## 1. Executive summary

> **UPDATE 2026-06-16 — Wix is effectively retired.** Both remaining couplings were migrated the
> same day: **Payment** moved to **direct PayPal** (`paypal_billing/`, LIVE — Wix Pricing Plans
> checkout retired; see `paypal_billing/README.md`), and **Authentication** moved to **Clerk**
> (`auth_v2/`, LIVE dual-mode — see `../growth-and-crm.md`). Marketing/data were already off Wix.
> The detailed sections below are the pre-migration map, kept for history. **The Wix auth `postMessage`
> handoff has since been REMOVED from the code (2026-06-17 — `portal.html` + `players_enclosure.html`);
> Clerk is the only login door.** Remaining cleanup: delete the legacy `CLIENT_API_KEY` (now a pure
> fallback) + the inactive `WIX_NOTIFY_*` env; payment-Wix relay + `external_wix_id` columns at baseline.

As of the 2026-06-15 marketing migration, Wix had been reduced to **three responsibilities, all hard-coupled and interdependent** (all now migrated — see the update above):

1. **Authentication** — Wix login was the identity source; handed off `email` + the shared `CLIENT_API_KEY` to our portal via `postMessage`. → **Now Clerk** (`auth_v2/`, dual-mode).
2. **Payment checkout** — Wix Pricing Plans → PayPal was the only payment path. → **Now direct PayPal** (`paypal_billing/`, LIVE), no Wix in the loop.
3. **Subscription webhook** — Wix POSTed lifecycle events to `/api/billing/subscription/event`. → **Now the PayPal webhook** (`/api/billing/paypal/webhook`) feeds the same `apply_subscription_event` grant path. The Wix endpoint remains for the rollback fallback.

Everything else — marketing site, member profile data, billing state storage, email — already lived on our side. One legacy coupling (`WIX_NOTIFY_*`) is inactive.

**Overall coupling (pre-migration): ⭐⭐⭐⭐ Very Tight** on the auth+payment axis; **⭐ None** on marketing/data. **Post-migration: ⭐ None** — Wix retained only as a rollback fallback until the handoff is removed.

---

## 2. Hard couplings

### 2.1 Authentication — ⭐⭐⭐⭐ Very High to replace

**Files:** `frontend/portal.html` (auth handoff + listener + child-iframe forwarding), `frontend/players_enclosure.html` (URL-param + postMessage parsing), `locker_room_app.py` (`APP_BASE_URL` default = Wix Studio URL), `client_api.py` (AUTH guard + `wix_member_id` on registration), `billing_service.py` (`external_wix_id`).

**How it works:**
1. User logs in on Wix (`info5945780.wixstudio.com/online-tennis-analyt/portal`).
2. Wix Velo code (Wix-side, not in repo) reads `CLIENT_API_KEY` from Wix Secrets Manager + the Wix member identity.
3. Wix `postMessage`s a `wix-handoff` payload (`email`, `key`=`CLIENT_API_KEY`, `firstName`, `surname`, `wixMemberId`, `api`) to the embedded portal iframe (`locker-room-…onrender.com/portal`).
4. `portal.html` listener → `applyAuth()` extracts them and **forwards them as URL query params** to every child iframe (`/media-room?email=…&key=…&api=…`).
5. Child pages call `/api/client/*` with header `X-Client-Key: <CLIENT_API_KEY>` + `?email=`. Backend checks the key matches the env var and returns that email's data.

**Why it's hard:**
- **There is no real auth.** It's a single shared key + an email param. No per-user token, session, password, or signature. The backend trusts whatever `email` is supplied as long as the one global key matches.
- Wix is the sole identity provider; the `postMessage` channel is the only path for the secret key to reach the browser without sitting in a URL.
- The 5s fallback (no handoff received) just renders "Configuration Required" — there is no alternative login.
- `wixMemberId` is stored as `external_wix_id` but isn't used for anything critical, so a new auth provider can ignore it.

### 2.2 Payment checkout — ⭐⭐⭐⭐⭐ Extreme to replace

**Files:** `frontend/pricing.html` (hardcoded `wixPlanId` UUIDs for 6 plans; `selectPlan()` posts `wix-checkout`), `frontend/portal.html` (relays `wix-checkout` to the Wix parent), Wix-side Velo (`checkout.startOnlinePurchase`).

**Flow:** user clicks Upgrade → `selectPlan(wixPlanId)` → `postMessage({type:'wix-checkout', planId})` → portal relays to Wix parent → Wix calls `checkout.startOnlinePurchase(planId)` → PayPal. Our code cannot intercept the cross-origin checkout.

**Why it's hard:**
- Every plan requires a `wixPlanId` UUID generated in Wix; plan/price changes mean editing **both** Wix and `frontend/pricing.html`.
- Switching processors means a new SDK in the frontend (Stripe.js etc.), a new checkout flow, and migrating existing subscriptions out of Wix.

### 2.3 Subscription webhook — ⭐⭐⭐⭐ Very High to replace

**File:** `subscriptions_api.py` — `POST /api/billing/subscription/event` (OPS_KEY auth).

- Events: `PLAN_PURCHASED`, `PLAN_CANCELLED`, `RECURRING_PAYMENT_CANCELLED`.
- Idempotent by sha256 `event_id`; safe on retries (writes `subscription_event_log`, upserts `subscription_state`).
- On `PLAN_PURCHASED + ACTIVE` → immediate `grant_entitlement()` (idempotent via `external_wix_id=purchase:{order_id}:{account_id}`) so the user can upload at once.
- The **monthly refill cron depends on `subscription_state`** existing. Without this webhook, users pay but get no credits.

**Why it's hard:** a new processor's webhook schema + lifecycle (e.g. Stripe) won't map 1:1; needs re-mapping + test events.

---

## 3. Legacy / inactive coupling

**`WIX_NOTIFY_UPLOAD_COMPLETE_URL` + `RENDER_TO_WIX_OPS_KEY`** (`render.yaml`, referenced in ingest/`coach_invite/video_complete_email.py` with a "remove once Wix retired" comment). Appears **inactive in prod** (env not set). Was used to tell Wix when video was ready; now we own the portal UI. Safe to delete when convenient.

---

## 4. Page hosting (already migrated — no coupling)

`locker_room_app.py` host-switches on `request.host` (`_is_marketing_host()` vs `MARKETING_HOSTS`). `www.ten-fifty5.com` / apex now serve **native Render HTML** (marketing site, fully crawlable); the Wix app moved to its free `wixstudio.com` URL. The portal SPA itself is **our** HTML embedded in a Wix iframe — Wix just provides the outer page + auth. So Wix has **zero** role in marketing/SEO and only an iframe-host role for the logged-in app.

---

## 5. Coupling tightness scorecard

| Dimension | Tightness | Why |
|---|---|---|
| Authentication | ⭐⭐⭐⭐⭐ | No alternative auth; entirely Wix-driven |
| Payment | ⭐⭐⭐⭐⭐ | PayPal only via Wix; no fallback |
| Subscription state ingress | ⭐⭐⭐ | Webhook-driven; replaceable but lifecycle-specific |
| Member data | ⭐ | Stored in our DB; Wix only seeds email/name |
| Marketing / SEO | ⭐ (none) | Fully on Render |
| **Overall** | **⭐⭐⭐⭐** | Can migrate pieces, but auth + payment must move together |

---

> **Detailed migration plan:** the AUTH-MIGRATION-PLAN section of this file — provider
> recommendation (Clerk/Auth0/Cognito), phased no-downtime auth migration with effort/risk per phase,
> and a payment-off-Wix appendix (PayPal-direct vs Stripe vs Merchant-of-Record sizing). The summary
> below stays here for context.

## 6. Migration plan (off Wix)

**Sequencing principle:** payment depends on identities existing, so **own auth first, then payment** (the reverse of how they're entangled today). Marketing + member data are already done. Keep Wix running in parallel for a gradual cutover — no big-bang.

### Phase 0 — capture Wix-side state (do before touching code)
- Export Wix Velo code on `/portal` (screenshot/PDF — not in source control).
- Document `CLIENT_API_KEY` rotation in Wix Secrets Manager.
- Export the Pricing Plans catalogue (UUID → price/interval/name).
- Export the full subscription event/state history from Wix (API or manual).

### Phase 1 — own authentication (the big one)
- Stand up an auth provider (Auth0 / Supabase / Clerk / custom OIDC). Build a real login page (no longer Wix).
- **Replace the shared `CLIENT_API_KEY` + email-param model with per-user tokens (JWT/session).** Update the `client_api.py` AUTH guard to validate a token and derive the account from it (not from a supplied email). *This is also the #1 security fix in `ARCHITECTURE.md` §6.1 — do it here regardless of Wix.*
- Update `portal.html` + child SPAs to carry a token instead of the handoff key.
- Migrate `account.external_wix_id` → new auth provider id (keep the column for audit).

### Phase 2 — own payment
- Choose a processor (Stripe/Paddle/Lemon Squeezy). Build our pricing/checkout page.
- Replace `wixPlanId` UUIDs in `frontend/pricing.html` with the new provider's price IDs; swap `postMessage` checkout for the provider SDK.
- Point the new provider's webhook at `/api/billing/subscription/event` (or a new endpoint) and re-map event types → our `subscription_state` upsert + `grant_entitlement()`.
- Migrate active subscriptions from Wix to the new processor (may require customer re-auth of payment).

### Phase 3 — decommission
- Remove the Wix iframe/handoff from `portal.html`; point CTAs at our own login.
- Delete `WIX_NOTIFY_*` + the inactive notify code.
- Retire the `wixstudio.com` app.

---

## 7. Difficulty & risks

**Difficulty: HIGH overall (⭐⭐⭐⭐).** Not because any single piece is huge, but because auth + payment are interlocked and both touch live customer money + access.

**Risks:**
1. **Auth rewrite is also a security upgrade** — the current shared-key model is unsafe (`ARCHITECTURE.md` §6.1), so Phase 1 isn't optional polish; it's a prerequisite for being a real SaaS. Scope it as such.
2. **Payment migration touches live revenue** — existing subscribers may need to re-enter payment details; mishandling = churn or double-billing. Migrate in cohorts, reconcile against Wix exports.
3. **Wix-side code is unversioned** — Velo + Secrets + Plans live only in the Wix editor. Losing access or a Wix change mid-migration could break login/payment with no rollback in git. Capture Phase 0 first.
4. **Subscription state drift** — there's no periodic reconcile *from* Wix today; a dropped webhook silently desyncs `subscription_state`. During migration, run both sources and diff.
5. **No test coverage** for billing/auth (`ARCHITECTURE.md` §6.4) — every change is validated against the live DB. Build at least smoke tests for the new auth + webhook before cutover.
6. **Cutover coordination** — auth, payment, and the portal iframe all change together for end users; stage behind a flag / cohort and keep Wix as fallback until the new path is proven.

---

## 8. Cannot determine from code (Wix-side)

Wix Secrets Manager (key storage/rotation), the `/portal` Velo handoff code, Pricing Plan definitions (only UUIDs are in our repo), PayPal integration config, `wixUsers.getCurrentUser()` behaviour, OAuth vs session model, Wix member-data retention/export. All must be inspected in the Wix editor/console.

---

# AUTH-MIGRATION-PLAN.md (de-Wix auth plan)

# AUTH-MIGRATION-PLAN.md — getting auth (and later, payment) off Wix

> **Status: ✅ ACTIONED — Phases 0-3 SHIPPED (2026-06-16/17). Phase 4 (delete the shared key) pending.**
> Clerk is LIVE in production (`clerk.ten-fifty5.com`, `pk_live`, own Google OAuth); `auth_v2/` verifies
> the JWT; the Wix `postMessage` handoff is REMOVED from the code; marketing CTAs point at `/login`.
> The legacy `CLIENT_API_KEY` is now a pure fallback across every client surface (Phase 4 = delete it,
> after a verification window). The text below is the original plan, **kept as the executed-record**;
> live state is in [`../growth-and-crm.md`](../growth-and-crm.md) and the phase table in §4 is
> annotated DONE/PENDING. Builds on the WIX-DEPENDENCY section of this file + the `core.user` identity
> entity + consent write-path (`core_db/`, `marketing_crm/consent/`).

## TL;DR
- **Do it pre-launch.** With ~0 real customers, the hard part of any auth/payment migration —
  *preserving existing logins/subscriptions with no downtime* — is essentially free right now. Every
  month of real users makes it harder. **The cost of waiting is the migration itself getting bigger.**
- **Recommended auth provider: Clerk** (primary), Auth0 (portable/enterprise alternative), AWS Cognito
  (cost-at-scale, AWS-native). The choice is low-stakes because **we already own roles, parental
  consent, and coach↔player links in our DB** (`core.person` / `core.relationship` / `core.consent`).
  The provider only needs to do identity (login, sessions, email verification, social/MFA) and hand us
  a stable user id — which `core.user.auth_provider` + `auth_provider_uid` is already built to store.
- **This also fixes our #1 security gap**: the single shared `CLIENT_API_KEY` + `email` param
  (anyone with the key can read any account). Per-user tokens replace it.
- **Payment is a separate track** — keep Wix/PayPal for billing during the auth move, decouple them.
  Direct-PayPal is a *medium* build (the billing backend is already provider-agnostic); see Appendix.

---

## 1. Current state (from WIX-DEPENDENCY.md)
Wix does three things: **(a) authentication** (login → `postMessage` handoff of `email` + the shared
`CLIENT_API_KEY` to our portal iframe), **(b) payment checkout** (Wix Pricing Plans → PayPal), and
**(c) the subscription webhook**. Everything else (marketing, member data, billing state, analysis) is
ours. Auth has no fallback: no Wix handoff → no login.

What we've since built that helps:
- `core.user` (login identity) with `auth_provider` + `auth_provider_uid` — purpose-built for an
  external IdP's stable id.
- The **consent write-path**: recording consent already creates `core.account/user/person`. New signups
  populate `core.*` regardless of provider.
- `core.person.role` (player/parent/coach), `core.relationship` (coach↔player, parent↔junior),
  `core.consent` (incl. `minor_processing_parental`) — our domain model, provider-independent.

## 2. Auth provider recommendation

| Provider | Fit | Watch-outs |
|---|---|---|
| **Clerk (recommended)** | Fastest to ship for B2C: drop-in hosted UI + components, sessions/JWT, email verification, password reset, social, MFA, magic links — all the painful identity bits done. Generous free tier (great pre-launch). `publicMetadata` can hold our role, but we keep roles authoritative in `core.*`. | Pricing scales with MAU; it's a managed dependency (we're trading one SaaS for a better-fit one — acceptable, it's a commodity). |
| **Auth0 (alternative)** | Most mature/portable OIDC; Actions for custom claims; bulk user import; RBAC. Good if we want vendor-neutral standards. | More config overhead; pricing climbs. |
| **AWS Cognito (alternative)** | AWS-native (we already use S3/Batch/SES), cheapest at scale, JWT/OIDC. | Rougher DX, dated hosted UI, more glue code. |
| Supabase Auth | Cheap/open (GoTrue). | Cleanest when paired with Supabase Postgres; we use Render Postgres, so it's auth-only and slightly awkward. |

**Why the choice is low-stakes:** roles, minors/parental consent, and coach-player links are *our*
domain (`core.*`), not the IdP's. The IdP just authenticates a person and gives us a stable id we map
to `core.user`. So we can pick on DX/price/migration-ease and swap later if needed. **Recommendation:
Clerk** for speed + lowest maintenance as a solo team, revisit only if cost-at-scale or AWS
consolidation becomes the priority (→ Cognito).

## 3. Server-side change that underpins everything
Replace the shared-key guard with **per-user token verification**:
- Client sends the IdP session JWT (Authorization: Bearer) instead of `X-Client-Key` + `?email=`.
- A middleware verifies the JWT (IdP JWKS), resolves `core.user` by `auth_provider_uid`, and derives
  the account/role server-side. **The client can no longer assert which account it is.**
- Admin endpoints check role from `core.*`, not a hardcoded email list.
- Keep the legacy `CLIENT_API_KEY` path alive *only* during the dual-run window, then delete it.

## 4. Phased migration (no downtime, preserve logins)

> **STATUS (2026-06-17): Phases 0-3 ✅ DONE, Phase 4 ⏳ PENDING.** 0 — `auth_v2/` JWT-verify middleware
> built, dual-mode. 1 — `/login` (Clerk) + signups → `core.*` live. 2 — trivial (pre-launch, only Tomo;
> his account auto-relinked dev→prod by email). 3 — portal flipped to Clerk, Wix `postMessage` handoff
> removed from code, CTAs → `/login`, Clerk PRODUCTION live. 4 — remaining: delete the shared
> `CLIENT_API_KEY` (now a pure fallback; every surface is dual-mode) + remove inactive `WIX_NOTIFY_*`.

| Phase | What | Effort | Risk |
|---|---|---|---|
| **0 — Prep** | Pick provider; create tenant/app; build JWT-verify middleware in the API (accepts new tokens *and* the legacy key during transition); map IdP id → `core.user.auth_provider_uid`. No user-facing change. | S (days) | Low — additive |
| **1 — New signup path** | Stand up our own login/signup UI (IdP components) on a Render route. New users sign up via the IdP → `core.*` (consent write-path already does this). Wix users still work via the old handoff. Both auth methods accepted by the API. | M (1–2 wks) | Low–Med — runs in parallel |
| **2 — Migrate existing users** | Bulk-import Wix members (email + name) into the IdP. **Wix passwords can't be exported**, so existing users do a one-time magic-link / password-reset on first new login. Backfill `core.user.auth_provider_uid`. *(Pre-launch: this set is ~empty → near-zero effort.)* | S–M (scales with user count) | Med — the classic migration risk; trivial now, grows later |
| **3 — Cut over** | Flip the portal to our auth; retire the Wix `postMessage` handoff and the `APP_BASE_URL` wixstudio dependency for login. Wix keeps *only* payment until that track moves. | M | Med — coordinated switch; gate behind a flag, keep Wix as fallback a few days |
| **4 — Harden** | Remove the shared `CLIENT_API_KEY` path entirely; enforce per-user tokens + role checks everywhere; rotate keys. | S | Low — cleanup, but verify no caller depends on the old path |

**Rollback:** Phases 0–2 are additive (old path still works). The risky moment is Phase 3 — mitigate
with a feature flag + parallel run, exactly as we've done with the dark-by-default switches.

## 5. Interaction with what's built
- Signup already records consent + creates `core.*` — the new IdP signup reuses that path (just swap
  "who proves the email" from Wix to the IdP).
- `marketing_opt_in`, roles, minor flags, coach links all stay in `core.*` — untouched by the auth swap.
- Payment stays on Wix throughout (decoupled). Don't do both at once.

---

## Appendix — Payment off Wix: how hard is direct PayPal? (answering the question)

**Short answer: it's a *medium* build, not a monster — mostly because our billing backend is already
provider-agnostic.** And like auth, it's far easier pre-launch (no live subscriptions to migrate).

**What's already done (the hard half):** `billing.*` (entitlement grants/consumption, subscription
state) + `subscriptions_api.py` already turn "a purchase happened" into granted credits, idempotently.
A new processor just needs to *feed that* a normalized event.

**What direct PayPal needs (the new half):**
1. **Catalog**: create Product + Billing Plans in PayPal (recurring) — mirrors the hardcoded plans in
   `frontend/pricing.html`.
2. **Checkout**: PayPal JS SDK buttons → create/approve a Subscription (recurring) or Order (one-off
   PAYG). Replaces the `postMessage` → Wix checkout.
3. **Webhook receiver**: verify PayPal webhook signatures; map `BILLING.SUBSCRIPTION.ACTIVATED/
   CANCELLED/EXPIRED` + `PAYMENT.SALE.COMPLETED` (renewals) → our existing grant/subscription logic.
4. **Reconcile + edge cases**: failed renewals/dunning, refunds, plan changes, sandbox testing.

**Good news:** PCI scope stays offloaded (no card data touches us). **Fiddly bits:** PayPal's
subscription API + webhook signature verification are the least pleasant of the major options, and
sandbox testing takes time.

**Sizing:** ~**1–2 weeks** for a solid, sandbox-tested recurring + PAYG + webhook integration
(less if PAYG-only). Not a big architectural lift — a contained, well-scoped one.

**Worth weighing before committing to PayPal-direct:**
- **Stripe** — generally smoother API/docs than PayPal, and Stripe Billing ships a hosted customer
  portal (manage/cancel subscription) we'd otherwise build. Cards + wallets. Slightly less work overall.
- **Merchant of Record (Paddle / Lemon Squeezy)** — *they* become the seller and **handle global sales
  tax / EU VAT**, dunning, and checkout. For an international B2C SaaS run by a small team, removing the
  VAT/tax-compliance burden is a real, ongoing win — usually worth the higher % fee. Strong contender.
- **PayPal-direct** — viable, and PayPal brand trust + our half-built backend help; but its API is the
  fiddliest, and it doesn't solve tax.

**Decision (2026-06-16): PayPal-direct.** Tomo's PayPal account settles to FNB (SA bank) and is already
wired to QuickBooks, with easy fund repatriation — concrete operational reasons that outweigh the
generic MoR/Stripe pitch for this business. So the ~1–2 week PayPal-direct build above is the path.

**On tax (clarified):** the "tax piece" is **not** a payment-integration problem and no processor
choice creates or removes it. Two buckets: (1) **SA taxes** (income; SA VAT only past the ~R1m
threshold) — handled by the accountant + QuickBooks, PayPal-direct is fine. (2) **Foreign consumption
tax** (EU/UK VAT on B2C digital services) — for a non-resident seller this technically applies from the
first sale; a Merchant of Record would absorb that liability, PayPal-direct leaves it with us. For an
early-stage SA business with low international consumer volume it's a **"handle as regional traction
becomes material"** obligation, **owned by the accountant**, not a launch blocker and not something the
integration touches. Action: ask the accountant "when do I need to worry about EU/UK VAT?" at some point;
don't let it shape the build.

**Build status (2026-06-16): LIVE.** The PayPal-direct integration in `paypal_billing/` is
**live in production** (`PAYPAL_ENABLED=1`, `PAYPAL_ENV=live`) — proven end-to-end on sandbox AND a
real live purchase (PAYG + subscribe + cancel). It is **vanilla PayPal, `billing.*` only** (core mirror
deferred): catalog tooling, the shared grant-path refactor (`subscriptions_api.apply_subscription_event`
serves both Wix and PayPal), a signature-verified webhook receiver that refetches before granting,
secure server-side checkout endpoints (dual-mode auth — Clerk JWT or legacy key), and PayPal Buttons +
cancel in `frontend/pricing.html`. **Wix Pricing Plans checkout is retired** — it remains only as the
`PAYPAL_ENABLED=0` rollback fallback. Full runbook: `paypal_billing/README.md`. So **payment is now off
Wix** — the §6.2/Phase-2 "own payment" work is done.

---

# HANDOVER.md (auth/payment migration kickoff)

# HANDOVER.md — picking up the growth stack / auth / payment in a fresh session

> For a new Claude Code chat continuing this work. Paste the relevant **kickoff prompt** (below) to
> start with full context. Built 2026-06-16; pre-launch, no real customers (free to deploy to prod).

## Read-order (5 minutes)
1. `../growth-and-crm.md` — the living hymn sheet: what's built, every enable switch, lanes/ownership, events, open items.
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
- **Verify against prod safely:** the dev box reaches prod Postgres. Use `core_db.seed` (`--force --allow-remote`) → assert → **purge**; never leave test rows. Update `../growth-and-crm.md` when state changes.
- **Don't do auth and payment in the same chat/session** (independent tracks; keep them decoupled). Run them sequentially to avoid two agents racing on `main`.

---

## KICKOFF PROMPT — AUTH (de-Wix authentication)

> **✅ DONE 2026-06-16/17 — this kickoff is historical.** De-Wix auth shipped: Clerk PRODUCTION live
> (`clerk.ten-fifty5.com`, `pk_live`, own Google OAuth), `auth_v2/` verifies the JWT, all client surfaces
> dual-mode, marketing CTAs → `/login`, Wix `postMessage` handoff removed from code. Only Phase 4 remains
> (delete the legacy `CLIENT_API_KEY` — now a pure fallback). Current state: `../growth-and-crm.md`;
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
> `../growth-and-crm.md` "Not built yet" → Wix PAYMENT deprecation.

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
