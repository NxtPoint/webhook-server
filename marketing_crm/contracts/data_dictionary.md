# Contract: Data dictionary (for Cowork analysis + reports)

The stable read surface Cowork can rely on for trend analysis, the Monday funnel snapshot, and
scheduled reports. Read from **views**, not raw tables, so definitions stay consistent.

## Views (read these)
| view | grain | key columns |
|---|---|---|
| `core.vw_account_credits` | 1 / account | matches_remaining, matches_granted_total, matches_consumed_total |
| `core.vw_subscription_current` | 1 / account | plan_code, plan_type, status, mrr_cents, current_period_end |
| `core.vw_mrr` | 1 row | active_subscriptions, mrr_cents_total, mrr_total |

> More analytic views land with the cockpit (Prompt 5): `vw_business_health`, `vw_customer_list`,
> `vw_at_risk`, `vw_processing_ops`. They will encode `lifecycle_stages.md` exactly.

## Tables (reference)
| table | grain | notes |
|---|---|---|
| `core.account` | customer | `public_id` is the external key; `email` unique (lower) |
| `core.app_user` | login | role via linked person; `marketing_opt_in` |
| `core.person` | profile | role player/parent/coach; `is_minor` derived; **PII** |
| `core.relationship` | link | coach_player / parent_junior, status |
| `core.subscription` | sub (current+history) | `mrr_cents`, status |
| `core.credit_ledger` | append-only | balance = SUM(matches_delta) |
| `core.match` | upload | status, `kpi_summary` (jsonb), `task_id` |
| `core.usage_event` | event | `event_type` per `events.md`, `occurred_at` |
| `core.nps_response` | NPS | score, bucket (promoter/passive/detractor) |
| `core.ticket` | support | status, channel |

## Definitions (so numbers match across reports)
- **MRR** = active recurring subs only (PAYG excluded). **PAYG revenue** reported separately.
- **Activation / conversion / churn / at-risk** = exactly `lifecycle_stages.md`.
- Money in cents; divide by 100 for display. Times are UTC (`timestamptz`).

## Access
- Read-only SQL via the existing `POST /ops/diag/sql` (header auth) or a cockpit endpoint.
- **Do not** select minor PII or biometric columns into shared reports/exports.
