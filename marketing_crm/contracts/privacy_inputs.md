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
