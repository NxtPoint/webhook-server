# Klaviyo — Coach Engagement Flow (copy + build spec)

_Owner: Cowork. Source of truth for naming: `../contracts/events.md` + `../contracts/lifecycle_stages.md`._
_Status: copy ready. NOT live — see "Go-live dependencies". Pairs with `trial_to_paid_flow.md` (player conversion)._
_Last updated: 2026-06-16. **Revised** to the player-initiated access model._

## ‼️ Product truth this flow must respect
**A coach can NEVER add, enrol, or invite a player.** Access is always **player-initiated and
player-controlled**: the *player* invites the coach and the *player* grants and revokes access.
Therefore this flow contains **no enrolment, no "add a player", no "go get players" asks**. It is
**reminders + product-feature education + the Coach Pro upsell only**, triggered by access the
player has already granted.

**Hard privacy rules:** marketing email only to coaches with `marketing_opt_in = true`; coach is an
adult; **no minor PII or biometric data** about players ever enters Klaviyo — players are referenced
only abstractly ("a player", "your roster"), never a child's name/DOB/pose data.

Voice: "Coach with data." Confident, practical, coach-to-coach. Every CTA points at viewing/using
what they already have access to — never at acquiring players.

---

## Coach lifecycle (proposed — needs adding to `lifecycle_stages.md`)
| coach stage | entry condition |
|---|---|
| `coach_signup` | account_created role=coach, **no player has granted access yet** |
| `coach_active` | ≥1 player has granted access (`coach_accepted`) **and** coach has viewed that player's data |
| `coach_pro` | active Coach Pro subscription (unlimited players) |
| `coach_at_risk` | active/pro but no coach activity in 30+ days |

> ⚠️ Flag for Claude Code: confirm/add these to `lifecycle_stages.md`.

---

## FLOW 3 — Coach Engagement

### Optional orientation (cold self-signup coach, no access yet)
A coach who signs up before any player has linked them has nothing to view. We send **one** purely
informational email — **no ask to recruit players** (that's not something the coach can do):

- **Trigger:** `account_created` where `role = coach`
- **Email C0 — how it works**
  - **Subject:** How Ten-Fifty5 works for coaches
  - **Preview:** When a player shares their match data, it lands here.
  - **Body:**
    > Welcome to Ten-Fifty5.
    >
    > Here's how coaching with data works on our platform: when one of your players analyses a match
    > and **chooses to share it with you**, their full dashboard appears on your roster — serve and
    > rally breakdowns, technique scores, and an AI coach trained on their matches. Players control
    > what they share, always.
    >
    > Your first shared player is **free, forever**. We'll let you know the moment a player connects
    > with you.
  - _(No CTA to add/recruit players — none exists for a coach.)_
- Exit C0 once `coach_accepted` fires (a player grants access) → into the main flow below.

### Main flow — triggered when a player grants access
- **Trigger:** `coach_accepted` (a player has invited the coach / granted access)
- **Flow filter:** `marketing_opt_in` true · role is coach
- **Goal:** get the coach using the data they now have access to (→ `coach_active`)

#### Email C1 — immediate · a player connected with you
- **Subject:** A player just shared their game with you
- **Preview:** Their dashboard is live on your roster.
- **Body:**
  > Good news — a player has connected with you on Ten-Fifty5 and shared their match data.
  >
  > It's all on your roster now: serve placement, rally patterns, technique scored frame-by-frame,
  > and an AI coach trained on their actual matches. Open it and find where their next gain is hiding.
  >
  > **[Open my roster →]**
  >
  > _(Your first shared player is free for you, forever.)_

#### Delay 2 days → (if coach hasn't viewed the data)
#### Email C2 — feature: read the match like a coach
- **Subject:** The 3 views that change how you coach
- **Preview:** Serve zones, rally drop-off, technique scores.
- **Body:**
  > Once you're in your roster, three views do most of the work:
  > - **Serve placement** — where their serves land and win, by zone.
  > - **Rally analysis** — the exact rally length where their points fall apart.
  > - **Technique** — strokes scored frame-by-frame, kinetic chain sequenced.
  >
  > Spot the pattern, set the next session around it.
  >
  > **[Open my roster →]**

#### Delay 4 days
#### Email C3 — feature: the AI coach on their data
- **Subject:** Ask the data about any player
- **Preview:** A tour coach's read, grounded in their matches.
- **Body:**
  > Your roster includes an AI coach for every shared player — ask it anything and it answers from
  > *their* real stats: *"Why is this player losing the second set?" "Where are the free points on
  > their serve?"* It's grounded in their matches, not generic tips. Unlimited, on every shared player.
  >
  > **[Try it on a player →]**

> Coach becomes `coach_active` once they've viewed a shared player's data.

---

## Coach Pro upsell (sub-flow) — also player-driven
Per the product: a coach's **first** shared player is free forever; when a **second** player grants
access, that's the moment for Coach Pro (unlimited shared players, $50/mo).

- **Trigger:** 2nd `coach_accepted` for the same coach (i.e. active shared-player count reaches 2)
- **Flow filter:** `coach_active`, not already `coach_pro`
- **Email — go unlimited**
  - **Subject:** A second player connected — time to go unlimited
  - **Preview:** Coach Pro: every player who shares with you, one price.
  - **Body:**
    > More of your players are sharing their data with you — that's the whole point.
    >
    > **Coach Pro ($50/mo)** opens your roster to **unlimited** shared players. Every one gets full
    > match analysis, technique scoring and their own AI coach; their credits still cover their
    > uploads. No choosing which players you can see.
    >
    > **[Upgrade to Coach Pro →]**

---

## Retention (reminders only)
- **Roster digest (monthly):** which shared players improved, who's gone quiet, biggest movers —
  pulls the coach back in. Keep it free of minor names (use the player's account display name only;
  never a child's name).
- **`coach_at_risk`** (30 days no activity) → "your players' latest matches are waiting" reminder.

---

## ⚠️ Go-live dependencies

**For Claude Code (events / traits):**
1. **`coach_accepted` already exists** in `events.md` — it's the real entry trigger (player granted access). ✅
2. **Active shared-player count per coach** needed to time the Coach Pro upsell (2nd grant). Suggested trait `coach_shared_player_count`, derived from `core.relationship` (coach_player, status=active). Not in the contract yet — please add.
3. **Access-revoked event** (player removes a coach) is worth adding (suggested `coach_access_revoked`) so we can suppress/branch — players control access and may revoke. Not in the contract yet.
4. A coach-attributable **`report_viewed`** (coach viewed a shared player's data) to mark `coach_active`. Confirm `report_viewed` can carry the viewer = coach.
5. **Profile traits to sync (coach, PII-only):** `first_name`, `email`, `marketing_opt_in`, `role=coach`, coach stage, shared-player count. No player minor data, ever.
6. **Coach lifecycle stages** (above) added to `lifecycle_stages.md`.

**For setup (same as the player flow):** marketing-consent capture; sender domain auth + default sender; postal address. Flows assembled in Klaviyo's Flow Builder (connector can't create flows) — I'll create templates and walk you through wiring, or do it via Chrome.

---

## How the assets fit together (corrected)
- **Players** drive everything: a player signs up, analyses matches (`trial_to_paid_flow.md`), and may
  invite a coach and grant access.
- **This coach flow** activates and retains a coach **once a player has connected them**, and upsells
  Coach Pro when a 2nd player connects. It never asks a coach to recruit players — coaches can't.
- Coach **acquisition** (getting coaches to create an account) is a separate, cold channel — and is
  **not** done through Klaviyo (permission-based only). See the cold-outreach plan.
