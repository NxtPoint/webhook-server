# Ten-Fifty5 Pricing Strategy

**Status:** Phase 1 (launch). Phase 2 items flagged inline.
**Owner:** Tomo
**Last updated:** 2026-04-17

This is the single source of truth for pricing, entitlement logic, and how the system decides what a given user can do. When the business model and the code disagree, this doc is correct and the code is wrong.

---

## 1. Positioning

**"ATP-level stats, AI coaching and technique analysis for serious competitive players and their coaches."**

Ten-Fifty5 is not a recreational app. It exists for people chasing measurable performance gains — junior tournament players, academy players, coached adults, and the coaches who work with them. The price point reflects that. The free trial is a *proof*, not a product.

Three reasons we can charge above most competitors:
1. **Match Analytics** — point-by-point, heatmaps, cross-match performance trends
2. **Technique Analysis** — biomechanical stroke breakdown (no mainstream tennis app has this)
3. **AI Coach** — Claude-powered conversational coaching over the user's own data (nobody else has this)

No competitor bundles all three. That is the pricing moat.

---

## 2. Tier overview

| Tier | Who | Price | Match upload | Technique | AI Coach | Dashboard view |
|---|---|---|---|---|---|---|
| **Free Trial** | New signups (player/parent) | £0, one-time | **1 lifetime** | **5 lifetime** | 🔒 Teaser only — aggressive upsell to paid | ✅ forever on trial content |
| **PAYG** | Casual / dip-in users | Per Wix plan | 1 / 3 / 5 credits | ✅ unlimited | ✅ unlimited | ✅ |
| **Starter** (monthly) | Light regular users | Per Wix plan | 3 /mo | ✅ unlimited | ✅ unlimited | ✅ |
| **Standard** (monthly) | Core competitive player | Per Wix plan | 5 /mo | ✅ unlimited | ✅ unlimited | ✅ |
| **Advanced** (monthly) | Serious competitor / academy | Per Wix plan | 10 /mo | ✅ unlimited | ✅ unlimited | ✅ |
| **Coach** (LAUNCH) | Invited coaches | Free | ❌ cannot upload | ✅ on linked players | ✅ on linked players | ✅ all linked players |
| **Coach** (PHASE 2) | Coaches with >1 player | Free ≤1 player / paid 2+ | ❌ | ✅ | ✅ | ✅ |

**Prices** live in Wix Payment Plans — not hard-coded here. The pricing page renders the Wix-driven plan catalogue.

---

## 3. The free trial — mechanics and conversion intent

### What the user gets

On registration (role = `player_parent`), the account is granted:

- **1 match credit** (lifetime, never expires, cannot be topped up)
- **5 technique credits** (lifetime, never expire)
- **Full dashboard access to anything they generate** — forever, regardless of subscription status
- **AI Coach: locked.** The Coach module UI appears but is visibly gated with an "Upgrade to unlock" state. See §7.

### What the user does NOT get

- Any free match credits after the first is used
- Any free technique credits after the 5th is used
- Any AI Coach response — not a preview, not a first-question-free. Hard gate.
- Access to `Player Performance` cross-match trends (requires ≥2 matches anyway — natural gate)

### Why this shape

- **One match, not one month.** One-time lifetime > monthly-recurring-free. "Free forever" trains users to never pay. "One shot at the full thing" trains them that each analysis is valuable. Matches premium positioning.
- **Five techniques, not one.** Technique compute cost is low and variety builds conviction — you want them uploading their forehand AND their serve AND their backhand.
- **AI Coach as the paywall lever.** Research (see `.claude/memory` for `project_coach_invite`-era notes and the pricing research synthesis in conversation history) supports that the single most persuasive differentiator is the one you should gate. The AI Coach teaser in-product does more work than any landing-page copy.
- **Dashboard retention after trial is essential.** If the user can't see their own trial match's dashboards after credits run out, the conversion hook evaporates. This is enforced in the entitlement rules (§5).

### The upsell surface

Three placements, in order of expected conversion:

1. **AI Coach teaser in match_analysis.html.** User opens AI Coach tab → sees a locked state with "Ask a question about your match" prompt, a blurred sample response, a gold CTA: "Unlock AI Coach — upgrade to a plan." Links to `/pricing`.
2. **Credits-exhausted modal on Upload.** User tries to upload a 2nd match with 0 credits remaining → modal listing the Wix plans + PAYG packs.
3. **Dedicated Plans tab (`/pricing`).** Reference — rarely the primary conversion driver.

---

## 4. Paid tiers — Player

All player paid tiers bundle the full platform. **Technique Analysis and AI Coach are included at no extra cost** in every paid plan, for every user on that plan, with no separate credit counters.

### PAYG credit packs

| Pack | Wix plan ID | Match credits |
|---|---|---|
| 1-match | `33d94f21-e1b3-467c-b355-8b6aa225b815` | 1 |
| 3-match | `3f4b2758-d92b-42fc-9df4-b73d42b51fc5` | 3 |
| 5-match | `e38df7f5-1681-4ee9-beee-7ebd6bcf5c7e` | 5 |

Credits never expire. Technique + AI Coach included for as long as the account has any active/paid source. If a PAYG user exhausts credits, AI Coach re-locks until they top up or subscribe.

### Monthly subscriptions

| Plan | Wix plan ID | Matches / month | Featured |
|---|---|---|---|
| Starter | `9b8b3bd1-430b-45d9-8d1e-bdd75ffed130` | 3 | No |
| Standard | `64f83c88-6720-4ab2-a1c4-858f49eda7a7` | 5 | Yes ⭐ |
| Advanced | `b405caec-9e29-45c9-a2af-d5d70c152855` | 10 | No |

Unused monthly match credits do **not** roll over — see `cron_monthly_refill.py` + `monthly_no_rollover_reset()`. Technique and AI Coach remain unlimited while the subscription is `ACTIVE`.

---

## 5. Entitlement rules — the code contract

This is the authoritative definition. If the code does anything different, that is a bug.

### State sources

- `billing.account.active` — account-level kill-switch
- `billing.member.role` — `player_parent` or `coach`
- `billing.subscription_state.status` — `ACTIVE`, `CANCELLED`, `EXPIRED` (from Wix webhook)
- `billing.entitlement_grant.matches_granted` / `techniques_granted` — credits granted
- `billing.entitlement_consumption.consumed_matches` / `consumed_techniques` — credits used

### Derived flags (written to `billing.entitlements` by `entitlements_api.py`)

```
account_active       = billing.account.active
paid_active          = (subscription_state.status = 'ACTIVE')
matches_remaining    = greatest(sum(matches_granted) - sum(consumed_matches), 0)
techniques_remaining = greatest(sum(techniques_granted) - sum(consumed_techniques), 0)
```

### Permissions

```
can_upload_match
  = account_active
    AND role <> 'coach'
    AND matches_remaining > 0
  -- NOTE: no longer requires paid_active. Credits alone authorise upload.
  -- This is what enables the free-trial flow.

can_upload_technique
  = account_active
    AND role <> 'coach'
    AND techniques_remaining > 0

can_view_dashboards
  = account_active
    AND (
          paid_active
          OR matches_consumed > 0
          OR techniques_consumed > 0
          OR role = 'coach'
        )
  -- Users who ever uploaded anything keep permanent view access to their history.
  -- Essential for trial conversion — user must see what they're giving up.
  -- Also handles post-cancellation: cancelled subscribers keep their data.
  -- Coaches always view (their access is covered by the players they're linked to).

can_use_ai_coach
  = account_active AND paid_active
  -- AI Coach is the premium differentiator. Free trial does NOT include it.
  -- This is enforced at the API layer (402 UPGRADE_REQUIRED response).
```

### Block reasons

| Flag false → reason | When user sees it |
|---|---|
| `ACCOUNT_INACTIVE` | Account suspended/terminated. Hard block across everything. |
| `NO_MATCH_CREDITS` | Credits = 0, user tries to upload. → Credits-exhausted modal. |
| `NO_TECHNIQUE_CREDITS` | Technique credits = 0, user tries technique upload. → Upsell. |
| `COACH_VIEW_ONLY` | Role = coach, tried to upload. → Explain coach model. |
| `UPGRADE_REQUIRED` | AI Coach called with `paid_active = false`. → Upgrade CTA. |

---

## 6. Coach model

### Launch (this weekend)

- Coaches register (or are invited) with `role = 'coach'`
- Coach gets full view access to all linked players' dashboards
- Coach can use AI Coach **on their linked players' data** — this is the premium value for coaches
- Coach can view technique reports on their linked players
- Coach **cannot** upload matches (`can_upload_match = false`)
- **No payment required** for any of the above at launch

Why coaches are free at launch:
- ~0 coach users today. Friction kills channel adoption.
- Coaches are an acquisition channel: 1 coach invites 5-20 players → players pay. £25/mo from a coach caps upside at <10% of indirect revenue.
- Research supports free-the-channel, monetise-the-end-user (SmartMusic, CoachNow, Hudl patterns).

### Coach value proposition (marketing anchor — use verbatim on For-Coaches page)

> *Coaches can't travel to every match. Their kids lose tournaments and the coach doesn't know why. Our data tells them where the kids are falling short — detailed stats, performance history trending over time, the AI Coach giving them game knowledge, technique analysis putting them above their peers. Data-driven coaching, in real time.*

### Phase 2 (post-launch, not yet built)

- First linked player: **free forever**
- 2nd+ linked player: coach must subscribe to a paid Coach Pro plan
- Paid coach plan Wix IDs already exist: `82694b71-888d-471a-9f6c-1e99feb5a253` (1 month), `d0f5eda4-380b-416c-ae08-a3d26c63d840` (ongoing)
- Grandfather rule: coaches already active at the time of rollout keep unlimited players free, forever
- Metric to watch before rolling this out: % of player signups that came through a coach invite. If >25% at 3 months, leave it free. If <10% at 6 months, roll out the cap.

### What Coach Pro gives (when we build it)

Thin additions on top of existing views — enough to justify the price:

- Multi-player comparison dashboard
- Priority AI Coach queue
- PDF "session brief" export for lesson prep
- Cross-session trend view across the coach's whole stable

---

## 7. AI Coach — the differentiator

AI Coach is the single most persuasive feature we have. It also has real marginal cost (~$0.01/call, 1.2-1.5k Claude tokens).

### Access rules

| User state | Can call `/api/client/coach/analyze` | Can call `/api/client/coach/cards/<task>` | Shown in UI? |
|---|---|---|---|
| Admin (`info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`) | ✅ always | ✅ always | ✅ |
| Coach, any state | ✅ (their players' matches) | ✅ | ✅ |
| Player, paid_active = true | ✅ | ✅ | ✅ |
| Player, free trial (paid_active = false) | ❌ 402 `UPGRADE_REQUIRED` | ❌ 402 `UPGRADE_REQUIRED` | ✅ **teaser locked state** |
| Player, account inactive | ❌ 403 | ❌ 403 | Hidden |

### Rate limits (unchanged from current — separate from the paywall)

Inside the paid cohort, existing rate limits remain:
- 5 freeform calls per (email, task_id) per day
- 20 freeform calls per email per day
- Cards are not rate-limited

### Phase 2 — referral-to-unlock mechanic

When AI Coach usage caps are introduced (not at launch), the cap screen will offer:

> *"Refer a player. They get a free trial, you get 10 more Coach questions."*

Referral credits model: new `billing.referral` table, each successful referral grants 10 AI Coach credits to the referrer and normal signup-bonus to the referee. Out of scope for launch; design it properly when we build it.

---

## 8. Backend changes — summary

Five code changes land this cycle. All idempotent, all safe to re-deploy.

### 8.1 Schema (idempotent `ADD COLUMN IF NOT EXISTS`)

- `billing.entitlement_grant.techniques_granted INT NOT NULL DEFAULT 0`
- `billing.entitlement_consumption.consumed_techniques INT NOT NULL DEFAULT 0`
- `billing.entitlements.techniques_granted / techniques_consumed / techniques_remaining INT NOT NULL DEFAULT 0`

### 8.2 `billing_service.py`

- `grant_entitlement(...)` accepts `techniques_granted` param (default 0)
- Add `'signup_bonus'` to the allowed `source` whitelist
- New helper `consume_technique_for_task(account_id, task_id)` — inserts row with `consumed_matches=0, consumed_techniques=1`
- New helper `grant_signup_bonus(account_id)` — idempotent, grants 1 match + 5 techniques, `source='signup_bonus'`, `plan_code='signup_trial'`

### 8.3 `client_api.py::register_member`

- After `create_account_with_primary_member`, if role == `'player_parent'` call `grant_signup_bonus(account_id)`. Idempotent.

### 8.4 `entitlements_api.py::UPSERT_SQL`

- Drop `paid_active` from `can_upload`
- Add technique sums (granted/consumed/remaining)
- `can_view_dashboards = account_active AND (paid_active OR matches_consumed > 0 OR techniques_consumed > 0 OR role = 'coach')`
- Add technique columns to `billing.entitlements`
- Extend block_reason cascade with `NO_MATCH_CREDITS`, `NO_TECHNIQUE_CREDITS`

### 8.5 `tennis_coach/coach_api.py`

- Both `/analyze` and `/cards` gain a `_check_ai_coach_entitled(email)` call after ownership check
- Non-admin, non-coach, non-paid → 402 `{ok:false, error:'UPGRADE_REQUIRED', upgrade_url:'/pricing'}`

### 8.6 `pricing.html` (copy only, no logic changes)

Add to each paid plan's features list:
- "Technique Analysis included — unlimited"
- "AI Coach included — unlimited queries"

---

## 9. Marketing copy — primary CTAs

Consistent across Wix public pages and the in-product upsell surfaces.

| Context | CTA text |
|---|---|
| Hero (Home page) | **"Analyse My First Match Free"** |
| Pricing sub-hero | **"No card required. One match, on us."** |
| AI Coach locked state | **"Unlock your Coach — upgrade to a plan"** |
| Credits-exhausted modal | **"Out of credits — choose a plan"** |
| For-Coaches hero | **"Coach with data. Free account."** |

---

## 10. What we are NOT doing at launch (explicitly)

- ❌ Separate pricing for Technique or AI Coach — both are bundled into every paid plan
- ❌ Coach paid tier — coaches are free at launch
- ❌ Referral credits mechanic — designed in §7, not built
- ❌ AI Coach usage caps — unlimited for paid users at launch
- ❌ Credit rollover on monthly plans — status quo (no rollover)
- ❌ Annual / multi-month discount plans — future
- ❌ Family / team / academy bulk plans — future
- ❌ Coupon / promo codes — future

Every one of these is a valid Phase 2 lever. None of them are needed to launch.
