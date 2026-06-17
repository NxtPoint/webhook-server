# core.* as the home for BILLING + USAGE — due diligence & recommendation

**Status:** ✅ APPROVED + IMPLEMENTED (Option C) — 2026-06-17. Cockpit + CRM traits now read the
`billing.*` SoR directly; the deferred `core.*` payment mirror is decided-against. See §10 for the
implementation record.
**Date:** 2026-06-17
**Author:** DD session (handover from prior session)
**Decision owner:** Tomo
**Constraint reaffirmed:** `billing.*` stays the system of record (SoR) until an explicit cutover. Anything we build is additive + dark-by-default + env-gated; seed→assert→purge for any prod write.

---

## TL;DR — recommendation

**Do Option C now: feed the cockpit (and CRM) from views over the real system-of-record (`billing.*` + `bronze.*` + `gold.*`), not from a second copy in `core.*`. Do NOT build the deferred payment→core "mirror" (Option A). Keep `core.*` identity + `usage_event` flowing forward as they already do. Defer "core.* becomes true SoR" (Option B) until there is a concrete driver (auth-SoR cutover, referrals/relationships product, or CRM that needs unified identity) — and when that day comes, do it as a real cutover, not a bolt-on mirror.**

Why, in one paragraph: the only thing actually broken today is the cockpit's billing/credit/MRR widgets reading empty `core.*` tables. Every number they want **already exists, correct, in `billing.*` / `bronze.*` / `gold.*`** (that's the SoR). Option A would stand up a *second, non-authoritative copy* of that data and keep it in sync via two fire-and-forget hooks on hot paths — which (a) is the classic "mirror that silently drifts from the SoR, so your analytics quietly lie," (b) walks straight into the ledger trap (grants and consumption must both be wired *and* replicate refill/expiry/no-rollover/technique logic), and (c) leans on an email-only identity join with no FK. Option C delivers the same cockpit value with **zero new write-path, zero drift, zero ledger trap** — because the views read the truth directly. The canonical-model ambition for `core.*` is real but it is an *identity/auth* ambition (the de-Wix endgame), not a reason to duplicate billing data for a dashboard.

---

## 1. What `core.*` is *for* (intent vs reality)

`core.*` (in `core_db/`, schema `core.*`) is the **canonical data model** designed during the de-Wix migration. Its declared purpose (`core_db/models.py:51,122`; `DB-SCHEMA-PROPOSAL.md`):

- **Unified identity** that separates the three things `billing.*` conflates: `account` (ownership/payer) vs `app_user` (a login — `auth_provider`/`auth_provider_uid`, the Clerk target) vs `person` (a tennis profile: player/parent/coach). This is the real prize and the actual de-Wix driver — `core.app_user` is the intended auth SoR.
- **Compliance-grade consent** (`core.consent`: versioned, per-type, biometric + parental-minor), DSARs, retention rules — things `billing.*` cannot model.
- **A clean analytics substrate**: `subscription` (with normalised `mrr_cents`), append-only `credit_ledger`, `match` (business record bridging to bronze via `task_id`), `usage_event` (product analytics stream), `nps_response`/`survey_response`.

The models say `core.account` "supersedes `billing.account`" and `core.person` "supersedes `billing.member`" — but **"supersedes" is aspirational**. No live system reads `core.*` for billing; `billing.*` is still SoR (`marketing_crm/STATUS.md:92`, `DATA-INVENTORY.md §9`). `core.*` is "live on prod (empty)" (`STATUS.md:28`).

---

## 2. What is fed into `core.*` TODAY vs not (the trace)

> **First, a stale-doc correction that reframes the whole question.** CLAUDE.md / STATUS describe `consent` / `feedback` / `tracking` / `cockpit` / `core_api` as "dark-by-default, env-gated." **As of the 2026-06-17 de-gate, their `register()` / `_enabled()` functions return `True` unconditionally** — the `if register_X(app):` guards and "DARK unless …=1" comments in `upload_app.py` are misleading dead conditions. So identity capture, feedback, and usage tracking write to `core.*` on the **live** service today. The one genuine exception is `core_api`: de-gated **but never wired** (no caller of `core_api.register(app)` exists anywhere) → its `/api/core/*` routes are dead.

| `core.*` table | Fed today? | By what (file:line) | Gating |
|---|---|---|---|
| `account` / `app_user` / `person` | **YES, live (lazy)** | `marketing_crm/consent/blueprint.py:66` → `accounts.ensure_identity`; `auth_v2/principal.py:142` first-login provisioning | consent: live (de-gated); auth_v2: only when `AUTH_V2_ENABLED=1` |
| `consent` | **YES, live** | `consent/blueprint.py:86` `record_consent` | live |
| `usage_event` | **YES, live** | `tracking/client.py:58` `matches.record_usage` (called from `upload_app.py:3652` `MATCH_UPLOADED`, feedback, etc.); `tracking/beacon.py:59` page beacon | live (de-gated) |
| `nps_response` / `survey_response` | **YES, live** | `feedback/blueprint.py:91/113/133` | live (de-gated) |
| `subscription` | **NO** | only `core_db/seed.py` (demo) + `core_db/backfill.py` (manual, 1 account) | — |
| `credit_ledger` | **NO** | only `seed.py` / `backfill.py` | — |
| `plan` | **NO** | only `seed.py` | — |
| `match` | **NO** | upload path emits a `usage_event` of type `match_upload` but **never calls `upsert_match`** | — |
| `relationship`, `acquisition`, `ticket*`, `data_subject_request`, `retention_rule` | **NO** (repos exist, no live caller) | — | — |

**Definitive (grep-verified):** no billing / payment / upload path imports `core_db`. `subscriptions_api.py`, `billing_service.py`, `billing_import_from_bronze.py`, `entitlements_api.py`, `paypal_billing/*` have **zero** `core_db` references. The `consume_match`/`grant_credits`/`consume_matches_for_task` names in `billing_service.py:433/451` and `subscriptions_api.py:495` are **`billing.*` functions** — name-collisions with the `core_db` repo methods, not core writes. The PayPal webhook header says it explicitly: "Touches `billing.*` only … (core mirror deferred)" (`paypal_billing/webhook.py:10`).

So the **handover's premise is half-right**: identity *is* fed forward; the billing slice (`subscription`/`credit_ledger`/`plan`) and `match` are *not*. But **`usage_event` IS fed** (correcting "usage are NOT fed"). The only data in the billing slice is the single manually-backfilled account (`STATUS.md:54` — tomo: 1 acct / 3 persons / 121 matches).

---

## 3. Who CONSUMES `core.*`, and what under-reports

**The admin cockpit (`marketing_crm/backoffice/`) is the primary consumer.** It is **live** (de-gated 2026-06-17, `blueprint.py:175`), admin-gated at runtime via `_admin_ok()`. Frontend `frontend/cockpit.html`. Its views (`marketing_crm/backoffice/views.py`) sit on the base views in `core_db/schema.py`. Today, given `subscription`/`credit_ledger`/`match` are empty:

| Cockpit element | Reads | Shows today |
|---|---|---|
| MRR card | `vw_business_health` ← `vw_mrr` ← `core.subscription` | **$0 / 0 active subs** |
| Active subs by plan | `vw_subs_by_plan` ← `core.subscription` | **empty** |
| PAYG revenue | `vw_business_health` ← `credit_ledger ⋈ plan` | **$0** |
| Churned this month | `core.subscription WHERE cancelled_at ≥ month` | **0** |
| Per-customer credits remaining | `vw_account_credits` ← `core.credit_ledger` | **0 for everyone** |
| Matches uploaded / completed | `core.match` | **0** (upload path never calls `upsert_match`) |
| Activation rate / free-to-paid | derived from the above | **~0% / 0 / NULL** |
| Lifecycle buckets (paid/payg/at_risk/churned) | `vw_account_lifecycle` | **~empty** — every account collapses to signup/trial |
| Customer list / total accounts | `core.account` | **sparse** — only lazily-created accounts, vs the real `billing.account` population |
| Processing-ops tab | `bronze.submission_context` | **✅ correct** (reads bronze, not core) |
| NPS / feedback tab | `core.nps_response` / `survey_response` | **✅ correct** (fed live) |

**CRM sync (`marketing_crm/crm_sync/sync.py`)** reads `core.vw_customer_list` only (never `billing.*`) → would push `ttf_mrr=0`, `ttf_plan=null`, `ttf_matches_remaining=0`, `stage=signup` for everyone to HubSpot/Klaviyo. Currently inert (self-gates on a provider key being set, `sync.py:35`), so this is a *latent* wrong-data risk, not a live one.

**`core_api` (`/api/core/*`)** — built, de-gated, but **unwired** → serves nothing. Irrelevant to current behaviour.

**No customer-facing surface reads `core.*` billing data.** Customer dashboards read `billing.*` / `gold.*` (correct, unchanged). So the blast radius of the empty tables is **internal analytics only** (cockpit + would-be CRM traits). Nothing customer-facing is wrong.

---

## 4. The ledger trap (why a payment-only bolt-on is dangerous)

`core.credit_ledger` is append-only; balance = `SUM(matches_delta)` (`core_db/repositories/subscriptions.py:152`; `vw_account_credits` at `schema.py:58`). Grants are `+`, consumption is `−`. **Both sides must be wired together** or balances are wrong by construction:

- Wire only grants (payment path) → ledger shows credits granted but never consumed → **inflated balances** → cockpit says customers have credits they've spent.
- Wire only consumption → **negative balances**.

This is precisely why `STATUS.md:85-93` scopes it as a paired session, not a payment-only addition. But it's worse than "two calls": to *match billing.\* truth* the mirror would also have to replicate **the monthly-refill cron** (`subscriptions_api.py:364`, currently Wix-only — PayPal subs are fenced out), **no-rollover expiry**, **technique consumption** (`consume_technique_for_task`), and **the exact idempotency keys**. That is re-implementing the billing engine in a second schema whose only consumer is a dashboard. Every gap = silent divergence from the SoR.

---

## 5. Identity reconciliation — `billing.account` vs `core.account`

The two identity stores are **independent, joined only by email — no foreign key.**

- `ensure_identity` (`core_db/repositories/accounts.py:189`) dedupes on **email only** (`func.lower(email)`, `norm_email`), **not** on `auth_provider_uid`. It defaults new users to `auth_provider='wix'`, uid `NULL`.
- Drift / dup risks (all latent today because core is near-empty, but they become live the moment core fills):
  - Email mismatch / case / whitespace / email-change between `billing.account.email` and `core.account.email` → a duplicate, unlinked core account.
  - A user seeded `auth_provider='wix'` then logging in via Clerk is re-linked **by email** (`set_auth_provider`, `accounts.py:83`). If the Clerk email ≠ billing email → a second `core.app_user`.
  - Two write-paths into core (consent, and a would-be billing mirror) both key on email — they converge iff emails match exactly.
- The one backfilled account links the two via `external_wix_id` (`backfill.py:42`), but that is unenforced and exists for exactly one account — and `external_wix_id` is now legacy (Wix auth removed).

**Implication for Option C:** the *real* customer population lives in `billing.account` (every payer/uploader is there). `core.account` is sparse (lazy). So a cockpit customer list keyed on `core.account` under-reports the population **regardless of billing data**. Option C should drive the customer list from `billing.account` and LEFT JOIN `core.*` identity/consent/NPS extras where present.

---

## 6. The three options

### Option A — `billing.*` stays SoR + `core.*` mirror for analytics (the deferred plan)
Wire fire-and-forget mirrors: payment path → `ensure_identity` + `upsert_subscription` + `grant_credits`; upload path → `consume_match`. Cockpit reads `core.*` as today.

- **Effort:** Medium-High. Two write-path hooks *plus* replicating refill/expiry/no-rollover/technique logic to keep parity, *plus* identity reconciliation hardening (email→FK), *plus* a one-time backfill of existing `billing.*` → `core.*` (else cockpit is empty for all pre-existing customers).
- **Risk:** **High.** A non-authoritative mirror that silently drifts from the SoR → analytics that lie. Ledger trap. Second consumption write on the hot upload path. Email-only identity join. You are maintaining two billing engines forever, for a dashboard.
- **Value delivered:** Cockpit works — *if and only if* the mirror stays perfectly in sync, which is the hard part.

### Option B — `core.*` becomes the true SoR; `billing.*` derived/retired
Cut over: backfill all `billing.*` → `core.*`, dual-read, move grant/consume/gate logic to `core_db`, retire `billing.*`.

- **Effort:** **Very High.** A full billing migration + cutover of the live, correct, money-handling path. Touches the upload gate, PayPal webhook, refill cron, every billing read in `client_api.py`/`usage_api.py`/`entitlements_api.py`.
- **Risk:** **Very High** *if forced now* (no driver, live revenue path). **But this is the only option that's strategically coherent** — one model, no drift, `core.*` earns its "canonical" title — *when there is a reason to pay the cost.*
- **Value:** Unified identity + billing + auth on one model. The real de-Wix endgame.

### Option C — skip the second write-path; feed the cockpit from views over the SoR
Rewrite the cockpit (and CRM-traits) views to read `billing.subscription_state` + `billing.entitlement_grant`/`consumption` (or `billing.vw_customer_usage`) + `bronze.submission_context` (+ `gold.match_kpi`) for the billing/credit/match facts, **driven off `billing.account` as the population**, LEFT JOIN `core.*` for the identity/consent/NPS/usage extras that core *does* feed live.

- **Effort:** **Low.** Pure SQL view changes in `marketing_crm/backoffice/views.py` (and the CRM `_TRAITS_SQL`). No new write-path, no schema change, no backfill. The data already exists and is correct. MRR needs a plan→`mrr_cents` mapping (plan prices already live in `docs/pricing_strategy.md` / pricing config).
- **Risk:** **Low.** Read-only over the SoR → cannot drift. No ledger trap, no hot-path write, no identity write-race. Worst case is a wrong JOIN, caught in review.
- **Value:** Cockpit MRR / credits / churn / matches correct *now*. Doesn't advance the canonical model — but it doesn't need to, because the cockpit is analytics, not identity.

---

## 7. Recommendation (expanded)

**Adopt Option C now. Reject Option A. Hold Option B for a real driver.**

1. **Reject A.** A mirror is the worst of both worlds for *analytics*: it carries all the cost of a second write-path (ledger trap, refill/expiry parity, hot-path consume write, identity reconciliation, backfill) while producing data that is *by definition* second-class — a copy that can diverge from the SoR. You'd be running the business off numbers that are only as trustworthy as the least-tested fire-and-forget hook. If a number matters enough to put on the cockpit, read it from the truth. (This also means the work already scoped in `STATUS.md:85-93` should be **descoped**, not picked up.)

2. **Adopt C.** It solves the *actual* pain — empty cockpit — at the lowest risk, with `billing.*` as the single source of truth so the analytics cannot lie. It respects the hard constraint ("billing stays SoR") not as a temporary truce but as the design. It's a few SQL views, reviewable in an afternoon, fully reversible.

3. **Keep `core.*` identity + `usage_event` flowing** (already live). These are the parts of `core.*` that are genuinely earning their keep — they feed the consent/compliance story and the product-analytics stream, and they're the seedcorn for the eventual B cutover. Nothing to do here except let them run.

4. **Hold B until a driver appears.** `core.*` as true SoR is the right *endgame*, but only with a forcing function: (a) cutting auth SoR to `core.app_user` (the de-Wix finish line), (b) shipping referrals / coach-player / parent-junior relationships that `billing.*` can't model, or (c) a CRM that needs unified identity. When one of those lands, do B as a phased cutover — **not** by first building the A mirror (a mirror is throwaway scaffolding that still has to be torn out at cutover).

5. **Optional, separately valuable, low-risk:** run the `billing.account → core.account` identity backfill (`STATUS.md §7`) so `core.account` is a complete population, not just the lazily-created subset. This de-risks the future B cutover and makes `core.*` identity trustworthy, and it's independent of the billing-analytics decision. **Not required for C** (C drives the population off `billing.account`). Recommend doing it opportunistically, after C, with seed→assert→purge.

### The honest one-liner
The cockpit is empty because we pointed it at a model we haven't filled — not because the data doesn't exist. Point it at the data. Don't copy the data to make a dashboard happy.

---

## 8. Phased plan — Option C (only if "proceed")

All steps additive, reversible (views are `CREATE OR REPLACE`), no hot-path writes. Validate each against the live DB (read-only) before the next.

- **C0 — Decide & scope (this doc).** Approve C; descope the deferred A mirror in `STATUS.md`.
- **C1 — MRR + active subscriptions.** Rewrite `vw_mrr` / `vw_subs_by_plan` (or cockpit-local equivalents) to read `billing.subscription_state` (status, plan_code, billing_provider) with a plan→`mrr_cents` mapping. Assert cockpit MRR == hand-counted active recurring subs.
- **C2 — Credits remaining.** Point `vw_account_credits` consumers at `billing.vw_customer_usage` (`matches_remaining` already = grants − consumption, the SoR formula). Assert against a known account.
- **C3 — Matches uploaded/completed.** Source from `bronze.submission_context` (status, counts) — same source the working processing-ops tab already uses. Optionally enrich with `gold.match_kpi`.
- **C4 — Customer list + lifecycle, billing-account-driven.** Rebuild `vw_account_lifecycle` / `vw_customer_list` keyed on `billing.account` (the real population), LEFT JOIN `core.*` for consent/NPS/acquisition/`usage_event` extras. Recompute lifecycle buckets from real subs/credits/matches.
- **C5 — CRM traits.** Repoint `crm_sync._TRAITS_SQL` to the C4 view so HubSpot/Klaviyo get real MRR/plan/credits (still gated on provider key).
- **C6 — Docs.** Update `STATUS.md`, `DATA-INVENTORY.md`, CLAUDE.md cockpit notes to reflect "cockpit reads billing.* SoR; core.* mirror not built (by decision)."

### Deferred plan — Option B (when a driver lands; recorded, not scheduled)
identity backfill (`billing.*`→`core.*`) → subscriptions cutover → credit ledger **both sides** → matches/usage → dual-read → cockpit/auth cutover → retire `billing.*`. Each phase env-gated, dual-read before flip, seed→assert→purge.

---

## 9. Decisions needed from Tomo

1. **Approve Option C** (views over `billing.*`) as the immediate path, and **descope the deferred A mirror**? (Recommended.)
2. **Run the `billing.account → core.account` identity backfill** opportunistically after C (de-risks future B), or leave `core.account` lazy-fed only?
3. **Any near-term driver for B** I should know about (auth-SoR cutover date, referrals product) that would change the calculus toward investing in `core.*` as SoR sooner?

---

## 10. Implementation record (Option C — shipped 2026-06-17)

- **`marketing_crm/backoffice/views.py`** rewritten: `vw_account_lifecycle` / `vw_business_health` /
  `vw_subs_by_plan` / `vw_customer_list` / `vw_at_risk` now read `billing.subscription_state` +
  `billing.vw_customer_usage` + `bronze.submission_context` + `billing.coaches_permission`, driven off
  `billing.account` as the population, LEFT-JOINing `core.*` (`usage_event`, `nps_response`) by email
  for the live extras. `vw_processing_ops` + the NPS views unchanged. View names + output columns
  unchanged → `blueprint.py` and `frontend/cockpit.html` needed no edits.
- **New `core.vw_plan_pricing`** (`plan_code → plan_class, mrr_cents, payg_cents`) built from
  `paypal_billing.plans` (`PRICES`/`PLANS`) + an explicit legacy-Wix code map. The single edit point for
  plan economics. `vw_business_health.unpriced_active_subs` flags any active sub whose plan_code isn't
  mapped, so a new code can never silently vanish from MRR.
- **`crm_sync._TRAITS_SQL`** repointed to bridge `core.*` by **email** (was a now-invalid join on
  `core.account.id = vw_customer_list.account_id`, which is a billing id post-rewrite).
- **Validated read-only against the live Render DB:** MRR `$120` (`MONTHLY_10` $70 + `coach_sub_ong`
  $50; PAYG `once off` correctly excluded), `active_subscriptions=2`, `total_accounts=10` (real billing
  population vs 1 in core), `payg_revenue=$50`, `unpriced_active_subs=0`, and **per-account
  `matches_remaining` reconciles exactly with `billing.vw_customer_usage`** (zero mismatches).
- **Legacy price assumptions to confirm** (in `_LEGACY_PLAN_PRICING`): `MONTHLY_10`→$70 (≈Advanced),
  `coach_sub_ong`→$50 (≈Coach Pro), `player_sub_5`→$40 (≈Standard), `player_sub_100`→$0 (grants-only).
- **Activation metric** changed to `matches_completed >= 1` (was `… AND reports_viewed >= 1`): report-view
  tracking lives only in `core.usage_event` (forward-only since 2026-06-17), so gating on it would
  zero-out every historical account. A completed match is the real activation moment; `reports_viewed`
  stays available as a column. Flag for confirmation.

### Appendix — key evidence (file:line)
- core schema/views: `core_db/schema.py:56-95` (`vw_account_credits`/`vw_subscription_current`/`vw_mrr`); models `core_db/models.py:50-444`; balance `core_db/repositories/subscriptions.py:152`.
- live core writers: `marketing_crm/consent/blueprint.py:66`; `auth_v2/principal.py:142`; `marketing_crm/tracking/client.py:58` + `beacon.py:59`; `feedback/blueprint.py:91`.
- de-gate (stale-doc): `marketing_crm/backoffice/blueprint.py:175`; `core_api/blueprint.py:137` (unwired — no caller).
- billing SoR grant path: `subscriptions_api.py:157-361` (`apply_subscription_event`), `grant_entitlement` `billing_service.py:243-363`; PayPal `paypal_billing/webhook.py:239` (header "billing.* only" :10).
- billing consume path: `billing_import_from_bronze.py:154`; `consume_match_for_task` `billing_service.py:433`; balance `billing.vw_customer_usage` / `get_remaining_matches` `billing_service.py:378`.
- cockpit consumers: `marketing_crm/backoffice/views.py:6,16,67,90,101,117`; CRM `marketing_crm/crm_sync/sync.py:21`.
- deferred-mirror note + identity backfill: `marketing_crm/STATUS.md:54,82-94`; `ensure_identity` `core_db/repositories/accounts.py:189`; backfill `core_db/backfill.py:42`.
