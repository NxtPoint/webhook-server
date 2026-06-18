# Privacy & Data-Protection Legal Research — Sign-off Pack

> **Part of the Ten-Fifty5 business documentation set** ([master index](README.md)). Companion to
> [`privacy-and-consent.md`](privacy-and-consent.md) (the canonical, interim-finalised policy/consent doc).
> **Owner: Cowork.** Produced 2026-06-18 by a multi-agent regulatory research pass (GDPR / UK GDPR /
> US / POPIA), fully cited. **This is informational regulatory research, NOT legal advice.** It exists to
> let a POPIA-fluent lawyer (who does not work in GDPR) review the six high-risk components efficiently:
> every GDPR/UK/US conclusion is mapped back to its POPIA equivalent, with the divergence and the
> stricter regime called out. The lawyer makes the final call on every item.

> **🟢 SCOPE UPDATE 2026-06-18 — the product is scoped "v2":** **juniors allowed via parent-managed accounts**
> (parental consent + attestation) but **biometric/pose data is processed, never stored** (technique video
> deleted after analysis; raw pose not persisted). So **§2 (minors) fully applies** — the parental-consent /
> children's-data layer is built — while **§1 (biometric) is de-risked**: with no stored pose and no
> identification use, the special-category *storage* problem falls away (the transient processing is still
> covered by explicit parental consent as a safety belt). §3–§6 (notices, retention, transfers, territorial
> scope / Art. 27 reps, SA Information-Officer duties) apply in full. Operative decisions live in
> [`privacy-and-consent.md`](privacy-and-consent.md) STATUS block.

## How to read this

- **§1–§6** = the six high-risk components. Each has: **(a)** the GDPR/UK/US rule, **(b)** the POPIA
  mapping, **(c)** which regime is stricter, **(d)** the practical recommendation.
- **§7** = the one-page "what to actually do" summary.
- **§8** = flagged uncertainties (things that are genuinely unsettled or move fast — read before relying).
- Sources are linked inline and consolidated at the end of each section.

**The single most important finding up front:** the binding constraint across almost every component is
**POPIA, because ten-fifty5's controller is a South African entity** — POPIA always applies as the home
regime, and on biometrics + children it is the *strictest* of the four. GDPR/UK then apply *on top*
(extraterritorially, because we target and monitor EU/UK users). So the right mental model is **"build to
POPIA's floor, add the GDPR/UK-specific bolt-ons"** — not "GDPR vs POPIA, pick one."

---

## 1. Biometric / pose data — is it special-category?

**The pivotal question:** does computer-vision pose/skeletal-keypoint data (used for shot/technique stats,
**not** to identify *who* a player is) count as special-category / sensitive biometric data?

**(a) GDPR / UK / US rule.** Under GDPR Art. 4(14) + Art. 9, biometric data is special-category **only when
processed "for the purpose of uniquely identifying a natural person."** The test is **purpose, not
capability**. The [EDPB Guidelines 3/2019, para 80](https://www.edpb.europa.eu/sites/default/files/files/file1/edpb_guidelines_201903_video_devices_en_0.pdf)
are explicit: processing that distinguishes/analyses but does **not** uniquely identify "does not fall
under Article 9." The [ICO](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/lawful-basis/biometric-data-guidance-biometric-recognition/key-data-protection-concepts/)
agrees on the special-category trigger (purpose-based), though it frames the *threshold definition* of
"biometric data" on capability — a nuance, not a reversal. Raw match **video is ordinary personal data**,
not biometric, unless run through facial-recognition ([EDPB 3/2019](https://www.edpb.europa.eu/sites/default/files/files/file1/edpb_guidelines_201903_video_devices_en_0.pdf)).
US state laws use **closed enumerated lists** — Illinois BIPA, Texas CUBI and Washington HB 1493 cover
retina/iris/fingerprint/voiceprint/face-geometry/hand-geometry, and **do not list body/pose/gait**
([Morrison Foerster](https://www.mofo.com/resources/insights/240503-getting-bipa-right-biometric-identifiers-must-identify);
[Biometric Update — gait not covered](https://www.biometricupdate.com/202407/scope-and-contours-of-bipa-biometric-identifiers-and-information)).
BIPA is the only one with a **private right of action** ($1,000–$5,000 per violation) — the single largest
US financial tail — but it is triggered by **face/voice/hand geometry, not pose data**.

**(b) POPIA mapping.** POPIA is the **strictest and purpose-agnostic** outlier. Its s1 definition —
"'biometrics' means a *technique of personal identification* based on physical, physiological or
behavioural characterisation **including**…" — is **non-exhaustive** and has **no "purpose of unique
identification" carve-out**. "Biometric information" is **special personal information per se** under
[s26](https://popia.co.za/section-26-prohibition-on-processing-of-special-personal-information/), processable
only on an [s27](https://popia.co.za/section-1-definitions/) ground (cleanest = **consent**). The SA
Information Regulator reads biometrics expansively ([Guidance Note](https://inforegulator.org.za/wp-content/uploads/2020/07/Guidance-Note-Processing-Special-PersonalInformation-20210628-004.pdf)).

**(c) Stricter:** **POPIA** (no purpose carve-out, inclusive definition, binds the controller directly). US
is the lowest *substantive* bar but carries the only meaningful *litigation* risk (BIPA) — and only if a
face/voice/hand-geometry feature is ever added.

**(d) Recommendation.** **Classify pose data as ordinary personal data on the legal merits (GDPR/UK/US),
but obtain explicit, upload-time consent anyway** — driven by POPIA and as Art. 9(2)(a) belt-and-braces.
Lawful basis for the EU/UK analysis: Art. 6 contract (the account-holder uploads their own match for their
own stats) + legitimate interests for any incidentally-captured opponent. **Hard governance line: never
build face recognition, cross-match player re-identification, or gait-identity templates** — crossing that
line simultaneously trips GDPR Art. 9, POPIA s26, and BIPA's damages engine. Keep pose extraction
architecturally separate from any identity matching, and document that separation.

---

## 2. Minors' consent + age gate

**(a) GDPR / UK / US rule.** GDPR Art. 8 sets a **digital-consent age of 13–16, member-state dependent**
(Germany/Netherlands/Ireland **16**; France **15**; Italy/Spain **14**; 13 in Belgium, Denmark, Sweden,
etc.) ([gdpr-info Art. 8](https://gdpr-info.eu/art-8-gdpr/); [PRIVO country map](https://www.privo.com/blog/gdpr-age-of-digital-consent)).
Below the threshold, **parental consent + "reasonable efforts to verify"** are required; the
[EDPB Statement 1/2025 on age assurance](https://www.edpb.europa.eu/system/files/2025-04/edpb_statement_20250211ageassurance_v1-2_en.pdf)
demands a **risk-based, data-minimising** approach and singles out biometric data for "the utmost
attention." The UK consent age is **13**, but the [ICO Children's Code](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/childrens-information/childrens-code-guidance-and-resources/age-appropriate-design-a-code-of-practice-for-online-services/code-standards/)
imposes **under-18 design duties** (high-privacy defaults, profiling off, mandatory DPIA) on any service
"likely to be accessed by children." US **COPPA** is a hard **under-13** rule with prescriptive
**verifiable-parental-consent** methods; the **2025 Final Rule** (effective 23 Jun 2025, compliance by
22 Apr 2026) added face-match/text-plus methods, a **separate opt-in for third-party disclosure**, and a
**written retention policy banning indefinite retention** ([16 CFR Part 312](https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-312)).
Penalties run to **~$53k per violation**.

**(b) POPIA mapping.** [POPIA s34](https://popia.co.za/section-34-prohibition-on-processing-personal-information-of-children/)
is a **flat prohibition** on processing **any under-18's** data, lifted only by an
[s35](https://popia.co.za/section-35-general-authorisation-concerning-personal-information-of-children/)
ground — in practice **prior consent of a "competent person"** (parent/guardian). **No graduated
digital-consent age** — every under-18 needs competent-person consent.

**(c) Stricter:** **POPIA** (under-18 blanket rule beats GDPR's 16 ceiling and COPPA's 13 line). Child +
biometric data = the protections **compound**: parental explicit consent **and** DPIA **and**
high-privacy-by-default **and** short retention.

**(d) Recommendation.** **Gate the *account* at 18+; never let a junior self-register.** Juniors are added
only by a verified adult (parent/guardian) who gives **explicit, granular, separately-itemised consent**:
(i) create junior record, (ii) process the junior's video/pose data, (iii) — separately — share with a
coach. **Verify the parent** via the **payment transaction you already run** (a COPPA-approved method that
also meets GDPR "reasonable efforts"); fall back to email/text-plus for free accounts. **Complete a DPIA**
before launch. Default to **high privacy, profiling off, no third-party sharing**. Keep junior pose data
as **transient, non-identifying biomechanics**; **delete source video promptly**. This single flow is
defensible across all four regimes.

---

## 3. Public privacy policy + consent notices

**(a) GDPR / UK rule.** [Arts. 13/14](https://gdpr-info.eu/art-13-gdpr/) mandate a specific disclosure
set: controller + **representative** identity, DPO if any, **purpose + lawful basis per purpose**,
legitimate interests, **named recipients** (ICO prefers named over vague categories), international
transfers + safeguard, **retention period/criteria** ("as long as necessary" is criticised), all
data-subject rights, right to withdraw consent, right to complain to a supervisory authority,
automated-decision-making/profiling. Non-essential **cookies need prior opt-in** under the ePrivacy
Directive ([gdpr.eu/cookies](https://gdpr.eu/cookies/)). DSAR/erasure: respond **within one month**
(extendable +2 for complex) ([ICO erasure](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/individual-rights/right-to-erasure/)).

**(b) POPIA mapping.** [s18](https://popia.co.za/section-18-notification-to-data-subject-when-collecting-personal-information/)
notification is the analogue (a **subset** of Art. 13/14, plus a voluntary/mandatory-supply line). Rights:
[s23 access / s24 correction-deletion](https://popia.co.za/category/data-subject/). **POPIA-only admin
machinery with no GDPR equivalent:** every private body must **register an Information Officer** with the
Regulator, **maintain a PAIA manual**, and **file a PAIA annual report (window 1 Apr – 30 Jun)**
([Info Regulator PAIA](https://inforegulator.org.za/paia/); [Bowmans — 30 Jun 2026 deadline](https://bowmanslaw.com/insights/south-africa-paia-reporting-season-is-here-dont-miss-the-deadline-of-30-june-2026/)).
DSAR clock is **30+30 days and a fee may apply** (softer than GDPR's free one-month).

**(c) Stricter:** **GDPR/UK** on the *notice* and *cookies* (longer mandatory list; no clean POPIA cookie
opt-in analogue). **POPIA** stricter on *admin machinery* (Information Officer registration + PAIA manual +
annual report are mandatory and independent).

**(d) Recommendation.** Publish **one layered notice built to the GDPR Art. 13/14 superset** — it
automatically covers POPIA s18. Name the controller + **EU & UK representatives** (see §6) + Information
Officer; map each purpose to a lawful basis; **name the five sub-processors** and each transfer mechanism;
disclose the automated pose analysis. Deploy a **prior-opt-in cookie banner** (reject-all as easy as
accept-all). Stand up a **DSAR/erasure runbook on a one-month clock**. Separately discharge the SA duties:
**register the Information Officer, maintain the PAIA manual, file the annual report by 30 June.**

---

## 4. Data retention

**(a) GDPR / UK rule.** [Art. 5(1)(e)](https://gdpr-info.eu/art-5-gdpr/) storage limitation — "no longer
than necessary." **No fixed numeric limits**, but you must **define, document, justify and actually
enforce** periods per purpose. Biometric and children's data attract the **shortest defensible** periods +
DPIA.

**(b) POPIA mapping.** [s14](https://popia.co.za/section-14-retention-and-restriction-of-records/) is the
direct analogue — "not longer than necessary," also **no fixed numbers**, longer retention only if required
by law / contract / consent / research-with-safeguards.

**(c) Stricter:** **Functionally equivalent** on the core rule; **GDPR/UK stricter on the special-category
overlay** (Art. 9 + children expectations more developed).

**(d) Recommendation — defensible schedule:**

| Data | Retain | Why |
|---|---|---|
| Raw uploaded match video | **30–90 days** after analytics derived (default 30; user can extend per match) | Most sensitive; value is the derived stats, not the footage |
| Derived pose/analytics | **Account life + 12 months**, then delete/anonymise | Core product value; anonymise for any aggregate reuse |
| Account & billing | **Active + 5–7 yrs** for *financial* records only (SA tax) | Legal-obligation basis; keep the financial subset only |
| Marketing (Klaviyo) | Until **unsubscribe**, then suppress; purge inactive ~24–36 mo | Consent-based; honour withdrawal immediately |
| Consent records | **Processing + ~3–6 yrs** | Must be able to *prove* consent to defend a complaint |

Document in a Records-of-Processing register and **enforce with automated deletion jobs** (documented-but-not-enforced is the classic finding).

---

## 5. International transfers + sub-processors

**(a) GDPR / UK / US rule.** EU→US: the **EU-US Data Privacy Framework (DPF)** adequacy is **valid as of
June 2026** — the General Court dismissed the *Latombe* challenge (3 Sep 2025) ([Hunton](https://www.hunton.com/privacy-and-information-security-law/eus-general-court-confirms-adequacy-of-eu-u-s-data-privacy-framework))
— **but it is under CJEU appeal (C-703/25 P, pending)**, so treat it as **valid-but-contingent and keep
2021 SCCs as a contractual fallback** ([WilmerHale](https://www.wilmerhale.com/en/insights/blogs/wilmerhale-privacy-and-cybersecurity-law/20251201-european-court-of-justice-to-review-challenge-to-eu-us-data-privacy-framework)).
UK→US: the **UK-US Data Bridge / UK Extension** works for DPF-certified firms, but **biometric/health data
must be flagged "sensitive"** when sent ([ICO](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/international-transfers/adequacy-regulations/how-does-the-uk-extension-to-the-eu-us-data-privacy-framework-work/));
otherwise use the **UK IDTA/Addendum**. Every processor needs a **written Art. 28 DPA**.

**(b) POPIA mapping.** [s72](https://popia.co.za/section-72-transfers-of-personal-information-outside-republic/)
permits cross-border transfer on any of: **adequate protection via a binding agreement substantially
similar to POPIA** (= the DPA/SCC route), **consent**, **contractual necessity**, or **benefit of the data
subject**. More flexible than GDPR — no prescribed clauses, no SA adequacy list.

**(c) Stricter:** **GDPR/UK** (formal adequacy, mandated SCC text, transfer-impact assessments, live
litigation). A **single well-drafted DPA + 2021 SCCs satisfies all three regimes at once** (GDPR Art. 46
safeguard, UK Addendum base, and POPIA s72(1)(a) "binding agreement").

**(d) Recommendation.** Execute each vendor's DPA; **rely on DPF where the vendor is validly certified,
with SCCs as the fallback in every DPA**; attach the **UK Addendum** for UK flows; run a short
transfer-impact assessment per US vendor. Vendor posture (verify each on the
[official DPF list](https://www.dataprivacyframework.gov/Program-Overview) before relying):

| Vendor | Role | DPA | SCCs | DPF | Note |
|---|---|---|---|---|---|
| Klaviyo (email) | Processor | Yes | Yes | Yes (self-cert) | verify listing |
| AWS (hosting) | Processor | Yes | Yes | Yes | verify listing |
| PayPal (payments) | **Often controller, not processor** | Yes | In DPA | **Uncertain** | disclose as recipient; relies on own posture |
| Clerk (auth) | Processor | Yes | Yes | Not confirmed | rely on SCCs |
| Anthropic (AI) | Processor | Yes | Yes (EU SCCs) | Not confirmed; US-stored | rely on SCCs; ensure no-training-on-customer-data term |

For biometric/pose + minors' data, prefer **EU/EEA data residency where a vendor offers it**, and lean on
**explicit consent** (independently satisfies POPIA s72(1)(b) and the Art. 9 condition).

---

## 6. Controller / legal entity + applicable law (the under-flagged finding)

**(a) GDPR / UK / US rule.** [GDPR Art. 3(2)](https://gdpr-text.com/read/article-3/) reaches a non-EU
controller that **(a) offers goods/services to** people in the EU (the *targeting* test — language,
currency, EU marketing, accepting EU sign-ups; **mere website accessibility is not enough**) **or
(b) monitors their behaviour** ([EDPB Guidelines 3/2018](https://www.edpb.europa.eu/sites/default/files/files/file1/edpb_guidelines_3_2018_territorial_scope_after_public_consultation_en_1.pdf)).
ten-fifty5 **meets both**: it markets to and accepts EU users (targeting) **and** runs
performance/behavioural analytics on their uploaded match video (monitoring/profiling). Therefore GDPR
applies, and **[Art. 27](https://gdprlocal.com/gdpr-art-27-requirements-explained/) requires a designated
EU representative** unless the narrow exemption (occasional **and** not large-scale special-category
**and** low risk) applies — which **almost certainly does not** for an ongoing analytics SaaS touching
biometric-adjacent data. **UK GDPR mirrors this** ([ICO](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/personal-information-what-is-it/who-does-the-uk-gdpr-apply-to/)),
needing a **separate UK representative**. Real enforcement exists (Dutch DPA fined Locatefamily.com
**€525,000** purely for lacking an EU rep). US: **CCPA/CPRA thresholds (~$26.6m revenue / 100k consumers)
are almost certainly not met** at current size — re-test as you scale; COPPA and BIPA apply by
subject-matter regardless.

**(b) POPIA mapping.** [POPIA s3](https://www.michalsons.com/blog/must-i-comply-with-the-popi-act/41827)
applies because the responsible party is **domiciled in South Africa** — the **mirror image** of GDPR:
POPIA reaches you because you are *in* SA; GDPR reaches you because your *users* are in the EU/UK. They
**stack — you comply with both**, and POPIA's GDPR-modelled structure means one core programme covers most
of it, with GDPR-specific bolt-ons.

**(c) Stricter:** Different axes, not directly comparable — but the **GDPR/UK Art. 27 representative
requirement is the concrete new obligation** a POPIA-only lens misses entirely.

**(d) Recommendation.** **Appoint both an EU Art. 27 representative (in a member state where users are —
Ireland is the common English-language choice) and a UK representative.** Bundled commercial services run
roughly **€500–€5,000/year** combined — the cheapest, highest-leverage box on the GDPR checklist, and a
recognised mitigating factor in any later enforcement. Name them in the privacy notice. The realistic risk
of a standalone fine while tiny is low, but the downside is asymmetric (a single complaint, a breach, or
investor/enterprise due diligence turns the omission into an easy, well-precedented finding).

---

## 7. One page — what to actually do

**Build to POPIA's floor; add the GDPR/UK bolt-ons. Concretely:**

1. **Pose data:** classify as ordinary personal data on the merits, **but take explicit upload-time
   consent anyway** (POPIA-driven). **Never** add face/gait *identification* — that one line trips GDPR
   Art. 9 + POPIA s26 + BIPA together.
2. **Minors:** **accounts 18+ only; juniors added solely by a verified parent/guardian** giving explicit,
   granular, itemised consent (verify via the existing payment transaction). High-privacy default,
   profiling off, prompt video deletion.
3. **DPIA:** complete one before any minors'/biometric processing at scale (mandatory under the UK
   Children's Code and GDPR Art. 35; also good POPIA practice).
4. **Privacy notice:** publish **one GDPR Art. 13/14-superset layered notice** (covers POPIA s18); name
   controller + EU/UK reps + Information Officer + the five sub-processors + transfer mechanisms.
5. **Cookies:** prior-opt-in banner, reject-all as easy as accept-all.
6. **DSAR/erasure:** one-month runbook.
7. **Retention:** publish + **enforce** the §4 schedule (shortest defensible for raw video & minors).
8. **Transfers:** signed DPA + SCCs (fallback behind DPF) with all five vendors; UK Addendum for UK flows;
   short TIA per US vendor. This one set discharges POPIA s72 too.
9. **EU + UK Art. 27 representatives:** appoint both (~€500–5k/yr) — the under-flagged, cheap, high-leverage
   action.
10. **SA home duties (independent):** register the Information Officer, maintain the PAIA manual, file the
    PAIA annual report **by 30 June**.

**For the lawyer specifically — the four GDPR-deltas a POPIA practitioner won't have on the radar:**
(i) the **Art. 27 EU + UK representatives**; (ii) the **"uniquely identifying purpose" test** that keeps
pose data out of Art. 9 (vs POPIA's purpose-agnostic biometric definition — POPIA is stricter here);
(iii) the **ePrivacy prior-opt-in cookie regime** (no clean POPIA analogue); (iv) the **DPF-valid-but-
under-appeal** transfer position requiring SCC fallbacks. Everything else maps cleanly onto POPIA concepts
she already knows.

---

## 8. Flagged uncertainties (read before relying)

- **Is pose data "biometric"?** Genuinely contestable; turns on the "unique identification purpose" test.
  Treat as special-category by default (POPIA forces this anyway); have counsel confirm against the actual
  feature set. *Same Art. 27 outcome either way, but it changes consent/DPIA burden.*
- **DPF longevity:** valid now, CJEU appeal C-703/25 P pending — **always keep SCC fallbacks.**
- **EU "Digital Omnibus" cookie reform:** a **Commission proposal (Nov 2025), NOT law** — earliest ~2027.
  Build to the current ePrivacy opt-in rule. (Some secondary sources wrongly stated ePrivacy was
  "withdrawn Feb 2026 / 6-month consent" — disregard.)
- **PayPal / Clerk / Anthropic DPF listing:** confirm each on the official list; where unlisted, the SCCs
  in their DPAs carry the transfer. PayPal is likely a **controller**, not your sub-processor, for payment
  data.
- **Country consent ages:** Spain = **14** (statute), reform to 16 *proposed not enacted*; Ireland = **16**
  — verify both against current national statutes.
- **BIPA extraterritoriality** is unsettled, but moot unless a face/voice/hand-geometry feature is added.
- **COPPA per-violation penalty** (~$53k) is inflation-adjusted annually — confirm at enforcement time.
- **ICO guidance** is under review post Data (Use and Access) Act 2025 — confirm currency on exact wording.

---

*Compiled 2026-06-18 from a four-stream cited research pass. Informational only — not legal advice. The
lawyer reviews and makes the final determination on every component.*
