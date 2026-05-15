# migrations

> One-off SQL backfill scripts. **There is no migration framework.** Schema is managed idempotently from code; this directory is for one-shot data fixes only.

## What this owns

- One-off SQL scripts that backfill data or drop legacy objects after a schema-shape change in code.
- Each script is **append-only** to the directory (we don't edit history) and **idempotent** (re-runnable without harm).

## What this is NOT

- A version-controlled migration framework (no Alembic, no Flyway, no `migrations` table).
- A schema-of-truth. Schema lives in code:
  - `db_init.py::bronze_init()` — bronze tables
  - `gold_init.py::gold_init_presentation()` — gold views
  - `tennis_coach/db.py::init_coach_cache()` — coach cache
  - `tennis_coach/coach_views.py::init_coach_views()` — gold coach views
  - `support_bot/db.py::init_support_schema()` — support_bot tables
  - `_ensure_member_profile_columns()` in `client_api.py` — billing columns
  - `_ensure_submission_context_schema()` in `upload_app.py` — submission_context columns
  - `coach_invite/db.py::ensure_invite_token_column()` — invite_token column
  - `billing_service.py::_ensure_technique_columns()` — technique credit columns

  These all use `ALTER TABLE … ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` / `DROP VIEW + CREATE VIEW` patterns and run on every boot. **If you need a schema change, add it to the right `init_*` function — don't drop a SQL file here.**

## Files

| File | Purpose | When run |
|---|---|---|
| `backfill_sport_type_and_ball_hit_columns.sql` | Default existing `submission_context.sport_type` to `'tennis_singles'`; populate scalar columns (`ball_hit_s`, `ball_hit_frame`, `ball_hit_location_x/y`, etc.) from legacy JSONB blobs in `bronze.player_swing`. | Once after the new columns landed. Re-runnable (all `WHERE … IS NULL` guarded). |
| `drop_vw_point_base.sql` | Drop legacy `silver.vw_point_base` view. | Once after view was retired. Idempotent (`DROP VIEW IF EXISTS … CASCADE`). |

## How to run

On the Render shell:

```bash
psql "$DATABASE_URL" -f migrations/<filename>.sql
```

Render shell already has `psql` and `$DATABASE_URL` in env. No additional auth setup.

## Conventions for new files

- Filename: `<verb>_<what>_<optional_date>.sql`. Examples: `backfill_X_columns.sql`, `drop_legacy_Y.sql`.
- First line: a comment explaining what it does and when it should be run.
- Every `UPDATE` guarded with `WHERE <new_col> IS NULL` so re-running is a no-op.
- Every `DROP` uses `IF EXISTS`.
- Every `ALTER` uses `IF NOT EXISTS` or its equivalent.
- **Never `DELETE FROM billing.*`** — see `docs/business.md` §7.

## See also

- [`../docs/business.md`](../docs/business.md) §7 — soft-delete contract (why migrations never touch `billing.*`)
- [`../CLAUDE.md`](../CLAUDE.md) §Testing & Code Quality — full list of the idempotent `init_*` schema entry points
