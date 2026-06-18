# Klaviyo Flow Build Spec — for Claude Code to create via REST API

> **Why this exists:** the Klaviyo MCP connector (Cowork's tool) can create templates + campaigns but **cannot create flows**. Klaviyo's REST API **can** (`POST /api/flows/` with a flow definition, plus the flow-action/message endpoints). This is a **code task → Claude Code's lane.** Cowork supplies this spec; Claude Code implements it with the live `KLAVIYO_API_KEY` (the `RENDER` private key, Full Access: Events + Profiles).
> Last updated: 2026-06-18. Author: Cowork.

## Ground rules
- Build both flows in **Manual/Draft status** (NOT live). Nothing sends until legal sign-off on consent → `marketing_opt_in`.
- Audience guard on every flow: only profiles with email marketing consent = SUBSCRIBED (opt-in). Klaviyo enforces this for flow emails, but set flow filters accordingly.
- Sender: account default (Ten-Fifty5 / info@ten-fifty5.com) — already set.
- If `POST /api/flows` is unavailable on the plan/API version, fall back to the Flow Builder UI using this same spec.

## Reference IDs (live in the account)

**Trigger metrics:**
| event | metric_id |
|---|---|
| account_created | UvEhHt |
| report_viewed | RRcmqL |
| match_uploaded | SxXTwc |
| subscription_started | THnJq4 |
| credit_purchased | S6dmAm |
| coach_accepted | Wi6bdW |
| nps_submitted | TaYW8P |

**Email templates (Trial→Paid):**
| step | template_id |
|---|---|
| 1.1 Welcome — first match free | U4uKSv |
| 1.2 Friction-buster — any camera | W9qEC3 |
| 1.3 Proof — what one match tells you | S9qBS7 |
| 2.1 The gap — one match vs a trend | TwGdDC |
| 2.2 AI Coach tease | SDMpYP |
| 2.3 The long game — progression | VEenA3 |
| 2.4 Last call — PAYG $25 | QWA2S6 |

**Email templates (Coach Engagement):**
| step | template_id |
|---|---|
| C0 How it works (orientation) | WuiVMV |
| C1 A player connected | SW5qXQ |
| C2 Three views | RTU6Cf |
| C3 AI coach | SEaDM9 |
| Coach Pro upsell (2nd player) | TfaGff |

---

## FLOW 1 — "Trial · Welcome & Activation"  (status: draft)
- **Trigger:** metric `account_created` (UvEhHt)
- **Flow filter:** has NOT done `match_uploaded` (SxXTwc) since starting this flow (so they exit once they upload)
- **Sequence:**
  1. Email → template **U4uKSv** (1.1 Welcome) — send immediately
  2. Time delay → **1 day**
  3. Conditional split: has done `match_uploaded` (SxXTwc) since flow start?
     - YES → exit flow
     - NO → Email → template **W9qEC3** (1.2 Friction-buster)
  4. Time delay → **2 days**
  5. Conditional split: `match_uploaded` since flow start?
     - YES → exit
     - NO → Email → template **S9qBS7** (1.3 Proof)

## FLOW 2 — "Trial → Paid Conversion"  (status: draft)
- **Trigger:** metric `report_viewed` (RRcmqL)
- **Flow filters (the "exit when converted" guard):**
  - has done `subscription_started` (THnJq4) **zero times** since starting this flow, AND
  - has done `credit_purchased` (S6dmAm) **zero times** since starting this flow
- **Sequence:**
  1. Time delay → **1 day**
  2. Email → template **TwGdDC** (2.1 The gap)
  3. Time delay → **2 days**
  4. Email → template **SDMpYP** (2.2 AI Coach tease)
  5. Time delay → **3 days**
  6. Email → template **VEenA3** (2.3 The long game)
  7. Time delay → **4 days**
  8. Email → template **QWA2S6** (2.4 Last call — PAYG $25)
- Add the same `subscription_started` / `credit_purchased` "zero times" check as a conditional split before each email so anyone who converts mid-sequence stops receiving.

---

## FLOW 3 — "Coach · Engagement"  (status: draft)
> Model guardrail: coaches never add players; a player grants access. Triggers are player-driven. No minor/biometric data — players referenced abstractly only.
- **Trigger A (orientation, optional):** metric `account_created` (UvEhHt) **filtered to role = coach** (the event must carry a role property; if it doesn't yet, skip Trigger A and rely on Trigger B). → Email **WuiVMV** (C0) immediately, then this branch ends.
- **Trigger B (main):** metric `coach_accepted` (Wi6bdW) — a player granted the coach access.
  - **Flow filter:** email marketing consent SUBSCRIBED.
  1. Email → **SW5qXQ** (C1 A player connected) — immediately
  2. Time delay → **2 days** → Email → **RTU6Cf** (C2 Three views)
  3. Time delay → **4 days** → Email → **SEaDM9** (C3 AI coach)

## FLOW 4 — "Coach Pro upsell"  (status: draft)
- **Trigger:** metric `coach_accepted` (Wi6bdW)
- **Flow filter:** has done `coach_accepted` (Wi6bdW) **at least 2 times** over all time (i.e. a 2nd player has connected) — this is the "outgrew the free player" signal. _(When a coach-shared-player-count trait exists, switch to that; for now the ≥2 event count is the proxy.)_ Optionally also exclude profiles already on Coach Pro.
  1. Email → **TfaGff** (Coach Pro upsell) — immediately (or after a short 1-hour delay)

---

## After Claude Code builds them
- Leave both **draft**. Ping Cowork — Cowork reviews the assembled flows (timing, filters, template mapping) via the connector's `get_flows`/`get_flow` (read) and confirms.
- Go-live gate (unchanged): legal sign-off on consent → real `marketing_opt_in` → set flows Live.
- Full copy + rationale for each email: `trial_to_paid_flow.md` (this folder).

---

## Subject lines + preview text (Cowork — final copy, no emoji per brand voice)

| template_id | subject_line | preview_text |
|---|---|---|
| U4uKSv | Your first match analysis is on us | One upload. 450+ data points. No card needed. |
| W9qEC3 | Still sitting on your first match? | Any camera works. One MP4 is all we need. |
| S9qBS7 | What one match actually tells you | The stat that ended a two-year losing streak. |
| TwGdDC | You've seen one match. Here's what you're not seeing yet. | One match is a snapshot. Your game is a trend. |
| SDMpYP | Ask your data why you lose the second set | A tour coach, trained on your matches. |
| VEenA3 | Every match teaches you something. Don't let it fade. | Your progression chart compounds. Your memory doesn't. |
| QWA2S6 | One more match? It's $25 — and your credits never expire. | Not ready to subscribe? Pay as you go. |
| WuiVMV | How Ten-Fifty5 works for coaches | When a player shares a match, it lands here. |
| SW5qXQ | A player just shared their game with you | Their dashboard is live on your roster. |
| RTU6Cf | The 3 views that change how you coach | Serve zones, rally drop-off, technique scores. |
| SEaDM9 | Ask the data about any player | A tour coach's read, grounded in their matches. |
| TfaGff | A second player connected — time to go unlimited | Coach Pro: every player who shares with you, one price. |

> Brand note: **no emoji** in subject lines (Ten-Fifty5 voice is confident/data-first). Replace the placeholder emoji subjects with these.

---

## Flow-filter condition schema (Cowork confirmation for Claude Code)

**What I can confirm from the live connector schema (ground truth):** filters live under `condition_groups`, attached at:
- flow level → `definition.profile_filter.condition_groups`
- per-action → `definition.data.trigger_filter.condition_groups` and `definition.data.action_output_filter.condition_groups`
- re-entry → `definition.reentry_criteria` (`duration`, `unit`)
- delays → `definition.data.value` + `definition.data.unit`; triggers → `trigger_type` / `trigger_id` / `trigger_subtype`

**What I CANNOT verify from memory (and won't guess):** the exact inner literals of a metric condition — i.e. the precise strings for `measurement` (count), the operator, and the `timeframe` ("since starting this flow"). The create-flow definition is **beta** and there's no existing flow in this account to read them back from, so I can't confirm the literal field names blind. Guessing risks exactly the failed call we're avoiding.

**Reliable path (recommended):** build the condition **once** — either in the Klaviyo UI on the first draft flow, or via a minimal API attempt — then **GET that flow's definition back** (`GET /api/flows/{id}?additional-fields[flow]=definition`). The returned JSON is the canonical schema; mirror its exact `condition_groups` structure for the rest. **The moment any flow with this filter exists, ping Cowork — I'll read it via the connector and hand back the exact literals.**

Conceptually the condition is correct: metric = `subscription_started` (and `credit_purchased`) · measurement = count · operator = equals/zero · timeframe = since starting this flow. Only the literal keys need ground-truth.

---

## CANONICAL SCHEMA — verbatim from Klaviyo's Create-Flow docs (2024-10-15)

Source: developers.klaviyo.com Create-and-retrieve-flows reference. These blocks are authoritative; mirror them exactly.

**Trigger + trigger filter (metric condition shown = a property filter on the triggering event):**
```json
"triggers": [
  { "type": "metric", "id": "<METRIC_ID>",
    "trigger_filter": { "condition_groups": [ { "conditions": [
      { "type": "metric-property", "metric_id": "<METRIC_ID>", "field": "price",
        "filter": { "type": "numeric", "operator": "greater-than", "value": 5 } }
    ] } ] } }
]
```
**Time-delay action:**
```json
{ "temporary_id": "t1", "type": "time-delay", "links": { "next": "t2" },
  "data": { "unit": "days", "value": 1, "secondary_value": 0, "timezone": "profile",
            "delay_until_time": null, "delay_until_weekdays": [] } }
```
**Conditional-split action (note `next_if_true` / `next_if_false`):**
```json
{ "temporary_id": "t2", "type": "conditional-split",
  "links": { "next_if_true": "t3", "next_if_false": "t4" },
  "data": { "profile_filter": { "condition_groups": [ { "conditions": [ { /* condition */ } ] } ] } } }
```
**Email-opt-in condition (USE THIS for the marketing-consent gate — verbatim from docs, channel switched to email):**
```json
{ "type": "profile-marketing-consent",
  "consent": { "channel": "email", "can_receive_marketing": true,
    "consent_status": { "subscription": "subscribed", "filters": null } } }
```
**Send-email action:**
```json
{ "temporary_id": "t3", "type": "send-email", "links": { "next": null },
  "data": { "message": { "from_email": "info@ten-fifty5.com", "from_label": "Ten-Fifty5",
      "reply_to_email": null, "cc_email": null, "bcc_email": null,
      "subject_line": "<from subject table>", "preview_text": "<from table>",
      "template_id": "<template_id>", "smart_sending_enabled": true, "transactional": false,
      "add_tracking_params": false, "custom_tracking_params": null, "additional_filters": null,
      "name": "Email #1" }, "status": "draft" } }
```

### ⚠️ The one gap (the exact condition CC asked about)
The docs example does **not** include a "**what someone has done → metric → zero times → since starting this flow**" condition — it only shows `metric-property` (property filter on the trigger event) and `profile-marketing-consent`. There is also no flow in the account to read back. So the exact literal for the *conversion-exit* condition (count of `subscription_started`/`credit_purchased` = 0 since flow start) is **still unconfirmed** — don't guess it.

**Recommended unblock:** create all 4 flows as **drafts now** with the verbatim blocks above + the email-opt-in gate, and **omit the conversion-exit filter for v1** (drafts send nothing; safe pre-legal). Capture the exit-filter literal before go-live by building that one condition in the UI once and reading the flow back (Cowork will paste the exact `conditions[0]`). That gets 4 clean drafts in immediately and defers only the one uncertain literal.

---

## ✅ BUILD STATUS (Claude Code) — v1 created 2026-06-18 (all DRAFT)

Created via `POST /ops/build-klaviyo-flows` (builder `flow_builder.py`) — exactly the recommended
unblock: verbatim send-email blocks + email opt-in gate, v1 linear, conversion-exit filters deferred.

| Flow | flow_id | trigger |
|---|---|---|
| Trial · Welcome & Activation | `XqsKqR` | account_created |
| Trial → Paid Conversion | `UqB2RZ` | report_viewed |
| Coach · Engagement | `WqTFg9` | coach_accepted |
| Coach Pro upsell | `YrcjEh` | coach_accepted |

**Now there ARE flows in the account** → Cowork can `get_flows`/`get_flow` to verify, and the v2
exit-filter literal can be read back from a UI-built split.

**v2 TODO before go-live:** (1) read back the canonical `flows-profile-metric` literal → patch
`flow_builder._done_metric_condition`; (2) add exit filters — Flow1 exit-on-`match_uploaded`, Flow2
exit-on-(`subscription_started` OR `credit_purchased`) + per-email splits; (3) **`YrcjEh` Coach Pro
MUST get the `coach_accepted` ≥ 2 (all-time) entry filter** or it targets a coach's first player;
(4) add via API re-create OR directly in the Klaviyo UI on these drafts; (5) legal consent → set Live.
