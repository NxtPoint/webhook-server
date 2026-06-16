# marketing_crm — STATUS (the hymn sheet)

> **Single living source of truth for the growth/CRM/admin stack. Both Claude Code and Cowork read
> AND update this file.** If you change what's built, switches, or ownership — edit here in the same
> change. Point-in-time details elsewhere drift; this wins. Last structural update: 2026-06-16.

## Lanes & file ownership (avoid collisions)
Both agents can now see + edit this repo. Stay in your lane:

| Path | Owner | Notes |
|---|---|---|
| `core_db/` (the `core.*` schema) | **Claude Code** | source of truth; don't hand-edit |
| `marketing_crm/tracking/` `crm_sync/` `backoffice/` `feedback/` | **Claude Code** | code |
| `marketing_crm/contracts/*` | **Shared, CC authoritative** | technical contracts must match code; propose changes, don't silently diverge |
| `marketing_crm/klaviyo/` | **Cowork** | flow copy + build specs |
| `marketing_crm/privacy/` | **Cowork** | policy drafts, consent copy, legal-decision specs |
| `frontend/*.html`, `*_app.py`, `upload_app.py` | **Claude Code** | running product code — Cowork: flag, don't edit |
| this `STATUS.md` | **Shared** | keep it current |

## What's built (Claude Code) — all DARK by default, flip the env to enable

| Capability | Where | Enable | Status |
|---|---|---|---|
| Canonical DB (`core.*`) | `core_db/` | live on prod (empty) | ✅ schema created |
| Cockpit (admin) | `marketing_crm/backoffice/` + `frontend/cockpit.html` (`/cockpit`) | `COCKPIT_ENABLED=1` | ✅ |
| Event tracking | `marketing_crm/tracking/` | `TRACKING_ENABLED=1` (+ `AMPLITUDE_API_KEY`) | ✅ partial events |
| Feedback + NPS | `marketing_crm/feedback/` + `frontend/feedback_widget.js` | `FEEDBACK_ENABLED=1` | ✅ |
| CRM sync (HubSpot+Klaviyo) | `marketing_crm/crm_sync/` | `CRM_SYNC_ENABLED=1` (+ `HUBSPOT_PRIVATE_APP_TOKEN` / `KLAVIYO_API_KEY`) | ✅ code (untested vs live APIs) |
| core_api | `core_api/` | `CORE_API_ENABLED=1` | ✅ |

**Env switches (set on the `webhook-server` Render service):** `COCKPIT_ENABLED`, `TRACKING_ENABLED`,
`FEEDBACK_ENABLED`, `CRM_SYNC_ENABLED`, `CORE_API_ENABLED` + keys `AMPLITUDE_API_KEY`,
`HUBSPOT_PRIVATE_APP_TOKEN`, `KLAVIYO_API_KEY`. Tunables: `NPS_TRIGGER_N` (3), `NPS_COOLDOWN_DAYS` (90).

## Events emitted (vs contract `contracts/events.md`)
- ✅ `match_uploaded`, `subscription_started`, `subscription_cancelled`, `credit_purchased`,
  `account_created`, `report_viewed`, `nps_submitted`, `feedback_submitted`,
  `cancellation_reason_submitted`
- ⬜ not yet: `match_processed`/`match_failed` (ingest paths, incl. the separate ingest-worker),
  `ai_coach_query`, `technique_uploaded`, `coach_invited`/`coach_accepted`, `login` (Wix-side)

## Critical path to make Cowork's Klaviyo flows LIVE
1. ✅ Emit funnel events (`account_created`, `match_uploaded`, `report_viewed`, `subscription_started`, `credit_purchased`).
2. ✅ DB→Klaviyo/HubSpot feed (`crm_sync`) — profiles + event forwarding.
3. ⬜ Set `CRM_SYNC_ENABLED=1` + `KLAVIYO_API_KEY` + `TRACKING_ENABLED=1` (and HubSpot token).
4. ⬜ **Consent capture** built (privacy spec Part B) → `marketing_opt_in` actually set → marketing emails allowed.
5. ⬜ Cowork: Klaviyo sender domain auth + postal address + assemble flows in Flow Builder.

## Open legal decisions (block minors'-biometrics at scale)
6 decisions in `contracts/privacy_inputs.md` §5 + Cowork's recommended defaults in
`privacy/privacy_decisions_and_consent_spec.md`. After lawyer sign-off, the final **policy version
string**, **retention day-counts**, and **age threshold** come back into `core.consent.policy_version`
+ `core.retention_rule` + minor-gating. Nothing formal in prod yet.

## Not built yet
- Consent-capture UI + loading `policy_version`/`retention_rule` (privacy spec Part B).
- §7 live-data backfill (`billing.*`/`bronze.*` → `core.*`) — or a write-path so `core.*` fills going forward.
- Referral system. De-Wix auth (Prompt 7, plan-only).

## Changelog
- 2026-06-16: core_db live · contracts hub · cockpit (P5) · tracking (P3) · feedback (P6) · crm_sync (P4) · this STATUS.md. Cowork added `klaviyo/` + `privacy/`.
