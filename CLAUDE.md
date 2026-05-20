# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here — what to read first

Pick the closest match and jump there before reading the rest of this file:

- **T5 ML pipeline / serve detector / Batch / silver_t5** → for *macro plan / current phase* see `docs/north_star.md`; for *how to run / validate / ship* see `.claude/handover_t5.md` (read "NEXT SESSION" + "TEST HARNESS" sections). Do **not** edit anything in `ml_pipeline/serve_detector/` without running the harness `bench` first.
- **Dashboard / gold view / endpoint mapping** → `docs/dashboards.md`.
- **Business rules / account model / credits / entitlement gates / soft-delete contract / share + referrals + pricing-pivot design** → `docs/business.md` (canonical for *how the product behaves*).
- **Pricing tier numerics / plan IDs / marketing copy** → `docs/pricing_strategy.md` (canonical for *what's sold*).
- **Billing implementation (file map, entry points, flows)** → `docs/billing.md`. Behaviour rules → `docs/business.md`.
- **Module-level orientation (any subdirectory)** → look for `<module>/README.md` first. Modules with READMEs: `coach_invite/`, `tennis_coach/`, `support_bot/`, `technique/`, `video_pipeline/`, `cleanup/`, `lambda/`, `migrations/`, `frontend/`. Each follows the same shape: purpose / files / entry points / flow / gotchas / see-also.
- **Ops endpoints / Render shell tasks / `/ops/*` reference** → `docs/ops_runbook.md` (every endpoint with auth, body, expected output, when to run, plus operational task playbooks).
- **Environment variables (any service)** → `docs/env_vars.md`.
- **Technique pipeline** → `docs/technique.md` (canonical) + `technique/README.md` (file orientation).
- **Support bot** → `docs/support_bot.md` (canonical) + `support_bot/README.md` (file orientation).
- **Anything else** → keep reading. The §Architecture Overview is the right next stop.

## Things not to do (load-bearing)

These look reasonable but will burn future sessions. Each is an explicit decision, not an oversight.

1. **Don't run `pytest`.** No test suite exists; testing is manual against the live Render DB. The closest thing to a regression test is `python -m ml_pipeline.diag.bench` for the T5 serve detector — that one is mandatory before any `serve_detector` push.
2. **Don't aggregate in Python or JavaScript if a gold view can do it.** The architecture rule is "SQL views own aggregation, Python is a thin passthrough, frontend is pure rendering." Adding `groupby` / `reduce` in `client_api.py` or a chart file means you skipped the right layer — extend or add a `gold.*` view instead.
3. **Don't import `upload_app` from the ingest worker.** The worker is deliberately self-contained (it calls `ingest_bronze_strict()` directly). Importing the main app pulls in Flask boot side-effects and breaks the worker timeout split (3600s vs 1800s).
4. **Don't `DELETE FROM billing.*` on match delete.** Matches are billable events — the consumption record stays. Match delete is soft-delete only via `submission_context.deleted_at`; workers honour this at four gates. See `cleanup/orphan_sweep.py`.
5. **Don't push T5 `serve_detector` changes without `bench` green.** The 20/24 baseline on `a798eff0` is locked in `ml_pipeline/diag/bench_baseline.json`. Three prior silent regressions are why this rule exists. The four remaining misses are upstream (pose / court projection) — gate-tuning to chase them backfires.
6. **Don't add ops endpoints with query-string `?key=` auth.** `_guard()` in `upload_app.py` deliberately rejects it to keep `OPS_KEY` out of access logs. Header-only (`X-Ops-Key` or `Authorization: Bearer`).
7. **Don't ask the user to rerun an ingest before `git push`.** Render deploys from `origin/main`; the Render shell would otherwise execute stale code and waste the rerun.
8. **Don't merge a T5 detector branch without the Batch-side change check.** Bench is green ≠ Batch is in sync. If `git diff origin/main HEAD --stat` against `ml_pipeline/roi_extractors/`, `ml_pipeline/__main__.py`, `ml_pipeline/pipeline.py`, `ml_pipeline/Dockerfile`, `ml_pipeline/requirements.txt`, or `ml_pipeline/serve_detector/` returns any rows, a Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1 are required before the user reruns. See `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST" — protocol exists because we shipped Phase 1 with code in `extract_far_pose` that lived only on Render and not in Batch on 2026-05-07.

## Services and How to Run

Python 3.12 / Flask + Gunicorn, deployed on Render (see `render.yaml`):

| Service | Start command | Entry point |
|---|---|---|
| **Main API** (`webhook-server`) | `gunicorn wsgi:app` | `wsgi.py` → `upload_app.py` |
| **Ingest worker** | `gunicorn ingest_worker_app:app` | `ingest_worker_app.py` |
| **Video trim worker** | Docker (`Dockerfile.worker`) | `video_pipeline/video_worker_wsgi.py` |
| **Locker Room** (static) | `gunicorn locker_room_app:app` | `locker_room_app.py` |

The Locker Room service serves HTML SPAs from `frontend/` via `send_file()` — Flask + gunicorn only, no DB access. Routes: `/` (locker room dashboard), `/media-room` (upload wizard), `/register`, `/backoffice`, `/portal` (entry point for Wix), `/pricing`, `/coach-accept`, `/practice`, `/match-analysis` (primary match dashboard), plus public marketing pages `/home`, `/how-it-works`, `/pricing-public`, `/for-coaches`. The main webhook-server serves all of them as same-origin backups for API access from within iframes.

**Local dev:**
```bash
source .venv/Scripts/activate  # Windows bash
pip install -r requirements.txt
gunicorn wsgi:app --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 1800
```

**Deploy:** Render auto-deploys all four services on push to `origin/main` (build config in `render.yaml`). Always `git push` *before* asking the user to rerun an ingest from the Render shell — otherwise the shell executes stale code and the rerun is wasted.

### Testing & Code Quality

No automated test suite and no linter. All functional testing is manual against the live Render database. Do not run `pytest`.

**One CI check exists** — `.github/workflows/bench.yml` is the entire `.github/` surface, no other workflows. It runs `python -m ml_pipeline.diag.bench` and triggers on every push to `main` and every PR touching one of:

- `ml_pipeline/serve_detector/**`
- `ml_pipeline/diag/bench*`
- `ml_pipeline/diag/replay_serves.py`
- `build_silver_v2.py`
- `.github/workflows/bench.yml` itself

It replays the committed CI fixture (`ml_pipeline/fixtures_ci/a798eff0.pkl.gz`) against the locked baseline (`ml_pipeline/diag/bench_baseline.json`, currently 20/24). Bench exits non-zero on any negative delta, which fails the PR check. Sub-second runtime; no DB, no AWS, no ML weights — see `.claude/handover_t5.md` §"TEST HARNESS". If the check goes red: revert the offending commit, reproduce locally with the same command, and only ship a fix that turns it green again. Do not skip or relax the check to land a PR — the regression is real (this is exactly the silent slip from `0cb645a` that motivated the harness).

Schema DDL is split across files:
- `db_init.py::bronze_init()` — bronze tables (idempotent, called on boot)
- `gold_init.py::gold_init_presentation()` — gold presentation views (idempotent, called on boot)
- `tennis_coach/db.py::init_coach_cache()` — coach cache table (idempotent)
- `tennis_coach/coach_views.py::init_coach_views()` — gold coach views (idempotent)
- `support_bot/db.py::init_support_schema()` — support_bot.conversations + faq_cache (idempotent)
- `_ensure_member_profile_columns()` in `client_api.py` — billing columns (on import)
- `_ensure_submission_context_schema()` in `upload_app.py` — submission_context columns (on import)
- `ensure_invite_token_column()` in `coach_invite/db.py`

All use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` / `DROP VIEW IF EXISTS + CREATE VIEW` patterns.

Gold view recreation (`gold_init.py`, `tennis_coach/coach_views.py`, `technique/gold_technique.py`) wraps the **entire** DROP+CREATE loop in a **single transaction**. Postgres DDL is transactional and takes AccessExclusiveLock on each view, so concurrent readers block until COMMIT and then see the new views atomically — no mid-boot window where a view is absent. A single view failure rolls back the whole transaction; we keep the previous working set rather than a half-applied mix.

---

## Architecture Overview

### Data Layers (medallion)

```
bronze.*  →  silver.*  →  gold.*  →  API  →  Dashboards + LLM Coach
  raw        analytical    thin          thin        rendering /
 ingest      point-level   per-chart     pass-       LLM context
             (fact)        views         through
```

**Bronze** (`bronze.*`): Raw SportAI JSON ingested verbatim. `db_init.py` owns schema. Key tables: `raw_result`, `submission_context`, `player_swing`, `rally`, `ball_position`, `ball_bounce`, `player_position`.

**Silver** (`silver.*`): The single source of truth for match-level analytics.
- `silver.point_detail` — one row per shot. Derived fields: serve zones (`serve_side_d`, `serve_bucket_d`), rally locations (A-D), aggression (`Attack`/`Neutral`/`Defence`), depth (`Deep`/`Middle`/`Short`), stroke (`Forehand`/`Backhand`/`Serve`/`Volley`/`Slice`/`Overhead`/`Other`), outcome (`Winner`/`Error`/`In`), serve try (`1st`/`2nd`/`Double`), ace/DF detection, normalised coordinates. Built by `build_silver_v2.py` (5-pass SQL). `model` column distinguishes `'sportai'` vs `'t5'` rows so both pipelines coexist.
- `silver.practice_detail` — practice equivalent. Built by `ml_pipeline/build_silver_practice.py` (3-pass).

**Gold** (`gold.*`): Presentation layer. Thin views — **one per chart or one per widget** — that aggregate silver into exactly the shape the frontend needs. No Python/JS aggregation downstream. Same views feed dashboards and LLM coach.

Full view catalogue, dashboard-endpoint mapping, LLM Coach data flow, and the Practice / Match Analysis SPAs: see **`docs/dashboards.md`**.

**Architecture rule**: **SQL views own aggregation. Python API endpoints are thin passthroughs. Frontend is pure rendering.** Never aggregate in Python or JavaScript if a view can do it once. This is enforced by code review — search `SELECT * FROM gold.` in `client_api.py` and confirm no new aggregation logic creeps in downstream.

### Silver V2 (`build_silver_v2.py`)

Current prod implementation. 5-pass SQL pipeline:
1. Insert from `player_swing` (core fields)
2. Update from `ball_bounce` (bounce coordinates)
3. Serve detection + point/game structure + exclusions
4. Zone classification + coordinate normalization
5. Analytics (serve buckets, stroke, rally_length, aggression, depth)

Court geometry constants live in `SPORT_CONFIG` at the top. T5 silver builders — `ml_pipeline/build_silver_match_t5.py` (matches) and `ml_pipeline/build_silver_practice.py` (practice) — call passes 3-5 directly from this module to share the derivation logic.

### Service Topology & Data Flow

Media Room uploads video to S3 → `POST /api/submit_s3_task` → main app routes by `sport_type`:
- **SportAI** (`tennis_singles`): async submit → poll status → delegate to ingest worker → bronze ingest → silver build → video trim → SES notify
- **T5** (`*_practice`, `tennis_singles_t5`): AWS Batch job → sentinel `t5://complete/{id}` → in-process `_do_ingest_t5` → bronze (from ml_analysis) → silver → trim → notify
- **Technique** (`technique_analysis`): single background thread → call technique API → bronze → silver → trim → notify (no auto-ingest routing, no sentinel URL)

**Key design**: the ingest worker is self-contained — it does NOT import `upload_app.py`. It calls `ingest_bronze_strict()` directly. Worker timeout 3600s vs main app 1800s.

### Main App (`upload_app.py`)

Primary service. Responsibilities: S3 presigned URLs + multipart lifecycle, SportAI/T5/Technique submission (routed by `sport_type`), task status orchestration, auto-ingest triggering, video trim callback, SES notification, CORS preflight for `/api/client/*`. Registered blueprints: grep `app.register_blueprint` in `upload_app.py`.

**On-boot init** (idempotent, each try/except-wrapped so one failure can't kill the service):
1. `gold_init_presentation()` — `gold.vw_player`, `gold.vw_point`, `gold.match_*`, `gold.player_performance`
2. `init_tennis_coach()` — `gold.coach_*` views + `tennis_coach.coach_cache`
3. `technique_bronze_init()` + `ensure_silver_schema()` + `init_technique_gold_views()` — bronze/silver tables + `gold.technique_*` views

### Video Trim Pipeline

Fire-and-forget async:
1. Ingest worker (match) or `_do_ingest_t5` (practice) calls `trigger_video_trim(task_id)`
2. Loads `silver.point_detail` (match) or `silver.practice_detail` (practice), builds EDL
3. POSTs to video worker at `VIDEO_WORKER_BASE_URL/trim`
4. Worker spawns detached subprocess → downloads from S3 → FFmpeg re-encodes → uploads `trimmed/{task_id}/review.mp4`
5. Worker callback updates `bronze.submission_context.trim_status`

For practice: trim source is `trim_output_s3_key` (the ML-produced practice.mp4), not the deleted original.

---

## Dashboards & Gold Views

Custom-built ECharts + canvas SPAs (`match_analysis.html`, `practice.html`) backed by thin gold views. Four-module match dashboard: Match Analytics, Placement Heatmaps, Player Performance, AI Coach. Practice is the reference design for new dashboards.

Full catalogue (gold view list, endpoint-to-view mapping, LLM Coach data flow, dashboard module breakdown): **`docs/dashboards.md`**.

LLM Coach design doc: `docs/llm_coach_design.md`.

---

## Billing System

Credit-based usage tracking in `billing.*`. Core files: `billing_service.py`, `models_billing.py`, `billing_import_from_bronze.py`.

Tables: `Account`, `Member`, `EntitlementGrant`, `EntitlementConsumption`. View: `billing.vw_customer_usage`.

Key patterns:
- 1 task = 1 match consumed (idempotent via `task_id` unique constraint)
- Entitlement grants idempotent via `(account_id, source, plan_code, external_wix_id)`
- **Immediate credit grant on purchase**: `subscription_event()` → `grant_entitlement()` instantly on `PLAN_PURCHASED + ACTIVE`
- `billing_import_from_bronze.py` syncs completed tasks into consumption records, auto-creating accounts
- `entitlements_api.py` gates uploads: allows if active subscription OR remaining credits

**`billing.member` is the single source of truth** for customer/player/child/coach profile data. Match-level `player_a_name` / `player_b_name` stored separately in `bronze.submission_context` as point-in-time snapshots.

API blueprints: `subscriptions_api`, `usage_api`, `entitlements_api`.

## Coach Invite Flow

Owner invites coaches from the Locker Room "Invite Coach" tab. Data in `billing.coaches_permission` (id, owner_account_id, coach_account_id, coach_email, status, active, invite_token, created_at, updated_at).

Module: `coach_invite/` — `db.py`, `email_sender.py`, `video_complete_email.py`, `accept_page.py`.

**Client endpoints** (`client_api.py`): `GET /api/client/coaches`, `POST /api/client/coach-invite` (creates row + token + SES email), `POST /api/client/coach-revoke` (clears invite_token).

**Accept flow** (self-contained on Render): `GET /coach-accept?token=...` serves `coach_accept.html` which POSTs to `/api/coaches/accept-token` (token IS the auth, validates against `billing.coaches_permission`, sets ACCEPTED, clears token, auto-redirects to portal).

**Idempotency**: re-inviting a revoked coach reuses the row (status → INVITED, new token, new email). Tokens single-use.

## Email System (AWS SES)

Module: `coach_invite/` (contains both email types).

| Email | Trigger |
|---|---|
| Coach invite | `POST /api/client/coach-invite` |
| Video complete | Ingest step 7 + task-status auto-fire (idempotent via `ses_notified_at`) |

**AWS SES setup**: region `eu-north-1` (Stockholm, matches Render). IAM user `nextpoint-uploader` needs `ses:SendEmail` / `ses:SendRawEmail`. Domain `ten-fifty5.com` verified via DKIM. Must be promoted out of sandbox to send to unverified recipients.

**Env vars**: `SES_FROM_EMAIL` (default `noreply@ten-fifty5.com`), `COACH_ACCEPT_BASE_URL` (default `https://api.nextpointtennis.com`), `LOCKER_ROOM_BASE_URL` (default `https://www.ten-fifty5.com/portal`).

## Support Bot (`support_bot/` + `frontend/support.html`)

Customer-service chat for the portal. **Claude Haiku 4.5** with prompt caching + forced tool-use for guaranteed structured output. FAQ-only — bot answers strictly from `support_bot/faq.md` and escalates anything not covered (or any account-specific question) to `info@ten-fifty5.com` via SES.

| Endpoint | Purpose |
|---|---|
| `POST /api/support/ask` | Main entry. Body `{message, email, page_context?, conversation_id?}` → `{answer, confidence, needs_human, cited_sections, actions}` |
| `POST /api/support/feedback` | Thumbs up/down on a turn |
| `POST /api/support/escalate` | Email transcript to `info@ten-fifty5.com`, Reply-To = customer |
| `GET /api/support/health` | Admin-only: FAQ hash, conversation counts, cost metrics |

Auth: `X-Client-Key` header (same as Client API). Admin endpoints require `email` in `ADMIN_EMAILS`. CORS: `/api/support/` is in `CORS_PATHS` next to `/api/client/`.

Tables (idempotent on boot via `init_support_schema()`): `support_bot.conversations` (every Q+A logged with tokens + cost) and `support_bot.faq_cache` (sha256-keyed dedup, invalidated when `faq.md` content hash changes).

**Surface**: dedicated page `frontend/support.html` served at `GET /help` (by both `locker_room_app.py` and `upload_app.py` as same-origin backup). Reached via the **Help & Support** item in the portal sidebar (`portal.html` `NAV_ITEMS`); loads inside the portal iframe via the standard `navigateTo()` flow with auth params populated by `authParams()`. Visually mirrors the AI Coach module (greeting + quick-prompt chips + input + green-callout answer + green-pill `[section.id]` citations + amber escalate CTA when `needs_human` or `confidence=low`).

**Cost**: ~$0.001 per cached query, ~$0.008 per cache-write. Realistic monthly spend at portal volumes: < $5.

**Anti-hallucination**: hard FAQ-only system rule, account-specific questions auto-escalated regardless of FAQ coverage, `confidence=high` filter on cache writes, AI-Coach redirect for stroke/match-data questions, kill switch via `SUPPORT_BOT_ENABLED=false`.

**The FAQ is the load-bearing artefact** — `support_bot/faq.md` is currently seeded with 5 example entries; real ~30 to be written by Tomo + co-worker based on actual inbound email volume.

Full implementation reference: **`docs/support_bot.md`**. Design history & rationale (note: predates the widget→page pivot): `docs/support_bot_design.md`.

## Client API (`client_api.py`) — non-dashboard endpoints

Auth: `X-Client-Key` header. Admin endpoints additionally require email in `ADMIN_EMAILS` (hardcoded: `info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`).

| Endpoint | Purpose |
|---|---|
| `GET /api/client/matches` | Match list for sidebar — from `gold.vw_client_match_summary` |
| `GET /api/client/players` | Distinct player names for autocomplete |
| `PATCH /api/client/matches/<task_id>` | Update match metadata (whitelisted fields) |
| `POST /api/client/matches/<task_id>/reprocess` | Rebuild silver via `build_silver_v2` |
| `GET /api/client/profile` / `PATCH` | Primary member profile on `billing.member` |
| `GET /api/client/usage` | Account usage summary |
| `GET /api/client/footage-url/<task_id>` | Presigned S3 GET URL for trimmed footage |
| `GET /api/client/entitlements` | Role, plan, credits, plans_page_url |
| `GET /api/client/members` / `POST` / `PATCH` / `DELETE` | Linked players (billing.member) |
| `POST /api/client/register` | Onboarding |
| `POST /api/client/children` | Add child member |
| `GET /api/client/profile-photo-upload-url` | Presigned S3 PUT |
| `GET /api/client/backoffice/pipeline` | Admin pipeline status |
| `GET /api/client/backoffice/customers` | Admin customer list |
| `GET /api/client/backoffice/kpis` | Admin KPI cards |

## Locker Room SPAs

All auth via URL params forwarded through the portal: `?email=&firstName=&surname=&wixMemberId=&key=&api=`.

**Design system**: all pages share CSS variables, Inter font, green/amber/red palette, `.toggle-group` / `.toggle-btn` buttons, ECharts helpers (`eBar`, `eStackedBar`, `ePie`, `eGauge`) defined identically in every file.

- **Locker Room** (`/`): dashboard. Header tabs (Account / My Details / Linked Players / Invite Coach), charts (matches per month, usage gauge), match history.
- **Media Room** (`/media-room`): 4-step upload wizard (game type → upload → details → progress). Game types: Singles (SportAI, prod), Singles T5 / Serve / Rally / Technique (dev-only, gated to `tomo.stojakovic@gmail.com`).
- **Pricing** (`/pricing`): fetches entitlements, renders one of three views (new plan / top-up only / coach view). Sends `postMessage({ type: 'wix-checkout', planId })` up to Wix for PayPal checkout.
- **Portal** (`/portal`): **entry point**. Collapsible sidebar, inner iframe with auth params forwarded. Embedded in Wix page `https://www.ten-fifty5.com/portal`. Main nav: Dashboard, Upload Match, My Profile, **Analytics** (with sub-items: Match Analytics, Placement Heatmaps), Plans & Pricing. Admin section: Backoffice, Practice (WIP). Sub-nav items show tree-line connectors.
- **Practice** (`/practice`): practice analytics (see Dashboards section).
- **Match Analysis** (`/match-analysis`): match analytics — 4 modules: Match Analytics, Placement Heatmaps, Player Performance, AI Coach (see Dashboards section).

**Wix remaining dependencies** (everything else has been retired):
1. Member authentication (Wix login → portal URL params)
2. Payment checkout (`checkout.startOnlinePurchase(planId)` via Wix Pricing Plans API / PayPal)
3. Subscription event webhook (`POST /api/billing/subscription/event`)

**iOS iframe CSS**: all pages run inside Wix → portal → page iframes. Use `height: 100%` (not `vh`), `viewport-fit=cover` meta tag, `padding-bottom: 300px` on mobile.

## Auth Pattern

- **Ops endpoints**: `OPS_KEY` via `X-Ops-Key` header or `Authorization: Bearer <key>`
- **Video worker**: `VIDEO_WORKER_OPS_KEY` (worker auth), `VIDEO_TRIM_CALLBACK_OPS_KEY` (callback auth, must match main API `OPS_KEY`)
- **Client API**: `CLIENT_API_KEY` via `X-Client-Key` header
- **Coach accept**: token-based (the invite token IS the auth)

## Idempotency Patterns

- Billing consumption: unique constraint on `task_id`
- Entitlement grants: unique on `(account_id, source, plan_code, external_wix_id)`
- Bronze ingest: advisory locks on `task_id`
- Customer notify: checks `wix_notified_at` / `ses_notified_at` before sending
- Coach invite token: unique partial index `WHERE invite_token IS NOT NULL`
- Gold views: `DROP VIEW IF EXISTS` + `CREATE VIEW` on every boot
- Coach cache: unique `(task_id, email, prompt_key)`

## Required Environment Variables

Full env-var matrix (main API required + optional + legacy, all worker services, crons, Lambda, ML pipeline Docker): **`docs/env_vars.md`**.

Quick reference for the main API: `DATABASE_URL`, `OPS_KEY`, `CLIENT_API_KEY`, `ANTHROPIC_API_KEY`, `S3_BUCKET`, `AWS_REGION`, AWS keys, `SPORT_AI_TOKEN`, plus worker-pair URLs/keys (`INGEST_WORKER_*`, `VIDEO_WORKER_*`, `VIDEO_TRIM_CALLBACK_*`).

## S3 CORS

Bucket `nextpoint-prod-uploads` requires CORS for browser-to-S3 multipart uploads + video playback:
- AllowedMethods: GET, PUT, POST, HEAD
- AllowedHeaders: `*`
- ExposeHeaders: `ETag` (required for multipart upload completion)
- AllowedOrigins: `https://locker-room-26kd.onrender.com`, `https://api.nextpointtennis.com`, ten-fifty5.com variants, Wix editor/site domains

## Diagnostics & Ops

All `/ops/*` endpoints use **header-only** auth (`X-Ops-Key: <OPS_KEY>` or `Authorization: Bearer <OPS_KEY>`). Query-string `?key=` is deliberately rejected to keep OPS_KEY out of access logs — see `_guard()` in `upload_app.py`.

- `GET /healthz` — liveness probe on the main API (no auth, returns "OK"). The Locker Room service has its own `/__alive` at `locker_room_app.py:113`; the main API does NOT serve `/__alive`.
- `GET /ops/routes` — list all registered routes
- `GET /ops/db-ping` — DB connectivity
- `POST /ops/compact-storage` — runs `VACUUM (FULL, ANALYZE)` on the bronze/silver/ml_analysis table list, returns per-table `before_bytes` / `after_bytes` / `freed_bytes` JSON. Optional body `{"only": ["schema.table", ...]}` to scope. Each VACUUM takes ACCESS EXCLUSIVE — trigger during low traffic.
- `POST /ops/orphan-sweep` — periodic mop-up for the soft-delete cascade. Two passes: (1) child rows whose parent `submission_context.deleted_at IS NOT NULL`, (2) true orphans whose `task_id` has no `submission_context` row at all. Body: `{"dry_run": true}` reports counts without changes; `{"include_orphans": false}` skips pass 2. Idempotent. Never touches `billing.*`. Implemented in `cleanup/orphan_sweep.py`.
- `POST /ops/diag/sql` — read-only SELECT runner for autonomous diagnostics (Tier-2 autonomy infra; future Claude sessions hit this via WebFetch instead of asking the user to paste Render-shell output). Body: `{"sql": "SELECT ...", "limit": 100}` (default 100, max 1000). Response: `{"columns": [...], "rows": [[...]], "row_count": N, "truncated": bool, "elapsed_ms": ms}`. Enforced via `sqlparse` + keyword denylist: only single-statement `SELECT` or `WITH ... SELECT`; rejects `INSERT/UPDATE/DELETE/DROP/TRUNCATE/ALTER/CREATE/GRANT/REVOKE/COPY/VACUUM/ANALYZE/CALL/DO/LOCK/EXECUTE/SET/RESET/BEGIN/COMMIT/ROLLBACK` and the phrases `FOR UPDATE` / `FOR SHARE` (CTE-wrapped DML is caught by the keyword check). Per-query transaction sets `statement_timeout = '5s'`; timeout returns 408. Bad SQL returns 400 with `offending_keyword`. Logs query text + IP + elapsed_ms but never row contents (PII). Residual risk: mutating server-side functions (`pg_terminate_backend`, advisory locks) are not enumerated — `OPS_KEY` is server-to-server only, so this is accepted. Implemented in `diag_sql/sql_endpoint.py`. Example: `curl -sS -X POST https://api.nextpointtennis.com/ops/diag/sql -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" -d '{"sql":"SELECT task_id, sport_type FROM bronze.submission_context ORDER BY created_at DESC LIMIT 5","limit":5}'`.

**Workers respect `submission_context.deleted_at`** — both the SportAI ingest worker (`ingest_worker_app.py::_do_ingest`) and the in-process T5 path (`upload_app.py::_do_ingest_t5`) check `deleted_at` at four gates (`pre_start`, `pre_bronze`, `pre_silver`, `pre_trim`) and abort cleanly without re-populating bronze rows if a delete races with an in-flight ingest.

## Code Organisation

New features **must live in their own subdirectory** with `__init__.py`. Examples: `video_pipeline/`, `ml_pipeline/`, `coach_invite/`, `tennis_coach/`, `cleanup/`. Repo root is for service entry points (`*_app.py`, `wsgi.py`, `gold_init.py`, `db_init.py`) and legacy top-level Flask blueprints.

**Root-level blueprints registered on the main API** (grep `app.register_blueprint` in `upload_app.py` for the full wiring):

- `client_api.py` — `/api/client/*`, CLIENT_API_KEY auth. Primary customer-facing API surface (dashboard endpoints, profile, entitlements, members, matches, footage URLs). Non-dashboard endpoints catalogued [above](#client-api-client_apipy--non-dashboard-endpoints); dashboard endpoints in `docs/dashboards.md`.
- `coaches_api.py` — `/api/coaches/*`, OPS_KEY auth. Server-to-server coach permission management over `billing.coaches_permission` (invite / accept / revoke). Companion to the token-based public accept page in `coach_invite/accept_page.py`; called internally by `client_api.py` coach endpoints.
- `members_api.py` — members CRUD blueprint.
- `subscriptions_api.py`, `usage_api.py`, `entitlements_api.py` — billing surface (see [Billing System](#billing-system)).
- `ui_app.py` — **legacy** admin UI mounted at `/upload/*`, OPS_KEY auth. Renders bronze/silver inspection pages via `render_template_string`. Not used by any SPA (`backoffice.html` is the real admin UI) — retained for shell/debugging only.

**Root-level cron scripts** (invoked by Render Cron Jobs, not registered as blueprints):

- `cron_capacity_sweep.py` — periodic billing/capacity sweep. See `docs/billing.md` and `docs/env_vars.md` for schedule + env vars.
- `cron_monthly_refill.py` — monthly entitlement refill for active subscriptions.

**Ignorable root directories** — present on disk but not part of the runtime:

- `_archive/` — deprecated code kept for reference (don't read unless investigating a specific historical regression).
- `diag_081e089c/`, `data/` — local investigation snapshots and scratch dumps (often git-ignored). Skip unless the current task explicitly references them.
- `static/`, `templates/` — Flask defaults; the actual SPAs live under `frontend/` and bronze/silver inspection templates are inlined in `ui_app.py`.

**`frontend/`** — all SPA HTML pages. Served by `locker_room_app.py` and (same-origin backups) `upload_app.py` via a `_html(name)` helper that resolves an absolute path under `frontend/`:

- Authenticated app: `locker_room.html`, `media_room.html`, `portal.html` (nav shell / Wix entry point), `backoffice.html`, `pricing.html`, `coach_accept.html`, `players_enclosure.html` (register wizard), `practice.html`, `match_analysis.html`, `support.html` (served at `/help`)
- Public marketing: `home.html`, `how_it_works.html`, `pricing_public.html`, `for_coaches.html`

**`docs/`** — design docs and strategy specs (`pricing_strategy.md`, `llm_coach_design.md`). Source of truth for business rules. Code links back to section numbers (e.g. "see docs/pricing_strategy.md §6").

---

## T5 ML Pipeline (`ml_pipeline/`)

In-house tennis video analysis pipeline. Runs on AWS Batch GPU (Spot G4dn.xlarge) for detection; runs on Render for serve detection + silver build. Handles `tennis_singles_t5`, `serve_practice`, `rally_practice`. Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`.

**Where to start a T5 session** — in this order:
1. `docs/north_star.md` — macro plan + phase ladder
2. The most recent `.claude/session_YYYY-MM-DD_review.md` if one exists — *live* handover with current design notes, validation commands, and the next path forward
3. `.claude/handover_t5.md` — canonical operational reference (architecture, how-to-run, validation, Docker/Batch deploy, training, file index, current task IDs, known gaps). The "TEST HARNESS" + "BATCH-SIDE CHANGE CHECKLIST" sections are mandatory reading before any detector edit or push.
4. `.claude/phase5_kickoff.md` and similar forward-looking docs — read if relevant to the chosen path

Then run `.venv/Scripts/python -m ml_pipeline.diag.bench` to confirm the floor is locked (currently a798eff0=20/24, 880dff02=23/24) before touching code.

The `.claude/` folder is **tracked in git** (handover docs + playbooks live there); only specific per-run artefacts (`debug_frames_*/`, `eval_*.txt`, `reconcile_*.txt`, `run_status_*.md`) are gitignored.

### Data flow (overview only — detail in handover)

```
video.mp4 → Batch (court/ball/player detection) → ml_analysis.*
          → Render (serve_detector) → ml_analysis.serve_events
          → Render (build_silver_match_t5) → silver.point_detail (model='t5')
          → gold.* views → dashboards
```

Both T5 and SportAI share passes 3-5 in `build_silver_v2.py` (repo root). The serve detector is a separate module (`ml_pipeline/serve_detector/`, pose-first architecture per Silent Impact 2025 + TAL4Tennis literature) that emits ServeEvent rows which the silver builder consumes.

### Key directories

| Dir | Purpose |
|---|---|
| `ml_pipeline/` | Core detection pipeline (court, ball, player), harness, evals |
| `ml_pipeline/serve_detector/` | Pose-first serve detection + rally state machine + schema |
| `ml_pipeline/training/` | TrackNet fine-tuning on dual-submit labels (note: `training/visual_debug/` is leftover local debug images, untracked — don't read or edit) |
| `ml_pipeline/stroke_classifier/` | Optical flow CNN for far-player stroke classification |
| `ml_pipeline/diag/` | Dev tools — serve viewer, pose probe, local pose extractor |

Weights in `ml_pipeline/models/` (~270 MB, git-ignored): TrackNet V2, YOLOv8x/m-pose, YOLOv8m, court_keypoints.pth, optional `stroke_classifier.pt` / `tracknet_v3.pt`.

### Most-used commands

See `.claude/handover_t5.md` for the full catalogue. The ones that come up constantly:

```bash
python -m ml_pipeline.harness validate <task_id>        # bronze + silver sanity
python -m ml_pipeline.harness eval-serve <task_id>      # pose-first serve detector vs SA
python -m ml_pipeline.harness reconcile <sa_tid> <t5_tid>
python -m ml_pipeline.harness rerun-silver <task_id>    # fast — no Batch needed
python -m ml_pipeline.diag.serve_viewer <task_id> --video <path>  # visual contact sheets
```

### Compute reality

Production is Spot-only in both regions (on-demand G-family vCPU quota is zero — confirmed 2026-04-15). Manual cross-region failover when Spot is tight. Playbook: `.claude/playbook_aws_batch_ondemand_fallback.md`.

Background / historical context lives in the auto-memory files (`project_t5_*.md`) referenced from `MEMORY.md`.

---

## Technique Analysis (`technique/`)

Biomechanics stroke analysis via external SportAI Technique API. Dev-only (gated to `tomo.stojakovic@gmail.com`). Sport type: `technique_analysis`. Synchronous streaming (single background thread in `upload_app.py::_technique_run_pipeline()` does download → API call → bronze → silver → trim copy → SES notify, end-to-end).

Tables, gold view list, key files, frontend swing-type list, full flow detail: **`docs/technique.md`**.

---

## Other

- **`docs/`**: feature-level design and reference docs. Active: `north_star.md` (T5 macro plan), `dashboards.md` (gold view + endpoint catalogue), `business.md` (canonical product behaviour), `billing.md` (billing implementation), `pricing_strategy.md` (pricing numerics), `ops_runbook.md` (every `/ops/*` endpoint), `env_vars.md` (full env-var matrix), `technique.md`, `support_bot.md`, `llm_coach_design.md`. Code links back by section number where relevant.
- **`migrations/`**: One-off backfill SQL scripts. No migration framework — schema is managed idempotently via `db_init.py` + `gold_init.py` + per-module `ensure_*` functions.
- **`_archive/`**: Deprecated/replaced code kept for reference.
- **`lambda/`**: AWS Lambda source (e.g., S3 trigger for ML pipeline).
- **`.claude/`**: Claude Code handover docs + AWS Batch playbooks (tracked in git, see list above); per-run artefacts (debug frames, eval txts, run status) are gitignored.
- **Auto-memory** (Claude Code's per-project memory dir, indexed by `MEMORY.md`): persistent cross-session notes loaded into every conversation. Historical T5 context (`project_t5_*.md`), user/feedback rules, and feature-launch records live here — check it for "why did we decide X" before re-deriving from code. Local to the machine, not in git.
