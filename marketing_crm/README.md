# marketing_crm — growth / CRM / admin cockpit

One home for the growth stack, the way T5 lives in `ml_pipeline/` + `ml_analysis.*`.
**Code lives here; data lives in `core.*` (`core_db/`).** This package is a *consumer* of
the source-of-truth DB — never the reverse.

## Why this exists
The product, billing, and marketing all read the same `core.*` data. The growth machinery
(tracking, CRM sync, admin cockpit, feedback widgets, referrals) is the layer on top. Packing
it in one namespace keeps it discoverable and keeps the layering honest.

## Lane split (Claude Code ⇄ Cowork)
- **Claude Code (this repo):** data model + API, event tracking, CRM sync code, admin cockpit,
  feedback/NPS widgets, auth/portal de-Wix, billing plumbing, referral system.
- **Cowork (no code):** Klaviyo flows (copy+setup), social content, SEO/blog, GEO/AI-visibility,
  coach/academy outreach, privacy-policy drafting, trend analysis, scheduled reports, HubSpot
  pipeline/segment *config* (not the sync code).

**The handshake = `marketing_crm/contracts/`.** Both lanes treat these as the contract:

| Contract | Used by |
|---|---|
| `contracts/events.md` | DB tracking, Amplitude, Klaviyo triggers, HubSpot |
| `contracts/lifecycle_stages.md` | cockpit, Klaviyo audiences, HubSpot lifecycle |
| `contracts/hubspot_field_map.md` | CRM sync code + HubSpot config |
| `contracts/data_dictionary.md` | Cowork analysis + scheduled reports |
| `contracts/privacy_inputs.md` | privacy-policy draft → lawyer → policy versions/retention back into `core.*` |

Build the pipes once; both sides reference the spec.

## Status
- `contracts/` — **drafted** (this commit). Living docs; update when the model changes.
- `tracking/` `crm_sync/` `backoffice/` `feedback/` `referral/` — not built yet (next prompts).

## Foundations it reads
`core_db/` (canonical `core.*` schema) — see `../DB-SCHEMA-PROPOSAL.md`, `../core_db/README.md`.
System map: `../ARCHITECTURE.md`, `../DATA-INVENTORY.md`, `../WIX-DEPENDENCY.md`.
