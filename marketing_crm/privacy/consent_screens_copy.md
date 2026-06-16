# Consent Screens — User-Facing Copy (DRAFT for legal review)

> Plain-language microcopy for every consent moment. Pairs with `privacy_decisions_and_consent_spec.md`
> (Part B) — each block maps to a `core.consent` type. For Claude Code to implement in the UI; for
> the lawyer (Tomo's wife 👋) to review. Tone: clear, honest, no dark patterns. Each consent stores
> its `policy_version` + timestamp. **Not legal advice.**

Design rules baked in: consent boxes are **unchecked by default**, marketing and biometric are
**separate** from terms, language is specific about pose data, and nothing is buried.

---

## 1. Signup screen — Terms + Privacy (`terms_of_service`, `privacy_policy`)

Placement: bottom of the signup form, above the "Create account" button.

> ☐ I agree to the [Terms of Service] and [Privacy Policy].

- **Required** (can't create account without it). Single checkbox can cover both, each linked.
- Microcopy under it (optional, small): _We'll only ever use your data to run your analysis and improve Ten-Fifty5. You're in control — change your mind anytime in Settings._

---

## 2. Signup screen — Marketing opt-in (`marketing_email`)

Placement: directly below the terms box. **Separate, unchecked, optional.**

> ☐ Send me tips, product updates and the occasional offer by email. (Optional — and you can unsubscribe anytime.)

- Sets `marketing_opt_in = true` only if ticked. This is the gate for the Klaviyo trial→paid flow.
- **EU/UK double opt-in** (Decision 5): if ticked, send a confirmation email (copy in §6) and only set `marketing_opt_in = true` after they click confirm.

---

## 3. Before first technique analysis — Biometric consent (`biometric_processing`)

Placement: a one-time modal shown the first time a user submits a **technique / pose** analysis
(not on ordinary match upload — show it at the moment it actually applies).

> **One quick thing before we analyse your technique**
>
> To break down your strokes, our technology maps the position of your body's joints across each
> frame of your video — your **skeletal "pose" data**. This is considered **biometric data**, so we
> ask for your explicit permission before we create it.
>
> We use it only to produce your technique analysis. We never share it, never use it for marketing,
> and you can withdraw this permission at any time — we'll stop pose processing and delete the pose
> data we hold.
>
> ☐ I explicitly consent to Ten-Fifty5 processing my biometric (pose) data to analyse my technique.
>
> [Learn more in our Privacy Policy]   **[Agree & analyse]**   [Not now]

- **Required to run technique analysis.** "Not now" cancels just that analysis; the rest of the product still works.
- For a **minor's** technique analysis, this consent is given by the managing adult (see §4) — show the parental framing instead.

---

## 4. Adding a junior — Parental/guardian consent (`minor_processing_parental`)

Placement: when an adult adds a junior player to their account.

> **You're setting up an account for a young player**
>
> Because [name] is under [16 — Decision 2], we need you, as their parent or guardian, to give
> permission for Ten-Fifty5 to process their data. That includes their profile details and — if you
> use technique analysis — their **biometric (pose) data** from match video.
>
> You stay in control of this account: you can review, export or delete their data at any time.
>
> ☐ I am the parent or legal guardian of this player and I consent to Ten-Fifty5 processing their
> data, including biometric (pose) data for technique analysis, as described in the [Privacy Policy].
>
> **[Confirm & continue]**

- Stores: `granted_by_user` = the adult, `subject_person` = the minor. Gates **all** processing of that minor's data, including biometric.
- If the parent declines the biometric portion, allow account setup but disable technique/pose for the junior.

---

## 5. Settings — Consent management / withdrawal

Placement: a "Privacy & consent" section in account settings. Show current state + a toggle each.

> **Your privacy choices**
>
> - **Marketing emails:** [On / Off] — _Turn off anytime; you'll still get essential service emails (like "your analysis is ready")._
> - **Biometric (pose) processing:** [On / Off] — _Turning this off stops future technique analysis and deletes the pose data we hold for you._
> - **Download my data** · **Delete my account**
>
> Questions or a specific request? Email info@ten-fifty5.com.

- Toggling marketing off = unsubscribe (sets `marketing_opt_in = false`).
- Toggling biometric off routes through `core.data_subject_request` → stop processing + delete pose data per retention rule.
- "Delete my account" / "Download my data" initiate DSARs.

---

## 6. EU/UK marketing double opt-in — confirmation email

Sent immediately if an EU/UK user ticks the marketing box (Decision 5). Plain text, from your
verified sender. Not a marketing email itself — it's a confirmation.

> **Subject:** Confirm your subscription to Ten-Fifty5 updates
>
> Almost there — please confirm you'd like tips, product updates and offers from Ten-Fifty5.
>
> **[Yes, confirm my subscription →]**
>
> If you didn't request this, just ignore this email and you won't hear from us.

- Only on click → `marketing_opt_in = true` and the welcome/marketing flows may begin.

---

## 7. Marketing email footer (compliance)

Required in every Klaviyo marketing email:

> You're receiving this because you opted in at ten-fifty5.com. [Unsubscribe] · [Update preferences]
> Ten-Fifty5, [postal address — set in Klaviyo].

---

## Mapping summary (for Claude Code)

| Screen | consent type | required? | sets |
|---|---|---|---|
| §1 Signup terms | `terms_of_service` + `privacy_policy` | yes | consent rows |
| §2 Signup marketing | `marketing_email` | no | `marketing_opt_in` (after confirm if EU) |
| §3 First technique | `biometric_processing` | yes (for technique) | biometric consent row |
| §4 Add junior | `minor_processing_parental` | yes (for minor) | parental consent; gates minor processing |
| §5 Settings | withdrawal of any of the above | — | updates/triggers DSAR |

Each row stores `policy_version` + timestamp. Final `policy_version` string comes from the lawyer
sign-off (per the decisions doc).
