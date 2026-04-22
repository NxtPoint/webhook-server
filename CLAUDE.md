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

The Locker Room service serves HTML SPAs from `frontend/` via `send_file()` — Flask + gunicorn only, no DB access. Routes: `/` (locker room dashboard), `/media-room` (upload wizard), `/register`, `/backoffice`, `/portal` (entry point for Wix), `/pricing`, `/coach-accept`, `/practice`, `/match-analysis` (primary match dashboard), plus public marketing pages `/home`, `/how-it-works`, `/pricing-public`, `/for-coaches`. The main webhook-server serves all of them as same-origin backups for API access from within iframes.

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

**`frontend/`** — all SPA HTML pages. Served by `locker_room_app.py` and (same-origin backups) `upload_app.py` via a `_html(name)` helper that resolves an absolute path under `frontend/`:

- Authenticated app: `locker_room.html`, `media_room.html`, `portal.html` (nav shell / Wix entry point), `backoffice.html`, `pricing.html`, `coach_accept.html`, `players_enclosure.html` (register wizard), `practice.html`, `match_analysis.html`
- Public marketing: `home.html`, `how_it_works.html`, `pricing_public.html`, `for_coaches.html`

**`docs/`** — design docs and strategy specs (`pricing_strategy.md`, `llm_coach_design.md`). Source of truth for business rules. Code links back to section numbers (e.g. "see docs/pricing_strategy.md §6").

**Known stale files at root** (audited 2026-04-19, candidates for deletion; none are imported anywhere outside `.claude/worktrees/`): `build_silver_point_detail.py` (replaced by `build_silver_v2.py`), `bronze_json_schema.py`, `inspect_bronze_blobs.py`, `probes.py`, `test_silver_diagnostics.py`.

---

## T5 ML Pipeline (`ml_pipeline/`)

In-house tennis video analysis pipeline. Runs on AWS Batch GPU (Spot G4dn.xlarge) for detection; runs on Render for serve detection + silver build. Handles `tennis_singles_t5`, `serve_practice`, `rally_practice`. Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`.

**All operational detail (architecture, how-to-run, validation, Docker/Batch deploy, training, file index, session log, current task IDs, known gaps) lives in `.claude/handover_t5.md`.** Read that file at the start of any T5 session — it's the single source of truth.

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
| `ml_pipeline/training/` | TrackNet fine-tuning on dual-submit labels |
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
