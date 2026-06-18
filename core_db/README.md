# core_db / core_api — canonical "single source of truth" layer

New `core.*` Postgres schema — the engagement/identity graph (account/user/person, `usage_event`,
nps/survey, consent, ticket). Design rationale + ER diagram:
[`../docs/business/_archive/db-schema-proposal.md`](../docs/business/_archive/db-schema-proposal.md).
Approved decisions: new `core.*` schema · append-only credit ledger · account+user+person identity
split · bigint PK + `public_id` uuid.

> **Status (2026-06-17): LIVE on prod and FED FORWARD.** `core_api` registers unconditionally and the
> consent / `auth_v2` / tracking write-paths fill `core.*` going forward (account/user/person/consent +
> `usage_event`); `tomo.stojakovic@gmail.com` was backfilled (1 acct, 3 persons, 121 matches). **`core.*`
> is NOT the billing system of record** — that stays `billing.*` (Option C, 2026-06-17): the planned
> payment→core mirror is decided-against and the cockpit reads `billing.*` directly. The full
> `billing.account → core.account` live-data backfill remains OPTIONAL, deferred to a real Option-B
> driver (auth-SoR cutover / referrals). The schema never touches `billing.*`/`bronze.*`. Current state +
> rationale: [`../docs/business/growth-and-crm.md`](../docs/business/growth-and-crm.md) and
> `docs/_investigation/core_db_billing_strategy.md`.

## Layout

| Path | Purpose |
|---|---|
| `core_db/models.py` | ORM models — **the authoritative schema definition** |
| `core_db/schema.py` | `core_init(engine)` — idempotent migration (create_all + indexes + views) |
| `core_db/db.py` | `session_scope()` (txn boundary), `as_dict()` (serialize), `norm_email()` |
| `core_db/repositories/` | clean data-access functions (callers never write raw SQL) |
| `core_db/seed.py` | synthetic seed data (prod-guarded) |
| `core_api/blueprint.py` | `/api/core/*` HTTP surface (registers unconditionally as of 2026-06-17) |

## Run

```bash
# Create / update the schema (idempotent, additive — uses DATABASE_URL via db_init.engine)
.venv/Scripts/python -m core_db.schema

# Seed synthetic data (refuses without --force; refuses a remote DB without --allow-remote)
.venv/Scripts/python -m core_db.seed --force            # local DB
.venv/Scripts/python -m core_db.seed --force --reset    # purge + reseed
.venv/Scripts/python -m core_db.seed --force --purge    # remove seed data only
```

All seed rows use the `@seed.ten-fifty5.test` email domain — trivially identifiable + purgeable.

## Using the DAL

```python
from core_db.db import session_scope
from core_db.repositories import accounts, subscriptions

with session_scope() as s:                       # one transaction, commits on success
    acct = accounts.create_account(s, email="a@b.com", display_name="A B")
    accounts.create_user(s, account_id=acct.id, email="a@b.com", is_account_owner=True)
    subscriptions.grant_credits(s, account_id=acct.id, matches=3, source="signup_bonus")
    bal = subscriptions.balance(s, acct.id)       # {"matches_remaining": 3, ...}
```

Aggregation lives in views (`core.vw_account_credits`, `core.vw_subscription_current`,
`core.vw_mrr`) — keep new aggregation in SQL, not Python (repo rule #2).

## Wiring the API

`core_api.register(app)` is wired into the main app's boot and **registers unconditionally** (the
`CORE_API_ENABLED` flag is inert as of 2026-06-17), try/except-wrapped like the other optional
blueprints. Auth is dual-mode (Clerk JWT or the legacy key).

Endpoints (auth: `X-Core-Key`/`Bearer` Clerk JWT, or `CORE_API_KEY`/`CLIENT_API_KEY` fallback):
`GET /api/core/health` · `GET /api/core/account?email=` · `GET /api/core/account/<email>/matches`
· `POST /api/core/usage` · `POST /api/core/feedback/nps` · `GET /api/core/admin/metrics`.

## Compliance

`core.consent` is versioned + per-type; for a minor, `subject_person_id` is the junior and
`granted_by_user_id` is the consenting parent's login. Types include `biometric_processing`
and `minor_processing_parental`. `core.data_subject_request` tracks erasure/access; retention
windows live in `core.retention_rule`. **Six legal/privacy decisions are still open** —
see [`../docs/business/privacy-and-consent.md`](../docs/business/privacy-and-consent.md).
