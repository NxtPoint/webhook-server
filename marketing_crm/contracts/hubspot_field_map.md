# Contract: DB → HubSpot field map

What the CRM sync code (`marketing_crm/crm_sync/`, Prompt 4) pushes, and what stays out.
Cowork configures the matching HubSpot properties/pipelines; Claude Code writes the sync.
**One-way: core.* → HubSpot** (HubSpot is a CRM mirror, never a source of truth).

## Contact (1 per `core.app_user`, keyed by email)
| HubSpot property | core source |
|---|---|
| email | `app_user.email` |
| firstname / lastname | `person.full_name` / `person.surname` (primary person) |
| ttf_account_public_id | `account.public_id` |
| ttf_role | `person.role` (player/parent/coach) |
| lifecyclestage (custom mapping) | `lifecycle_stages.md` |
| ttf_plan | `vw_subscription_current.plan_code` |
| ttf_mrr | `vw_subscription_current.mrr_cents` / 100 |
| ttf_matches_remaining | `vw_account_credits.matches_remaining` |
| ttf_last_activity | max(`usage_event.occurred_at`) |
| ttf_matches_uploaded | count(`match`) |
| ttf_nps | latest `nps_response.score` |
| ttf_signup_source / medium / campaign | `acquisition.*` |
| marketing opt-in | `app_user.marketing_opt_in` (gates marketing emails) |

## Company / Deal (optional, later)
- Company ≈ `account` (for coach/academy multi-seat). Deal pipeline ≈ subscription lifecycle.

## ⛔ DO NOT SYNC (privacy boundary — see docs/business/privacy-and-consent.md)
- **Minor PII:** `person.dob`, child names/notes, anything where `person.is_minor=true`.
- **Biometric data:** pose keypoints, video, any `ml_analysis.*` content.
- **Consent evidence / DSAR contents.**
- Marketing emails only to contacts with `marketing_opt_in=true` AND a valid `marketing_email` consent.

## Sync model
- Idempotent upsert by email; batch nightly + event-driven on `subscription_*`/`nps_submitted`.
- A contact's HubSpot id cached on the user row (add `hubspot_contact_id` later) for updates.
