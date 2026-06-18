# Privacy & Consent

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)). Merges the privacy/consent inputs, the open legal decisions + consent-capture spec, the privacy policy draft, and the user-facing consent screen copy. All consent types map to the `core.consent` model. **DRAFT — not yet legally reviewed.**

Sources merged (verbatim): `marketing_crm/contracts/privacy_inputs.md`, `marketing_crm/privacy/privacy_decisions_and_consent_spec.md`, `marketing_crm/privacy/privacy_policy_draft.md`, `marketing_crm/privacy/consent_screens_copy.md`.
---

# Privacy inputs (factual basis)

# Contract: Privacy inputs (brief for the consent/privacy policy)

Cowork drafts the privacy + consent policy from this; a lawyer reviews; the **final policy
version strings + retention day-counts come back here** and get loaded into
`core.consent.policy_version` and `core.retention_rule`. This file is the factual basis — keep it
accurate as the system changes.

## What we collect & where it lives
| Data | Category | Store | Notes |
|---|---|---|---|
| Email, name, phone, country | PII | `core.account` / `core.person` | |
| Child DOB, skill level, club/school, notes | **Minor PII** | `core.person` | `is_minor` flag |
| Match video | Personal (image) | S3 (`nextpoint-prod-uploads`) | original deleted post-trim; trimmed kept |
| Pose / skeletal keypoints | **Biometric (likely Art. 9 special category)** | `ml_analysis.player_detections` | the sensitive one |
| Match analytics | Derived | `silver.*` / `gold.*` / `core.match.kpi_summary` | |
| Usage events | Behavioural | `core.usage_event` | |
| Payment | Financial | **PayPal** (not our DB) | we store no card data |

## Processors / sub-processors (data leaves us to)
SportAI API (analysis) · AWS (S3, Batch GPU, SES email — `us-east-1` + `eu-north-1`) · Anthropic
(AI coach / support bot) · **Clerk** (authentication — LIVE 2026-06-17) · **PayPal** (payment — direct,
LIVE 2026-06-16) · **HubSpot** (CRM — PII only, no minors/biometrics) · **Klaviyo** (lifecycle email —
opt-in only) · **Amplitude** (product analytics). *(Wix retired 2026-06-16/17 — no longer a processor;
rollback path only.)* **⚠️ Cowork: update `privacy/privacy_policy_draft.md` sub-processor list to match
(Wix→Clerk for auth; payment = PayPal, not "PayPal/Wix").**

## Consent model already built (core.consent)
Per-type, versioned, with `subject_person` vs `granted_by_user` so a **parent consents for a minor**:
`terms_of_service`, `privacy_policy`, `marketing_email`, `biometric_processing`,
`minor_processing_parental`. DSARs tracked in `core.data_subject_request`; retention in
`core.retention_rule`.

## ⚖️ Decisions the policy must resolve (still OPEN — DB-SCHEMA-PROPOSAL.md §5)
1. **Biometric basis** — is pose data Art. 9 special-category? Explicit-consent wording + lawful basis.
2. **Age of digital consent** — single conservative threshold (e.g. 16) vs per-country; parental-consent verification method.
3. **Retention windows** — concrete days for video / biometrics / analysis / PII after closure or withdrawal.
4. **Erasure vs financial records** — keep anonymised billing rows ~6–7 yrs while erasing PII/biometrics?
5. **Marketing consent** — explicit opt-in (+ double opt-in?) is the assumed default for EU.
6. **Data residency** — EU subjects' video/PII currently span `us-east-1` + `eu-north-1`; EU-only required?

Current reality (confirmed 2026-06-16): **no formal consent capture or retention exists in prod yet.**
The policy + these decisions are launch-blocking for processing minors' biometrics at scale.

---

# Open decisions + consent-capture spec

# Privacy — Open Decisions + Consent-Capture Spec

> Companion to `privacy_policy_draft.md`. Two jobs:
> (A) give your lawyer a tight brief on the **6 open decisions** with a recommended default for each, and
> (B) specify the **consent capture** Claude Code needs to build (mapped to `core.consent`).
> Outputs that must come BACK here after legal sign-off: final policy **version string**, the
> **retention day-counts**, and confirmed **age threshold** — these load into `core.consent.policy_version`
> and `core.retention_rule`. **Not legal advice.**

---

## Part A — The 6 open decisions (recommended defaults for the lawyer to confirm)

These mirror `privacy_inputs.md` §"Decisions the policy must resolve". For each: the question, my
recommended sensible default, and why. Treat the recommendation as a starting position, not a ruling.

### 1. Biometric basis (pose data)
**Question:** Is skeletal/pose data Article 9 special-category, and what's the lawful basis?
**Recommended default:** Treat it as special-category and rely on **explicit, separate, opt-in
consent** for biometric processing — distinct from the general terms and collected before the first
technique analysis. Rationale: pose-based body data is high-sensitivity; explicit consent is the
cleanest, most defensible basis and you already have a `biometric_processing` consent type built.

### 2. Age of digital consent + parental verification
**Question:** One conservative age threshold or per-country? How is parental consent verified?
**Recommended default:** Single conservative threshold — **under 16 requires verifiable parental
consent** — applied globally for simplicity at this stage. Verification: account created by the
adult, adult confirms guardianship at consent time, and consent is re-affirmable. Rationale: avoids
per-country complexity now; 16 is the GDPR ceiling so it's safe across the EU. Revisit per-market
later (e.g. 13 in the US under COPPA may be acceptable, but conservative-global is simpler/safer).

### 3. Retention windows (the numbers the system needs)
**Question:** Concrete day-counts per data type.
**Recommended defaults (to confirm, in days):**

| Data | Recommended default |
|---|---|
| Original uploaded video | Deleted immediately post-processing (already the case) |
| Trimmed review clip | Kept while account active; delete **90 days** after account closure |
| Biometric / pose keypoints | Delete **30 days** after account closure **or immediately on biometric-consent withdrawal** |
| Derived match analytics | Kept while account active; delete **90 days** after closure |
| Account & profile PII | Delete **90 days** after closure |
| Anonymised financial records | Retain **7 years** (tax), PII stripped — see Decision 4 |

Rationale: short windows for the sensitive stuff (video/biometrics), modest grace period for
everything else so a returning user isn't wiped instantly.

### 4. Erasure vs financial records
**Question:** Can we keep billing rows while erasing PII/biometrics?
**Recommended default:** **Yes** — on erasure, delete/anonymise PII, video and biometrics, but
retain **anonymised** billing/transaction records (~7 years) to meet tax/accounting law. This is a
standard and defensible carve-out from the right to erasure.

### 5. Marketing consent
**Question:** Explicit opt-in? Double opt-in?
**Recommended default:** **Explicit opt-in** everywhere (unchecked box, separate from terms).
**Double opt-in for EU/UK** signups (confirmation click) to maximise deliverability and defensibility.
Rationale: protects the domain reputation for the Klaviyo flows and is the safest GDPR posture. This
is the item that directly **unblocks the trial→paid email flow**.

### 6. Data residency
**Question:** Must EU subjects' video/PII stay EU-only?
**Recommended default (interim):** Process globally across us-east-1 + eu-north-1 under **Standard
Contractual Clauses**, and disclose it (policy §13). Flag EU-only residency as a **later
infrastructure decision** — pinning EU subjects to eu-north-1 is a real engineering change, not a
policy toggle. Rationale: lawful now via SCCs; don't block launch on a data-locality migration.

> ⚠️ Reality check from `privacy_inputs.md`: there is **no consent capture or retention in prod yet**,
> and this is **launch-blocking for processing minors' biometrics at scale**. Decisions 1, 2, 3 and 5
> are the critical path.

---

## Part B — Consent-capture spec (for Claude Code to build)

Maps directly to the `core.consent` model already built (per-type, versioned, with
`subject_person` vs `granted_by_user`). Each consent is recorded with: type, policy_version,
timestamp, subject_person, granted_by_user, and method/UI source.

| consent type (`core.consent`) | When captured | UI requirement | Subject vs grantor |
|---|---|---|---|
| `terms_of_service` | Signup | Required checkbox | self |
| `privacy_policy` | Signup | Required checkbox (link to policy) | self |
| `marketing_email` | Signup + profile settings | **Separate, unchecked** opt-in; EU/UK → double opt-in (Decision 5) | self; sets `marketing_opt_in` |
| `biometric_processing` | Before **first technique/pose** analysis | **Separate, explicit** opt-in, clearly explaining pose data (Decision 1) | self, or parent for minor |
| `minor_processing_parental` | When an adult adds a junior | Adult affirms guardianship + age; gates all minor processing (Decision 2) | granted_by_user = adult; subject_person = minor |

**Rules:**
- `marketing_email = true` is the **only** gate that lets a contact enter a Klaviyo marketing flow. No opt-in → no marketing email. (This is the dependency the trial→paid flow waits on.)
- `biometric_processing` must be granted before pose data is generated; withdrawal stops future pose processing and triggers biometric deletion per Decision 3.
- Re-consent on **material** policy version changes; store which `policy_version` each consent was given against.
- Consent withdrawal routes through the existing `core.data_subject_request` flow.

**What comes back to code after the lawyer signs off:**
1. Final `policy_version` string → `core.consent.policy_version`.
2. Confirmed retention day-counts (Decision 3/4) → `core.retention_rule`.
3. Confirmed age threshold (Decision 2) → minor-gating logic.
4. EU double-opt-in confirmed yes/no (Decision 5) → marketing consent flow.

---

## Suggested next steps
1. **You:** send `privacy_policy_draft.md` + Part A to a lawyer (ideally one who knows GDPR + biometric/special-category data and minors). The recommended defaults make this a fast, cheap review rather than a blank-page engagement.
2. **Lawyer → back here:** confirm the 6 decisions; finalise the policy.
3. **Cowork (me):** fold the lawyer's answers into the final policy version(s).
4. **Claude Code:** build consent capture (Part B) + load `policy_version` and `retention_rule`.
5. **Then:** marketing consent exists → the Klaviyo trial→paid flow can switch on.

---

# Privacy policy (DRAFT)

# Ten-Fifty5 — Privacy Policy (DRAFT for legal review)

> **DRAFT — not yet legally reviewed.** Cowork prepared this from `../contracts/privacy_inputs.md`.
> Bracketed placeholders `[…]` mark the 6 open decisions in `privacy_decisions_and_consent_spec.md`
> — a lawyer must confirm these before publishing. This is not legal advice.
>
> **Effective date:** [TBC] · **Version:** draft-0.1 · **Last updated:** 2026-06-16

---

## 1. Who we are
Ten-Fifty5 ("Ten-Fifty5", "we", "us") provides AI-powered tennis match analysis. You upload match
video and we return performance statistics, biomechanical (pose-based) technique analysis, and an AI
coaching assistant grounded in your own data.

- **Data controller:** [Legal entity name + registered address — TBC]
- **Contact:** info@ten-fifty5.com
- **Data protection contact / representative:** [TBC — see Decision 6 on jurisdiction]

This policy explains what we collect, why, the legal bases we rely on, who we share it with, how
long we keep it, and the rights you have.

## 2. Who this applies to
Players, parents/guardians managing a junior's account, and coaches who use Ten-Fifty5. Because we
analyse video of people playing tennis, a match you upload may contain personal data about **other
people** (e.g. your opponent). See Section 9 on footage of third parties.

## 3. The data we collect

**Account & profile (personal data)**
Name, email, phone (optional), country, role (player / parent / coach), and login credentials
(authentication is handled by our provider, Clerk).

**Junior / minor profile data**
Where a parent or guardian sets up a junior, we may hold the child's date of birth, skill level,
club or school, and coaching notes. Accounts for minors are managed by a consenting adult — see
Section 8.

**Match video**
The video you upload. The original upload is **deleted after processing**; a trimmed review clip is
retained as part of your match history.

**Biometric data (special category)**
To produce technique analysis we extract **skeletal pose / keypoint data** (the position of body
joints across frames). We treat this as biometric data and, where applicable, as a **special
category of personal data** under Article 9 GDPR. We only process it on the basis described in
Section 6 and Decision 1.

**Match analytics (derived data)**
Statistics derived from your video — serve placement, rally metrics, KPIs, technique scores.

**Usage data**
Events such as logins, uploads, report views, and AI-coach queries, used to operate and improve the
service.

**Payment data**
Payments are processed by **PayPal**. We do **not** store your card details.

## 4. Where your data comes from
Almost all of it comes directly from you (your account details and the video you upload) and from
your use of the service (usage events, derived analytics). For juniors, the managing adult provides
the child's details.

## 5. How we use your data
- Provide the core service: process your video into statistics, technique analysis, and AI coaching.
- Maintain your match history and progression over time.
- Operate, secure, troubleshoot and improve the platform.
- Communicate with you about your account and analyses (service messages).
- Send marketing communications **only where you have opted in** (Section 7).
- Comply with legal, tax and accounting obligations.

## 6. Legal bases for processing (GDPR / UK GDPR)
- **Contract** — to deliver the analysis you sign up for (account, video processing, analytics, AI coach).
- **Consent** — for **biometric/pose processing** [Decision 1: explicit consent as the assumed basis], for **marketing email** (Section 7), and for processing a **minor's** data via parental consent (Section 8). You can withdraw consent at any time (Section 10).
- **Legitimate interests** — to secure, maintain and improve the service, where not overridden by your rights.
- **Legal obligation** — to retain certain financial records (Section 11).

## 7. Marketing communications
We send marketing email (tips, product updates, offers) **only to people who have explicitly opted
in** [Decision 5: explicit opt-in; double opt-in for EU/UK under review]. Every marketing email has
an unsubscribe link, and you can opt out at any time without affecting the service. Service/
transactional messages (e.g. "your analysis is ready") are not marketing and are sent as part of
delivering the product. Our marketing email is delivered via Klaviyo (Section 12).

## 8. Children's data and parental consent
Ten-Fifty5 is designed to be used **by adults**, including parents/guardians and coaches who manage
junior players. Accounts involving a minor must be created and managed by a consenting adult.

- We process a minor's data (including biometric/pose data from their match video) only with **verifiable parental/guardian consent** [Decision 2: age threshold + verification method].
- The consenting adult can review, export or delete the minor's data at any time.
- We do not knowingly let minors create their own accounts or direct marketing at minors.
- If you believe a minor's data has been provided without proper consent, contact info@ten-fifty5.com and we will act promptly.

## 9. Footage of other people
A match video may show other individuals (e.g. your opponent or doubles partners). By uploading, you
confirm you have a proper basis to share that footage with us for analysis. We process incidental
third-party data only to deliver your analysis, apply the same retention and security to it, and
will respond to any rights request from an identifiable individual in the footage.

## 10. Your rights
Subject to your jurisdiction, you may have the right to: access your data; correct it; erase it
("right to be forgotten"); restrict or object to processing; data portability; and to **withdraw
consent** at any time (including for biometric processing and marketing). Where we rely on consent
for biometric data, withdrawing it means we stop technique/pose processing going forward.

To exercise any right, contact **info@ten-fifty5.com**. We handle requests as data-subject requests
and respond within the timeframe required by law (generally one month under GDPR/UK GDPR). You also
have the right to complain to your data protection authority.

## 11. How long we keep your data (retention)
We keep personal data only as long as needed for the purposes above. Concrete retention periods are
[Decision 3 — to be set in days per data type and loaded into our system]:

| Data | Retention (to confirm) |
|---|---|
| Original uploaded video | Deleted after processing |
| Trimmed review clip | [TBC] |
| Biometric / pose keypoints | [TBC — Decision 3] |
| Derived match analytics | [TBC] |
| Account & profile PII | Until account closure + [TBC] |
| Anonymised financial records | [Decision 4 — e.g. ~6–7 years for tax] |

On account closure or consent withdrawal we delete or anonymise data per these rules, except where
we must retain limited (anonymised) financial records to meet legal obligations [Decision 4].

## 12. Who we share data with (sub-processors)
We do not sell your data. We share it with vetted providers strictly to run the service:

| Provider | Purpose | Notes |
|---|---|---|
| SportAI | Match video analysis | Processes uploaded footage |
| Amazon Web Services (AWS) | Storage (S3), GPU processing, transactional email (SES) | Regions: US (us-east-1) and EU (eu-north-1) — see Section 13 |
| Anthropic | AI coach & support assistant | Processes your stats/queries to generate responses |
| Clerk | Authentication | Login / account / session |
| PayPal | Payments | We store no card data |
| HubSpot | CRM | **PII only — never minors' data or biometrics** |
| Klaviyo | Marketing email | **Opt-in contacts only — never minors' data or biometrics** |
| Amplitude | Product analytics | Behavioural usage data |

We contractually require these providers to protect your data and process it only on our
instructions.

## 13. International transfers
We operate in both the **United States (us-east-1)** and the **European Union (eu-north-1)**. Your
data — including video and biometric data — may be processed in either region [Decision 6: whether
EU subjects' data is kept EU-only]. Where data is transferred internationally, we rely on
appropriate safeguards (e.g. Standard Contractual Clauses) as required by law.

## 14. Security
We use appropriate technical and organisational measures to protect your data, including access
controls and encryption in transit. No system is perfectly secure, but biometric data and minors'
data receive heightened care, and we never route them into marketing or CRM tools.

## 15. Cookies & analytics
We use necessary cookies to run the site and analytics (e.g. Amplitude) to understand and improve
usage. [Cookie banner / consent for non-essential cookies — to confirm with Section 7 and Decision 5.]

## 16. Changes to this policy
We may update this policy. Material changes will be notified and the version/effective date above
will change. Continued use after an update means the updated policy applies.

## 17. Contact
Questions or requests: **info@ten-fifty5.com**.

---

# Consent screens — user-facing copy (DRAFT)

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
