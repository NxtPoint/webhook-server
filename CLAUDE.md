# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Services and How to Run

Python 3.12 / Flask + Gunicorn, deployed on Render (see `render.yaml`):

| Service | Start command | Entry point |
|---|---|---|
| **Main API** (`webhook-server`) | `gunicorn wsgi:app` | `wsgi.py` → `upload_app.py` |
| **Ingest worker** | `gunicorn ingest_worker_app:app` | `ingest_worker_app.py` |
| **Video trim worker** | Docker (`Dockerfile.worker`) | `video_pipeline/video_worker_wsgi.py` |
| **Locker Room** (static) | `gunicorn locker_room_app:app` | `locker_room_app.py` |

The Locker Room service serves HTML SPAs via `send_file()` — Flask + gunicorn only, no DB access. Routes: `/` (locker room dashboard), `/media-room` (upload wizard), `/register`, `/backoffice`, `/portal` (entry point for Wix), `/pricing`, `/coach-accept`, `/practice`, `/match-analysis` (primary match dashboard). The main webhook-server serves all of them as same-origin backups for API access from within iframes.

**Local dev:**
```bash
source .venv/Scripts/activate  # Windows bash
pip install -r requirements.txt
gunicorn wsgi:app --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 1800
```

### Testing & Code Quality

No automated test suite, no CI, no linter. All testing is manual against the live Render database. Do not run `pytest`.

Schema DDL is split across files:
- `db_init.py::bronze_init()` — bronze tables (idempotent, called on boot)
- `gold_init.py::gold_init_presentation()` — gold presentation views (idempotent, called on boot)
- `tennis_coach/db.py::init_coach_cache()` — coach cache table (idempotent)
- `tennis_coach/coach_views.py::init_coach_views()` — gold coach views (idempotent)
- `_ensure_member_profile_columns()` in `client_api.py` — billing columns (on import)
- `_ensure_submission_context_schema()` in `upload_app.py` — submission_context columns (on import)
- `ensure_invite_token_column()` in `coach_invite/db.py`

All use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` / `DROP VIEW IF EXISTS + CREATE VIEW` patterns.

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

See [Dashboards & Gold Views](#dashboards--gold-views) below for the full catalogue.

**Architecture rule**: **SQL views own aggregation. Python API endpoints are thin passthroughs. Frontend is pure rendering.** Never aggregate in Python or JavaScript if a view can do it once. This is enforced by code review — search `SELECT * FROM gold.` in `client_api.py` and confirm no new aggregation logic creeps in downstream.

### Silver V2 (`build_silver_v2.py`)

Current prod implementation. 5-pass SQL pipeline:
1. Insert from `player_swing` (core fields)
2. Update from `ball_bounce` (bounce coordinates)
3. Serve detection + point/game structure + exclusions
4. Zone classification + coordinate normalization
5. Analytics (serve buckets, stroke, rally_length, aggression, depth)

Court geometry constants live in `SPORT_CONFIG` at the top. T5 silver builders call passes 3-5 directly from this module to share the derivation logic.

### Service Topology & Data Flow

Media Room uploads video to S3 → `POST /api/submit_s3_task` → main app routes by `sport_type`:
- **SportAI** (`tennis_singles`): async submit → poll status → delegate to ingest worker → bronze ingest → silver build → video trim → SES notify
- **T5** (`*_practice`, `tennis_singles_t5`): AWS Batch job → sentinel `t5://complete/{id}` → in-process `_do_ingest_t5` → bronze (from ml_analysis) → silver → trim → notify
- **Technique** (`technique_analysis`): single background thread → call technique API → bronze → silver → trim → notify (no auto-ingest routing, no sentinel URL)

**Key design**: the ingest worker is self-contained — it does NOT import `upload_app.py`. It calls `ingest_bronze_strict()` directly. Worker timeout 3600s vs main app 1800s.

### Main App (`upload_app.py`)

Primary service. Responsibilities: S3 presigned URLs + multipart lifecycle, SportAI/T5/Technique submission (routed by `sport_type`), task status orchestration, auto-ingest triggering, video trim callback, SES notification, CORS preflight for `/api/client/*`.

**Registered blueprints**: `coaches_api`, `members_api`, `subscriptions_api`, `usage_api`, `entitlements_api`, `client_api`, `coach_accept`, `ml_analysis_bp`, `ingest_bronze`, `tennis_coach.coach_api`.

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

The primary analytics experience. Custom-built ECharts + canvas dashboards that read from thin SQL views.

### The Dashboard (`match_analysis.html`)

Single-page app at `/match-analysis`. Loaded inside the portal iframe with `?email=&key=&api=` auth params.

Four modules selectable via the top green strip:

1. **Match Analytics** (8 tabs) — Summary (KPI strip + H2H bars + speed gauges), Serve Performance, Serve Detail, Return Summary, Return Detail, Rally Summary, Rally Detail, Point Analysis. Reads `gold.match_kpi` + breakdowns.
2. **Placement Heatmaps** (5 tabs) — Serve Placement, Player Return Position, Return Ball Position, Groundstrokes, Rally Player Position. All tabs have: Player A/B toggle (green/blue convention), Set filter, tab-specific filters (serve try, stroke, depth, aggression). Blue court `#1a4a8a` on green `#2d6a4f`, near-side plotting with normalised coords. Reads `gold.match_shot_placement`.
3. **Player Performance** (3 tabs, Player A only) — KPI Scorecard (18 KPIs across Serve/Return/Rally/Games/Speed, rolling 5-match avg vs benchmark, sparkline), Trend Charts, Last Match vs Average. Reads `gold.player_performance` (email-scoped).
4. **AI Coach** — standalone module. See [LLM Coach](#llm-tennis-coach) below.

**Cross-module**: collapsible match list sidebar (280px → 46px, auto-collapse <1200px), filter persistence within module, `gold.vw_player` / `gold.vw_client_match_summary` filter to `sport_type = 'tennis_singles'` (excludes T5/technique dev matches).

### Gold Presentation Views

Created idempotently on boot by `gold_init.py::gold_init_presentation()` (`DROP VIEW IF EXISTS ... CASCADE` + `CREATE VIEW` per view, each try/except-wrapped).

**Base layer** (dim + fact):
- `gold.vw_player` — dim. Resolves `first_server` → `player_a_id` / `player_b_id`. Filtered to `sport_type = 'tennis_singles'` (excludes T5 dev matches). Generates monotonic `session_id`.
- `gold.vw_point` — fact. `silver.point_detail` flattened + joined to `vw_player`.

**Per-match presentation layer** (one view per chart/table):
| View | Feeds | Shape |
|---|---|---|
| `gold.match_kpi` | Summary tab, speed gauges, head-to-head, point analysis | 1 row per match, both players in `pa_*` / `pb_*` columns. ~120 columns including games won, 1st/2nd serve win%, unreturned serves, serve speed split, rally outcomes |
| `gold.match_serve_breakdown` | Serve Performance/Detail tabs, Serve Placement table | 1 row per (task, player, side, direction, serve_try) |
| `gold.match_return_breakdown` | Return Summary/Detail tabs | 1 row per player, with returns made/won/depth/stroke/vs-1st/vs-2nd |
| `gold.match_rally_breakdown` | Rally Summary/Detail tabs, aggression profile | 1 row per player, aggression/depth/stroke/speed counts |
| `gold.match_rally_length` | Rally Detail length distribution + length-bucket win comparison | 1 row per (task, length_bucket) with pa/pb wins |
| `gold.match_shot_placement` | All Placement Heatmap tabs + Point Analysis zone tables + return/rally cross-tab charts | 1 row per shot — coords, outcome, stroke, phase |

**Cross-match performance layer** (Player A only):
| View | Feeds | Shape |
|---|---|---|
| `gold.player_match_kpis` | Intermediate — consumed by `player_performance` | 1 row per (email, task_id) with 18 KPIs for Player A |
| `gold.player_performance` | Player Performance module scorecard | 1 row per (email, kpi_name) with benchmark, rolling avg, delta, trend, status, sparkline |

**Coach-specific views** (created by `tennis_coach/coach_views.py::init_coach_views()`):
- `gold.coach_rally_patterns` — per (task, player, stroke, depth, aggression) error/winner rates
- `gold.coach_pressure_points` — **STUB** (returns zero rows with correct column shape; break-point detection needs window-function score reconstruction which isn't implemented yet)

**Legacy**:
- `gold.vw_client_match_summary` — created by `db_init.py`, feeds `/api/client/matches` match list. Will be replaced by `gold.match_kpi` eventually but currently live.

### Client API — Dashboard Endpoints

All under `/api/client/match/*`, CLIENT_API_KEY auth, `email` query param for tenant isolation. Thin passthroughs: `SELECT * FROM gold.<view> WHERE task_id = CAST(:tid AS uuid)` → JSON.

| Endpoint | View |
|---|---|
| `GET /api/client/match/kpi/<task_id>` | `gold.match_kpi` |
| `GET /api/client/match/serve-breakdown/<task_id>` | `gold.match_serve_breakdown` |
| `GET /api/client/match/return-breakdown/<task_id>` | `gold.match_return_breakdown` |
| `GET /api/client/match/rally-breakdown/<task_id>` | `gold.match_rally_breakdown` |
| `GET /api/client/match/rally-length/<task_id>` | `gold.match_rally_length` |
| `GET /api/client/match/shot-placement/<task_id>` | `gold.match_shot_placement` |
| `GET /api/client/player/performance` | `gold.player_performance` (email-scoped, not task_id) |
| `GET /api/client/technique/report/<task_id>` | `gold.technique_report` |
| `GET /api/client/technique/comparison/<task_id>` | `gold.technique_comparison` |
| `GET /api/client/technique/kinetic-chain/<task_id>` | `gold.technique_kinetic_chain_summary` |
| `GET /api/client/technique/progression` | `gold.technique_progression` (email-scoped) |

On load, `match_analysis.html::selectMatch()` fires all six match endpoints in parallel via `Promise.all()` and caches as `selectedData.kpi / .serve / .return / .rally / .rallyLength / .placement`. The performance endpoint is fetched lazily when the Player Performance module is first opened.

Other dashboard endpoints:
- `/api/client/matches` — match list for sidebar (uses `gold.vw_client_match_summary`, filtered to `sport_type = 'tennis_singles'`)
- `/api/client/matches/<task_id>` — legacy raw silver.point_detail fetch
- `/api/client/match-analysis/<task_id>` — legacy full silver fetch

### LLM Tennis Coach

Package: `tennis_coach/`. Design doc: `docs/llm_coach_design.md`. Its own dashboard module.

**Endpoints** (CLIENT_API_KEY auth):
- `POST /api/client/coach/analyze` — named prompt or freeform. Returns `{response, data_snapshot, cached, tokens_used}`.
- `GET /api/client/coach/cards/<task_id>?email=` — pre-generated 3-card insight summary. Cached forever per (task, email).
- `GET /api/client/coach/status/<task_id>?email=` — poll for card generation status.
- `GET /api/client/coach/debug/<task_id>?email=` — **admin only**. Raw payload Claude sees, without calling Claude.

**Data flow**: `coach_api.py::_fetch_data_for_task()` auto-routes by `sport_type`:
- Match tasks → `tennis_coach/data_fetcher.py` → reads `gold.match_kpi`, `gold.match_*_breakdown`, `gold.coach_rally_patterns`
- Technique tasks → `technique/coach_data_fetcher.py` → reads `gold.technique_report`, `gold.technique_kinetic_chain_summary`, `gold.technique_comparison`

Then `prompt_builder.py` builds one of 5 templates (serve_analysis / weakness / tactics / cards / freeform) → `claude_client.py` calls Anthropic SDK (`claude-sonnet-4-6`, temp 0.3, max 600 tokens) → response cached in `tennis_coach.coach_cache` keyed on (task_id, email, prompt_key).

**Guardrails**: Player-A-only coaching (never analyses opponents). Small-sample suppression (MIN_SAMPLE=5) drops dimensions with too few shots. Rate limits: 5 freeform calls per (email, task_id) per day, 20 per email per day; cards excluded.

**Cost**: ~$0.01 per call, ~1.2-1.5k tokens. Realistic usage: $5-20/month. Requires `ANTHROPIC_API_KEY`.

**Credit integration**: NOT yet implemented — rate-limited only. Will require `billing_service.consume_entitlement()` integration.

### Practice Analytics Dashboard (`practice.html`)

Full-featured dashboard for serve/rally practice sessions. Apache ECharts + canvas. Route: `GET /practice`.

Tabs: Overview, Performance, Court Placement, Serve/Rally Analysis, Heatmaps (S3-rendered), Video.

Client API (practice-specific, not gold-layer):
- `GET /api/client/practice-sessions?email=` — list sessions
- `GET /api/client/practice-detail/<task_id>?email=` — `silver.practice_detail` rows + summary
- `GET /api/client/practice-heatmap/<task_id>/<type>?email=` — presigned S3 URL for heatmap images

Practice is the **reference design** for all custom dashboards. New dashboards should mirror its CSS, chart styling (`eBar`, `eStackedBar`, `ePie`, `eGauge`), mobile breakpoints, sidebar layout.

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

### Main API (webhook-server)

**Required** (service boots but degraded without these):
| Env Var | Notes |
|---|---|
| `DATABASE_URL` | Postgres, falls back to `POSTGRES_URL` / `DB_URL`, normalized to `postgresql+psycopg://` |
| `OPS_KEY` | Ops auth, server-to-server |
| `CLIENT_API_KEY` | `/api/client/*` auth |
| `ANTHROPIC_API_KEY` | **LLM Coach** — Claude Sonnet 4.6 via Anthropic SDK |
| `S3_BUCKET` | Uploads, footage, ML bronze JSON, debug frames |
| `AWS_REGION` | Default `us-east-1`. Actual: `eu-north-1` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | implicit boto3 |
| `SPORT_AI_TOKEN` | SportAI API |
| `TECHNIQUE_API_BASE` | **Technique Analysis** — base URL, required when technique module used |
| `TECHNIQUE_API_TOKEN` | Optional bearer token for Technique API (if auth-protected) |
| `INGEST_WORKER_BASE_URL` + `INGEST_WORKER_OPS_KEY` | Worker calls |
| `VIDEO_WORKER_BASE_URL` + `VIDEO_WORKER_OPS_KEY` | Video trim |
| `VIDEO_TRIM_CALLBACK_URL` + `VIDEO_TRIM_CALLBACK_OPS_KEY` | Trim callback (must match main API `OPS_KEY`) |

**Optional** (sensible defaults):
- `SES_FROM_EMAIL` (`noreply@ten-fifty5.com`), `COACH_ACCEPT_BASE_URL`, `LOCKER_ROOM_BASE_URL`, `PLANS_PAGE_URL`
- `SPORT_AI_BASE`, `SPORT_AI_SUBMIT_PATH`, `SPORT_AI_STATUS_PATH`, `SPORT_AI_CANCEL_PATH`
- `AUTO_INGEST_ON_COMPLETE=1`, `INGEST_REPLACE_EXISTING=1`, `ENABLE_CORS=0`
- `MAX_CONTENT_MB=150`, `MULTIPART_PART_SIZE_MB=25`, `S3_PREFIX=incoming`, `S3_GET_EXPIRES=604800`
- `BATCH_REGIONS_PRIORITY=eu-north-1,us-east-1` (T5 Batch region failover)
- `BATCH_JOB_QUEUE=ten-fifty5-ml-queue`, `BATCH_JOB_DEF=ten-fifty5-ml-pipeline`
- `BILLING_OPS_KEY` (falls back to `OPS_KEY`)

**Legacy (Wix payment transition — remove when own payment auth is built):**
`WIX_NOTIFY_UPLOAD_COMPLETE_URL`, `RENDER_TO_WIX_OPS_KEY`, `WIX_NOTIFY_TIMEOUT_S`, `WIX_NOTIFY_RETRIES`

### Other Services

- **Ingest Worker**: `INGEST_WORKER_OPS_KEY` (required — startup crash), `DATABASE_URL`, `VIDEO_WORKER_*` for trim trigger.
- **Video Trim Worker** (Docker): `VIDEO_WORKER_OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, AWS credentials. FFmpeg tunables: `VIDEO_CRF=28`, `VIDEO_PRESET=veryfast`, `FFMPEG_TIMEOUT_S=1800`.
- **Locker Room**: `PORT=5050` only. No DB or S3.
- **Cron `cron_capacity_sweep.py`**: `OPS_KEY`, `DATABASE_URL`, `INGEST_STALE_S=1800`, `TRIM_STALE_S=1800`.
- **Cron `cron_monthly_refill.py`**: `BILLING_OPS_KEY` or `OPS_KEY`.
- **Lambda `lambda/ml_trigger.py`**: `BATCH_JOB_QUEUE`, `BATCH_JOB_DEF`, `DATABASE_URL`.
- **ML Pipeline Docker** (`ml_pipeline/__main__.py`): `S3_BUCKET`, `DATABASE_URL`, `AWS_REGION=us-east-1`.

## S3 CORS

Bucket `nextpoint-prod-uploads` requires CORS for browser-to-S3 multipart uploads + video playback:
- AllowedMethods: GET, PUT, POST, HEAD
- AllowedHeaders: `*`
- ExposeHeaders: `ETag` (required for multipart upload completion)
- AllowedOrigins: `https://locker-room-26kd.onrender.com`, `https://api.nextpointtennis.com`, ten-fifty5.com variants, Wix editor/site domains

## Diagnostics

- `GET /__alive` — liveness probe
- `GET /ops/routes?key=<OPS_KEY>` — list all registered routes
- `GET /ops/db-ping?key=<OPS_KEY>` — DB connectivity

## Code Organisation

New features **must live in their own subdirectory** with `__init__.py`. Examples: `video_pipeline/`, `ml_pipeline/`, `coach_invite/`, `tennis_coach/`. Repo root is for service entry points only (`*_app.py`, `wsgi.py`, `gold_init.py`, `db_init.py`).

**Exception**: the SPA HTML files (`locker_room.html`, `media_room.html`, `portal.html`, `backoffice.html`, `pricing.html`, `coach_accept.html`, `players_enclosure.html`, `practice.html`, `match_analysis.html`) live in the repo root because `locker_room_app.py` serves them with `send_file()` from the working directory.

---

## T5 ML Pipeline (`ml_pipeline/`)

In-house ML pipeline for tennis video analysis. Runs on AWS Batch GPU (Spot G4dn.xlarge). Handles `tennis_singles_t5`, `serve_practice`, `rally_practice`. Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`.

### Architecture

```
SportAI JSON ──→ bronze.player_swing ──→ silver.point_detail (model='sportai')
T5 ML Pipeline ──→ ml_analysis.* ──────→ silver.point_detail (model='t5')
```

Both share passes 3-5 in `build_silver_v2.py`. T5 silver builders: `build_silver_match_t5.py` (match), `build_silver_practice.py` (practice).

### T5 flow

Media Room → S3 upload → `_t5_submit()` → AWS Batch job → sentinel `t5://complete/{id}` → auto-ingest (`bronze_ingest_t5.py` COPY → silver build → video trim → SES notification). Region failover: eu-north-1 → us-east-1.

### Pipeline components (`ml_pipeline/`)

**`court_detector.py`** — CNN (14 keypoints) + Hough-lines fallback + geometry-validated calibration lock + lens calibration integration.
- CNN keypoints via threshold 170 + Hough circles + `refine_kps()` (matches yastrebksv/TennisProject).
- **Calibration (frames 0-299)**: CNN runs every 30 frames; each candidate homography is geometry-validated. Hough fallback only when CNN returns zero valid keypoints (`cnn_kps_count == 0`) — Hough is otherwise unreliable on wide-angle indoor footage where it picks up sponsor-banner and logo edges as "baselines".
- **Geometry validator** (`_validate_homography_geometry`): far baseline above near baseline; vertical span **25-90%** of frame height (90% upper bound — wide-angle indoor cameras legitimately have courts filling 80%+ of frame); far baseline below top 4%; near baseline above bottom 2%; far width < **0.85 × near width**; logs specific rejection reason per frame.
- **Lock @ frame 300**: prefer highest-inlier geometry-validated detection, fallback to any-best, then last-good. Fail-fast only if no detection at all AND no calibration produced.
- **Lens calibration** (new Apr 15, see `camera_calibration.py`): at lock time, pipes accumulated keypoint observations into `fit_calibration(observations, img_shape, rms_threshold_px=10.0)`. Result stored on `self._calibration`.
- **`to_court_coords(px, py, strict=True)`**: when `self._calibration` is set, routes through `project_pixel_to_metres` (radial or piecewise). Otherwise falls back to `_locked_detection.homography`. Strict=True applies ±5m slop bounds; debug annotations pass `strict=False`.
- **`to_pixel_coords(mx, my)`**: inverse projection — metric → raw pixel via `cv2.projectPoints(rvec, tvec, K, dist)`. Used by debug grid overlay.
- **`get_court_corners_pixels()`**: when calibration is set, projects `(0,0), (10.97,0), (0,23.77), (10.97,23.77)` metric corners through calibration. Otherwise returns raw keypoint corners. This gives the pixel-polygon gate a polygon matching the real court lines.
- Keypoint pixel positions + per-keypoint error (metres) logged at lock for diagnostics (`court_kp_err[NN] bl_top_L err=0.132m`).

**`camera_calibration.py`** — Lens distortion calibration for wide-angle MATCHI indoor cameras. Single module implementing **both Option A (primary) and Option C (fallback)**.
- `fit_calibration(observations, img_shape, rms_threshold_px=10.0)` top-level entry.
- **Probe step**: one-shot fit on raw observations identifies keypoint indices with per-keypoint error > 1m (typical failure mode: CNN collapses `bl_top_L`/`sg_top_L`/`sv_top_L` into the same pixel on wide-angle footage). Those indices are marked missing across all observations before the main fit.
- **Option A (radial, Brown-Conrady k1/k2)**: `cv2.calibrateCamera` with `CALIB_FIX_PRINCIPAL_POINT | CALIB_FIX_ASPECT_RATIO | CALIB_ZERO_TANGENT_DIST | CALIB_FIX_K3 | CALIB_USE_INTRINSIC_GUESS`. Pre-filter via RANSAC `findHomography(threshold=3px)` drops per-point outliers before bundle adjustment. **Iterative refinement** up to 3 passes: fit → evaluate per-keypoint error → drop indices with error > 1m → refit. Returns `CalibrationResult(mode='radial', ...)` on RMS ≤ 10 px. Working RMS on MATCHI footage: **6.26 px** (≈15cm metric error across the 23.77m court).
- **Option C (piecewise)**: 4-zone homographies split at `net_y_px × centre_x_px`. Each zone gets 3-4 keypoints. Mirror-fallback: if FR zone is unfit but FL has keypoints, derive FR from FL reflection across the centre line. Inverse-distance blend within 80 px of zone boundaries. Used when Option A fails to converge.
- `project_pixel_to_metres(px, py, calib)` — raw-pixel → metre. Radial mode does `undistortPoints` + `homography_undistorted`; piecewise applies the zone's H directly.
- `project_metres_to_pixel(mx, my, calib)` — inverse, via `cv2.projectPoints` (radial) or inverse zone H (piecewise). Used for debug grid overlay and court-corner polygon.
- `evaluate_calibration(calib, observations)` — per-keypoint metric error. Used at lock for self-check and iteratively during refinement.
- `undistort_frame(frame, calib)` — kept in module but **not used at runtime** (pipeline.py doesn't remap frames). Per-point undistortion at projection time keeps all pixel-space geometry (court polygon, bboxes, motion masks) in a single coordinate space.

**`ball_tracker.py`** — TrackNet V2 (3-frame, 9ch) + frame-delta Hough fallback. V2 detects ~36% of frames; delta recovers ~63%. Three-tier heatmap extraction: Hough circles → CC centroid → argmax. TrackNetV3 architecture ported in `tracknet_v3.py` — activates when `models/tracknet_v3.pt` exists.

**`player_tracker.py`** — Multi-strategy detection + three-tier court-metre scoring.
- **Detection**: SAHI tiled inference (640×640 tiles, 15% overlap, `sahi==0.11.18`) + full-frame YOLOv8x-pose + detection-only YOLOv8m for far baseline. Conditional SAHI skip when full-frame YOLO already has a far-baseline candidate (`court_y ≤ 5`). **SAHI crop margin: 30%** (was 10% — raised because `court_bbox` from raw keypoints on wide-angle footage cropped out the real far baseline; 30% covers the gap).
- **Scoring** (`_choose_two_players`): one player from each pixel-half (near/far). Three zones based on calibrated metric coords:
  - Tier 1 (3000): INSIDE doubles court — `0 ≤ x ≤ 10.97, 0 ≤ y ≤ 23.77`
  - Tier 2 (2000): BEHIND baseline — `-3 ≤ x ≤ 13.97, -4 ≤ y < 0 OR 23.77 < y ≤ 31.77` (**near-side +8m** to cover calibration extrapolation slack at the extreme bottom of the frame)
  - Tier 3 (1000): WIDE alley — `-1 ≤ x < 0 OR 10.97 < x ≤ 11.97, 0 ≤ y ≤ 23.77`
  - Tier 0: Everything else (umpire, spectator, coach, bench)
  - Bonuses: MOG2 motion (+500), baseline-closeness (0-500), bbox area (0-200)
  - **tier 0 → score = 0 (no bonuses)**. Spectators/linespeople can't accidentally win a half by stacking bonuses when no real player is in the candidate list.
- **`MIN_SELECTABLE_SCORE = 1000`**: a candidate must score ≥ tier-3 floor to be picked. If no candidate in a half meets this, the half is correctly left empty — no "best of a bad lot" selection.
- **Pixel-polygon gate**: feet > 300 px outside detected court polygon → tier 0. Previously 150 px but the polygon is a 4-corner straight-line quadrilateral while real baselines curve at the edges on wide-angle cameras; 150 was rejecting legitimate detections. With correct calibration, metric tier rules are the primary filter; pixel gate is a safety net for extreme outliers.
- **Null-projection handling**: when calibration exists and `to_court_coords` returns None (strict bounds failed), tier 0 and `score = motion_bonus only` — no legacy pixel-space fallback (which had been over-scoring spectators).
- **Debug frames**: `x=` and `y=` labels per bbox. **Metric grid overlay**: yellow (outer — baselines, net, sidelines) and cyan (inner — service lines, centre service) projected from metric space via `to_pixel_coords`. Frame-accurate visual calibration check.
- **Legacy pixel-space branch** (pre-calibration fallback): also applies `tier==0 → score=0` for symmetry.
- **MOG2 background subtraction** (`pipeline.py`): foreground mask fed every frame, motion ratio per candidate bbox.

**`pipeline.py`** — Orchestrates court → ball → MOG2 → player per frame. Raw (distorted) frames pass through unchanged — per-point undistortion happens inside `project_pixel_to_metres`. Post-processing: interpolation, bounce detection, speed calc, stationary player filter, optical-flow stroke classification (second video pass at bounce frames).

### AWS Batch & Docker

Primary: eu-north-1, fallback: us-east-1. Spot G4dn.xlarge, bid 100%. Base image: `nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04`.

```bash
docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline .
ACCOUNT=696793787014
aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com
docker tag ten-fifty5-ml-pipeline:latest $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
# Repeat for us-east-1
```

Weights in `ml_pipeline/models/` (~270MB, git-ignored): TrackNet V2, YOLOv8x-pose, YOLOv8m-pose, YOLOv8m, court_keypoints.pth.

### Database (`ml_analysis.*`)

`video_analysis_jobs` (job tracking), `ball_detections` (per-frame), `player_detections` (per-frame bbox + court coords + keypoints JSONB), `match_analytics` (aggregate).

### Test harness & eval (`ml_pipeline/harness.py`)

```bash
# Validation & comparison
python -m ml_pipeline.harness validate <task_id>
python -m ml_pipeline.harness reconcile <sportai_tid> <t5_tid>
python -m ml_pipeline.harness golden-snapshot <task_id> --name N
python -m ml_pipeline.harness golden-check <name>

# Per-component evaluation (persisted to eval_history.jsonl)
python -m ml_pipeline.harness eval-ball <task_id>      # detection rate, bounces, speed
python -m ml_pipeline.harness eval-player <task_id>    # player count, coord variance, path length
python -m ml_pipeline.harness eval-court <task_id>     # confidence, homography success
python -m ml_pipeline.harness eval-history [--last N]

# Training data
python -m ml_pipeline.harness export-ball-labels <task_id> <out.json>
python -m ml_pipeline.harness export-sportai-labels <task_id> <out.json>
python -m ml_pipeline.harness extract-frames <video_or_s3> <out_dir> [--fps 25]
```

`ml_pipeline/eval_store.py` persists eval results to `ml_pipeline/eval_history.jsonl`.

### Training pipeline (`ml_pipeline/training/`)

Fine-tune TrackNet V2 on own footage using SportAI ground truth as labels:
- `export_labels.py` — extract ball labels from DB (T5 detections + SportAI hits)
- `tracknet_dataset.py` — PyTorch Dataset: 3-frame windows → Gaussian heatmap labels (σ=2.5px)
- `train_tracknet.py` — freeze encoder (conv1-10), train decoder, BCELoss pos_weight=100, Adam lr=1e-4
- `extract_frames.py` — extract frames from video/S3 matching `ml_analysis.ball_detections` frame indices

Every dual-submit pair (SportAI + T5 on same video) produces free training labels.

### Debug frames

`DEBUG_FRAME_INTERVAL > 0` in config. Green = KEPT, red = FILTERED. Uploaded live to `s3://{bucket}/debug/{job_id}/frame_*.jpg`.

### Reconciliation reference

- SportAI ground truth: `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` (88 rows, 17 points, 2 games, 24 serves; ball speed avg 358 km/h)
- Latest T5 reference run (pre-calibration, known-wrong): `ad763368-eb3d-40f0-b9fe-84e0c9755c90` — 162 rows, 1 point, 1 serve, ball speed avg 30 km/h (ball y range `[10.69, 24.29]`).
- **First clean T5 run with radial calibration** (Apr 15 evening): `90ad59a8-8853-4014-9fd8-c32af7c4a2e9`. Lock `mode=radial rms=6.26 px from 11 observations`. Key numbers from reconcile vs SportAI ground truth `4a194ff3`:

| Metric | Pre-cal (ad763368) | Post-cal (90ad59a8) | SportAI |
|---|---|---|---|
| Silver rows | 162 | 160 | 88 |
| Serves (raw) | 1 | **21** | 24 |
| Serves (serve_d after Pass 3 gate) | 1 | **17** | 24 |
| Points | 1 | 2 | 17 |
| Games | 1 | 1 | 2 |
| Volley classifications | 156 | **2** | 5 |
| stroke_d Backhand | 0 | 0 | 15 |
| stroke_d Forehand | 21 | 80 | 41 |
| Ball court_y range | [10.7, 24.3] | **[-3.4, 28.6]** | full court |
| Ball speed avg (km/h) | 30 | 44 | 359 |
| server_end_d populated | 20% | **100%** | 100% |

- T5 golden baseline: take a snapshot once remaining silver bugs resolved (points collapse, ball speed, backhand detection).

### Deployment

Job definition revisions — **eu-north-1 revision 24**, **us-east-1 revision 13**. Both point to digest `sha256:9107d338...` (adds on top of rev 23: legacy pixel-space branch `tier == 0 → score = 0` + `MIN_SELECTABLE_SCORE = 1000` gate in `_choose_two_players::best_far/best_near` selection). Retry strategy: up to 3 attempts, auto-retry only on `Host EC2*` status reasons.

**Compute environment reality**: account has **zero on-demand G-family vCPU quota** in both regions (confirmed via `VcpuLimitExceeded` error). Production is Spot-only despite on-demand being listed as fallback in the job queue. When Spot capacity is tight (Stockholm was flat most of Apr 15), manual failover between regions via `aws batch submit-job --region us-east-1 --job-definition ten-fifty5-ml-pipeline:13 ...`. Quota increase request recommended for operational resilience. Full setup playbook in `.claude/playbook_aws_batch_ondemand_fallback.md`.

### Stroke classification (`build_silver_match_t5.py`)

**Near player** (200-400px, has pose keypoints): Four-tier heuristic from COCO keypoints — serve (arm raised at baseline), overhead (arm raised mid-court), volley (near net + compact arm), forehand/backhand (wrist position relative to shoulders, three signal tiers). Handles both handedness.

**Far player** (30-40px, no pose): Optical flow classifier in `ml_pipeline/stroke_classifier/`. Three-tier cascade in silver builder: keypoints → optical flow → position fallback.

- `flow_extractor.py` — Farneback dense optical flow on bbox crop ±5 frames around hit events, resized to canonical 64×48
- `model.py` — StrokeFlowCNN: lightweight 3D-CNN (~50K params), 5-class (fh/bh/serve/volley/other). Runs on CPU <5ms/hit
- `train.py` — Training script with augmentation (temporal flip, mirror, magnitude scaling)
- `export_training_data.py` — Aligns SportAI ground truth with T5 player detections from dual-submit pairs

**Pipeline integration**: After bounce detection, `pipeline.py::_classify_far_player_strokes()` re-reads video at bounce frames ±5, extracts flow, classifies, stores `stroke_class` on `PlayerDetection` → persisted to `ml_analysis.player_detections.stroke_class`. Silver builder reads it as tier 2 in the cascade.

**Training workflow**:
```bash
python -m ml_pipeline.harness export-stroke-data --sportai-task <id> --t5-task <id> --video <path> --output <dir>
python -m ml_pipeline.harness train-stroke --data <dir> --epochs 50
```
Weights saved to `ml_pipeline/models/stroke_classifier.pt`. Auto-detected by `StrokeClassifier` at pipeline runtime. Target: 75-85% accuracy on 200+ labeled examples from dual-submit pairs.

### Known gaps + current focus

**SOLVED Apr 15 — Lens distortion**. Primary blocker for weeks is now fixed. Radial calibration locks at RMS 6.26 px (≈15cm metric error) on MATCHI wide-angle footage. Yellow grid overlay on debug frames traces the real court lines. See `project_t5_apr15_breakthrough.md` memory for full chronology.

**SOLVED Apr 15 — Player detection cascade**:
- Far player detected in >95% of frames post-calibration (was 0% at the low point)
- Near player full-body bbox stable (previous head-only-KEPT artifact resolved by raising near-side behind_baseline to +8m and pixel-gate tolerance to 300 px)
- Side spectators, linespeople, umpires filtered (tier 0 → score 0 + MIN_SELECTABLE_SCORE 1000)

### Master plan — finish all dev before training (Apr 16+)

Strategy: lock the pipeline **before** burning dual-submit SportAI credits on training labels. Three phases, strictly sequential: A = correct, B = fast, C = ops. Authoritative checklist lives in `.claude/handover_t5_current.md`.

**Phase A — Correctness (blocks training)**. Close reconcile vs SportAI on the reference video to serves 22-24 / points 12-15 / backhand 10-15 / ball speed 200-400 km/h.
- **A0** ✅ done. Tier-2 `behind_baseline` expanded ±10m (was -4m / +8m) in `player_tracker.py::_choose_two_players`. Far player observed at metric y=-7 was being rejected; calibration extrapolation on the far image edge appears over-negative (physical ~4m → measured -6/-7m). Needs a follow-up investigation into k1/k2 residuals at the top of frame; for now the wider tier works around it.
- **A1** Points collapse (17 serves → 2 points). Every sampled silver row has identical hitter coords (hx=7.13, hy=-4.16) → `serve_side_d` never alternates. Root cause: `build_silver_match_t5.py::_find_nearest_detection` returns stale data because far-player coverage is ~10% of frames. Fix: require a detection within ±5 frames of the hit event; flag `hitter_resolved=False` on miss, don't silently reuse.
- **A2** Player 1 identity stability (var_y=155 in fd623ed2) — tighten IOU + distance gating so tracking doesn't jump between two disjoint spatial clusters.
- **A3** Ball speed 48 km/h vs 359. `ball_tracker.py::compute_speeds` averages across all ~1983 bounce detections including inter-rally low-velocity samples. Semantic fix: per-rally peak-at-hit ±3 frames (or 95th pct).
- **A4** Backhand 0 vs 15. Near-player heuristic in `build_silver_match_t5.py` — dump COCO keypoints for one SportAI-labeled backhand, validate wrist/shoulder x signal, check confidence floor didn't filter them out.
- **A5** 4 serves lost Pass 1→3 (`build_silver_v2.py:515-525` gate). Enable serve-diag logging, cross-ref swing_type for the 4 drops — likely `fh`/`bh` instead of `overhead`, or hitter_y just inside [0.30, 23.47].
- **A6** Wide serve bucket 0 vs 43 — bounce x-thresholds in Pass 3 likely off vs actual MATCHI geometry.
- **A7** shot_ix_in_point / rally_length — cascades from A1, verify after A1 lands.

**Phase B — Performance (only after A stable)**. 55 min → target 30-45, stretch 20.
- **B1** Per-stage timing instrumentation in `pipeline.py::_process_frame` (court / ball / MOG2 / player / SAHI) — baseline before tuning anything else.
- **B2** `PLAYER_DETECTION_INTERVAL` 5 → 8 (interpolation already bridges gaps).
- **B3** SAHI tile 640 → 800, overlap 15% → 10%.
- **B4** Skip SAHI when full-frame YOLO already has a valid far candidate.
- **B5** FFmpeg CUDA decode (`h264_cuvid`).
- **B6** YOLO frame batching (stretch; ~1 day eng).

**Phase C — Ops (non-blocking)**.
- **C1** AWS on-demand G-family vCPU quota request, both regions. Current 0-quota = Spot-only; starvation = manual region migration. See `.claude/playbook_aws_batch_ondemand_fallback.md`.
- **C2** `harness dual-submit` — one-command SportAI + T5 submission on same video; returns both task_ids + reconcile URL when done.
- **C3** `T5_DEBUG=1` env var toggle for diagnostic logging and debug-frame upload.

**Stopping condition for training**: 6 of 7 Phase A items green, wall-clock < 45 min, dual-submit tool working. Then 5 dual-submit matches → export stroke data → train → re-benchmark.

**Stroke classification readiness (unchanged)**: near-player COCO-pose heuristic works once A4 is fixed. Far-player optical-flow CNN architecture ready in `ml_pipeline/stroke_classifier/`; blocked on training data from 5+ clean dual-submit pairs.

**Lower-priority known gaps**:
- Ball delta fallback quality unvalidated (may detect racket motion, not just ball)
- TrackNetV3 weights not yet available (architecture ready in `tracknet_v3.py`)
- Calibration extrapolation bias on far image edge (see A0 note)

---

## Technique Analysis (`technique/`)

Biomechanics stroke analysis via the external SportAI Technique API. Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`. Sport type: `technique_analysis`.

### Flow

Unlike SportAI (async + URL polling) and T5 (AWS Batch + sentinel URL), the Technique API is **synchronous streaming**. A single background thread in `upload_app.py::_technique_run_pipeline()` does everything end-to-end:

```
Media Room → /api/submit_s3_task {gameType: 'technique'}
  → _technique_submit() creates task_id, spawns daemon thread:
    1. Download video from S3 (in memory, no intermediate storage)
    2. POST multipart/form-data to TECHNIQUE_API_BASE/process
    3. Read streaming JSON lines until status=done
    4. Bronze ingest → bronze.technique_* tables
    5. Silver build → silver.technique_* tables
    6. Copy video → trimmed/{task_id}/technique.mp4
    7. Mark complete (session_id + ingest_finished_at on submission_context)
    8. SES notify via existing _notify_ses_completion
```

Status tracked via standard `bronze.submission_context` columns (same as SportAI/T5). No in-memory tracker, no sentinel URL, no auto-ingest routing — `_technique_status()` just reads the DB.

### Tables

**Bronze** (`bronze.technique_*`, created by `technique/db_schema.py::technique_bronze_init()`):
- `technique_analysis_metadata` (1 row per task: uid, status, sport, swing_type, dominant_hand, height, warnings, errors)
- `technique_features` (1 row per feature: name, level, score, value, observation, suggestion, ranges, highlight_joints/limbs)
- `technique_feature_categories` (category → score, feature_names)
- `technique_kinetic_chain` (per body segment: peak_speed, peak_timestamp, plot_values)
- `technique_wrist_speed` (raw wrist_speed JSON, 1 row per task)
- `technique_pose_2d` / `technique_pose_3d` (full pose JSON blob, 1 row per task)

**Silver** (`silver.technique_*`, built by `technique/silver_technique.py::build_silver_technique()`):
- `technique_summary` — per-analysis: overall_score, level, top_strength, top_improvement
- `technique_features_enriched` — features joined with category scores + score_vs_category delta
- `technique_kinetic_chain_analysis` — peak ordering/sequencing, speed/time deltas between segments, is_sequential flag
- `technique_pose_timeline` — per-frame 2D+3D consolidated with confidence extraction
- `technique_trends` — cross-session (email-scoped): feature score history per (email, swing_type, feature_name, task_id)

**Gold** (`gold.technique_*`, created by `technique/gold_technique.py::init_technique_gold_views()` — DROP+CREATE pattern like `gold_init.py`):
- `technique_report` — per-analysis complete report (overall_score, category_scores, top_strengths/improvements, all_features as JSON arrays)
- `technique_comparison` — per-feature benchmarks (beginner/intermediate/advanced/professional ranges)
- `technique_kinetic_chain_summary` — simplified: chain_sequence, fastest/slowest segment, duration, is_sequential
- `technique_progression` — cross-session improvement (rolling_avg_5, delta_vs_prev, trend: improving/declining/stable)

### Key files

| File | Purpose |
|---|---|
| `technique/api_client.py` | `call_technique_api(video_bytes, metadata)` — streaming POST, reads JSON lines until status=done/failed |
| `technique/db_schema.py` | Bronze table DDL, idempotent |
| `technique/bronze_ingest_technique.py` | `ingest_technique_bronze(conn, payload, task_id, replace=True)` — extracts JSON into bronze tables |
| `technique/silver_technique.py` | Silver builder — same pattern as `build_silver_v2.py` |
| `technique/gold_technique.py` | Gold view DDL + `init_technique_gold_views()` |
| `technique/coach_data_fetcher.py` | Assembles technique data for LLM Coach (reads gold views) |

### Frontend

Media Room Step 3 `renderTechniqueForm()` collects: sport (currently tennis-only), swing type (12 dropdown options: forehand/backhand drive/topspin/slice, 3 serve types, 2 volleys, overhead), dominant hand toggle, height in cm (converted to mm on submit), date, location.

### Notes

- Unlike SportAI, **no intermediate S3 storage of the JSON result** — the payload stays in memory and goes straight into bronze ingest.
- Swing type list in the form is currently hardcoded; spec says to fetch dynamically from API when available.
- Pickleball sport is recognised by the API but out of scope for this build.
- Video trim is a simple `s3.copy_object` to `trimmed/{task_id}/technique.mp4` — no EDL, no FFmpeg (technique videos are 3-10s).

---

## Other

- **`docs/`**: `llm_coach_design.md` (LLM Tennis Coach spec, ~400 lines). Any new feature-level design docs live here.
- **`migrations/`**: One-off backfill SQL scripts. No migration framework — schema is managed idempotently via `db_init.py` + `gold_init.py` + per-module `ensure_*` functions.
- **`superset/`**: Optional Superset BI deployment config. Not in `render.yaml`.
- **`_archive/`**: Deprecated/replaced code kept for reference.
- **`lambda/`**: AWS Lambda source (e.g., S3 trigger for ML pipeline).
- **`.claude/`**: Local Claude Code settings (git-ignored).
