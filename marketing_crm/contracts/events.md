# Contract: Event taxonomy

The canonical list of product events. **One name per event, everywhere** — DB (`core.usage_event`),
Amplitude, Klaviyo triggers, HubSpot timeline. If a name isn't here, it isn't an event yet; add it
here first. Naming: `snake_case`, `object_verb` (past tense).

> Status: draft. Names already wired in `core.usage_event` (today): `login`, `match_upload`,
> `technique_upload`, `report_view`, `ai_coach_query`, `dashboard_view`.

## Core events

| event | when | key properties | fires Klaviyo? |
|---|---|---|---|
| `account_created` | signup completes (free, no card) | source, medium, campaign, role | trial welcome |
| `login` | user authenticates | — | — |
| `match_uploaded` | upload accepted (`POST /api/submit_s3_task`) | task_id, sport_type, pipeline, subject_person_id | activation |
| `match_processed` | analysis complete (ingest done) | task_id, pipeline, duration_s | "your match is ready" (today via SES) |
| `match_failed` | pipeline failed | task_id, stage, error | ops alert |
| `report_viewed` | dashboard/match-analysis opened | task_id | activation signal |
| `ai_coach_query` | LLM coach question asked | task_id | engagement |
| `technique_uploaded` | technique analysis submitted | task_id | — |
| `credit_purchased` | PAYG top-up purchased | order_id, plan_code, matches | trial→paid |
| `subscription_started` | first active recurring sub | plan_code, mrr_cents | trial→paid done |
| `subscription_cancelled` | sub cancelled/expired | plan_code, reason | win-back |
| `coach_invited` | owner invites a coach | invited_email | coach onboarding |
| `coach_accepted` | coach accepts invite | coach_person_id | coach onboarding |
| `nps_submitted` | NPS survey answered | score, bucket | detractor follow-up |
| `feedback_submitted` | in-app feedback widget | sentiment, area | — |
| `cancellation_reason_submitted` | post-cancel survey | reason, comment | win-back tailoring |

## Property conventions
- `task_id` is the universal match key (bridges `core.match` ↔ `bronze.submission_context`).
- Identify by `account.public_id` + `email` (never internal bigint id) in external tools.
- Money in **cents** (USD) — matches the DB.
- **Never** put minor PII (DOB, child name) or biometric data in event properties — see `privacy_inputs.md`.

## Where each event is emitted (planned)
- DB: `marketing_crm/tracking/` calls `core_db.repositories.matches.record_usage(...)`.
- Amplitude: same tracking module, dual-emit.
- Klaviyo/HubSpot: downstream of the DB event (via CRM sync), not emitted from the client directly.
