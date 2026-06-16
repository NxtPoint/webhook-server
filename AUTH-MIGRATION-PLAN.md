# AUTH-MIGRATION-PLAN.md — getting auth (and later, payment) off Wix

> **Status: PLAN ONLY — not actioned.** Decision doc for when we choose to move. Builds on
> [`WIX-DEPENDENCY.md`](WIX-DEPENDENCY.md) (what Wix owns + coupling) and the `core.user` identity
> entity + consent write-path already built (`core_db/`, `marketing_crm/consent/`). Companion:
> [`ARCHITECTURE.md`](ARCHITECTURE.md) §6.1 (the shared-key auth problem this fixes).

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
