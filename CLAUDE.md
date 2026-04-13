# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Power BI has been REMOVED.** The custom-built dashboards (`match_analysis.html`, `practice.html`) on the gold presentation layer are the single source of truth for all analytics. The `powerbi-service` Render service, `powerbi_app.py`, `powerbi_embed.py`, `azure_capacity.py`, `powerbi_capacity_sessions.py`, and `analytics.html` have been deleted. All `PBI_*` / `AZ_*` env vars should be removed from Render environment settings. The `pbi_refresh_*` columns remain in `bronze.submission_context` (harmless, no code writes to them). `gold.vw_client_match_summary` is still live (feeds match list sidebar).

## Services and How to Run

Python 3.12 / Flask + Gunicorn, deployed on Render (see `render.yaml`):

| Service | Start command | Entry point | Status |
|---|---|---|---|
| **Main API** (`webhook-server`) | `gunicorn wsgi:app` | `wsgi.py` → `upload_app.py` | Active |
| **Ingest worker** | `gunicorn ingest_worker_app:app` | `ingest_worker_app.py` | Active |
| **Video trim worker** | Docker (`Dockerfile.worker`) | `video_pipeline/video_worker_wsgi.py` | Active |
| **Locker Room** (static) | `gunicorn locker_room_app:app` | `locker_room_app.py` | Active |
| ~~Power BI service~~ | ~~deleted~~ | ~~deleted~~ | **REMOVED** |

The Locker Room service serves eleven HTML SPAs via `send_file()`. No DB access — only Flask + gunicorn installed, not the full `requirements.txt`:

- `GET /` → `locker_room.html` (dashboard)
- `GET /media-room` → `media_room.html` (video upload wizard)
- `GET /register` → `players_enclosure.html` (onboarding)
- `GET /backoffice` → `backoffice.html` (admin)
- `GET /portal` → `portal.html` (unified nav shell — the **entry point** for Wix)
- `GET /pricing` → `pricing.html`
- `GET /coach-accept` → `coach_accept.html`
- `GET /practice` → `practice.html` (practice analytics dashboard)
- `GET /match-analysis` → `match_analysis.html` (**the primary match dashboard**)
- ~~`GET /analytics`~~ (PBI embed — **REMOVED**)

The main webhook-server also serves all of these as same-origin backups (for API access from within iframes).

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

**Gold** (`gold.*`): Presentation layer. Thin views — **one per chart or one per widget** — that aggregate silver into exactly the shape the frontend needs. No Python/JS aggregation downstream. Same views feed dashboards, LLM coach, and legacy PBI.

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

On upload completion:
1. **Media Room** uploads video to S3, submits via `POST /api/submit_s3_task`
2. **Main app** routes to SportAI or T5 based on `gameType` → `sport_type`
3. Main app polls status until complete
4. On completion, main app POSTs to **ingest worker** `/ingest` (returns 202)
5. Ingest worker runs: bronze ingest → silver build → video trim trigger → billing sync
6. **Video worker** trims footage → callback updates `trim_status`
7. **Customer notification**: SES email → "Your match analysis is ready" (idempotent via `ses_notified_at`)
8. User opens `/portal` → `/match-analysis` → gold views serve pre-aggregated data

**Key design**: the ingest worker is self-contained — it does NOT import `upload_app.py`. It calls `ingest_bronze_strict()` directly. Worker timeout 3600s vs main app 1800s.

### Main App (`upload_app.py`)

Primary service. Responsibilities:
- S3 presigned URL generation (single-part + multipart upload/GET)
- S3 multipart lifecycle
- SportAI + T5 Batch submission — routed by `sport_type`
- Task status orchestration, auto-ingest triggering
- Video trim callback
- Customer notification (SES + Wix legacy)
- CORS preflight for all `/api/client/*` paths

**Registered blueprints**: `coaches_api`, `members_api`, `subscriptions_api`, `usage_api`, `entitlements_api`, `client_api`, `coach_accept`, `ml_analysis_bp`, `ingest_bronze`, `tennis_coach.coach_api`.

**On-boot init** (idempotent, each try/except-wrapped so one failure can't kill the service):
1. `gold_init_presentation()` — creates gold.vw_player, gold.vw_point, gold.match_* views
2. `init_tennis_coach()` — creates gold.coach_* views + tennis_coach.coach_cache table

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

The primary analytics experience. Custom-built ECharts + canvas dashboards that read from thin SQL views. Replaces Power BI entirely.

### The Dashboard (`match_analysis.html`)

Single-page app at `/match-analysis`. Loaded inside the portal iframe with `?email=&key=&api=` auth params.

Four modules, selectable via the green module strip at the top:

**Match Analytics module** (9 tabs):
1. **Match Summary** — score box, KPI strip, head-to-head comparison bars (points won, service/return/rally pts won, 1st serve %, aces, DFs, winners, errors, games won, service/return games won), free points + return pts won + rally pts won donut pairs, aggression profile (Attack/Neutral/Defence horizontal stacked bar per player), speed profile (1st Serve / 2nd Serve / FH / BH quad gauge per player), points-by-phase chart, outcome distribution
2. **Serve Performance** — full comparison table (UTR, total serves, svc pts won, serve in%, won%, DFs, unreturned, avg/fastest speed), serve direction bars, outcomes by direction (won/lost stacked), unreturned by direction, Deuce/Ad strategy tables
3. **Serve Detail** — per-player compare bars (svc pts played/won/won%, 1st/2nd serve in%/win%, DFs), 1st serve location × win rate grouped bars, 2nd serve location × win rate grouped bars
4. **Return Summary** — full comparison table, return effectiveness by hit zone (A/B/C/D), return points won by bounce zone (A/B/C/D)
5. **Return Detail** — per-player H2H card, depth bars, stroke pies, outcome bars, return outcomes by serve try / stroke / serve location (stacked bars from shot_placement)
6. **Rally Summary** — full comparison table (pts played/won/lost/won%/lost%/winner%/error%/W:E), rally effectiveness by zone
7. **Rally Detail** — rally H2H card, length distribution, bucket win comparison, aggression/depth/stroke per player, overall rally outcome donuts, depth distribution pies
8. **Point Analysis** — match result summary, how points won/lost pie charts (aces/winners/errors/DFs), net position by phase table, winners & errors by stroke table, zone effectiveness tables
9. *(Coach moved to own module)*

**Placement Heatmaps module** (5 tabs):
1. **Serve Placement** — blue court with green surround, dots on near side coloured by point outcome (green=won, red=lost), filter 1st/2nd serve, Deuce/Ad strategy tables
2. **Player Return Position** — hit coordinates (where player stood), Deuce/Ad split table by received side × stroke
3. **Return Ball Position** — bounce coordinates (where return landed), received side × stroke table
4. **Groundstrokes** — rally shots with stroke + depth filters, depth split into Deep/Middle/Short column tables
5. **Rally Player Position** — hit coordinates with aggression filter, aggression breakdown table

Each heatmap tab has:
- **Player toggle** (Player A = green, Player B = blue — enforced convention)
- **Set filter** (appears when match has 2+ sets; cross-filters across tabs)
- **Tab-specific filters** (serve try, stroke, depth, aggression as appropriate)
- **Right-side info panel** ("How to read this") — collapsed by default
- **Near-side plotting** — dots plot on the near half of the court using normalised coordinates
- **Blue court surface** (`#1a4a8a`) with green surround (`#2d6a4f`), ~2m side / ~4m baseline padding

**Player Performance module** (3 tabs):
1. **KPI Scorecard** — 18 KPIs across 5 categories (Serve 7, Return 2, Rally 4, Games 2, Speed 2). Each row shows: KPI name, benchmark target, rolling 5-match avg, delta vs benchmark, status dot (green/amber/red), trend arrow (improving/neutral/declining), SVG sparkline trendline. Expert-judgment benchmarks for club-level players.
2. **Trend Charts** — ECharts line chart per KPI with benchmark target dashed line. Shows last 10 matches.
3. **Last Match vs Average** — horizontal grouped bar chart comparing last match values to 5-match rolling average.
Player A only (the customer). Focus is improvement, not winning/losing. Data from `gold.player_performance` (email-scoped, cross-match).

**AI Coach module** (standalone):
Elevated from a tab inside Match Analytics to its own top-level module. See [LLM Coach](#llm-tennis-coach) below.

**Cross-module features:**
- **Sidebar** — match list with collapse toggle (280px → 46px). Default collapsed on tablet (<1200px).
- **Cross-filter persistence** — Player, Set, and filter selections persist within heatmaps. Reset on match change.
- **T5 filtering** — `gold.vw_player` and `gold.vw_client_match_summary` filter to `sport_type = 'tennis_singles'` only. T5 dev matches excluded from customer-facing views.

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

On load, `match_analysis.html::selectMatch()` fires all six match endpoints in parallel via `Promise.all()` and caches as `selectedData.kpi / .serve / .return / .rally / .rallyLength / .placement`. The performance endpoint is fetched lazily when the Player Performance module is first opened.

Other dashboard endpoints:
- `/api/client/matches` — match list for sidebar (uses `gold.vw_client_match_summary`, filtered to `sport_type = 'tennis_singles'`)
- `/api/client/matches/<task_id>` — legacy raw silver.point_detail fetch
- `/api/client/match-analysis/<task_id>` — legacy full silver fetch

### LLM Tennis Coach

Package: `tennis_coach/`. Blueprint registered on webhook-server. Documented in `docs/llm_coach_design.md`. Elevated to its own module in the dashboard (4th module tab, after Player Performance).

**Endpoints** (CLIENT_API_KEY auth):
- `POST /api/client/coach/analyze` — named prompt or freeform question. Body: `{task_id, email, prompt_key, freeform_text?}`. Returns Claude's response + `data_snapshot` (the JSON passed to Claude, for trust validation).
- `GET /api/client/coach/cards/<task_id>?email=` — pre-generated 3-card insight summary for the match. Cached forever per (task, email). First call generates, subsequent calls are free.
- `GET /api/client/coach/status/<task_id>?email=` — poll for card generation status.
- `GET /api/client/coach/debug/<task_id>?email=` — **admin only**. Returns the raw JSON payload the data fetcher would send to Claude, without calling Claude. Critical for trust validation ("does my dashboard match what Claude sees").

**Data flow**:
1. `tennis_coach/data_fetcher.py::fetch_match_data(task_id)` reads from `gold.match_kpi`, `gold.match_serve_breakdown`, `gold.match_rally_breakdown`, `gold.match_return_breakdown`, `gold.coach_rally_patterns`. Assembles a compact nested dict.
2. Suppresses any dimension where shot count < 5 (MIN_SAMPLE) — prevents Claude from citing unreliable stats.
3. `tennis_coach/prompt_builder.py` builds the (messages, system) tuple for one of 5 templates (serve_analysis / weakness / tactics / cards / freeform).
4. `tennis_coach/claude_client.py` calls Anthropic SDK: `claude-sonnet-4-6`, temperature 0.3, max 600 output tokens.
5. Response cached in `tennis_coach.coach_cache` table keyed on (task_id, email, prompt_key).

**Rate limits** (`tennis_coach/rate_limiter.py`):
- 5 freeform calls per (email, task_id) per calendar day
- 20 freeform calls per email per day across all matches
- Cards excluded from rate limits (one-shot per match, cached forever)
- 429 with `resets_at` on limit hit

**Cost**: ~$0.01 per call (Claude Sonnet 4.6: $3/M input + $15/M output). ~1,200-1,500 tokens per call. Realistic usage: $5-20/month.

**Required env var**: `ANTHROPIC_API_KEY` on webhook-server.

**Player-only guardrails**: System prompt restricts coaching to Player A only. The coach NEVER analyses the opponent's game, weaknesses, or how to "beat" them. If asked about the opponent, it redirects: "My job is to make YOU better — let's focus on what you can control." The tactics prompt was rewritten from "exploit opponent weaknesses" to "what should you improve." Both the standard and cards system prompts enforce this.

**Anti-hallucination**: data is pre-aggregated SQL numbers (not raw rows), small-sample suppression, low temperature, system prompt forbids fabrication. The `/debug/<task_id>` endpoint lets us verify the data shape Claude sees matches the dashboard.

**Credit integration**: NOT yet implemented. Currently rate-limited only (5/day per match, 20/day per email). Credit burn-down per coach interaction is planned — will require `billing_service.consume_entitlement()` integration.

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
| ~~`GET /api/client/pbi-embed` / `pbi-heartbeat` / `pbi-session-end`~~ | **DEPRECATED** (PBI retirement) |

## Locker Room SPAs

All auth via URL params forwarded through the portal: `?email=&firstName=&surname=&wixMemberId=&key=&api=`.

**Design system**: all pages share CSS variables, Inter font, green/amber/red palette, `.toggle-group` / `.toggle-btn` buttons, ECharts helpers (`eBar`, `eStackedBar`, `ePie`, `eGauge`) defined identically in every file.

- **Locker Room** (`/`): dashboard. Header tabs (Account / My Details / Linked Players / Invite Coach), charts (matches per month, usage gauge), match history.
- **Media Room** (`/media-room`): 4-step upload wizard (game type → upload → details → progress).
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

**Legacy (Power BI — to be removed)**: `POWERBI_SERVICE_BASE_URL`, `POWERBI_SERVICE_OPS_KEY`, `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_CLIENT_SECRET`, `PBI_WORKSPACE_ID`, `PBI_REPORT_ID`, `PBI_DATASET_ID`, and all `AZ_*` vars on the `powerbi-service` service. Delete when `powerbi-service` is removed from `render.yaml`.

### Other Services

- **Ingest Worker**: `INGEST_WORKER_OPS_KEY` (required — startup crash), `DATABASE_URL`, `VIDEO_WORKER_*` for trim trigger. PBI-related vars no longer needed (service no longer triggers PBI refresh).
- **Video Trim Worker** (Docker): `VIDEO_WORKER_OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, AWS credentials. FFmpeg tunables: `VIDEO_CRF=28`, `VIDEO_PRESET=veryfast`, `FFMPEG_TIMEOUT_S=1800`.
- **Locker Room**: `PORT=5050` only. No DB or S3.
- **Cron `cron_capacity_sweep.py`**: `OPS_KEY`, `DATABASE_URL`, `INGEST_STALE_S=1800`, `TRIM_STALE_S=1800`. PBI sweep (`PBI_REFRESH_STALE_S`) becomes a no-op once PBI is removed.
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

**`court_detector.py`** — CNN (14 keypoints) + Hough-lines fallback. CNN keypoints extracted via threshold 170 + Hough circles + `refine_kps()` line-intersection snap (matching yastrebksv/TennisProject reference). Calibration lock: best detection in first 300 frames, then frozen. `get_court_corners_pixels()` returns 4 baseline corners for player scoring geometry.

**`ball_tracker.py`** — TrackNet V2 (3-frame, 9ch) with frame-delta Hough fallback for missed frames. V2 detects ~36% of frames; delta fallback recovers ~63%. Three-tier heatmap extraction: Hough circles → CC centroid → argmax. TrackNetV3 architecture ported in `tracknet_v3.py` (U-Net, 8-frame + background median = 27ch, sigmoid output) — activates when `models/tracknet_v3.pt` exists.

**`player_tracker.py`** — Multi-strategy detection + three-tier court-geometry scoring:
- **Detection**: SAHI tiled inference (416×416 overlapping tiles, `sahi==0.11.18`) + full-frame YOLOv8x-pose + detection-only YOLOv8m for far baseline. Falls back to manual 3-pass if SAHI unavailable.
- **Scoring** (`_choose_two_players`): One player from each half (near/far). When court corners available:
  - Tier 1 (3000): Inside court quadrilateral (`cv2.pointPolygonTest`)
  - Tier 2 (2000): Behind baseline, within sideline extensions (±20% depth, ±15% width)
  - Tier 3 (1000): Near sideline corridor (baseline-to-net)
  - Tier 0: Off-court
  - Tiebreakers: MOG2 motion (+500), bbox area (0-200, bigger = closer to camera), center-line proximity (0-100)
  - Falls back to centering + motion heuristic when no court corners
- **MOG2 background subtraction** (`pipeline.py`): foreground mask fed every frame, motion ratio computed per candidate bbox. Moving player > stationary spectator.

**`pipeline.py`** — Orchestrates court → ball → MOG2 → player per frame. Post-processing: interpolation, bounce detection, speed calc, stationary player filter.

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

- SportAI: `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` (88 rows, 17 points, 2 games, 24 serves)
- T5 baseline: track in `golden_datasets.json` once metrics stable

### Stroke classification (`build_silver_match_t5.py`)

**Near player** (200-400px, has pose keypoints): Four-tier heuristic from COCO keypoints — serve (arm raised at baseline), overhead (arm raised mid-court), volley (near net + compact arm), forehand/backhand (wrist position relative to shoulders, three signal tiers). Handles both handedness.

**Far player** (30-40px, no pose): Currently defaults to "Other". Planned: optical flow classifier on bbox crop ±5 frames around hit events, trained on SportAI labels from dual-submit pairs. Research recommends OpenCV Farneback flow → small CNN, targeting 75-85% accuracy. See `memory/project_far_player_stroke_research.md`.

### Known gaps

- Ball delta fallback quality unvalidated (may detect player movement, not just ball)
- TrackNetV3 weights not yet available (architecture ready in `tracknet_v3.py`)
- Far-player stroke classification not yet implemented (optical flow approach planned)
- Speed calculation underestimates ~50% vs SportAI

---

## Other

- **`docs/`**: `llm_coach_design.md` (LLM Tennis Coach spec, ~400 lines). Any new feature-level design docs live here.
- **`migrations/`**: One-off backfill SQL scripts. No migration framework — schema is managed idempotently via `db_init.py` + `gold_init.py` + per-module `ensure_*` functions.
- **`superset/`**: Optional Superset BI deployment config. Not in `render.yaml`.
- **`_archive/`**: Deprecated/replaced code kept for reference.
- **`lambda/`**: AWS Lambda source (e.g., S3 trigger for ML pipeline).
- **`.claude/`**: Local Claude Code settings (git-ignored).
