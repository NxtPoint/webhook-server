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
