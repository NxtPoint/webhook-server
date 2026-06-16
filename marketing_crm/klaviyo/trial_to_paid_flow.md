# Klaviyo — Trial→Paid Flow (copy + build spec)

_Owner: Cowork. Source of truth for naming: `../contracts/events.md` + `../contracts/lifecycle_stages.md`._
_Status: copy ready. NOT live — see "Go-live dependencies" at the bottom (Claude Code + setup tasks)._
_Last updated: 2026-06-16._

This is the **trial→paid machine**: two connected flows that take a person from signup to their
free first match to a paying plan, automatically. All triggers use canonical event names from
`events.md`. All audience rules use stages from `lifecycle_stages.md`.

**Hard rules honoured:** marketing email goes only to contacts with `marketing_opt_in = true`
(opt-in). The contact is always the **account owner / parent** — never a minor. No biometric/pose
data, no child PII, no match video is referenced or used as a trait. Match KPIs (aces, serve %)
are derived analytics and are safe to personalise with *if* fed as profile traits (optional, below).

Sender voice: confident, data-first, second person, short sentences. "Stop guessing." "Bring the
data, we'll find the edge." No hype, no emojis, specific numbers over adjectives.

---

## FLOW 1 — Welcome & Activation

**Goal:** get the new signup to upload their free first match and open the report (reach
`activated`). This is the biggest single drop-off point in any free-trial SaaS.

- **Trigger:** metric `account_created`
- **Flow filter:** `marketing_opt_in` is true · person `role` is player OR parent (coaches → separate coach flow) · has NOT `match_uploaded` since starting the flow
- **Exit on:** `match_uploaded` (they've activated — hand off to Flow 2 after they view the report)

### Email 1.1 — immediate
- **Subject:** Your first match analysis is on us
- **Preview:** One upload. 450+ data points. No card needed.
- **Body:**
  > Welcome to Ten-Fifty5.
  >
  > Here's how it works — three steps, about 20 minutes of your time:
  >
  > **1. Record one match.** Phone at the back of the court, club cam, any MP4.
  > **2. Upload it.** We track every shot, bounce and pose — 450+ data points.
  > **3. Read your game.** Serve heatmaps, rally breakdowns, technique scores. Ready in 1–2 hours.
  >
  > Your first match is free. No credit card.
  >
  > **[Analyse my first match →]**
  >
  > Bring the data. We'll find the edge.

### Delay: 1 day → (if still no `match_uploaded`)

### Email 1.2 — friction-buster
- **Subject:** Still sitting on your first match?
- **Preview:** Any camera works. One MP4 is all we need.
- **Body:**
  > Quick one — your free analysis is still waiting.
  >
  > The only thing we need is a single video of one match: one fixed camera at the back of the
  > court, roughly baseline height, both players in frame. Most club and indoor cameras already
  > produce exactly this. iPhone, Android, DSLR — all fine.
  >
  > Upload it and in an hour or two you'll see where your free points are hiding.
  >
  > **[Upload my match →]**

### Delay: 2 days → (if still no `match_uploaded`)

### Email 1.3 — what you'll see / proof
- **Subject:** What one match actually tells you
- **Preview:** The stat that ended a two-year losing streak.
- **Body:**
  > Most players guess at what's going wrong. One analysed match replaces the guessing with a map:
  >
  > — **Serve placement** by zone, with win-rate on each (wide / body / T).
  > — **Rally length** vs win-rate — exactly where your points fall apart.
  > — **Technique** scored frame-by-frame against beginner → pro.
  >
  > One of our players put it simply: *"One look at my rally-length heatmap told me I was hitting
  > middle under pressure. Fixed in a fortnight."*
  >
  > Your first match is still free.
  >
  > **[See my game in data →]**

---

## FLOW 2 — Trial→Paid Conversion

**Goal:** convert an activated free-trial player into a subscription or PAYG purchase.
Starts once they've *seen the value* (viewed their free report). Default first nudge is **the day
after** the report is viewed — let it land first.

- **Trigger:** metric `report_viewed` (first occurrence)
- **Flow filter:** `marketing_opt_in` is true · stage is `trial` (free match used, no active sub, no PAYG credits) · has `subscription_started` zero times · has `credit_purchased` zero times
- **Exit on:** `subscription_started` OR `credit_purchased`

### Delay: 1 day after report viewed

### Email 2.1 — the gap
- **Subject:** You've seen one match. Here's what you're not seeing yet.
- **Preview:** One match is a snapshot. Your game is a trend.
- **Body:**
  > Now you've seen what a single match reveals. But one match is a snapshot — your real edge is in
  > the **pattern across matches**.
  >
  > On a paid plan, every match you upload stacks into one picture:
  >
  > — Your 18 KPIs **trending** against your own baseline, match after match.
  > — Technique scored over time — is your kinetic chain actually improving?
  > — And your **AI Coach**, unlocked — ask anything about how *you* play, answered from your own data.
  >
  > Plans start at $25/mo. Every plan includes match analysis, technique and unlimited AI Coach —
  > no feature tiers, no surprise paywalls.
  >
  > **[See plans →]**

### Delay: 2 days → (exclude anyone who converted)

### Email 2.2 — the AI Coach tease
- **Subject:** Ask your data why you lose the second set
- **Preview:** A tour coach, trained on your matches.
- **Body:**
  > Here's the kind of thing the AI Coach does once it's unlocked. A real exchange:
  >
  > *"Why am I losing the second set so often lately — is it fitness?"*
  >
  > *"Not fitness. Across your last 5 matches your 1st-serve % drops from 64% in set one to 52% in
  > set two, and your backhand errors jump 40%. It's decision quality under fatigue."*
  >
  > That answer is grounded entirely in your own stats — not generic coaching content. It's
  > included, unlimited, on every paid plan.
  >
  > **[Unlock my AI Coach →]**

### Delay: 3 days → (exclude converted)

### Email 2.3 — the long game
- **Subject:** Every match teaches you something. Don't let it fade.
- **Preview:** Your progression chart compounds. Your memory doesn't.
- **Body:**
  > Your coach moves on. Your stats fade. But a progression chart — every KPI, every match, every
  > month — compounds.
  >
  > That's the part no notebook and no memory can give you: proof you're actually getting better,
  > and early warning when a part of your game slips.
  >
  > - **Starter — $25/mo** · 3 matches/month
  > - **Standard — $40/mo** · 5 matches/month
  > - **Advanced — $70/mo** · 10 matches/month
  >
  > All include unlimited Technique and AI Coach. Cancel anytime — and you keep full read-access to
  > everything you've analysed.
  >
  > **[Choose my plan →]**

### Delay: 4 days → (exclude converted)

### Email 2.4 — last call + low-commitment option
- **Subject:** One more match? It's $25 — and your credits never expire.
- **Preview:** Not ready to subscribe? Pay as you go.
- **Body:**
  > If a monthly plan isn't right yet, you don't have to commit to one.
  >
  > Analyse a single match for **$25** — pay as you go, credits never expire, no subscription. Same
  > full analysis, same technique breakdown.
  >
  > And if you do subscribe later, everything you've already analysed is still there waiting.
  >
  > **[Analyse one match — $25 →]**  ·  **[Or see monthly plans →]**
  >
  > Bring the data. We'll find the edge.

---

## Optional personalisation (enhancement, not required)
If Claude Code feeds a **headline KPI** from `core.match.kpi_summary` as a profile/event trait
(e.g. `last_match_aces`, `last_match_first_serve_pct` — all derived analytics, NOT biometric), the
"your match" emails get sharper: _"12 aces — a personal best. Imagine that tracked every match."_
Use a generic fallback when absent so the email still renders. **This is the only place match data
would enter Klaviyo, and only non-biometric KPIs.** Flagging for the contract.

## How this gets assembled in Klaviyo
The Klaviyo connector can manage templates and campaigns but **cannot create flows via API** —
flow assembly (trigger + delays + conditional splits) is done in Klaviyo's visual Flow Builder.
Two options: (a) I create the email **templates** in your account now and walk you click-by-click
through wiring the two flows, or (b) via Chrome I assemble the flows on your behalf while you watch.
Recommend (a).

---

## ⚠️ Go-live dependencies (this flow cannot send until these are done)

**For Claude Code (data + emission):**
1. **Profiles + events must feed into Klaviyo** (downstream of `core.*`, per briefing rule 3). This flow triggers on: `account_created`, `match_uploaded`, `report_viewed`, `subscription_started`, `credit_purchased`.
2. **Three of those aren't emitted yet.** `core.usage_event` today emits `match_upload`, `report_view` (note: singular/spelling differs from the canonical `match_uploaded` / `report_viewed` in `events.md` — needs reconciling) and does **not** yet emit `account_created`, `subscription_started`, or `credit_purchased`. These must be added to `events.md` status + emitted before Flow triggers work.
3. **Trait to sync per profile:** `first_name`, `email`, `marketing_opt_in`, lifecycle `stage`, and (optional) a non-biometric headline KPI. Keyed by `account.public_id` + email.

**For setup (Tomo / Cowork, no code):**
4. **Marketing-consent capture must exist.** `privacy_inputs.md` confirms no consent capture in prod yet. Marketing email is opt-in only — we cannot email anyone without `marketing_opt_in = true`. This is a launch blocker for sending.
5. **Sending domain authentication** (SPF/DKIM/DMARC) in Klaviyo, and **set a default sender email** (currently blank in the account — likely `hello@` or `coach@ten-fifty5.com`).
6. **Add a physical postal address** in Klaviyo account settings (legally required in the email footer; currently blank).
7. **Don't duplicate the transactional "your match is ready" email** — that stays in SES (`match_processed`). Klaviyo only listens to that event for timing; it does not resend it.
