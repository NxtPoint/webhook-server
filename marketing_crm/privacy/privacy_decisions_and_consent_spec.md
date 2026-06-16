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
