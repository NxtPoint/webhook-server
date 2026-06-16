# Klaviyo — Coach Invite Campaign (copy + build notes)

_Owner: Cowork. A one-off **campaign** (broadcast), not a triggered flow._
_Status: draft created in Klaviyo 2026-06-16 for a self-test to info@ten-fifty5.com._

## Purpose
Invite a coach/academy to sign up for Ten-Fifty5 (free first linked player). Used here as a
**deliverability + experience test** sent to Tomo (NextPoint Tennis) so he can receive it, click
through, and join ten-fifty5.

## ⚠️ Important channel note
Klaviyo is for **permission-based** email. Use it for coaches who **opted in / signed up / were
invited in-product**. For **cold** outreach to coaches & academies who've never heard of us, use a
dedicated cold-email tool (Instantly, Smartlead, etc.) — sending cold lists through Klaviyo risks
the domain reputation and may breach Klaviyo's terms. This test send is to ourselves, so it's fine.

> **Note (corrected model):** coaches never add/enrol players — a *player* invites the coach and
> grants access. So this acquisition email gets a coach to **create a free account**; their players
> then choose to share match data with them. No "add a player" ask.

## Email copy
- **Subject:** Coach your players with the data the pros use
- **Preview:** When your players share their matches, you see everything.
- **From:** coach@ten-fifty5.com (label "Ten-Fifty5") — _sender must be verified before send_
- **Body:**
  > Hi {{ first_name|default:'Coach' }},
  >
  > Your players are already recording their matches. Ten-Fifty5 turns those videos into ATP-grade
  > analysis — serve placement, rally patterns, biomechanical technique scores — plus an AI coach
  > trained on each player's own data.
  >
  > How it works for coaches:
  > - Create your **free coach account**.
  > - When a player analyses a match and **shares it with you**, their full dashboard lands on your roster.
  > - Your **first shared player is free, forever**; go **unlimited on Coach Pro ($50/mo)** when more connect.
  >
  > Bring the data. We'll find the edge — for every player on your roster.
  >
  > **[Create my free coach account →]**  (links to the portal signup)

## To actually send the test (Tomo, ~2 min)
1. Klaviyo → **Settings → Email → set a default sender email** (e.g. coach@ten-fifty5.com) and verify it.
2. Open the draft campaign **"Coach Invite — TEST"** and either send to yourself or hit Send (audience is the default "Email List", which currently only contains you).
3. (If asked to confirm a double opt-in, click the confirmation — the default list is double-opt-in.)

## Build state in Klaviyo
- Template: "Coach Invite — Ten-Fifty5" (CODE editor).
- Campaign: "Coach Invite — TEST", audience = Email List (SeWC2n), draft.
- Recipient profile: info@ten-fifty5.com (Tomo / NextPoint Tennis), persona=coach.

## Reality on testing the *flows* vs this *campaign*
This campaign tests sending + the join experience. It does **not** test the trial→paid **flows** —
those are triggered automations that need Claude Code's event/profile feed into Klaviyo first
(see `trial_to_paid_flow.md` → Go-live dependencies). Joining ten-fifty5 won't auto-fire flow emails
until that pipe exists.
