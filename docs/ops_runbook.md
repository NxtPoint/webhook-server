# Ops Runbook

> Every non-product endpoint you'd hit from the Render shell or curl: health probes, diagnostics, maintenance, billing operations. Auth, body shape, expected output, when to run.

**Auth conventions, in one place:**

- **Header-only `OPS_KEY`**: `X-Ops-Key: <OPS_KEY>` *or* `Authorization: Bearer <OPS_KEY>`. **Query-string `?key=` is deliberately rejected** to keep the key out of access logs (`_guard()` in `upload_app.py`). The same rule applies to every `/ops/*` endpoint and most `/api/billing/*` endpoints.
- **Admin email** (where noted): `X-Client-Key` header *plus* `email` parameter must be in the admin allowlist (`info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`).
- **Token-only**: just possession of a secret token (used by the coach-accept flow only).

The base URL in production is `https://api.nextpointtennis.com`. From the Render shell you can also use `http://localhost:$PORT` if you exec into the running service.

---

## Health probes (no auth)

### `GET /__alive`
**What**: Liveness check. Two services answer this:
- Main API ("Sport AI - API call" on Render): returns Flask default JSON
- Locker-room: returns `{"ok": true, "service": "locker-room"}`

**When**: Render's health check pings this. Use to confirm a service is up before debugging deeper.

**Try it:**
```bash
curl -s https://api.nextpointtennis.com/__alive
```

### `GET /healthz`
**What**: Plain-text "OK" liveness probe on the main API.

**When**: Same as `__alive` but for monitors that expect plain text rather than JSON.

```bash
curl -s https://api.nextpointtennis.com/healthz
```

---

## Diagnostic reads (`OPS_KEY` header)

### `GET /ops/routes`
**What**: Dumps the full Flask URL map (rule, endpoint, methods).

**When**: "What's actually registered on this deploy?" Sanity check after a blueprint change.

```bash
curl -s https://api.nextpointtennis.com/ops/routes \
  -H "X-Ops-Key: $OPS_KEY" | jq '.routes | length'
```

### `GET /ops/env`
**What**: Returns a curated set of env-var values: SportAI base URL, S3 bucket/prefix, AWS region, default-replace-on-ingest flag, `has_TOKEN` boolean. **Does NOT echo secrets** — `has_TOKEN` is just a boolean, not the value.

**When**: "Is this service pointed at the right SportAI environment? Is the right S3 bucket configured?"

```bash
curl -s https://api.nextpointtennis.com/ops/env -H "X-Ops-Key: $OPS_KEY" | jq
```

### `POST /ops/diag/sql` — read-only SQL (the autonomous-agent endpoint)
**What**: Read-only SELECT/WITH queries against the live DB. Hardened with sqlparse single-statement check, keyword denylist (INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, CREATE, GRANT, REVOKE, COPY, VACUUM, ANALYZE, CALL, DO, LOCK, EXECUTE, SET, RESET, BEGIN, COMMIT, ROLLBACK), per-query `statement_timeout = 5s`, hard row cap `min(body.limit or 100, 1000)`. Defined in `diag_sql/sql_endpoint.py`.

**When**: Any read-only investigation. This is the safest SQL endpoint. Future Claude sessions can call it via WebFetch instead of asking you to paste shell output.

```bash
curl -s https://api.nextpointtennis.com/ops/diag/sql \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT count(*) FROM bronze.submission_context WHERE deleted_at IS NULL", "limit": 10}'
```

**Response shape:**
```json
{
  "columns": ["count"],
  "rows": [[1234]],
  "row_count": 1,
  "truncated": false,
  "elapsed_ms": 8
}
```

**Errors**: 400 (parse failure / forbidden keyword), 408 (`statement_timeout`), 500 (DB error).

### `GET /ops/sqlq?q=...` and `POST /ops/sqlx`
**What**: Full-power SQL endpoints — **not** restricted to SELECT. `sqlq` takes the query in the URL; `sqlx` takes it in `body.q`.

**When**: You need to run a one-off `UPDATE` / `ALTER` / etc. from the Render shell or a script. Prefer `/ops/diag/sql` for reads — it's safer.

**⚠️ Power tools.** No keyword denylist, no row cap. Easy to nuke a table. If you can use `/ops/diag/sql`, use it.

```bash
# Read example (prefer /ops/diag/sql)
curl -s "https://api.nextpointtennis.com/ops/sqlq?q=SELECT%20count(*)%20FROM%20billing.account" \
  -H "X-Ops-Key: $OPS_KEY"

# Write example (only when you really mean it)
curl -s https://api.nextpointtennis.com/ops/sqlx \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"q": "UPDATE bronze.submission_context SET deleted_at = now() WHERE task_id = ''xxx''"}'
```

---

## Maintenance (`OPS_KEY` header)

### `POST /ops/orphan-sweep`
**What**: Two-pass sweep that removes child rows for soft-deleted (`deleted_at IS NOT NULL`) and truly-orphan (no parent) `submission_context` rows. **Never touches `billing.*`.**

**When**: After bulk soft-deletes; after a known race between delete and ingest; before doing a `compact-storage` to maximise space reclaimed.

**Body (optional JSON):**
- `{"dry_run": true}` — count what would be deleted, change nothing
- `{"include_orphans": false}` — pass 1 only (soft-deleted parents); skip pass 2 (true orphans)

**Always run with `dry_run: true` first.**

```bash
curl -s https://api.nextpointtennis.com/ops/orphan-sweep \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}' | jq
```

Full reference: [`../cleanup/README.md`](../cleanup/README.md).

### `POST /ops/compact-storage`
**What**: Runs `VACUUM (FULL, ANALYZE)` on the bronze / silver / ml_analysis tables that grow with match volume. Returns per-table `before_bytes` / `after_bytes` / `freed_bytes` JSON.

**When**: After a bulk delete, when you want bytes returned to the OS (autovacuum reclaims internally for reuse, but doesn't shrink files). **Each VACUUM takes `ACCESS EXCLUSIVE`** — trigger during low traffic.

**Body (optional):**
- `{"only": ["schema.table", ...]}` — scope to specific tables. Otherwise sweeps the full hardcoded list (~25 tables).

```bash
# Full sweep — heavy, do during quiet hours
curl -s https://api.nextpointtennis.com/ops/compact-storage \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{}' | jq

# Scoped — just the big ones
curl -s https://api.nextpointtennis.com/ops/compact-storage \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"only": ["bronze.raw_result_chunk", "bronze.player_swing"]}' | jq
```

### `POST /ops/ingest-task`
**What**: Manually re-runs ingest for a known `task_id`. Resolves the SportAI result URL, then either delegates to the ingest worker (`mode: "worker"`, async) or runs in-process (`mode: "sync"`, blocks until done).

**When**: A SportAI task completed but the auto-ingest didn't fire (rare); or you need to re-ingest after a silver-builder fix; or testing pipeline changes.

**Body (required):**
```json
{"task_id": "<task_id>", "mode": "worker" | "sync"}
```

`mode: "worker"` returns immediately with a session id. `mode: "sync"` blocks for ~30–120s and returns the full status row.

```bash
curl -s https://api.nextpointtennis.com/ops/ingest-task \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task_id": "abc123", "mode": "worker"}' | jq
```

### `POST /ops/dual-submit-t5`
**What**: T5 dual-submit — given a SportAI task_id, kicks off the parallel T5 ML pipeline run for the same video. Used for SportAI-as-teacher labelled-data generation.

**When**: T5 training data collection. **Skip unless you're on T5 work.**

```bash
curl -s https://api.nextpointtennis.com/ops/dual-submit-t5 \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sportai_task_id": "abc123"}' | jq
```

Full T5 context: `.claude/handover_t5.md`.

### `POST /ops/dual-submit-t5-backfill`
**What**: Retro-trigger T5 dual-submit across all SA `tennis_singles` tasks that don't yet have a paired T5 job. Phase 5c.1 of the dual-submit pipeline. Idempotent — inherits the per-task skip logic from `/ops/dual-submit-t5`. Throttled between submits to keep Batch queue depth manageable.

**When**: After flipping `AUTO_DUAL_SUBMIT_T5=1` on Render, to fold historical SA matches into the training corpus. **Always run `dry_run=true` first** to size the backfill — each submitted job is ~$0.12-0.15 on Spot G4dn.

**Body (all optional)**:
- `dry_run` (default `true`) — list eligible tasks, submit nothing
- `limit` (default `50`, max `500`) — cap submits per call. Run repeatedly to drain a large queue.
- `delay_ms` (default `1000`) — throttle between submits (0-60000)

**Response**: `{scanned, eligible, submitted, skipped[], errors[], next_cursor, sample[]}`. `sample` is present in dry-run mode (first 5 eligible).

```bash
# Step 1 — dry-run to count eligible
curl -s https://api.nextpointtennis.com/ops/dual-submit-t5-backfill \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d '{"dry_run": true, "limit": 500}' | jq

# Step 2 — actually submit (decide limit based on dry-run + Batch capacity)
curl -s https://api.nextpointtennis.com/ops/dual-submit-t5-backfill \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d '{"dry_run": false, "limit": 20, "delay_ms": 1500}' | jq
```

Full T5 context: `.claude/handover_t5.md`. Dual-submit pipeline status: `.claude/strategy/dual_submit_status_2026-05-20.md`.

---

## Billing operations (`OPS_KEY` header)

### `POST /api/billing/paypal/webhook` (LIVE payment path)
**What**: PayPal lifecycle webhook receiver (`paypal_billing/webhook.py`). Verifies PayPal's signature, **refetches** the resource from PayPal, then maps to the shared `apply_subscription_event(provider='paypal')`. Recurring grants on `PAYMENT.SALE.COMPLETED`, PAYG on capture; idempotent by PayPal resource id. **This is the live payment ingress** since 2026-06-16.

**When**: Called by PayPal automatically (registered webhook). Auth is the PayPal signature, not `OPS_KEY`.

### `POST /api/billing/subscription/event` (Wix — rollback fallback)
**What**: Wix subscription lifecycle webhook receiver. Idempotent per event by sha256 of canonical fields. On `PLAN_PURCHASED + ACTIVE` immediately calls `grant_entitlement()`. Now feeds the SAME `apply_subscription_event(provider='wix')` — retained only as the `PAYPAL_ENABLED=0` rollback.

**When**: Would only fire if payment is rolled back to Wix. Manually invoke to replay a missed event.

**Body**: Wix-formatted event JSON. See `subscriptions_api.py` (`apply_subscription_event`) for the normalized shape.

### `POST /api/billing/cron/monthly_refill`
**What**: Refills credits for ACTIVE recurring **Wix** subscriptions (`billing_provider='wix'`) to their plan allowance. PayPal subs are excluded — they grant per renewal payment via the webhook. Idempotent per `(account_id, YYYY-MM)`. Skips unless today is the 1st (overridable).

**When**: Render cron fires this on the 1st of each month via `cron_monthly_refill.py`. Manual invocation only for testing or backfill.

```bash
# Force-run mid-month (e.g. for a testing scenario)
curl -s https://api.nextpointtennis.com/api/billing/cron/monthly_refill \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"force": true}' | jq
```

### `GET /api/billing/summary?email=<email>`
**What**: Account usage summary: `matches_granted`, `matches_consumed`, `matches_remaining`, role, last processed task. Reads from `billing.vw_customer_usage`.

**When**: Customer support — "how many credits do I have?". Auditing — "is this account in the state I expect?".

```bash
curl -s "https://api.nextpointtennis.com/api/billing/summary?email=tomo@example.com" \
  -H "X-Ops-Key: $OPS_KEY" | jq
```

### `GET /api/billing/entitlement/check?email=<email>`
**What**: Upload-gate decision for an email: returns `allowed` (bool) and `reason` if not.

**Possible reasons**: `account_not_found`, `coach_cannot_upload`, `subscription_inactive`, `insufficient_credits`.

**When**: Debugging "why can't this user upload?". Faster than pulling the full entitlements row.

```bash
curl -s "https://api.nextpointtennis.com/api/billing/entitlement/check?email=tomo@example.com" \
  -H "X-Ops-Key: $OPS_KEY" | jq
```

### `POST /api/billing/entitlement/grant`
**What**: Manually grants credits to an account. Idempotent by `external_wix_id` if provided. Resolves account by `account_id` → `external_wix_id` → `email` (in that order).

**When**: Goodwill credit, manual top-up, billing correction.

**Body**: at least one of `account_id` / `external_wix_id` / `email`, plus `matches_granted`. Optional `techniques_granted`, `plan_code`, `external_wix_id`, `valid_to`.

```bash
# Goodwill: 2 free matches
curl -s https://api.nextpointtennis.com/api/billing/entitlement/grant \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "tomo@example.com",
    "matches_granted": 2,
    "plan_code": "manual_goodwill",
    "source": "manual_adjustment"
  }' | jq
```

### `GET /api/entitlements/summary?email=<email>`
**What**: Triggers the big UPSERT in `entitlements_api.py` and reads back the resulting `billing.entitlements` row. This is the **derived flags** view: `can_upload`, `can_view_dashboards`, `can_link_additional_player`, all the block reasons, current period end.

**When**: When you need the full picture (not just remaining credits). Newer than `/api/billing/summary` and broader.

```bash
curl -s "https://api.nextpointtennis.com/api/entitlements/summary?email=tomo@example.com" \
  -H "X-Ops-Key: $OPS_KEY" | jq
```

---

## Admin reads (`X-Client-Key` + admin email)

### `GET /api/support/health?email=<admin_email>`
**What**: Support bot operational metrics: FAQ hash, FAQ load timestamp, FAQ char count, conversation counts (24h / 7d), token usage, cost in cents (and converted to USD).

**When**: Cost spike investigation, FAQ-change verification (the hash should change when you edit `support_bot/faq.md`), volume tracking.

```bash
curl -s "https://api.nextpointtennis.com/api/support/health?email=info@ten-fifty5.com" \
  -H "X-Client-Key: $CLIENT_API_KEY" | jq
```

---

## Render cron jobs

These run automatically per `render.yaml`. Listed for completeness.

| Cron | Frequency | What it does |
|---|---|---|
| `cron_monthly_refill.py` | 1st of each month | POST `/api/billing/cron/monthly_refill` |
| `cron_capacity_sweep.py` | Every few minutes | Detects stuck ingests (>30 min) + stuck trims (>30 min) by reading `bronze.submission_context` directly. |

Both require `OPS_KEY` (or `BILLING_OPS_KEY`) in env.

---

## Common operational tasks

### "A user reports they can't upload — investigate"

```bash
# 1. What does the upload gate say?
curl -s "https://api.nextpointtennis.com/api/billing/entitlement/check?email=$EMAIL" \
  -H "X-Ops-Key: $OPS_KEY" | jq

# 2. Full picture
curl -s "https://api.nextpointtennis.com/api/entitlements/summary?email=$EMAIL" \
  -H "X-Ops-Key: $OPS_KEY" | jq

# 3. Recent ingest activity for this email
curl -s https://api.nextpointtennis.com/ops/diag/sql \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d '{"sql": "SELECT task_id, last_status, ingest_started_at, ingest_finished_at FROM bronze.submission_context WHERE email = '"'$EMAIL'"' ORDER BY ingest_started_at DESC LIMIT 10"}'
```

### "A user reports a match is stuck — investigate"

```bash
TASK_ID=abc123

# Status snapshot
curl -s https://api.nextpointtennis.com/ops/diag/sql \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d "{\"sql\": \"SELECT task_id, last_status, ingest_started_at, ingest_finished_at, trim_status, trim_finished_at, ses_notified_at, deleted_at FROM bronze.submission_context WHERE task_id = '$TASK_ID'\"}"

# If stuck, manually re-run ingest
curl -s https://api.nextpointtennis.com/ops/ingest-task \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d "{\"task_id\": \"$TASK_ID\", \"mode\": \"worker\"}"
```

### "DB looks bloated — reclaim space"

```bash
# 1. Sweep orphans (dry run first!)
curl -s https://api.nextpointtennis.com/ops/orphan-sweep \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d '{"dry_run": true}' | jq

# 2. If counts look right, run for real
curl -s https://api.nextpointtennis.com/ops/orphan-sweep \
  -H "X-Ops-Key: $OPS_KEY"

# 3. VACUUM FULL to return bytes to OS — pick a quiet window
curl -s https://api.nextpointtennis.com/ops/compact-storage \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" -d '{}' | jq
```

### "Goodwill credit for a customer who reported a bad match"

```bash
curl -s https://api.nextpointtennis.com/api/billing/entitlement/grant \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "matches_granted": 1,
    "plan_code": "manual_goodwill_<ticket-id>",
    "source": "manual_adjustment"
  }' | jq
```

### "What's deployed right now?"

```bash
curl -s https://api.nextpointtennis.com/ops/env -H "X-Ops-Key: $OPS_KEY" | jq
curl -s https://api.nextpointtennis.com/ops/routes -H "X-Ops-Key: $OPS_KEY" | jq '.count'
```

---

## Things to never do

- **Don't run `/ops/sqlx` or `/ops/sqlq` for reads** when `/ops/diag/sql` will do — denylist matters.
- **Don't `DELETE FROM billing.*`.** The match was a real billing event. See [`business.md`](business.md) §7.
- **Don't run `/ops/compact-storage` during peak hours.** `VACUUM FULL` takes `ACCESS EXCLUSIVE`. Match analysis pages will hang for the duration.
- **Don't pass `OPS_KEY` in the URL.** All `/ops/*` endpoints reject `?key=...` deliberately. Header-only.
- **Don't skip `dry_run: true` on `orphan-sweep`** for the first run after any schema change — table list might have shifted.

---

## Cron failover playbook (T5-only)

For AWS Batch Spot capacity issues during T5 runs, see [`../.claude/playbook_aws_batch_ondemand_fallback.md`](../.claude/playbook_aws_batch_ondemand_fallback.md). Out of scope for general ops.

---

## See also

- [`../CLAUDE.md`](../CLAUDE.md) §Diagnostics & Ops — short version with auth rules
- [`../cleanup/README.md`](../cleanup/README.md) — orphan sweep deep-dive
- [`business.md`](business.md) §7 — soft-delete contract
- [`billing.md`](billing.md) — full billing endpoint catalogue with code citations
- [`env_vars.md`](env_vars.md) — every env var these endpoints read
- `diag_sql/sql_endpoint.py` — `/ops/diag/sql` source + hardening rationale
