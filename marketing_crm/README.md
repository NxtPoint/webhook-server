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
| `contracts/cowork_briefing.md` | Cowork lane briefing |

> Privacy inputs / consent spec / policy draft moved to [`../docs/business/privacy-and-consent.md`](../docs/business/privacy-and-consent.md); Klaviyo flows + outreach moved to [`../docs/business/marketing-and-seo.md`](../docs/business/marketing-and-seo.md). The `klaviyo/`, `privacy/`, `outreach/` subdirs are now empty (Cowork drafts land in the merged business docs).

Build the pipes once; both sides reference the spec.

## Status (2026-06-17)
All sub-packages are **BUILT and LIVE** (registered unconditionally as of the 2026-06-17 de-gate — the `*_ENABLED` flags are inert):
- `contracts/` — living docs; update when the model changes.
- `tracking/` `crm_sync/` `backoffice/` `feedback/` `consent/` — **built**. `crm_sync` self-gates on HubSpot/Klaviyo key presence; `referral/` is the one piece still not built.

Living status + the full capability table: [`../docs/business/growth-and-crm.md`](../docs/business/growth-and-crm.md).

## Foundations it reads
`core_db/` (canonical `core.*` schema) — see [`./README` for core_db](../core_db/README.md) and
[`../docs/business/_archive/db-schema-proposal.md`](../docs/business/_archive/db-schema-proposal.md).
System map: [`../docs/business/architecture.md`](../docs/business/architecture.md). Wix migration
history: [`../docs/business/_archive/wix-migration-record.md`](../docs/business/_archive/wix-migration-record.md).
