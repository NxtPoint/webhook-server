# De-Wix Decommission Plan (Ten-Fifty5)

**Status: PLANNED, not started (noted 2026-07-18). Do NOT rush — no business upside, real downside if botched.**

## Context
Ten-Fifty5 started on Wix and migrated to the Render custom stack. As of 2026-07 the LIVE product is 100%
Render: auth = **Clerk** (`AUTH_V2_ENABLED=1`), payments = **PayPal** (`PAYPAL_ENABLED=1`, live), all pages
served by `locker_room_app`. **Wix is now DNS/routing only at the infra level, and there were never any Wix
customers** — so nothing user-facing touches Wix, and the Wix webhooks/onboarding never fire.

**BUT at the code/DB level, Wix is still structural, not cosmetic** — ~48 files reference it, including the
data model. This is dormant legacy scaffolding, inert at runtime, but load-bearing schema. It is harmless to
leave; removing it is a deliberate migration project, not a cleanup.

## What's entangled (the real scope)
- **DB schema (the hard part):**
  - `core_db.account.external_wix_id` (Text) — a sync/reconcile + identity key. `members_api._find_account`
    still looks accounts up by it.
  - `core_db.credit_ledger.external_wix_id` + a **UNIQUE INDEX** `uq_credit_grant_idem (account_id, source,
    plan_code, external_wix_id)` — **billing grant idempotency depends on this column.**
  - A billing **CHECK constraint** allowing sources `'wix_subscription'`, `'wix_payg'` — **existing rows may
    carry these values.**
  - `account.auth_provider_uid` today holds the wixMemberId for legacy rows.
- **APIs / services (dormant runtime):** `members_api.py` (Wix onboarding sync, server-to-server), 
  `subscriptions_api.py` (Wix subscription webhook), `billing_service.py` (Wix source types + idempotency),
  `usage_api.py`, upload-complete notify (`WIX_NOTIFY_UPLOAD_COMPLETE_URL` / `RENDER_TO_WIX_OPS_KEY`).
- **Frontend (dormant fallbacks):** `pricing.html` Wix checkout fallback (`wixPlanId` / `wix-checkout`
  postMessage) — dormant while `PAYPAL_ENABLED=1`; `portal.html` / `players_enclosure.html` `wixMemberId`
  handling; `APP_BASE_URL` still defaults to the Wix portal URL (override/retire).

## Why not just delete it
Dropping `external_wix_id` / the CHECK constraint / the idempotency index is a **DB migration** that billing
idempotency and account lookup depend on. Get it wrong on the live product and you break billing or account
resolution for real customers. This is a staged project with a rollback at each step — not a bulk delete.

## Staged plan (each stage: verify against `scripts`/scenario harness, keep a rollback)
1. **Audit dead vs live.** Log/confirm the Wix webhook + onboarding + upload-notify paths never execute now
   (no callers). Confirm `external_wix_id` is not written by any live path (Clerk provides identity).
2. **Frontend first (lowest risk).** Remove the dormant Wix checkout fallback from `pricing.html` and the
   `wixMemberId` handling from `portal.html`/`players_enclosure.html`; repoint/retire `APP_BASE_URL`. Verify
   PayPal checkout + Clerk login unaffected.
3. **Retire dormant API handlers.** Remove the Wix subscription webhook (`subscriptions_api`), Wix onboarding
   sync (`members_api`), and the upload-complete Wix notify — after confirming zero live callers. Remove the
   `WIX_*` env from `render.yaml`.
4. **DB migration (last, most care).** Generalise or drop `external_wix_id` (consider renaming to a neutral
   `external_ref` if any historical idempotency value must be preserved); relax/rewrite the grant-idempotency
   UNIQUE INDEX and the source CHECK constraint (migrate/retain historical `wix_*` source rows). Idempotent
   DDL, run twice, second run a no-op.
5. **Cleanup.** Remove residual `wix` references, comments, and `docs/wix_auth.md` once the code is gone.

## Recommendation
**Leave it for now.** It's inert, invisible to users, and costs nothing. Schedule this as its own reviewed
task only if code hygiene warrants it. Cross-brand marketing engine + reporting is documented in the
courtflow repo at `docs/specs/MARKETING-ENGINE.md`.
