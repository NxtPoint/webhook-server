# cleanup

> Periodic mop-up for the soft-delete cascade. One Flask blueprint, two passes, never touches `billing.*`.

## What this owns

- `POST /ops/orphan-sweep` — a single ops endpoint that walks the bronze / silver / ml_analysis / tennis_coach / technique tables and deletes rows whose parent `bronze.submission_context` is soft-deleted or missing.
- The list of "child tables that follow `task_id`" (`_CHILD_TABLES` in `orphan_sweep.py`). **This list is the cascade contract.** When a new feature adds a table keyed on `task_id`, add it here.

## What this is NOT

- **Not a billing operation.** `billing.*` is never queried, never modified. The match was a real billing event regardless of whether its bronze rows survive. See [`../docs/business.md`](../docs/business.md) §7.
- **Not a delete endpoint.** `client_api.delete_match` is what the user-facing soft-delete calls — it sets `bronze.submission_context.deleted_at = now()`. This sweep is the asynchronous cleanup that finishes the job.
- **Not a worker.** It's a Flask blueprint registered on `upload_app`. Triggered manually (or by a cron/external scheduler) hitting the endpoint.

## Files

| File | Purpose |
|---|---|
| `orphan_sweep.py` | Blueprint + two-pass sweep logic |
| `__init__.py` | Empty package marker |

## Entry points

- Endpoint: `POST /ops/orphan-sweep` (`orphan_sweep.py:221-243`). Header-only OPS_KEY auth (`X-Ops-Key` or `Authorization: Bearer …`); query-string `?key=` is rejected.
- Body (optional JSON):
  - `{"dry_run": true}` — count what *would* be deleted, change nothing
  - `{"include_orphans": false}` — run pass 1 only, skip true-orphan sweep

## The two passes

**Pass 1 — soft-deleted parents** (`_sweep_soft_deleted`, `orphan_sweep.py:112-187`)
For every row in `bronze.submission_context` with `deleted_at IS NOT NULL`, delete every child row in every child table whose `task_id::text` matches.

**Pass 2 — true orphans** (`_sweep_orphans`, `orphan_sweep.py:190-218`)
For every child table, delete rows whose `task_id` has no matching row in `bronze.submission_context` at all. Catches early-test inserts and ingest races that left the parent un-created.

Plus: `ml_analysis.ball_detections` and `ml_analysis.player_detections` are keyed on `job_id`, not `task_id`, so they're swept via a sub-query on `ml_analysis.video_analysis_jobs`.

## Gotchas

- **The `_CHILD_TABLES` tuple is the cascade contract.** New feature tables keyed on `task_id` must be added here or they'll leak rows after deletes. Mirror the same list in `client_api.delete_match`.
- **Per-table `_table_exists` check.** Each table is probed via `information_schema.tables` before SELECT/DELETE. This prevents a missing table (e.g. on a fresh DB) from poisoning the whole transaction. See memory `feedback_postgres_missing_table.md` for why this pattern is mandatory.
- **`task_id::text` cast everywhere.** Some child tables key `task_id` as `text`, some as `uuid`. The cast handles both.
- **Failures are reported per-table, not raised.** If a single table errors, the response includes `"<schema>.<table>": "error: <ExceptionClass>"` and the sweep continues. The endpoint returns 200 unless the whole transaction blows up.
- **Single transaction.** Both passes run in `engine.begin()`. A pass-2 failure rolls back pass 1's deletes. Postgres handles this correctly because every operation is `task_id`-scoped.
- **Workers also honour `deleted_at`.** Both ingest paths (`ingest_worker_app.py::_do_ingest`, `upload_app.py::_do_ingest_t5`) check `deleted_at` at four gates and abort cleanly. The orphan sweep is the *belt* to the workers' *braces*.

## Operational notes

- **Run cadence.** No automated schedule today. Intended use: ad-hoc when the dashboard shows stale rows, or after a known race (e.g., user deleted a match while ingest was mid-flight).
- **`dry_run` first.** Always run with `{"dry_run": true}` first to see counts before committing.
- **The endpoint is idempotent.** Repeated calls return all-zeros once everything is clean.

## Example

```bash
# Dry run — count what would be deleted
curl -X POST https://api.nextpointtennis.com/ops/orphan-sweep \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'

# Real run, both passes
curl -X POST https://api.nextpointtennis.com/ops/orphan-sweep \
  -H "X-Ops-Key: $OPS_KEY"

# Soft-deleted parents only (skip true-orphan pass)
curl -X POST https://api.nextpointtennis.com/ops/orphan-sweep \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"include_orphans": false}'
```

## See also

- [`../docs/business.md`](../docs/business.md) §7 — soft-delete contract (the bright line on `billing.*`)
- [`../CLAUDE.md`](../CLAUDE.md) §Diagnostics & Ops — full ops endpoint catalogue
- `client_api.py::delete_match` — the user-facing soft-delete that this sweep follows up
