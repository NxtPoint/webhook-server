# Cowork briefing — paste this into Cowork before it suggests anything

You (Cowork) handle no-code growth work for Ten-Fifty5 (Klaviyo, social, SEO, GEO, outreach,
privacy-policy drafting, HubSpot config, analysis/reports). A separate agent (Claude Code) owns
the codebase, data, and running product. **This is what already exists — build on it, don't
reinvent it, and don't assume a data source other than the one below.**

## What the product is
AI tennis match analysis SaaS. Users upload match video → get ATP-grade stats + biomechanical
(pose) analysis + an AI coach. B2C + a coach channel. Free first match (no card) is the hook.

## What is ALREADY built (don't propose building these)
- **A canonical source-of-truth database** (`core.*` schema, live). It already holds: accounts,
  users (logins), persons (player/parent/coach, minors flagged), subscriptions, an append-only
  **credit ledger** (PAYG balance), matches, **usage events**, **NPS + survey + tickets**, and a
  full **consent + retention model** (incl. biometric + minor-parental consent).
- **Shared contracts** in `marketing_crm/contracts/`: `events.md` (event names), `lifecycle_stages.md`
  (trial/activated/paid/at-risk/churned definitions), `hubspot_field_map.md`, `data_dictionary.md`.
  Privacy inputs moved to `docs/business/privacy-and-consent.md`. **These are the source of truth for naming + definitions.**
- System maps: `docs/business/architecture.md`; Wix migration record + DB-schema proposal: `docs/business/_archive/`.

## How to pull our data — the CRM API (`/api/crm/*`)  ← THIS IS YOUR INTERFACE
A read-only, key-authenticated pull API built for you (Cowork) to fetch the customer 360 + product
events and build Klaviyo segments/flows. **Klaviyo is the only destination today** (HubSpot deferred —
we decided we don't need a separate CRM tool; we expose what we've built and add more if you need it).
- **Auth:** header `X-CRM-Key: <CRM_API_KEY>` (or `Authorization: Bearer`). Ask Claude Code/Tomo to
  provision your key. Every data endpoint is 401 until the key is set.
- `GET /api/crm/health` — `{configured, klaviyo_configured}` (no key needed; detect availability).
- `GET /api/crm/customers` — paginated marketing profiles (owner-level only). Filters: `stage`, `plan`,
  `role`, `opted_in=true`, `since`, `until`, `limit`, `offset`. Each row carries `marketing_opt_in`,
  `stage`, `plan_code`, `mrr_cents`, `matches_remaining`, `last_activity`, `nps_latest`,
  `signup_source/medium/campaign`.
- `GET /api/crm/events` — paginated product-event stream. Filters: `event_type`, `email`, `since`,
  `until`. Event names are in `events.md` (all now emit live).
- `GET /api/crm/cohort?stage=…&opted_in=true` — emails (+ key traits) matching a segment, for Klaviyo
  list import (requires ≥1 filter).
- **Consent rule:** every customer carries `marketing_opt_in`; **only message opted-in contacts** —
  gate your Klaviyo flows on it (use `?opted_in=true`). The flag is set when a user grants
  `marketing_email` consent (privacy signed off 2026-06-18).
- **Push side (already live on key):** when `KLAVIYO_API_KEY` is set, we also PUSH profiles + the
  product events into Klaviyo automatically (so flows trigger in real time). The pull API is for
  backfill / reconciliation / building segments. You don't have to choose — both run.

## Non-negotiable integration rules
1. **`core.*` is the single source of truth.** Klaviyo / HubSpot / Amplitude are *downstream
   mirrors*, never the master. Don't design anything that treats Klaviyo as the customer DB.
2. **Use the contract names.** Events = `events.md`. Lifecycle stages = `lifecycle_stages.md`.
   If you need an event/stage that isn't there, flag it — it has to be *added to the contract and
   emitted by code* before any flow can trigger on it. It won't exist just because a flow expects it.
3. **The data feed now EXISTS (built 2026-06-18).** All `events.md` events emit live; profiles +
   events push into Klaviyo automatically once `KLAVIYO_API_KEY` is set; and you can pull anything
   via the `/api/crm/*` API above. You design the flows, audiences, segments, and copy. If you need
   an event/trait that isn't emitted yet, flag it (rule #2) — but the pipe itself is done.
4. **Privacy boundary (hard):** never route **minor PII** (DOB, child names) or **biometric data**
   (pose, video) into Klaviyo / HubSpot / any marketing tool. Marketing email only to contacts with
   explicit opt-in. See `docs/business/privacy-and-consent.md`.
5. **Auth is now Clerk** (LIVE 2026-06-17 — migrated off Wix; `/login`, `auth_v2/`). Payment is now
   **direct PayPal** (LIVE 2026-06-16; off Wix). We control the signup/login + checkout flows.

## Before you suggest something, check:
- Does it assume a data source other than `core.*`? → realign to `core.*`.
- Does it rely on an event or trait we don't emit yet? → it's a code dependency; flag it.
- Does it touch minors or biometric data? → stop, that's gated.
- Does it duplicate something already built (a field, a table, a stage)? → check `data_dictionary.md`.

## Your immediate parallel tasks (independent of the build)
- **Privacy/consent policy:** SIGNED OFF 2026-06-18. The consent capture path is live (granting
  `marketing_email` sets `marketing_opt_in`). Remaining: confirm the final `policy_version` string +
  retention day-counts so Claude Code stamps them on consent records.
- **Klaviyo flows (copy + design + GO LIVE):** use `events.md` + `lifecycle_stages.md`. The data feed
  is built (rule #3) and privacy is signed off — once `KLAVIYO_API_KEY` is set, your flows can go
  live. Build segments from `/api/crm/cohort` (gate on `opted_in=true`).
