# WIX-DEPENDENCY.md

> **Purpose.** Exactly what Wix is responsible for today, how tightly we're coupled, and what it would take to migrate auth + the portal off Wix later — with a difficulty rating and risk list. Audience: Tomo + future Claude sessions. Companions: [`ARCHITECTURE.md`](ARCHITECTURE.md), [`DATA-INVENTORY.md`](DATA-INVENTORY.md).
>
> **Freshness.** 2026-06-16 from code. Front-end line numbers (`frontend/*.html`) drift — file is the anchor. Wix-side internals (Velo code, Secrets Manager, Pricing Plans) are invisible to this repo and noted as such.

---

## 1. Executive summary

As of the 2026-06-15 marketing migration, Wix is reduced to **three responsibilities, all hard-coupled and interdependent**:

1. **Authentication** — Wix login is the *only* identity source; it hands off `email` + the shared `CLIENT_API_KEY` to our portal via `postMessage`.
2. **Payment checkout** — Wix Pricing Plans → **PayPal** is the *only* payment path (confirmed: no Stripe/other processor).
3. **Subscription webhook** — Wix POSTs lifecycle events to `/api/billing/subscription/event`, the only way credits get granted.

Everything else — marketing site, member profile data, billing state storage, email — already lives on our side. The remaining three are **tightly woven and must be migrated together** (you can't replace payment without owning identity, and you can't grant credits without the webhook). One legacy coupling (`WIX_NOTIFY_*`) is inactive.

**Overall coupling: ⭐⭐⭐⭐ Very Tight** on the auth+payment axis; **⭐ None** on marketing/data.

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

> **Detailed migration plan:** [`AUTH-MIGRATION-PLAN.md`](AUTH-MIGRATION-PLAN.md) — provider
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
