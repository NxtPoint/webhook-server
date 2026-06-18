# Data Protection Impact Assessment (DPIA)

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)). Structured on the ICO's
> DPIA template (Annex D of the Children's Code) and GDPR Art. 35 / POPIA s.q. **Owner: Cowork (draft).**
> **DRAFT — informational, not legal advice; for the lawyer (Tomo's wife) to review and the Information
> Officer to sign off.** Reflects **scope v2** (juniors allowed via parent-managed accounts; biometric/pose
> **processed but never stored**). Evidence base: [`privacy-legal-research.md`](privacy-legal-research.md);
> processing facts: [`privacy-and-consent.md`](privacy-and-consent.md). Last updated 2026-06-18.

**Controller:** TEN-FIFTY5 AI (PTY) LTD · 1814 Kunene Drive, Midrand, 2066, Gauteng, South Africa
**Information Officer:** Tomo Stojakovic — info@ten-fifty5.com
**DPIA owner / author:** Information Officer (with Cowork) · **Status:** Draft v0.1, pending legal review
**Review date:** on material change, on lawyer sign-off, or annually — whichever first.

---

## Why a DPIA is required

A DPIA is mandatory where processing is "likely to result in a high risk" (GDPR Art. 35) and is expected by
the ICO Children's Code for any service likely to be accessed by children. Ten-Fifty5 triggers this because it
involves: **(a) data of children** (junior players), **(b) systematic analysis of uploaded video** of
identifiable people, **(c) processing of pose/biomechanical data** (sensitive even though not stored), and
**(d) cross-border transfers**. A DPIA is therefore appropriate even though the design deliberately removes the
highest-risk elements (no stored biometric; no identification use).

---

## Step 1 — Describe the processing: nature

**What the product does.** Ten-Fifty5 is an AI tennis-analysis SaaS. An adult account holder uploads match
video; the platform returns performance statistics, an optional pose-based **technique** analysis, and an AI
coaching assistant grounded in the user's own data. Junior players are supported as **profiles managed by an
adult** (parent/guardian).

**Data collected** (full inventory in `privacy-and-consent.md` §3):

| Data | Category | Stored? | Notes |
|---|---|---|---|
| Account/profile: name, email, phone, country | Personal | Yes | Account holder (adult, 18+) |
| Junior profile: name, skill level, optional DOB/club | **Children's personal data** | Yes | Added + managed by the adult |
| Match video (original upload) | Personal (image) | **No** — deleted post-process | |
| Trimmed match clip | Personal (image) | Yes (while account active) | Match history/review |
| Technique video | Personal (image) | **No** — deleted when analysis done | |
| Pose / skeletal keypoints | Biometric (behavioural) | **No** — processed transiently, never persisted | Never used to identify |
| Derived match & technique stats | Derived (non-biometric) | Yes | The product's value |
| Usage events | Behavioural | Yes | Operate/improve service |
| Payment | Financial | No (held by PayPal) | No card data stored |

**Method.** Video is uploaded to AWS S3; ML pipelines (AWS Batch GPU + Render) compute detections and, for
technique, pose. Derived statistics are written to the analytics store; **source video and raw pose are
deleted/never persisted** as above. Marketing email runs through Klaviyo for opted-in adults only.

**Sub-processors:** AWS (storage/compute/email), Anthropic (AI coach/support), Clerk (auth), PayPal (payments —
likely an independent controller), Klaviyo (opt-in marketing only — never minors'/biometric data), SportAI
(match analysis), Amplitude (product analytics).

## Step 1 — scope

**Volume / extent:** early-stage, low volume; expected to grow. Geographic reach: South Africa (home),
EU/EEA, UK, US. **Data subjects:** adult players, **junior players (under 18)**, coaches, and **incidental
third parties** (opponents/others visible in uploaded footage). **Duration / retention:** per the schedule in
`privacy-and-consent.md` §11 (originals deleted post-process; technique video deleted on completion; clips and
derived stats 90 days after account closure; anonymised financial 7 years).

## Step 1 — context

Tennis skews young, so **junior players and their parents are a core audience** — children's data is central,
not incidental. The founder operates solo (evenings/weekends). Processing pose data is sensitive, mitigated by
**never storing it** and **never using it for identification**. There is an inherent **third-party** dimension
(opponents appear in match footage). Public concern and regulatory attention around children's data and
biometrics is high, which is precisely why the design minimises both.

## Step 1 — purposes

Deliver the analysis the customer signed up for (contract); provide technique feedback (consent); operate,
secure and improve the service (legitimate interests); send marketing only to opted-in adults (consent); meet
tax/accounting obligations (legal obligation). **No advertising profiling, no selling of data, no use of
children's data for marketing.**

---

## Step 2 — Consultation

- **Internal:** Information Officer (Tomo); Cowork (privacy drafting); Claude Code (implementation).
- **External:** **Legal review pending** — Tomo's qualified lawyer (POPIA-fluent) to review on return; this
  DPIA + `privacy-legal-research.md` are the brief.
- **Data subjects / representatives:** not formally surveyed at this stage (early product); the privacy notice
  + consent screens are written in plain language and tested for clarity. Re-consult on material change.
- **Processors:** rely on each sub-processor's published DPA + security documentation (to be executed/on file).

---

## Step 3 — Necessity and proportionality

- **Lawful bases** (per `privacy-and-consent.md` §6): contract (core analysis); consent (technique/pose,
  marketing, and a junior's data via parental consent); legitimate interests (security/improvement + incidental
  third-party footage); legal obligation (financial records). Documented and mapped per purpose.
- **Special-category / biometric:** the "uniquely identifying purpose" test is **not met** (pose is for
  performance analytics, never identification), so in EU/UK terms it is not Art. 9 data; under POPIA's broader
  definition it is treated cautiously — and the risk is removed at source by **not storing it** and capturing
  **explicit (parental) consent** before technique runs.
- **Children:** processed only via a managing adult's parental consent + guardianship attestation; no
  self-registration by minors; no marketing to minors; no profiling of minors.
- **Data minimisation:** originals and technique video deleted; raw pose never persisted; only derived,
  non-biometric stats retained; Klaviyo receives no minors'/biometric data.
- **Accuracy / retention:** defined retention schedule enforced by automated deletion jobs.
- **Transfers:** EU/UK→US via vendor DPF certification where valid **with 2021 SCCs as fallback**; UK Addendum
  for UK flows; this set also satisfies POPIA s72.
- **Data-subject rights:** access/correction/erasure/portability/objection + consent withdrawal via the
  in-product settings and `core.data_subject_request` (one-month response clock).
- **Processor compliance:** Art. 28 DPAs with each sub-processor; published sub-processor list.

---

## Step 4 — Identify and assess risks

Likelihood/severity are pre-mitigation; residual risk after Step 5 measures is shown in Step 6.

| # | Risk to individuals | Likelihood | Severity | Pre-mitigation |
|---|---|---|---|---|
| R1 | **A child's data** processed without genuine parental consent (attestation can't verify guardianship) | Possible | High | High |
| R2 | **Opponent / third party** filmed without their (or their guardian's) permission | Possible | Medium–High | Medium–High |
| R3 | **Biometric/pose** misuse or breach | Unlikely (not stored) | High | Low–Medium |
| R4 | **Cross-border transfer** to the US without adequate safeguards | Possible | Medium | Medium |
| R5 | **Retention creep** — video/data kept longer than necessary | Possible | Medium | Medium |
| R6 | **Re-identification** if pose were ever used for identity | Unlikely (prohibited by design) | High | Low |
| R7 | **Security breach** of stored clips/stats/PII | Possible | Medium–High | Medium |
| R8 | **Marketing to a minor** or without consent | Unlikely | Medium | Low–Medium |

---

## Step 5 — Measures to reduce risk

| Risk | Measure(s) | Effect | Residual |
|---|---|---|---|
| R1 | Adult-managed accounts; explicit parental consent + guardianship attestation; payment transaction strengthens verification where present; no minor self-registration; easy parental review/delete | Reduce | **Low–Medium** (residual: attestation can't fully verify guardianship — industry-normal; flagged to lawyer) |
| R2 | Per-upload `footage_permission` attestation (incl. under-18s' guardians); legitimate-interests basis + §9 disclosure; honour objection/erasure; no opponent identity profiling | Reduce | **Low–Medium** (child-opponent residual managed, not eliminated) |
| R3 | **Pose never stored; technique video deleted on completion; no identification use**; explicit consent before technique | Reduce/avoid | **Low** |
| R4 | Vendor DPF (where valid) + 2021 SCCs fallback in every DPA; UK Addendum; satisfies POPIA s72 | Reduce | **Low–Medium** (DPF under CJEU appeal — SCC fallback hedges) |
| R5 | Documented retention schedule **enforced by automated deletion jobs**; originals/technique video purged | Reduce | **Low** |
| R6 | Hard governance rule: no face/gait/identity matching; pose pipeline architecturally separate from identity | Avoid | **Low** |
| R7 | Access controls, encryption in transit, minimisation (less stored = smaller blast radius); reputable processors | Reduce | **Low–Medium** |
| R8 | Marketing only on explicit opt-in (EU/UK double opt-in); minors'/biometric data never sent to Klaviyo; suppression on withdrawal | Reduce | **Low** |

---

## Step 6 — Sign off and record outcomes

| Item | Position |
|---|---|
| **Measures approved by** | Information Officer (Tomo) — *pending* |
| **Residual risks accepted by** | Information Officer — *pending* |
| **Lawyer advice** | **Pending** — qualified review on return; this DPIA + `privacy-legal-research.md` are the brief. Open items for the lawyer: (i) guardianship-attestation sufficiency; (ii) child-opponent footage basis; (iii) confirm pose-not-stored keeps it out of Art. 9 / POPIA special information; (iv) Art. 27 representatives. |
| **DPIA to be reviewed** | On material change (e.g. if pose ever stored, or face/identity added → re-do), on lawyer sign-off, else annually. |
| **Residual risk overall** | **Low–Medium**, driven by the irreducible attestation/third-party-child elements common to all video-analysis products. Acceptable for launch in **draft/limited** mode; full go-live (real marketing sends; scaled children's processing) gated on lawyer sign-off. |

> **Note on the build:** this DPIA assumes the technique pipeline can **derive stats then purge raw pose**
> (no `ml_analysis` pose retention) and delete the technique video. If Claude Code confirms the pipeline must
> retain pose to function, **R3/R6 residual rises and this DPIA must be revised** (the biometric-storage layer
> re-engages).

---

*Informational only — not legal advice. The Information Officer signs off; the qualified lawyer reviews.*
