# Contract: Lifecycle stages

One definition of each stage, computed from `core.*`. The cockpit (Prompt 5), Klaviyo audiences,
and HubSpot lifecycle all use **these exact rules** — don't redefine "activated" or "at-risk"
anywhere else.

| stage | entry condition (from core.*) | exit |
|---|---|---|
| `visitor` | no `account` yet (pre-signup; tracked anonymously upstream) | → signup |
| `signup` | `account` exists, no match uploaded yet | → activated / trial |
| `activated` | ≥1 `match` with `status='complete'` **and** ≥1 `report_viewed` | sticky flag |
| `trial` | used the free first match, **no active subscription, no PAYG credits left** | → paid / payg / churned |
| `payg` | no active recurring sub, but credit balance > 0 (bought top-ups) | → paid / lapsed |
| `paid` | `vw_subscription_current.status='active'` AND `plan_type='recurring'` | → at_risk / churned |
| `at_risk` | `paid` AND no usage event in **30+ days** | → reactivated / churned |
| `churned` | subscription `cancelled`/`expired` AND credit balance ≤ 0 | → reactivated |
| `reactivated` | was churned, then new sub or purchase | → paid/payg |

## Conversion / activation metrics (cockpit)
- **Activation rate** = activated accounts ÷ signups (rolling 30d).
- **Free→paid conversion** = accounts that reached `paid` ÷ accounts that ever hit `trial`.
- **Churn (month)** = subs moving active→cancelled/expired in the month ÷ active at month start.
- **MRR** = `core.vw_mrr.mrr_cents_total` (active recurring only). **PAYG revenue** = sum of
  `credit_purchased` order amounts in period (not MRR).

## Notes
- "Activity" for at-risk = any `core.usage_event` for the account (login counts). Tunable.
- A `coach` account's lifecycle is tracked separately from the player accounts it's linked to.
- Stages are **derived** (computed in a view/query), not stored — single definition, no drift.
