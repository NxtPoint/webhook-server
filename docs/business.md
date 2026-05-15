# Ten-Fifty5 — Business Rules & Product Behaviour

**Status:** canonical. **Owner:** Tomo. **Last updated:** 2026-04-30. **Excludes:** T5 ML pipeline (lives in `.claude/handover_t5.md` — pipeline, not business logic).

This is the single source of truth for **how the product behaves**: account model, credits, entitlement gates, consumption, coach model, soft-delete, and the rules enforced only in code. Pricing tier numbers (plan IDs, monthly match counts) live in [`pricing_strategy.md`](pricing_strategy.md) — that's the spec for *what's sold*; this doc is the spec for *what happens*.

When code and this doc disagree, **this doc wins** for behaviour rules; `pricing_strategy.md` wins for tier numerics.

---

## 1. The mental model

Three nouns. Get these right and the rest follows.

| Noun | Definition | Identity key |
|---|---|---|
| **Account** | A billing entity. One per paying email. Owns credits, owns subscriptions, owns matches. | `billing.account.email` (lowercase, trimmed) |
| **Member** | A *person* on an account. One account holds 1+ members: the primary, plus children + linked coaches. | `billing.member.id`, scoped to `account_id` |
| **Match** | One ingested SportAI/T5/Technique task. Owned by exactly one account today. Players within a match are point-in-time text labels, not member references. | `bronze.submission_context.task_id` |

**The convention you must internalise:** *"Player A is always the customer."* This is **not enforced in schema** — it's a UX/data convention. `submission_context.player_a_name` is the upload form's text input. There's no FK from match → member. The dashboard treats `player_a` as the viewer, hardcoded across 12 gold views and `match_analysis.html`. Any feature that introduces "viewing the match from another perspective" has to break that assumption explicitly. See §11.

---

## 2. Identity & accounts

### Account creation

- Single entry point: `billing_service.create_account_with_primary_member()` (`billing_service.py:122-196`). Idempotent by email.
- Email is normalised: `.strip().lower()` (`billing_service.py:82-84`). All comparisons use the normalised form.
- On first creation: account row + exactly one primary member (`is_primary=true`).
- On re-call with same email: nothing is overwritten *except* `external_wix_id` (one-shot fill if account had it null). Wix's separate `sync_account` flow owns full snapshot replacement.
- Currency defaults to `USD` and is hardcoded at account-create (`billing_service.py:156`). No currency migration path exists.

### Roles

`billing.member.role` is one of two values:

- `player_parent` — can upload matches, owns child members, can invite coaches
- `coach` — view-only over linked players' data, cannot upload, cannot consume credits

Input `'player'` is normalised to `'player_parent'` (`billing_service.py:86-96`). Any other value raises `ValueError`. Role is decided at member creation time.

### Member rules

- **Exactly one primary member per account.** Enforced as a guard rail: if a primary member is missing on account lookup, one is created from the account's primary_full_name (`billing_service.py:179-194`).
- Children + linked coaches are non-primary members of the same account.
- Member soft-delete uses `active=false`, not row deletion.

### The `wix_member_id` linking quirk

If a Wix-driven event arrives for an account that exists with no `external_wix_id`, the field is one-shot filled (`billing_service.py:175-177`). Once set, it's never overwritten. This handles the case of Render-side signup followed by later Wix purchase.

---

## 3. Credits — the model

**You're already on credits, not tiers.** Match remaining is computed every time as `sum(grants) - sum(consumption)` — see `billing.vw_customer_usage` and the entitlements UPSERT. Match-pack tiers are just different grant sizes.

### Two separate credit pools

| Pool | Granted column | Consumed column | What it spends on |
|---|---|---|---|
| **Match credits** | `matches_granted` | `consumed_matches` | Any non-technique upload (singles, T5 variants, practice) |
| **Technique credits** | `techniques_granted` | `consumed_techniques` | Technique analysis uploads only |

Pools never swap. A user with 10 match credits and 0 technique credits cannot do a technique analysis.

### Where credits come from

`billing.entitlement_grant.source` is constrained to four values (`billing_service.py:228-233`):

- `signup_bonus` — one-time on registration, 1 match + 5 techniques, lifetime, never expires (`billing_service.py:235-237, 363-372`)
- `wix_subscription` — monthly grants from Wix subscription webhooks
- `wix_payg` — one-off match-pack purchases
- `manual_adjustment` — admin top-ups / corrections

### Grant idempotency (the three-way rule)

`grant_entitlement()` at `billing_service.py:240-360` uses three different idempotency strategies depending on source:

| Has `external_wix_id`? | Source = `signup_bonus`? | Idempotency key |
|---|---|---|
| Yes | (any) | `(account_id, source, plan_code, external_wix_id)` |
| No | Yes | `(account_id, source, plan_code)` — **one signup bonus per account, ever** |
| No | No | `(account_id, source, plan_code, valid_from)` — same start time = same grant |

The signup bonus rule is load-bearing: re-registering the same email never re-grants the trial.

### Monthly subscription credits don't roll over

`cron_monthly_refill.py::monthly_no_rollover_reset()` runs at period boundary and zeros excess match credits. Technique pool is unlimited for paid subscribers, so technique credits aren't reset.

---

## 4. Entitlement gates — the contract

Computed by `entitlements_api.py::UPSERT_SQL` (one big SQL statement, `entitlements_api.py:70-247`). This is the single place all permission flags are derived. Every gate in the app reads from `billing.entitlements`.

### The gates, exactly as enforced

```text
account_active        = billing.account.active
paid_active           = (most_recent subscription_state.status = 'ACTIVE')
matches_remaining     = greatest(sum(matches_granted) - sum(consumed_matches), 0)
techniques_remaining  = greatest(sum(techniques_granted) - sum(consumed_techniques), 0)

can_upload            = account_active
                        AND role <> 'coach'
                        AND matches_remaining > 0
                        -- paid_active is NOT required: free trial credits authorise upload

can_view_dashboards   = account_active
                        AND (paid_active
                             OR role = 'coach'
                             OR matches_consumed > 0
                             OR techniques_consumed > 0)
                        -- Trial graduates keep their dashboard FOREVER. The conversion hook.

can_link_additional_player  -- coach cap (Phase 2, live 2026-04-19)
                      = role <> 'coach'
                        OR coach_linked_players < 1
                        OR is_coach_pro
```

Two gates are enforced *outside* the entitlements table because they're API-level concerns:

- **AI Coach** (`tennis_coach/coach_api.py`): `account_active AND paid_active`. Admins (`info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`) bypass. Coaches always allowed.
- **AI Coach rate limits** (paid cohort only): 5 freeform calls per `(email, task_id)` per day; 20 freeform calls per email per day. Cards aren't rate-limited.

### Block reason cascade (what the UI sees on a `false`)

| `false` source | `block_reason` | What the user sees |
|---|---|---|
| `account_active=false` | `ACCOUNT_INACTIVE` | Hard block across all surfaces |
| `role=coach` on upload | `COACH_VIEW_ONLY` | "Coaches can't upload" message |
| `matches_remaining=0` | `NO_MATCH_CREDITS` | Credits-exhausted modal → upsell |
| `paid_active=false` on AI Coach | `UPGRADE_REQUIRED` (HTTP 402) | Locked teaser → `/pricing` |
| Coach at cap + no Coach Pro | `COACH_UPGRADE_REQUIRED` (HTTP 402) | Coach Pro upgrade card |

### "Most recent subscription wins"

When multiple `billing.subscription_state` rows exist for one account, `paid_active` derives from `ORDER BY updated_at DESC NULLS LAST LIMIT 1` (`entitlements_api.py:83-89`). Stale rows can't block a recently-renewed subscription.

---

## 5. Match consumption

### One task = one credit, deducted exactly once

- `billing.entitlement_consumption.task_id` is the unique key (`models_billing.py`).
- `consume_match_for_task()` at `billing_service.py:430-445` uses `INSERT ... ON CONFLICT (task_id) DO NOTHING`. Re-runs are no-ops.
- Non-UUID task IDs are coerced to UUID via `uuid5(NAMESPACE_URL, task_id_string)` (`billing_service.py:105-115`). Same string → same UUID → same idempotency key. This matters because SportAI returns string task IDs and Render pipelines pass them through.

### Sport-type → which pool

Routed by `billing_import_from_bronze.py` based on `bronze.submission_context.sport_type`:

| sport_type | Consumes |
|---|---|
| `technique_analysis` | 1 technique credit |
| anything else (`tennis_singles`, `serve_practice`, `rally_practice`, `tennis_singles_t5`, ...) | 1 match credit |

### What never happens

- **No refunds.** A consumption row, once written, is never deleted or modified.
- **No partial consumption.** Default is 1, hardcoded in SQL (`consumed_matches INT DEFAULT 1`).
- **No re-consumption on reprocess.** Reprocess (`/api/client/matches/<task_id>/reprocess`) rebuilds silver but doesn't touch billing — `task_id` already has a consumption row.

---

## 6. Coach invite — the protocol

### Permission row

`billing.coaches_permission`: `(id, owner_account_id, coach_account_id, coach_email, status, active, invite_token, created_at, updated_at)`. Schema in `coach_invite/db.py`.

- `status ∈ {INVITED, ACCEPTED}` — never anything else
- `active` is a boolean kill-switch, set false on revoke
- One row per `(owner_account_id, coach_email)` pair, ever — re-invites UPDATE the same row, they don't INSERT a new one

### Token lifecycle

- `secrets.token_urlsafe(32)` — 32 bytes of randomness, single-use
- Unique partial index `WHERE invite_token IS NOT NULL` prevents collisions (`coach_invite/db.py:46-49`)
- Token IS the auth — accept endpoint is public, no API key
- On accept or revoke: token is NULL'd immediately (`coach_invite/db.py:70-81`)
- Re-inviting a revoked coach: same row, new token, status reset to `INVITED`

### Coach cap (Phase 2, live 2026-04-19)

`billing_service.coach_accept_gate(email)` at `billing_service.py:594-610`:

- First accepted+active link: free
- 2nd+ link: requires Coach Pro subscription (any active sub *except* the free Coach Access plan `cd2b6772-1880-42ec-9049-4d9e4decc42b`)
- Gate fires at **accept time**, not invite time. Existing accepted links are grandfathered.
- **Fails open on DB error** — never blocks an invite due to infrastructure noise. This is deliberate; coach acquisition matters more than strict cap enforcement during outages.

### What coaches cannot do

- Upload matches (`role <> 'coach'` is in `can_upload`)
- Consume credits (no upload path means no consumption)
- Invite further coaches (no UI, no endpoint)

---

## 7. Soft-delete contract — the bright line

**Match delete is soft-delete only. Billing is never touched.**

### What "delete a match" actually does

`bronze.submission_context.deleted_at = now()` and that's it. The match row stays in bronze, silver, gold; queries that should hide deleted matches filter `WHERE deleted_at IS NULL`.

### Why we never touch `billing.*`

The match consumption was a real billing event the customer paid for. Refunding it on delete would:

1. Break revenue-recognition audit trails
2. Encourage upload-then-delete-then-upload abuse
3. Diverge from Wix's source-of-truth subscription state

This is why `match delete: soft-delete only, never touch billing` is one of the load-bearing rules (memory: `feedback_match_delete_design.md`; CLAUDE.md "Things not to do" #4).

### The four worker gates

Both ingest paths check `deleted_at` at four checkpoints to handle delete-during-ingest races:

1. `pre_start` — before any work begins
2. `pre_bronze` — before bronze ingest
3. `pre_silver` — before silver build
4. `pre_trim` — before video trim trigger

If `deleted_at IS NOT NULL` at any gate, the worker exits cleanly without re-populating bronze. See `ingest_worker_app.py::_do_ingest` and `upload_app.py::_do_ingest_t5`.

### Orphan sweep

`POST /ops/orphan-sweep` (header-auth) cleans up *bronze + silver* orphans whose parent `submission_context` is deleted or missing. **It explicitly never queries `billing.*`** (`cleanup/orphan_sweep.py`).

---

## 8. Hidden invariants — rules enforced only in code

These are load-bearing and not documented elsewhere. If you change any of these without updating this doc, you've broken the contract.

| # | Invariant | Enforced at |
|---|---|---|
| 1 | Email always lowercase + trimmed before any account lookup | `billing_service.py:82-84`; every endpoint does it again at the boundary |
| 2 | Task IDs are UUID-normalised via `uuid5(NAMESPACE_URL, str)` for any non-UUID input | `billing_service.py:105-115` |
| 3 | When ≥2 subscription rows exist, the most recent (by `updated_at`) wins | `entitlements_api.py:83-89` |
| 4 | Technique pool never substitutes for match pool (separate columns) | `billing_service.py:489-520`, `entitlements_api.py:152-157` |
| 5 | Exactly one signup bonus per account, lifetime | `billing_service.py:289-307` (idempotency on `(account_id, source, plan_code)` for `signup_bonus`) |
| 6 | Coach permission rows are never DELETEd; revoke is `active=false`, re-invite is UPDATE on the same row | `coach_invite/db.py`, `billing.coaches_permission` |
| 7 | Coach cap check fails open on DB error | `billing_service.py:608-610` |
| 8 | Trial graduates retain dashboard view forever once any credit is consumed | `entitlements_api.py:196-203` |
| 9 | `wix_member_id` is one-shot fill on existing account, never overwritten | `billing_service.py:175-177` |
| 10 | `consumed_matches` defaults to 1 at the SQL layer; consumption rows never have 0 | `models_billing.py`, `billing_service.py:54-58` |
| 11 | Free Coach Access plan (Wix ID `cd2b6772…`) does NOT count as paid for any check | `billing_service.py:530-534, 583-585`; `entitlements_api.py:122-131` |
| 12 | Admins (`info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`) bypass AI Coach paywall | `tennis_coach/coach_api.py:117-161` |

---

## 9. State today: sharing & referrals

**Both are zero-built.** This section captures *what doesn't exist* so future work doesn't double-discover.

### Sharing

- `bronze.submission_context.share_url` exists but is **just a copy of the input video URL**, not an access gate
- All match queries enforce single-account scoping via `WHERE email = :email`
- No `match_share` / `match_access` table
- No public match URL, no read-only token, no "share with opponent" flow
- The closest precedent is `coach_invite/` (token + accept page + permission row) — see §11 for how to reuse it

### Referrals

- No referral table, no referral code field, no signup attribution
- `pricing_strategy.md §7` describes a referral-credits design as Phase 2; nothing is built
- New account creation has no `referrer_account_id` parameter

---

## 10. State today: pricing model

You are **already on a credit-based model**. The mental shift to "Claude-style flat fee + credits + top-ups" is largely a Wix product reconfiguration plus a small webhook tweak. Schema doesn't change.

### What's already true

- `billing.entitlements.matches_remaining = matches_granted - matches_consumed`. Credit arithmetic, not tier counters.
- `entitlements_api.py` gates on `matches_remaining > 0`, not on plan name.
- Wix subscription webhook already grants N matches per period via `subscription_event() → grant_entitlement()`.
- Top-ups are a solved problem: PAYG match packs already work via `wix_payg` source.

### What changes for "flat-fee + credits"

- Wix product config: replace tier-based plans (3 / 5 / 10 matches) with one flat monthly plan that grants N credits/month
- `subscription_event()` handler: same logic, different grant size
- Optional: introduce `credit_cost_per_action` config (e.g., match=1, technique=2, AI coach reanalysis=0.5). This is new — today every action costs exactly 1 from its respective pool.

### What does not change

- Schema (`entitlement_grant`, `entitlement_consumption`, `entitlements`)
- Idempotency rules
- Soft-delete contract
- Coach model

---

## 11. Future state: share / referrals / credit-pricing pivot

This is the **decision artefact**. When we build, we build to this spec.

### 11.1 Share-with-opponent (Option 2 — read-only link)

**The MVP move.** Reuses ~80% of `coach_invite/`.

- New table `billing.match_share`: `(id, task_id, owner_account_id, opponent_email, opponent_account_id NULLABLE, role 'viewer', status, share_token, created_at, accepted_at)`
- Owner clicks "Share" on match page → token + email (reuse `coach_invite/email_sender.py`)
- Opponent receives email → clicks accept page (reuse pattern from `coach_invite/accept_page.py`) → no signup required for read-only
- Match-list query for opponent_email returns shared matches via UNION with their own
- **No perspective flip** — opponent sees the dashboard exactly as the owner sees it (Player A is still the owner)
- Soft-delete propagation: if owner deletes the match, opponent's view goes blank (`deleted_at IS NULL` filter applies). Consider a "match was deleted by owner" message.

**Effort: M.** Mostly mechanical; the new table + token lifecycle is the core work.

### 11.2 Share + perspective flip (Option 3 — invite to signup)

**The strategic move, and the only L-XL feature in this set.**

The "flip" problem: today, `player_a` is hardcoded as the viewer in 12 gold views (171 references) and `match_analysis.html` (~50 references). Two viable approaches:

**Approach A — viewer perspective parameter.** Pass `viewer_account_id` through API → views become parameterised CTEs that compute `'me' = CASE WHEN :viewer_id = player_a_id THEN 'a' ELSE 'b' END`. Frontend reads `me.serve_pct` etc. instead of `player_a.serve_pct`. One set of views, perspective decided at query time.

**Approach B — dual views.** Generate `vw_*_perspective_a` and `vw_*_perspective_b` for each of the 12 views. Endpoint picks the view based on viewer. More boilerplate; cleaner separation; potentially faster (no CASE evaluation).

Recommendation: **Approach A**, because new dashboards added later only need to be built once.

**Other moving parts:**
- `match_share` gets a `role` column: `'owner' | 'co_owner' | 'viewer'`. `co_owner` = perspective-flipped, `viewer` = read-only same perspective.
- Signup-from-share flow: `coach_accept.html`-equivalent that creates a new `billing.account` for the opponent and adds a `match_share` row with `role='co_owner'`.
- "Player A is the customer" convention is now broken. Document in this doc that **viewer's account_id determines perspective**, full stop.
- GDPR: opponent's name lives on owner's `submission_context.player_b_name`. If opponent later wants their name redacted from owner's view, do we soft-delete the name? **Decision needed before launch** — propose: name shown to owner stays as captured at upload time; opponent's account-level name change does not propagate.

**Effort: L–XL.** The view + frontend parameterisation is the bulk; ~3–6 weeks of focused work.

### 11.3 Referral credits

Cheapest of the three. Schema change is one column.

- New column on `billing.entitlement_grant`: `referrer_account_id BIGINT NULLABLE`
- Referral code: `base64url(referrer_account_id)` (or signed JWT if abuse becomes real)
- Signup flow accepts `?ref=<code>` → decodes referrer_account_id → stashes on new account row
- **Grant trigger: first-match-completed**, not signup itself (prevents fake-account abuse). On the new account's first consumption row, fire two grants:
  - Referee: bonus credits (e.g., +2 matches), `source='referral_signup_bonus'`
  - Referrer: bonus credits (e.g., +5 matches), `source='referral'`, `referrer_account_id=<the referrer>`
- Idempotency: unique constraint on `(account_id, source, 'referral_signup_bonus')` for referee; on `(referrer_account_id, account_id, 'referral')` for referrer

**Effort: S.**

### 11.4 Credit-pricing pivot (flat fee + credits + top-ups)

Already mostly possible. Today's lift:

1. New Wix subscription product: flat monthly fee → grants N credits per period (existing pathway works)
2. Decide credit cost per action. Suggested defaults:
   - Match analysis: 1 credit
   - Technique analysis: 2 credits (subsumes the technique pool — one less concept)
   - AI Coach question: 0 credits (free with subscription) or 0.1 credits (rate-limit-as-billing)
3. If subsuming the technique pool: migration path = grant existing technique-pool holders a one-time match-credit equivalent, deprecate `techniques_*` columns
4. Top-ups: existing `wix_payg` source covers it; just need new Wix product SKUs

**Effort: S–M.** S if technique pool stays separate; M if you collapse it into one credit pool.

### 11.5 Risk surfaces (decisions to make before building any of the above)

1. **Soft-delete + share.** When owner deletes a shared match, does opponent see "deleted by owner" or silent disappear? → Recommendation: silent (less drama) but log it.
2. **Opponent name privacy.** If opponent has account, do they get to redact their name from owner's view? → Recommendation: name as captured at upload is a historical fact; doesn't propagate.
3. **Reprocess on shared match.** Owner reprocesses match they shared with opponent. Both see the new analysis. No extra credit charge (reprocess never charges).
4. **Coach + share interaction.** Coach linked to owner sees shared-out matches in their dashboard? → Recommendation: yes, coaches see everything the owner sees.
5. **Referral abuse.** Single email creates 100 fake referrals. → Mitigation: grant only on first-match-completed (real consumption), unique constraint per `(referrer, referee)`.
6. **Multi-account claim of pre-share matches.** Opponent signs up months after a match was uploaded, wants the historical match credited to their profile. → Out of scope for v1; surface only if a customer asks.

---

## 12. Decision log

Append-only. New rules, reversed rules, reasons.

| Date | Decision | Reason |
|---|---|---|
| 2026-04-17 | Free trial = 1 match + 5 techniques, lifetime, no rollover | One-shot full experience > recurring tiny free tier; AI Coach is the paywall |
| 2026-04-17 | AI Coach is a hard paywall; not even one free question | Differentiator + cost; teaser-only converts better than freemium dilution |
| 2026-04-17 | Trial graduates keep dashboard view forever | Conversion hook — must see what they're giving up |
| 2026-04-19 | Coach cap Phase 2 live: 1 player free, 2+ requires Coach Pro | Acquisition channel free, monetise the stable |
| 2026-04-19 | Coach gate fails open on DB error | Channel acquisition > strict cap during infra noise |
| 2026-04-30 | Match delete is soft-delete only; `billing.*` is never touched | Match was a real billing event; refund-on-delete breaks audit + invites abuse |
| 2026-04-30 | PDF export rejected | Kid data privacy; opt for token-based share instead |
| 2026-04-30 | Sharing v1 = read-only link (Option 2); perspective-flip (Option 3) deferred | Option 3 is L–XL because of `player_a` hardcoding in 12 views + frontend |
| 2026-04-30 | Pricing pivot to flat-fee + credits + top-ups | System is already credit-based; pivot is mostly Wix product config |

---

## 13. Cross-references

- **Pricing tier numerics** — [`pricing_strategy.md`](pricing_strategy.md) (tier prices, Wix plan IDs, AI Coach access matrix, marketing copy)
- **Architecture & data layers** — [`../CLAUDE.md`](../CLAUDE.md) §Architecture Overview
- **Dashboards & gold views** — [`dashboards.md`](dashboards.md)
- **Coach invite implementation** — `coach_invite/` module; CLAUDE.md §Coach Invite Flow
- **Support bot escalation rules** — [`support_bot.md`](support_bot.md)
- **Soft-delete worker behaviour** — CLAUDE.md §Diagnostics & Ops; `cleanup/orphan_sweep.py`
- **Env vars** — [`env_vars.md`](env_vars.md)

T5 ML pipeline business behaviour (pricing impact: same as any non-technique sport_type — 1 match credit per task) is otherwise out of scope for this doc.
