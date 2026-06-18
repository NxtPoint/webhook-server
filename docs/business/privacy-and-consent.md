# Privacy & Consent

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)). Merges the privacy/consent inputs, the open legal decisions + consent-capture spec, the privacy policy draft, and the user-facing consent screen copy. All consent types map to the `core.consent` model. **DRAFT — decisions are research-backed (see [`privacy-legal-research.md`](privacy-legal-research.md)), pending final legal confirmation.**

Sources merged (verbatim): `marketing_crm/contracts/privacy_inputs.md`, `marketing_crm/privacy/privacy_decisions_and_consent_spec.md`, `marketing_crm/privacy/privacy_policy_draft.md`, `marketing_crm/privacy/consent_screens_copy.md`.
---

## ⏱️ STATUS — SCOPE v2 adopted 2026-06-18 (Cowork), pending final legal confirmation

**Scope decision (Tomo, 2026-06-18) — supersedes the earlier "18+ lean" note.** Juniors ARE in scope (they're
core to tennis), handled through **parent-managed accounts**; the protective move is **not storing biometric
data**, not excluding kids. The model:

1. **Adult account holder (18+) who can add junior player profiles.** The adult **attests** they are the
   parent/guardian of each junior (we cannot technically verify guardianship — attestation is the mechanism;
   a payment on the account strengthens it where present). The account is the adult's, password-protected.
2. **Match analysis:** the trimmed match video **is stored** (kept while the account is active). Footage may
   contain other players (opponents). The uploader **attests at upload** that everyone in the video — *and, if
   under 18, that player's parent/guardian* — has given permission to be recorded (T&C warranty). Our basis for
   incidental third parties is **legitimate interests** + disclosure + honouring objections.
3. **Technique analysis (the biometric one):** we **process** pose/skeletal movement to produce technique
   feedback, **then delete the source video** and **do not persist the raw pose/keypoint data** — we keep only
   the **derived, non-biometric technique stats.** A one-tick **explicit (parental) consent** is captured before
   technique runs (belt-and-braces — light).

Net effect: **minors' layer is ON** (parental consent/attestation + children's-data care); **stored-biometric
layer is OFF** (no retained pose, technique video deleted). Evidence base:
[`privacy-legal-research.md`](privacy-legal-research.md). **Cowork is not a lawyer — not legal advice; the
lawyer confirms on return.** The framing still holds: **POPIA** is the home regime (controller is a SA entity);
GDPR/UK apply on top because we target/monitor EU/UK users.

**Adopted decisions (load these):**

| Decision | Value (scope v2) |
|---|---|
| 1. Biometric (technique) | **Processed, NOT stored.** Pose computed → technique stats derived → **source video deleted + raw pose NOT persisted.** Keep only derived non-biometric stats. **Explicit (parental) consent before technique.** **Hard rule: never add face/gait/identity matching.** |
| 2. Minors | **Allowed via parent-managed accounts.** Adult (18+) creates the account and adds junior profiles; **attests guardianship** + gives **parental consent** to process the child's data. No hard guardianship verification (industry-normal); payment strengthens where present. |
| 2b. Footage of others (opponents) | **Uploader attests at upload** that all players in the video — *and, if under 18, their parent/guardian* — consented to being recorded. Basis for incidental third parties = legitimate interests + disclosure + erasure on request. |
| 3. Retention (days) | original uploaded video: **0** (delete post-process) · **technique/pose video: 0 (delete when analysis done)** · raw pose keypoints: **not persisted** · trimmed match clip: **90** after closure · derived match + technique stats: **90** after closure · account+profile PII (incl. junior profiles): **90** after closure · anonymised financial: **2555 (7y)**. **ENFORCE via deletion jobs.** |
| 4. Erasure vs finance | erase PII/video; retain **anonymised** billing ~7y |
| 5. Marketing consent | **explicit opt-in** everywhere; **double opt-in for EU/UK** |
| 6. Data residency / transfers | global (us-east-1 + eu-north-1). Each US vendor: signed **DPA + 2021 SCCs** (DPF primary where validly certified, SCCs the fallback — DPF under CJEU appeal); **UK Addendum** for UK flows. Also discharges **POPIA s72**. |

**Interim `policy_version` string → `1.0-interim-2026-06-18`.** CC: load into `core.consent.policy_version`.

### Consent types (`core.consent`) — all ACTIVE under scope v2

- `terms_of_service`, `privacy_policy` — signup, required.
- `marketing_email` — separate opt-in; EU/UK double opt-in. Gate for any Klaviyo send.
- `minor_processing_parental` — when an adult adds a junior: attests guardianship + consents to process the
  child's data (`granted_by_user` = adult, `subject_person` = minor).
- `biometric_processing` — before first technique analysis (parent gives it for a junior). Wording makes clear
  pose is **not stored** and the **video is deleted** after.
- **Per-upload `footage_permission` attestation** — recorded on each match submission (not a standing consent):
  uploader confirms recording permission for everyone in the video (and their guardian if under 18).

### What still applies (baseline)

Privacy notice (Art. 13/14 superset → covers POPIA s18) · prior-opt-in cookie banner · DSAR/erasure on a
one-month clock · enforced retention jobs · sub-processor DPAs + SCCs (Klaviyo, AWS, PayPal, Clerk, Anthropic;
HubSpot removed/dormant; PayPal likely a controller) · **DPIA** (now applies again — children's data; Cowork
drafts from the ICO Annex D template) · **EU + UK Art. 27 representatives** (triggered; appoint when convenient,
~€500–5k/yr) · **SA home duties** (Information Officer registration + PAIA manual + annual report by 30 June).

### 🧍 Information Officer — doesn't disappear

Under POPIA the **head of a private body is automatically the Information Officer** and must register with the
Regulator — for a solo venture that's **you** (or your company if you incorporate). It's **light admin** (a
registration + a PAIA manual Cowork can draft + a yearly report), **not a job**. **Whether being IO for your own
side project is OK alongside your bank job is a question for your bank's outside-business-activity policy + your
wife — Cowork can't rule on it.** Incorporating ring-fences personal liability and makes it the *company's* role
but your name still lands on records. **Flag this to your wife.**

### ✅ CC build checklist (scope v2, 2026-06-18)

1. **Adult account + junior profiles.** Account holder affirms 18+; can **add junior player profiles**. On
   adding a junior, capture `minor_processing_parental` (attest guardianship + consent).
2. **Per-upload footage attestation.** On every match upload, require the `footage_permission` checkbox
   ("everyone in this video — and any under-18's parent/guardian — has agreed to be recorded"); store it on the
   submission record.
3. **Match video:** store the **trimmed** clip (delete the original post-process, as today). Storable — fine.
4. **Technique pipeline = process-then-delete:** run pose → derive technique stats → **delete the technique
   source video** and **do NOT persist raw pose/keypoint rows** (`ml_analysis` pose) → keep only derived stats.
   Capture `biometric_processing` consent (parent for a junior) before it runs. **If the pipeline cannot run
   without retaining pose, tell Cowork — wording changes.**
5. **Consent capture:** all 5 mechanisms above, loading `policy_version = 1.0-interim-2026-06-18`.
6. **Retention enforcement** — load Decision 3 into `core.retention_rule` + run deletion jobs (original video,
   technique video, 90-day clips/stats/PII).
7. **DSAR/erasure runbook** — one-month clock via `core.data_subject_request`.
8. **Cookie banner** — prior opt-in, granular, reject-all parity.
9. **Sub-processor DPAs + SCCs** on file; publish sub-processor list; PayPal as controller; HubSpot removed;
   keep "no biometric/no minors' data into Klaviyo" enforced in the feed.
10. **Klaviyo flows stay in DRAFT** until consent capture is live + lawyer confirms.

**⚠️ Needs Tomo:** (a) ✅ **DONE** — controller = TEN-FIFTY5 AI (PTY) LTD, 1814 Kunene Drive, Midrand, 2066,
Gauteng, SA; Information Officer = Tomo Stojakovic; (b) **register the IO**
with the SA Information Regulator + publish the PAIA manual (drafted — `paia-manual.md`); (c) appoint Art. 27
EU+UK reps when convenient; (d) final lawyer review on return.

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
| Pose / skeletal keypoints | Biometric — **processed transiently, NOT stored** (scope v2) | not retained (technique video deleted after analysis) | the sensitive one — kept out of storage by design |
| Match analytics | Derived | `silver.*` / `gold.*` / `core.match.kpi_summary` | |
| Usage events | Behavioural | `core.usage_event` | |
| Payment | Financial | **PayPal** (not our DB) | we store no card data |

## Processors / sub-processors (data leaves us to)
SportAI API (analysis) · AWS (S3, Batch GPU, SES email — `us-east-1` + `eu-north-1`) · Anthropic
(AI coach / support bot) · **Clerk** (authentication — LIVE 2026-06-17) · **PayPal** (payment — direct,
LIVE 2026-06-16; **likely an independent controller** for payment data, not our processor — disclose as a
recipient) · **Klaviyo** (lifecycle email — opt-in only, **never minors'/biometric data**) · **Amplitude**
(product analytics). *(Wix retired 2026-06-16/17 — no longer a processor; rollback path only.* **HubSpot
removed — dormant; we are our own CRM.)* **Each needs a signed Art. 28 DPA + 2021 SCCs (fallback behind
DPF); UK Addendum for UK flows — this set also discharges POPIA s72.** See `privacy-legal-research.md` §5.

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

> **⚠️ Updated by SCOPE v2 (STATUS block at top, 2026-06-18).** Decision 1 now reads "biometric **processed
> but NOT stored** (technique video deleted, pose not persisted), with explicit parental consent" and Decision
> 2 is "**juniors allowed via parent-managed accounts + attestation**" (not "18+ only"). The text below is the
> original lawyer-facing rationale; the STATUS block is authoritative where they differ.

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
**Research-backed decision (upgraded 2026-06-18, was "under 16"):** **Accounts are 18+ only; a junior
is never self-registered — only added by a verified adult, and every under-18 requires competent-person
(parent/guardian) consent.** Applied globally. Why 18 not 16: **POPIA s34** (the home regime) is a *blanket
prohibition* on processing any under-18's data lifted only by competent-person consent — stricter than
GDPR's 16 ceiling and COPPA's 13 line, so under-18 is the single defensible cross-regime floor. Verification:
account created + affirmed 18+ by the adult; the adult confirms guardianship at consent time; **verify the
parent via the existing payment transaction** (a COPPA-approved method that also satisfies GDPR "reasonable
efforts"), with email/text-plus fallback for free accounts. See `privacy-legal-research.md` §2.

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

> **SCOPE v2:** all rows below are **ACTIVE**, plus a per-upload `footage_permission` attestation.

| consent type (`core.consent`) | When captured | UI requirement | Subject vs grantor |
|---|---|---|---|
| `terms_of_service` | Signup | Required checkbox | self |
| `privacy_policy` | Signup | Required checkbox (link to policy) | self |
| `marketing_email` | Signup + profile settings | **Separate, unchecked** opt-in; EU/UK → double opt-in | self; sets `marketing_opt_in` |
| `biometric_processing` | Before **first technique** analysis | **Separate, explicit** opt-in; wording states pose is **not stored** + technique **video deleted** after | self, or **parent for a junior** |
| `minor_processing_parental` | When an adult adds a junior | Adult **attests guardianship** + consents; gates all processing of that junior | granted_by_user = adult; subject_person = minor |
| `footage_permission` *(per-upload attestation, not a standing consent)* | Every match upload | Required checkbox: all players in the video — and any under-18's parent/guardian — agreed to be recorded | recorded on the submission |

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

## Next steps (updated 2026-06-18 — interim-finalised)
1. ✅ **Decisions adopted (interim)** — see STATUS block at top. Values are set so the build can proceed.
2. **Claude Code (now):** build consent capture (Part B), load the interim `policy_version` (`1.0-interim-2026-06-18`) into `core.consent.policy_version`, and the retention day-counts (Decision 3) into `core.retention_rule`; **wire the under-18 minor gate (accounts 18+, juniors added only by a verified adult)**. Leave the Klaviyo flows + sends in **draft**. Full build list: STATUS block §"CC build checklist".
3. **Lawyer (on return, ~2 weeks):** confirm/adjust the 6 decisions. This is the gate for **going live** on marketing sends + minors'/biometric processing at scale.
4. **Cowork (me):** fold any lawyer changes into the final policy + bump `policy_version` (triggers re-consent).
5. **Tomo:** supply the registered legal entity name + address for §1 before public publish.

---

# Privacy policy (DRAFT)

# Ten-Fifty5 — Privacy Policy (DRAFT for legal review)

> **INTERIM v1.0 — finalised with Cowork's recommended defaults; pending final legal review.** Not legal advice. The interim decisions (see STATUS block at top) are filled in below. A lawyer confirms before this is published publicly and before the high-risk pieces go live.
>
> **Effective date:** [on publish] · **Version:** 1.0-interim-2026-06-18 · **Last updated:** 2026-06-18

---

## 1. Who we are
Ten-Fifty5 ("Ten-Fifty5", "we", "us") provides AI-powered tennis match analysis. You upload match
video and we return performance statistics, biomechanical (pose-based) technique analysis, and an AI
coaching assistant grounded in your own data.

- **Data controller:** **TEN-FIFTY5 AI (PTY) LTD**, 1814 Kunene Drive, Midrand, 2066, Gauteng, South Africa
- **Contact:** info@ten-fifty5.com
- **Information Officer / data protection contact:** Tomo Stojakovic — info@ten-fifty5.com

This policy explains what we collect, why, the legal bases we rely on, who we share it with, how
long we keep it, and the rights you have.

## 2. Who this applies to
Adult players (18+), parents/guardians who manage a junior player's profile, and coaches who use Ten-Fifty5.
Accounts are created and controlled by an adult; junior players are added as profiles managed by that adult.
Because we analyse video of people playing tennis, a match you upload may contain personal data about **other
people** (e.g. your opponent). See Section 9 on footage of third parties.

## 3. The data we collect

**Account & profile (personal data)**
Name, email, phone (optional), country, role (player / parent / coach), and login credentials
(authentication is handled by our provider, Clerk).

**Junior / minor profile data**
Where an adult adds a junior player, we hold the profile details the adult provides (e.g. name, skill level,
and optionally date of birth or club). Junior profiles are created and managed by a consenting adult who
confirms they are the player's parent/guardian — see Section 8.

**Match video**
The video you upload. The original upload is **deleted after processing**; a trimmed review clip is
retained as part of your match history.

**Pose data (processed, not retained)**
To produce **technique** feedback our technology reads body-position (pose) information from your video while
processing it. **We do not store this pose data, and we delete the technique video once the analysis is done**
— we keep only the resulting non-biometric technique statistics. We do not use pose to identify anyone, and we
never run facial recognition or any identity matching. We ask for your explicit permission before running
technique analysis (for a junior, the managing adult gives it) — see Section 6 and Section 8.

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
- **Contract** — to deliver the analysis you sign up for (account, match video processing, analytics, AI coach).
- **Consent** — for **technique/pose processing** (explicit opt-in, given by the managing adult for a junior),
  for **marketing email** (Section 7), and for processing a **junior's** data via parental consent (Section 8).
  You can withdraw consent at any time (Section 10).
- **Legitimate interests** — to secure, maintain and improve the service, and to process the incidental data of
  other people captured in uploaded footage (Section 9), where not overridden by their rights.
- **Legal obligation** — to retain certain financial records (Section 11).

## 7. Marketing communications
We send marketing email (tips, product updates, offers) **only to people who have explicitly opted
in** (double opt-in for EU/UK). Every marketing email has
an unsubscribe link, and you can opt out at any time without affecting the service. Service/
transactional messages (e.g. "your analysis is ready") are not marketing and are sent as part of
delivering the product. Our marketing email is delivered via Klaviyo (Section 12).

## 8. Children's data and parental consent
Accounts must be created and managed **by an adult (18+).** A junior player can be added only as a profile
managed by that adult, who confirms they are the player's **parent or legal guardian**.

- We process a junior's data — profile details, match video, and (if you use technique analysis) the
  transient pose processing in Section 3 — only with the managing adult's **parental/guardian consent**, given
  at the time the junior is added and again before technique analysis runs.
- The managing adult can review, export or delete the junior's data at any time.
- We do not let minors create their own accounts and we do not direct marketing at minors.
- We rely on the adult's confirmation of guardianship; we cannot independently verify family relationships.
- If you believe a junior's data has been provided without proper consent, contact info@ten-fifty5.com and we
  will act promptly.

## 9. Footage of other people
A match video may show other individuals (e.g. your opponent or doubles partners). By uploading, you
confirm you have a proper basis to share that footage with us for analysis. We process incidental
third-party data only to deliver your analysis, apply the same retention and security to it, and
will respond to any rights request from an identifiable individual in the footage.

## 10. Your rights
Subject to your jurisdiction, you may have the right to: access your data; correct it; erase it
("right to be forgotten"); restrict or object to processing; data portability; and to **withdraw
consent** at any time (e.g. for marketing, or for technique/pose processing — withdrawing the latter
means we stop running technique analysis going forward).

To exercise any right, contact **info@ten-fifty5.com**. We handle requests as data-subject requests
and respond within the timeframe required by law (generally one month under GDPR/UK GDPR). You also
have the right to complain to your data protection authority.

## 11. How long we keep your data (retention)
We keep personal data only as long as needed for the purposes above. Retention periods (interim,
pending final legal confirmation):

| Data | Retention |
|---|---|
| Original uploaded match video | Deleted immediately after processing |
| Technique video | Deleted as soon as the technique analysis is done |
| Pose / keypoint data | Not retained — processed transiently, never stored |
| Trimmed match clip | Kept while account active; deleted 90 days after account closure |
| Derived match & technique stats | Kept while account active; deleted 90 days after closure |
| Account & profile PII (incl. junior profiles) | Deleted 90 days after account closure |
| Anonymised financial records | Retained ~7 years for tax/accounting, with PII removed |

On account closure or withdrawal of technique consent we delete or anonymise data per these rules, except
where we must retain limited (anonymised) financial records to meet legal obligations.

## 12. Who we share data with (sub-processors)
We do not sell your data. We share it with vetted providers strictly to run the service:

| Provider | Purpose | Notes |
|---|---|---|
| SportAI | Match video analysis | Processes uploaded footage |
| Amazon Web Services (AWS) | Storage (S3), GPU processing, transactional email (SES) | Regions: US (us-east-1) and EU (eu-north-1) — see Section 13 |
| Anthropic | AI coach & support assistant | Processes your stats/queries to generate responses |
| Clerk | Authentication | Login / account / session |
| PayPal | Payments | We store no card data; PayPal acts as an independent controller for payment data |
| Klaviyo | Marketing email | **Opt-in contacts only — never minors' data or biometrics** |
| Amplitude | Product analytics | Behavioural usage data |

We contractually require these providers to protect your data and process it only on our
instructions.

## 13. International transfers
We operate in both the **United States (us-east-1)** and the **European Union (eu-north-1)**. Your
data — including video and biometric data — may be processed in either region. Where data is
transferred internationally, we rely on appropriate safeguards (Standard Contractual Clauses) as
required by law. (EU-only data residency is under review as a possible future option.)

## 14. Security
We use appropriate technical and organisational measures to protect your data, including access
controls and encryption in transit. No system is perfectly secure, but we minimise what we hold —
we do not retain pose data and we never route account data into tools beyond those listed in Section 12.

## 15. Cookies & analytics
We use strictly-necessary cookies to run the site. Non-essential cookies — including analytics
(e.g. Amplitude) and any marketing cookies — are set **only with your prior opt-in consent** via our
cookie banner, where rejecting is as easy as accepting. You can change your choice at any time.

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

Design rules baked in: consent boxes are **unchecked by default**, marketing is **separate** from terms,
language is specific about pose data, and nothing is buried. **Scope v2:** §1b is an **18+ affirmation for the
account holder** (juniors are added as managed profiles, not their own accounts); §3 (technique/pose), §4
(parental) and §4b (footage permission) are all **active**.

---

## 1. Signup screen — Terms + Privacy (`terms_of_service`, `privacy_policy`)

Placement: bottom of the signup form, above the "Create account" button.

> ☐ I agree to the [Terms of Service] and [Privacy Policy].

- **Required** (can't create account without it). Single checkbox can cover both, each linked.
- Microcopy under it (optional, small): _We'll only ever use your data to run your analysis and improve Ten-Fifty5. You're in control — change your mind anytime in Settings._

---

## 1b. Signup screen — 18+ affirmation (age gate, lean scope)

Placement: on the signup form. Neutral, not pre-checked.

> ☐ I confirm I am 18 or older. (Accounts must be managed by an adult; junior players are added as profiles.)

- **Required.** Blocks account creation if unchecked. Not a `core.consent` type — it's an eligibility gate for
  the account holder. Junior players are added later as managed profiles (§4), not via their own signup.
- Keep the wording neutral (don't hint "tick to proceed"); store the affirmation + timestamp.

---

## 2. Signup screen — Marketing opt-in (`marketing_email`)

Placement: directly below the terms box. **Separate, unchecked, optional.**

> ☐ Send me tips, product updates and the occasional offer by email. (Optional — and you can unsubscribe anytime.)

- Sets `marketing_opt_in = true` only if ticked. This is the gate for the Klaviyo trial→paid flow.
- **EU/UK double opt-in** (Decision 5): if ticked, send a confirmation email (copy in §6) and only set `marketing_opt_in = true` after they click confirm.

---

## 3. Before first technique analysis — Biometric/pose consent (`biometric_processing`) — ✅ ACTIVE (scope v2)

Placement: a one-time modal shown the first time a user runs a **technique / pose** analysis
(not on ordinary match upload — show it at the moment it actually applies).

> **One quick thing before we analyse your technique**
>
> To break down your strokes, our technology reads the position of your body's joints across each
> frame of your video — your **"pose" data**. We ask for your explicit permission before we do this.
>
> We use it **only** to produce your technique feedback. **We don't store the pose data, and we delete
> the technique video once the analysis is done** — we keep only the resulting stats. We never use it to
> identify anyone and never for marketing. You can withdraw permission anytime; we'll stop running
> technique analysis.
>
> ☐ I explicitly consent to Ten-Fifty5 processing pose data to analyse this player's technique.
>
> [Learn more in our Privacy Policy]   **[Agree & analyse]**   [Not now]

- **Required to run technique analysis.** "Not now" cancels just that analysis; the rest of the product still works.
- For a **junior's** technique analysis, this consent is given by the managing adult (see §4) — show the parental framing ("…to analyse your player's technique").

---

## 4. Adding a junior — Parental/guardian consent (`minor_processing_parental`) — ✅ ACTIVE (scope v2)

Placement: when an adult adds a junior player to their account.

> **You're setting up an account for a young player**
>
> Because this player is under 18, we need you, as their parent or guardian, to give
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

## 4b. Match upload — recording-permission attestation (`footage_permission`) — ✅ ACTIVE (scope v2)

Placement: on every match-upload step. Required checkbox, stored on the submission record (per-upload, not a
standing consent).

> ☐ Everyone playing in this video has agreed to be recorded and analysed. For any player under 18, I have
> their parent or guardian's permission.

- **Required to upload.** Records `footage_permission = true` + timestamp on the match submission.
- Microcopy (small): _This keeps things fair to other players in your footage. We only use their incidental
  appearance to produce your analysis, and we'll remove it on request._

---

## 5. Settings — Consent management / withdrawal

Placement: a "Privacy & consent" section in account settings. Show current state + a toggle each.

> **Your privacy choices**
>
> - **Marketing emails:** [On / Off] — _Turn off anytime; you'll still get essential service emails (like "your analysis is ready")._
> - **Technique (pose) analysis:** [On / Off] — _Turning this off stops future technique analysis. We don't store pose data, so there's nothing to delete._
> - **Download my data** · **Delete my account**
>
> Questions or a specific request? Email info@ten-fifty5.com.

- Toggling marketing off = unsubscribe (sets `marketing_opt_in = false`).
- Toggling technique off withdraws `biometric_processing` consent → stop running technique analysis going forward (no stored pose to delete).
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
> TEN-FIFTY5 AI (PTY) LTD, 1814 Kunene Drive, Midrand, 2066, Gauteng, South Africa.

---

## Mapping summary (for Claude Code)

| Screen | consent type | required? | sets |
|---|---|---|---|
| §1 Signup terms | `terms_of_service` + `privacy_policy` | yes | consent rows |
| §1b Signup 18+ affirmation | (age gate for the account holder, not a consent type) | yes | account holder must be adult |
| §2 Signup marketing | `marketing_email` | no | `marketing_opt_in` (after confirm if EU) |
| §3 First technique | `biometric_processing` | yes (for technique) | biometric/pose consent row |
| §4 Add junior | `minor_processing_parental` | yes (to add a junior) | parental consent; gates that junior's processing |
| §4b Match upload | `footage_permission` (per-upload attestation) | yes (to upload) | stored on submission |
| §5 Settings | withdrawal of marketing / technique | — | updates/triggers DSAR |

Each row stores `policy_version` + timestamp. Final `policy_version` string comes from the lawyer
sign-off (per the decisions doc).
