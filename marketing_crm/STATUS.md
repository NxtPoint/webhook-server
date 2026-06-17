# marketing_crm — STATUS (the hymn sheet)

> **Single living source of truth for the growth/CRM/admin stack. Both Claude Code and Cowork read
> AND update this file.** If you change what's built, switches, or ownership — edit here in the same
> change. Point-in-time details elsewhere drift; this wins. Last structural update: 2026-06-16.

## Lanes & file ownership (avoid collisions)
Both agents can now see + edit this repo. Stay in your lane.
**Enforced by a git hook:** `.githooks/pre-commit` BLOCKS any commit that stages code/product files
unless `CLAUDE_CODE=1` (only Claude Code sets it). Docs (`.md`/`.txt`) + `marketing_crm/{klaviyo,privacy}/`
always pass. Activate in a fresh clone with `git config core.hooksPath .githooks`. (Guardrail, not a
vault — for a hard lock add GitHub branch protection + CODEOWNERS.)

| Path | Owner | Notes |
|---|---|---|
| `core_db/` (the `core.*` schema) | **Claude Code** | source of truth; don't hand-edit |
| `marketing_crm/tracking/` `crm_sync/` `backoffice/` `feedback/` | **Claude Code** | code |
| `marketing_crm/contracts/*` | **Shared, CC authoritative** | technical contracts must match code; propose changes, don't silently diverge |
| `marketing_crm/klaviyo/` | **Cowork** | flow copy + build specs |
| `marketing_crm/privacy/` | **Cowork** | policy drafts, consent copy, legal-decision specs |
| `frontend/*.html`, `*_app.py`, `upload_app.py` | **Claude Code** | running product code — Cowork: flag, don't edit |
| this `STATUS.md` | **Shared** | keep it current |

## What's built (Claude Code) — all DARK by default, flip the env to enable

| Capability | Where | Enable | Status |
|---|---|---|---|
| Canonical DB (`core.*`) | `core_db/` | live on prod (empty) | ✅ schema created |
| Cockpit (admin) | `marketing_crm/backoffice/` + `frontend/cockpit.html` (`/cockpit`) | `COCKPIT_ENABLED=1` | ✅ |
| Event tracking | `marketing_crm/tracking/` | `TRACKING_ENABLED=1` (+ `AMPLITUDE_API_KEY`) | ✅ partial events |
| Feedback + NPS | `marketing_crm/feedback/` + `frontend/feedback_widget.js` | `FEEDBACK_ENABLED=1` | ✅ |
| CRM sync (HubSpot+Klaviyo) | `marketing_crm/crm_sync/` | `CRM_SYNC_ENABLED=1` (+ `HUBSPOT_PRIVATE_APP_TOKEN` / `KLAVIYO_API_KEY`) | ✅ code (untested vs live APIs) |
| Consent capture | `marketing_crm/consent/` + `frontend/consent.js` + `/privacy-settings` | `CONSENT_ENABLED=1` | ✅ (DRAFT copy, pre-legal) |
| core_api | `core_api/` | `CORE_API_ENABLED=1` | ✅ |
| De-Wix auth (Clerk) — Phase 0+1 | `auth_v2/` + `client_api._guard()` + `frontend/login.html` (`/login`) | `AUTH_V2_ENABLED=1` (+ `CLERK_PUBLISHABLE_KEY` / `AUTH_JWKS_URL` / `AUTH_ISSUER`) | ✅ code, dark — awaiting Clerk app keys |
| Direct PayPal payments | `paypal_billing/` + `frontend/pricing.html` | `PAYPAL_ENABLED=1` + `PAYPAL_ENV=live` (+ `PAYPAL_CLIENT_ID` / `PAYPAL_SECRET` / `PAYPAL_WEBHOOK_ID`) | ✅ **LIVE 2026-06-16** (env=live). Replaces Wix Pricing Plans checkout. PAYG + subscribe + cancel proven end-to-end on sandbox AND a real live purchase. Reuses `apply_subscription_event`; `billing.*` only (core mirror deferred). Dual-mode auth (Clerk JWT or legacy key, via `client_api._guard`). Rollback: `PAYPAL_ENABLED=0` → instant Wix fallback. Runbook: `paypal_billing/README.md` |

**Consent = the forward write-path into `core.*`:** recording consent ensures the core account/user/
person exist, so `core.*` fills going forward (no backfill needed for new signups). Copy lives in
`frontend/consent.js` + Cowork's `privacy/consent_screens_copy.md` (DRAFT). After legal sign-off, set
the `policy_version` string (consent.js `TF_Consent.setPolicyVersion(...)` + pass into record calls)
and the retention day-counts into `core.retention_rule`. **All three consent moments are wired:**
signup block (players_enclosure), biometric modal before technique submit (media_room), and parental
modal when adding juniors at signup (records `minor_processing_parental` per child).

**Env switches (set on the `webhook-server` Render service):** `COCKPIT_ENABLED`, `TRACKING_ENABLED`,
`FEEDBACK_ENABLED`, `CRM_SYNC_ENABLED`, `CORE_API_ENABLED` + keys `AMPLITUDE_API_KEY`,
`HUBSPOT_PRIVATE_APP_TOKEN`, `KLAVIYO_API_KEY`. Tunables: `NPS_TRIGGER_N` (3), `NPS_COOLDOWN_DAYS` (90).

## ACTIVATED 2026-06-16 (pre-launch, no customers)
`render.yaml` sets `TRACKING_ENABLED=1`, `CONSENT_ENABLED=1`, `FEEDBACK_ENABLED=1`, `COCKPIT_ENABLED=1`
on the webhook-server service. `CRM_SYNC_ENABLED` stays OFF until HubSpot/Klaviyo keys are set.
Consent screen is LIVE on signup (players_enclosure) — copy is DRAFT pending legal; set `policy_version`
after sign-off. tomo.stojakovic@gmail.com backfilled into core (1 acct, 3 persons, 121 matches).

## Page-view analytics (navigation / drop-off)
`frontend/analytics.js` (auto-injected into every Locker-Room-served HTML via `_html()`) → sendBeacon →
`POST /api/track/page` → `core.usage_event` (event_type `page_view`, account by email when authed,
anonymous on public pages) + Amplitude. Self-gates on `TRACKING_ENABLED`. Funnel/drop-off analysis:
query `core.usage_event` by path (or Amplitude once `AMPLITUDE_API_KEY` is set — better for funnels).

## Events emitted (vs contract `contracts/events.md`)
- ✅ `page_view`, `match_uploaded`, `subscription_started`, `subscription_cancelled`, `credit_purchased`,
  `account_created`, `report_viewed`, `nps_submitted`, `feedback_submitted`,
  `cancellation_reason_submitted`, `consent_recorded`
- ⬜ not yet: `match_processed`/`match_failed` (ingest paths, incl. the separate ingest-worker),
  `ai_coach_query`, `technique_uploaded`, `coach_invited`/`coach_accepted`, `login` (Wix-side)

## Critical path to make Cowork's Klaviyo flows LIVE
1. ✅ Emit funnel events (`account_created`, `match_uploaded`, `report_viewed`, `subscription_started`, `credit_purchased`).
2. ✅ DB→Klaviyo/HubSpot feed (`crm_sync`) — profiles + event forwarding.
3. ⬜ Set `CRM_SYNC_ENABLED=1` + `KLAVIYO_API_KEY` + `TRACKING_ENABLED=1` (and HubSpot token).
4. ⬜ **Consent capture** built (privacy spec Part B) → `marketing_opt_in` actually set → marketing emails allowed.
5. ⬜ Cowork: Klaviyo sender domain auth + postal address + assemble flows in Flow Builder.

## Open legal decisions (block minors'-biometrics at scale)
6 decisions in `contracts/privacy_inputs.md` §5 + Cowork's recommended defaults in
`privacy/privacy_decisions_and_consent_spec.md`. After lawyer sign-off, the final **policy version
string**, **retention day-counts**, and **age threshold** come back into `core.consent.policy_version`
+ `core.retention_rule` + minor-gating. Nothing formal in prod yet.

## Not built yet
- Consent-capture UI + loading `policy_version`/`retention_rule` (privacy spec Part B).
- §7 live-data backfill (`billing.*`/`bronze.*` → `core.*`) — or a write-path so `core.*` fills going forward.
  - **`core.*` payment mirror (deferred from the PayPal launch, 2026-06-16).** Direct PayPal writes
    `billing.*` only (entitlement_grant + subscription_state) — same as the Wix flow always did; `core.*`
    is unfed for billing. To populate `core.subscription` / `core.credit_ledger` (cockpit MRR + credit
    analytics), wire a fire-and-forget mirror inside `subscriptions_api.apply_subscription_event`
    (resolve the core account via `core_db.repositories.accounts.ensure_identity`, then
    `repositories.subscriptions.upsert_subscription` + `grant_credits`). **Do it together with the
    CONSUMPTION side** (match-upload path → `consume_match`) or the ledger balance will be inflated —
    that's why it's a scoped session, not a payment-only bolt-on. No payment impact; `billing.*` stays
    the system of record. Repos already exist in `core_db/repositories/subscriptions.py`.
- **Wix PAYMENT deprecation (migration — NOT done in the PayPal launch, by agreement).** PayPal is live;
  the Wix payment path is retained ONLY as the `PAYPAL_ENABLED=0` rollback. Retiring it is a **migration,
  not a delete**, and overlaps the auth lane's `external_wix_id` work — keep it in that lane.
  **SOAK GATE: do not start until PayPal has processed real customer traffic cleanly (~first paying
  customers / ~2 weeks).** Scope:
  - **Load-bearing — must migrate, not delete:** `billing.entitlement_grant.external_wix_id` is **reused
    by live PayPal** (`purchase:{order_id}:{account_id}`) — rename `external_wix_id → external_id` across
    the unique index `(account_id, source, plan_code, external_wix_id)`, `billing_service.grant_entitlement`,
    `subscriptions_api.apply_subscription_event`, `monthly_refill`, `models_billing.py`. The `wix_subscription`/
    `wix_payg` grant sources + CHECK constraint stay (historical rows carry them). The Wix webhook endpoint
    `/api/billing/subscription/event` + `provider='wix'` path and the `pricing.html` Wix `postMessage`
    fallback stay until the soak gate clears, then retire.
  - **Dead now — safe to remove in the same migration:** `WIX_NOTIFY_UPLOAD_COMPLETE_URL` +
    `RENDER_TO_WIX_OPS_KEY` (render.yaml both services + the notify code in `coach_invite/video_complete_email.py`/
    ingest — already inactive); verify `account.external_wix_id` (stored wixMemberId) is truly unused before dropping.
  - Kickoff prompt: `HANDOVER.md` → "WIX PAYMENT DEPRECATION".
- Referral system.
- **De-Wix auth — LIVE 2026-06-16 (Phases 0-3 done; Phase 4 hardening pending).**
  Clerk auth is the front door: marketing CTAs → `/login`, all 10 portal SPAs run
  dual-mode via `TFAuth` (`frontend/auth_client.js`; Clerk loads once in the portal,
  children relay tokens via postMessage), `client_api._guard()` accepts the Clerk JWT
  alongside the legacy key, new signups land in `core.*`. `AUTH_V2_ENABLED=1` on both
  services; Clerk **dev** instance (`definite-terrapin-9`). Proven on real Google sign-in.
  STILL TODO: (4) **harden** — remove the shared `CLIENT_API_KEY` path once nothing
  depends on it; swap to a Clerk **production** instance (`pk_live_` + own Google OAuth)
  before real launch; retire the Wix `postMessage` handoff after a fallback window.

## Changelog
- 2026-06-16: **Direct PayPal payments LIVE** (`paypal_billing/`). Vanilla PayPal — Subscriptions (recurring) + Orders (PAYG) via the JS SDK, signature-verified webhook → refetch → the shared `apply_subscription_event` grant path (idempotent, `billing.*` only). PAYG/subscribe/cancel proven on sandbox + a real live purchase (grant id 24). Replaces Wix Pricing Plans checkout; works on both the Wix-embedded and standalone Clerk portals (dual-mode auth). `PAYPAL_ENABLED=1` + `PAYPAL_ENV=live` in `render.yaml`; rollback = `PAYPAL_ENABLED=0`. Runbook: `paypal_billing/README.md`.
- 2026-06-16: core_db live · contracts hub · cockpit (P5) · tracking (P3) · feedback (P6) · crm_sync (P4) · this STATUS.md. Cowork added `klaviyo/` + `privacy/`.
- 2026-06-16: de-Wix auth Phase 0+1 (Clerk) built dark — `auth_v2/` verifier+principal, wired into `client_api._guard()` (legacy-identical with flag off, proven), `/login` page. `AUTH_V2_ENABLED=0` everywhere; Wix login untouched. Awaiting Clerk app keys.
- 2026-06-16: de-Wix auth WENT LIVE (Phases 2-3). All 10 portal SPAs converted to `TFAuth` dual-mode (`frontend/auth_client.js` — auth-once: Clerk in the portal, children relay tokens via postMessage); marketing CTAs cut over to `/login`; `AUTH_V2_ENABLED=1` both services. Clerk dev instance `definite-terrapin-9`. Wix login kept as fallback. Remaining: Phase-4 harden (drop shared key) + Clerk prod instance before launch.
- 2026-06-17: Clerk PRODUCTION live (`clerk.ten-fifty5.com`, `pk_live`, own Google OAuth, 5/5 DNS). Growth stack DE-GATED (cockpit/consent/feedback/tracking/core_api register unconditionally — no more `*_ENABLED` flags / env-precedence gap; crm_sync self-gates on HubSpot/Klaviyo keys). **Wix AUTH removed from code** (portal + players_enclosure handoff gone — Clerk is the only door). E-audit found + fixed shared-key-only auth in support_bot + tennis_coach (AI Coach) + core_api → dual-mode (were 403'ing under Clerk). Business Cockpit linked in portal admin nav. BASELINE TODO: delete `CLIENT_API_KEY` + fallbacks (now pure fallback, all surfaces dual-mode); remove inactive `WIX_NOTIFY_*` + ingest reads; payment-Wix + `external_wix_id` columns (other agent / migration).
