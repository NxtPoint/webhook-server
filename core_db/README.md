# core_db / core_api — canonical "single source of truth" layer

New `core.*` Postgres schema that will become authoritative for customers, subscriptions,
matches, usage, relationships, and feedback. Design rationale + ER diagram:
[`../DB-SCHEMA-PROPOSAL.md`](../DB-SCHEMA-PROPOSAL.md). Approved decisions: new `core.*` schema ·
append-only credit ledger · account+user+person identity split · bigint PK + `public_id` uuid.

> **Status: ADDITIVE + DARK.** Nothing here is wired into prod boot, and no live data has
> been migrated. The schema is safe to create on any DB (it never touches `billing.*`/`bronze.*`).
> Live-data migration is a separate, gated step — see `DB-SCHEMA-PROPOSAL.md` §7.

## Layout

| Path | Purpose |
|---|---|
| `core_db/models.py` | ORM models — **the authoritative schema definition** |
| `core_db/schema.py` | `core_init(engine)` — idempotent migration (create_all + indexes + views) |
| `core_db/db.py` | `session_scope()` (txn boundary), `as_dict()` (serialize), `norm_email()` |
| `core_db/repositories/` | clean data-access functions (callers never write raw SQL) |
| `core_db/seed.py` | synthetic seed data (prod-guarded) |
| `core_api/blueprint.py` | `/api/core/*` HTTP surface (env-gated, dark) |

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

## Wiring the API (when ready — currently dark)

`core_api.register(app)` is a no-op unless `CORE_API_ENABLED=1`. To switch it on, add to the
main app's blueprint registration (try/except-wrapped, like the other optional blueprints):

```python
try:
    from core_api import register as register_core_api
    register_core_api(app)          # only registers if CORE_API_ENABLED=1
except Exception as e:
    log.warning("core_api not registered: %s", e)
```

Endpoints (auth: `X-Core-Key`/`Bearer` == `CORE_API_KEY` or `CLIENT_API_KEY`):
`GET /api/core/health` · `GET /api/core/account?email=` · `GET /api/core/account/<email>/matches`
· `POST /api/core/usage` · `POST /api/core/feedback/nps` · `GET /api/core/admin/metrics`.

## Compliance

`core.consent` is versioned + per-type; for a minor, `subject_person_id` is the junior and
`granted_by_user_id` is the consenting parent's login. Types include `biometric_processing`
and `minor_processing_parental`. `core.data_subject_request` tracks erasure/access; retention
windows live in `core.retention_rule`. **Six legal/privacy decisions are still open** —
see `DB-SCHEMA-PROPOSAL.md` §5.
