# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Services and How to Run

This repo defines five Render services (see `render.yaml`). All are Python 3.12.3 / Flask + Gunicorn:

| Service | Start command | Entry point |
|---|---|---|
| Main API (webhook-server) | `gunicorn wsgi:app` | `wsgi.py` → `upload_app.py` |
| Ingest worker | `gunicorn ingest_worker_app:app` | `ingest_worker_app.py` |
| Power BI service | `gunicorn powerbi_app:app` | `powerbi_app.py` |
| Video trim worker | Docker (see `Dockerfile.worker`) | `video_pipeline/video_worker_wsgi.py` → `video_pipeline/video_worker_app.py` |
| Locker Room | `gunicorn locker_room_app:app` | `locker_room_app.py` (serves HTML SPAs, no DB) |

The Locker Room service serves seven pages:
- `GET /` → `locker_room.html` (dashboard)
- `GET /media-room` → `media_room.html` (video upload)
- `GET /register` → `players_enclosure.html` (onboarding wizard)
- `GET /backoffice` → `backoffice.html` (admin dashboard)
- `GET /analytics` → `analytics.html` (Power BI embed)
- `GET /portal` → `portal.html` (unified nav shell — main entry point for Wix)

The main webhook-server also serves `/media-room`, `/backoffice`, `/analytics`, and `/portal` as same-origin backups for API access.

Note: The Locker Room service only installs `flask` + `gunicorn` (not full `requirements.txt`).

**Local dev:**
```bash
# Activate venv
source .venv/Scripts/activate  # Windows bash

# Install deps
pip install -r requirements.txt

# Run main app locally (requires DATABASE_URL, OPS_KEY, S3_BUCKET, etc.)
gunicorn wsgi:app --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 1800

# Run video worker locally (requires VIDEO_WORKER_OPS_KEY)
gunicorn video_pipeline.video_worker_wsgi:app --bind 0.0.0.0:8001
```

**Manual integration smoke test** (requires live DB connection):
```bash
python video_pipeline/test_video_timeline.py
```

**Bronze JSON schema explorer** (requires live DB):
```bash
python bronze_json_schema.py <session_id>
```

**Silver diagnostics** (requires live DB):
```bash
python test_silver_diagnostics.py
```

### Testing & Code Quality

No automated test suite, CI pipeline, or linter is configured. All testing is manual against the live Render database. There is no pytest, no conftest, no test runner. Do not attempt to run `pytest` — it will find nothing useful.

Schema DDL is split across multiple files: `db_init.py` (bronze tables, called on boot), `_ensure_member_profile_columns()` in `client_api.py` (billing columns, runs on import), and `_ensure_submission_context_schema()` in `upload_app.py`. These all use idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` or `CREATE TABLE IF NOT EXISTS` patterns.

## Architecture Overview

### Service Topology & Data Flow

On upload completion, the system follows this flow:
1. **Media Room** uploads video to S3, submits to SportAI via `POST /api/submit_s3_task`
2. **Main app** polls SportAI status until complete
3. Main app POSTs to **ingest worker** `/ingest` (returns 202)
4. Ingest worker runs full pipeline: bronze ingest → silver build → video trim trigger → billing sync → PBI refresh
5. Video worker trims footage, POSTs callback to `/internal/video_trim_complete` → `trim_status` = `completed`
6. **Locker Room** displays match data + trimmed footage playback

Key design: the ingest worker is self-contained — it does NOT import `upload_app.py`. It calls `ingest_bronze_strict()` directly from `ingest_bronze.py` (function call, not HTTP). Worker timeout is 3600s vs main app 1800s.

### Data Layers (PostgreSQL)

- **Bronze** (`bronze.*`): Raw SportAI JSON ingested verbatim. `db_init.py` owns schema creation (idempotent, called on boot). Key tables: `raw_result`, `submission_context`, `player_swing`, `rally`, `ball_position`, `ball_bounce`, `player_position`.
- **Silver** (`silver.*`): Structured/normalized data. `silver.point_detail` is the key table consumed by the video timeline and client API. Built by `build_silver_v2.py` (5-pass SQL approach; `build_silver_point_detail.py` is the legacy Python-based version kept for reference).
- **Gold**: Materialized view tables (`point_log_tbl`, `point_summary_tbl`) built on demand via `/ops/build-gold`. `gold.vw_client_match_summary` is consumed by the Locker Room client API.
- **Billing** (`billing.*`): Separate schema for credit-based usage tracking. See Billing System below.

Architecture rule: **Python owns all business logic; SQL is only for I/O** (enforced in `build_video_timeline.py`).

### Silver V2 (`build_silver_v2.py`)

Current prod implementation. 5-pass SQL pipeline:
1. Insert from `player_swing` (core fields)
2. Update from `ball_bounce` (bounce coordinates)
3. Serve detection + point/game structure + exclusions
4. Zone classification + coordinate normalization
5. Analytics (serve buckets, stroke, rally_length, aggression, depth)

Court geometry constants live in `SPORT_CONFIG` dict at top of file.

### Main App (`upload_app.py`)

The primary service. Responsibilities:
- S3 presigned URL generation (single-part + multipart upload, GET)
- S3 multipart lifecycle: `initiate`, `presign-part`, `list-parts`, `complete`, `abort`
- SportAI job submission (`POST /api/statistics/tennis`) and status polling
- Task status orchestration: auto-ingest trigger, PBI refresh polling, Wix notify
- Video trim callback (`POST /internal/video_trim_complete`)
- CORS preflight handling (global `before_request` for OPTIONS on all client/upload paths)
- Wix backend notification on completion (legacy — will be removed when Wix is retired)

Registered blueprints: `coaches_api`, `members_api`, `subscriptions_api`, `usage_api`, `entitlements_api`, `client_api`, `ml_analysis_bp`, and `ingest_bronze` (mounted at root).

### Video Trim Pipeline

Fire-and-forget async flow:
1. **Ingest worker** calls `trigger_video_trim(task_id)` in `video_pipeline/video_trim_api.py`
2. Builds EDL (Edit Decision List) from `silver.point_detail` via `build_video_timeline_from_silver()`
3. POSTs to the **video worker** service at `VIDEO_WORKER_BASE_URL/trim`
4. **Video worker** (`video_pipeline/video_worker_app.py`) accepts the request, spawns a detached subprocess, returns 202 immediately
5. Subprocess: downloads source from S3, FFmpeg re-encodes keep segments, concatenates, uploads `trimmed/{task_id}/review.mp4` to S3
6. Worker POSTs callback to `VIDEO_TRIM_CALLBACK_URL` with status + output S3 key (authenticated via `VIDEO_TRIM_CALLBACK_OPS_KEY` as Bearer token)

State is tracked in `bronze.submission_context.trim_status` (`queued` → `accepted` → `completed`/`failed`).

**Critical env var**: `VIDEO_TRIM_CALLBACK_OPS_KEY` on the ingest worker must match `OPS_KEY` on the main API — otherwise the callback returns 401.

### Billing System

Credit-based usage tracking in the `billing` schema. Core files: `billing_service.py` (logic), `models_billing.py` (ORM), `billing_import_from_bronze.py` (sync pipeline).

Tables: `Account`, `Member`, `EntitlementGrant`, `EntitlementConsumption`. Views: `billing.vw_customer_usage`.

Key patterns:
- 1 task = 1 match consumed (idempotent by `task_id` unique constraint)
- Entitlement grants are idempotent by `(account_id, source, plan_code, external_wix_id)`
- `billing_import_from_bronze.py` syncs completed tasks from `bronze.submission_context` into billing consumption records, auto-creating accounts from email + customer_name if missing
- `entitlements_api.py` gates uploads on remaining credit check

**`billing.member` is the single source of truth for all customer/player/child/coach profile data.** Every client-facing page (Locker Room, Media Room, Players' Enclosure) reads from and writes back to this one table. Match-level data (`player_a_name`, `player_b_name` etc.) is stored separately in `bronze.submission_context` as point-in-time snapshots — this is by design so that editing a player's name doesn't rewrite historical match records. Player A dropdowns in match edit forms pull member names from `billing.member`, but the selected value is written to `bronze.submission_context`.

**Member profile columns** on `billing.member`: `surname`, `phone`, `utr`, `dominant_hand`, `country`, `area`, `dob`, `skill_level`, `club_school`, `notes`, `profile_photo_url`. All fields are viewable and editable from the Locker Room Linked Players section. Added idempotently via `_ensure_member_profile_columns()` in `client_api.py` (runs on import).

API blueprints: `subscriptions_api`, `usage_api`, `entitlements_api`.

### Client API (`client_api.py`)

Backend for all client-facing SPAs (Locker Room, Media Room, Players' Enclosure). Uses separate auth: `X-Client-Key` header checked against `CLIENT_API_KEY` env var (not OPS_KEY). CORS headers manually injected for `/api/client/*` routes.

Key endpoints:
- `GET /api/client/matches` — list matches with stats, scores, trim status, footage keys
- `GET /api/client/players` — distinct player names for autocomplete
- `GET /api/client/matches/<task_id>` — point-level detail from silver
- `PATCH /api/client/matches/<task_id>` — update match metadata
- `POST /api/client/matches/<task_id>/reprocess` — rebuild silver via `build_silver_v2`
- `GET /api/client/profile` — primary member profile
- `PATCH /api/client/profile` — update profile fields on `billing.member`
- `GET /api/client/usage` — account usage summary (matches granted/consumed/remaining)
- `GET /api/client/footage-url/<task_id>` — time-limited S3 presigned URL for trimmed match footage
- `GET /api/client/entitlements` — entitlement check (role, plan_active, credits_remaining, account_status, plans_page_url). Handles missing `billing.subscription_state` table gracefully.
- `GET /api/client/members` — all active members on an account (full profile fields)
- `POST /api/client/members` — add a linked player (child or coach)
- `PATCH /api/client/members/<id>` — update a linked member's profile
- `DELETE /api/client/members/<id>` — soft-delete (sets `active=false`, preserves history)
- `POST /api/client/register` — onboarding registration
- `POST /api/client/children` — add child member profiles (Players' Enclosure onboarding)
- `GET /api/client/profile-photo-upload-url` — presigned S3 PUT URL for profile photo
- `GET /api/client/pbi-embed` — Power BI embed token (proxies to PBI service: session/start + embed/config + embed/token)
- `POST /api/client/pbi-heartbeat` — keep PBI capacity session alive
- `POST /api/client/pbi-session-end` — end PBI capacity session on page unload
- `GET /api/client/backoffice/pipeline` — admin: pipeline status table (task/stage tracking)
- `GET /api/client/backoffice/customers` — admin: customer list with usage/subscription stats
- `GET /api/client/backoffice/kpis` — admin: KPI cards (active accounts, tasks today/month/all-time, credits)

Admin endpoints require email in `ADMIN_EMAILS` whitelist (hardcoded set in `client_api.py`): `info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`.

### Locker Room (`locker_room.html`)

Dashboard SPA embedded as Wix iframe. Auth via URL params: `?email=...&key=...&api=...`.

**Page layout (top to bottom):**
1. **Header** — TEN-FIFTY5 logo + tabbed sections:
   - **Account tab** (default) — name, email, account status, subscription status, matches remaining (usage pill), matches used/granted, role, registration date. Read-only. Data from `billing.account`, `billing.member`, `billing.vw_customer_usage`, and `/api/client/entitlements`.
   - **My Details tab** — editable profile: first name, surname, email (read-only), mobile, UTR, dominant hand, country, area. Save button PATCHes `/api/client/profile`.
   - **Linked Players tab** — cards for each non-primary member (children/coaches). Each card is individually collapsible with editable fields matching My Details. "Deactivate" soft-deletes (keeps history). "+ Add Player" inline form.
   - **Invite Coach tab** — placeholder (coming soon). Will allow owners to invite coaches by email. See "Coach Invite Flow" section below.
2. **Charts** — 70/30 grid: matches per month line chart | usage gauge
3. **Latest Match** — hero card inside a white block. Shows player names, date, location, score, key stats (points, games, aces, avg rally, duration). "Watch Footage" button opens modal HTML5 video player (or "Processing..." badge). Entire card clickable to open edit panel.
4. **Match History** — single white card block. Year headers → month headers (indented) → match rows (indented further). Years and months newest first. Matches within a month sort latest to oldest. Each row shows Player A vs Player B, date, location, status badge, score, play icon (footage), edit button.

**Edit panel** (slide-in from right): match stats grid, then editable fields — Player A (dropdown of active account members only), Player A UTR, Player B (free text), Player B UTR, match date, venue, "First Point: Player A was..." (Server/Returner toggle buttons matching Media Room), score (3 sets), start time offset. Save + Reprocess buttons.

**Video modal**: fullscreen overlay player, shared between hero card and match row play icons. Fetches presigned URL from `/api/client/footage-url/<task_id>`.

**Entitlement guards**: coach role shows view-only notice; exhausted credits show dismissible banner linking to plans page (`PLANS_PAGE_URL` env var).

**Design system**: all pages share the same CSS variables, Inter font, green/amber/red colour palette. Toggle buttons (`.toggle-group` / `.toggle-btn`) are identical between Locker Room and Media Room.

### Media Room (`media_room.html`)

Video upload page replacing the Wix-based upload flow. Served at `GET /media-room` from the Locker Room service. Also served at `GET /media-room` from the main webhook-server (backup/same-origin for upload APIs). Auth via URL params: `?email=...&key=...&api=...`. API_BASE defaults to `https://api.nextpointtennis.com` if `?api=` is omitted.

**4-step wizard flow:**
1. **Game Type Selection** — Singles (active), Technique Session / Doubles Training / Serve Practice (coming soon). `getFormConfig(gameType)` stub for future game types.
2. **Video Upload** — Chunked multipart upload directly to S3 via presigned URLs. 10 MB chunks, 3 retries + exponential backoff. Browser Wake Lock API prevents screen sleep on mobile (graceful fallback with warning, re-acquires on `visibilitychange`). Resumable: upload state (uploadId, key, completed parts, file identity) persisted in `localStorage` with 24h expiry. On page load, checks for interrupted uploads and offers Resume/Discard. Progress shows: % bar, chunk counter, upload speed (MB/s), ETA. After all chunks uploaded, calls `POST /upload/api/multipart/list-parts` for reliable server-side ETag retrieval, then completes the multipart upload.
3. **Match Details Form** — Player A dropdown from `/api/client/members` (account members only, shows `full_name + surname`). Same fields as Locker Room edit panel: Player A UTR, Player B (opponent), Player B UTR, match date, location, first server (Server/Returner toggle), start time offset, score (inline grid: player name + 3 set boxes per row, names update live). Submit calls `POST /api/submit_s3_task` then PATCHes `first_server` to `player_a`/`player_b` format.
4. **Analysis Progress** — Polls `GET /upload/api/task-status` every 5s. Progress bar uses raw `sportai_progress_pct` (0-100%, no artificial stage-based jumps). Customer-friendly transaction log with timestamped entries. Cancel button calls `POST /upload/api/cancel-task`. On completion: success card with auto-built Locker Room link. On failure: error card with full reference ID and retry button.

**Entitlement gate**: calls `/api/client/entitlements` on load. Blocks coaches (view-only message), no-plan users (link to plans page), zero-credit users (link to buy more). Upload API endpoints also enforce server-side via `_upload_entitlement_gate(email)`.

**CORS**: `upload_app.py` has a global `before_request` handler for OPTIONS preflight on all CORS-enabled paths (`/api/client/*`, `/upload/api/*`, `/api/submit_s3_task`, `/media-room`). The S3 bucket CORS must include the Locker Room Render domain for direct browser-to-S3 uploads.

### Players' Enclosure (`players_enclosure.html`)

Member registration/onboarding page. Served at `/register`. On load, fetches `/api/client/profile` to check if the user already exists:
- **Existing user** (profile found): shows "Your Profile — already set up" summary with a "Go to Locker Room" button. Registration is one-time only.
- **New user** (no profile): runs the multi-step wizard: Welcome → Role Selection (Player/Parent Solo, Parent with Children, Coach) → Child Profiles (conditional for "Parent with Children") → Completion + optional profile photo upload (S3 presigned PUT).

New users' names are pre-populated from Wix handoff data (postMessage or URL params). The page never asks the user to re-enter name or email.

### Backoffice Dashboard (`backoffice.html`)

Admin-only SPA served at `/backoffice`. Auth: same `X-Client-Key` as client API, plus email must be in `ADMIN_EMAILS` whitelist (hardcoded in `client_api.py`). Sections: KPI cards (tasks today/month, success rates, active accounts/subs, credits), monthly trend bar chart (12 months), credit utilisation gauge, pipeline monitor tab (per-task stage tracking with date filters), customer tab (usage/subscription stats per account). Uses the same design system as other SPAs.

### Analytics (`analytics.html`)

Power BI embed page served at `/analytics`. Fetches embed token from `GET /api/client/pbi-embed` (which proxies to the PBI service: session/start → embed/config → embed/token with RLS by email). Uses `powerbi-client@2.23.1` JS library. Auto-layout (FitToPage/FitToWidth based on viewport). Heartbeat every 60s keeps Azure capacity alive. Session ends on page unload via `fetch` with `keepalive: true`.

### Portal (`portal.html`)

Unified navigation shell served at `/portal`. **This is the main entry point for Wix embedding.** One Wix page → one iframe → `/portal?email=...&key=...&api=...`.

Collapsible sidebar with navigation: Dashboard (/), Upload (/media-room), My Profile (/register), Analytics (/analytics), Backoffice (/backoffice, admin only). Content pages load in an inner iframe with auth params forwarded via `authParams()`. Sidebar state persists in `localStorage`.

**Inter-page navigation**: child pages can send `postMessage({ type: 'portal-navigate', target: 'dashboard' })` to navigate within the portal instead of breaking out of the iframe. The portal also forwards `wix-handoff` postMessages to the active content iframe.

Profile fetch on init shows user name in sidebar footer with connection status indicator (Connected/API key invalid/Connection failed).

### Coach Invite Flow (partially built — next priority)

**Current state**: The "Invite Coach" tab in the Locker Room is a placeholder. The Render-side coach endpoints already exist in `coaches_api.py`:
- `POST /api/coaches/invite` — creates a `billing.coach_permission` row (status=INVITED), requires `owner_email` + `coach_email`, OPS_KEY auth
- `POST /api/coaches/accept` — sets status=ACCEPTED, requires `permission_id` + `coach_email`, OPS_KEY auth
- `POST /api/coaches/revoke` — sets status=REVOKED, requires `permission_id` OR (`owner_email` + `coach_email`), OPS_KEY auth

**Current Wix flow** (to be ported):
1. Owner clicks "Invite Coach" in Wix frontend → calls `/_functions/coachInviteNow`
2. Wix backend calls Render `/api/coaches/invite` (creates permission row)
3. Wix backend upserts a row in Wix CMS `AuthorizedCoaches` collection (status=INVITED, with invite token)
4. Wix triggers email via `COACH_INVITE_WEBHOOK_URL` webhook with accept URL containing token
5. Coach clicks accept link → Wix `/_functions/coach_accept` consumes token, updates CMS, calls Render `/api/coaches/accept`
6. Revoke: owner clicks revoke → Wix calls Render `/api/coaches/revoke`, updates CMS

**What needs building**:
1. Client-facing endpoint `POST /api/client/coach-invite` in `client_api.py` that proxies to the coaches API (similar to how `pbi-embed` proxies to PBI service)
2. Client-facing endpoint `POST /api/client/coach-revoke` 
3. Client-facing endpoint `GET /api/client/coaches` to list invited/accepted coaches
4. The Invite Coach tab UI: email input, invite button, list of coaches with status badges, revoke buttons
5. Email: can continue to go through Wix webhook (`COACH_INVITE_WEBHOOK_URL`) for now — the invite endpoint triggers it
6. Accept flow: stays on Wix acceptance page (`ten-fifty5.com/coach-accept?token=...`) — no change needed

### Wix → HTML Data Handoff

Client-facing pages receive identity data from Wix via:
1. **postMessage** (preferred): `{ type: 'wix-handoff', email, firstName, surname, wixMemberId }`
2. **URL params** (fallback): `?email=...&firstName=...&surname=...&wixMemberId=...&key=...&api=...`

### Entitlement System

**Server-side gate** (`entitlements_api.py`): `GET /api/entitlements/summary?email=` (OPS_KEY auth). Returns `can_upload`, `block_reason`. Used by upload APIs.

**Client-side gate** (`client_api.py`): `GET /api/client/entitlements` (CLIENT_API_KEY auth). Returns role, plan status, credits, plans page URL. Used by Locker Room / Media Room for UX rendering only.

**Subscription lifecycle** (`subscriptions_api.py`): Processes Wix subscription events. Writes to `billing.subscription_event_log` and `billing.subscription_state`. Note: `subscription_state` table is created lazily — the entitlements endpoint handles its absence gracefully.

| Condition | Locker Room | Media Room |
|---|---|---|
| **Coach role** | Green notice: view only | Blocked with message |
| **No active plan** | Amber banner + plans link | Blocked with plans link |
| **Credits = 0** | Red dismissible banner | Blocked with top-up link |
| **Account terminated** | Full-screen overlay | Full-screen overlay |
| **Active, credits > 0** | Normal view | Normal upload flow |

### HTML Templates

Client-facing SPAs are **root-level standalone HTML files** (not in a templates folder):
- `locker_room.html`, `media_room.html`, `players_enclosure.html`, `backoffice.html` — served by `locker_room_app.py`
- `templates/ui/upload.html` — legacy admin UI template (served by `ui_app.py` Blueprint)

### Admin UI (`ui_app.py`)

Flask Blueprint mounted at `/upload`. Provides:
- Sessions table with per-session ops (reconcile, repair, peek, delete)
- Read-only SQL runner
- Diagnostic route `/__which` to verify template paths

### Cron Jobs

- **`cron_capacity_sweep.py`**: Runs periodically. Detects stuck ingests, video trims, and PBI refreshes by checking timestamps against configurable thresholds (default: ingest 30m, trim 30m, PBI 10m). Marks them as `stale_timeout`/`failed`. Also sweeps stale PowerBI capacity leases.
- **`cron_monthly_refill.py`**: HTTP POST trigger that calls `/api/billing/cron/monthly_refill` on the main app.

### Auth Pattern

- **Ops endpoints**: `OPS_KEY` via `X-Ops-Key` header or `Authorization: Bearer <key>` (never via query string)
- **Video worker**: `VIDEO_WORKER_OPS_KEY` for worker auth, `VIDEO_TRIM_CALLBACK_OPS_KEY` for callback auth (must match main API's `OPS_KEY`)
- **Client API**: `CLIENT_API_KEY` via `X-Client-Key` header

### Idempotency Patterns

- **Billing consumption**: unique constraint on `task_id`
- **Entitlement grants**: unique on `(account_id, source, plan_code, external_wix_id)`
- **Bronze ingest**: advisory locks on `task_id` to prevent concurrent ingests
- **Wix notify**: checks `wix_notified_at` before sending

### Required Environment Variables

Main service: `DATABASE_URL`, `OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `SPORT_AI_TOKEN`, `VIDEO_WORKER_BASE_URL`, `VIDEO_WORKER_OPS_KEY`, `VIDEO_TRIM_CALLBACK_URL`, `CLIENT_API_KEY`, `PLANS_PAGE_URL` (optional, default `https://www.tenfifty5.com/plans`)

Ingest worker: same as main service plus `VIDEO_TRIM_CALLBACK_OPS_KEY` (must match main API's `OPS_KEY`), `INGEST_WORKER_OPS_KEY`

Video worker: `VIDEO_WORKER_OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `FFMPEG_BIN`, `FFPROBE_BIN`

Hardcoded defaults in `render.yaml`: `SPORT_AI_BASE=https://api.sportai.com`, `S3_PREFIX=wix-uploads`, `AWS_REGION=us-east-1`, `S3_GET_EXPIRES=604800`, `INGEST_REPLACE_EXISTING=1`, `AUTO_INGEST_ON_COMPLETE=1`

### S3 CORS

The S3 bucket (`nextpoint-prod-uploads`) requires CORS configuration for browser-to-S3 multipart uploads (Media Room) and video playback (Locker Room). Configuration:
- **AllowedMethods**: GET, PUT, POST, HEAD
- **AllowedHeaders**: `*`
- **ExposeHeaders**: `ETag` (required for multipart upload completion)
- **AllowedOrigins** must include: `https://locker-room-26kd.onrender.com`, tenfifty5.com variants, Wix editor/site domains

### Diagnostics

- `GET /__alive` — liveness probe (from `probes.py`)
- `GET /ops/routes?key=<OPS_KEY>` — list all registered routes (auth required)
- `GET /ops/db-ping?key=<OPS_KEY>` — DB connectivity check

### Future: Post-Wix Cleanup

When Wix is retired, `upload_app.py` can be significantly simplified:
- Remove: Wix notify flow, old `/upload` HTML form routes, `_upload_entitlement_gate` Wix checks, Wix-specific admin ops
- Refactor: split monolith into app factory + `sportai_api.py` + `s3_helpers.py` + `trim_callback.py`
- Consolidate: `_ensure_submission_context_schema` DDL into `db_init.py`

### Code Organisation

New features **must live in their own subdirectory** (not loose files in the repo root). Examples: `video_pipeline/`, `ml_pipeline/`, `migrations/`. Each directory should be a self-contained package with its own `__init__.py`, `requirements.txt` (if it has extra deps), and `config.py` (if it has tunable parameters). The repo root is for service entry points only (`*_app.py`, `wsgi.py`).

### ML Pipeline (`ml_pipeline/`)

ML inference pipeline for tennis video analysis. Supports both local dev mode and AWS Batch production mode (S3 input → GPU processing → PostgreSQL + S3 output).

**Run:**
```bash
# Install ML-specific deps (in addition to main requirements.txt)
pip install -r ml_pipeline/requirements.txt

# Local mode: analyse a video file
python -m ml_pipeline <video_path>

# AWS Batch mode: download from S3, process, save to DB + S3
python -m ml_pipeline --job-id <job_id> --s3-key <s3_key>

# Run test suite
python -m ml_pipeline.test_pipeline

# Deploy AWS infrastructure (ECR + Batch + Lambda)
bash ml_pipeline/deploy_aws.sh   # ECR repo, Batch compute/queue/job def
bash lambda/deploy.sh             # Lambda trigger + S3 event + DLQ

# End-to-end test (requires live AWS)
bash ml_pipeline/test_e2e.sh <video_path>
```

**Architecture:**
```
ml_pipeline/
  config.py              # All tunable parameters (thresholds, model paths, court dimensions)
  video_preprocessor.py  # OpenCV frame extraction, generator-based (memory efficient)
  court_detector.py      # 14 court keypoints → homography matrix → to_court_coords()
  ball_tracker.py        # TrackNet V2 ball detection, bounce/speed/in-out analysis
  player_tracker.py      # YOLOv8 person detection + IoU tracking
  pipeline.py            # Orchestrator: frame-by-frame, produces AnalysisResult + progress callbacks
  heatmaps.py            # Ball landing + player position heatmaps (matplotlib on 2D court)
  db_schema.py           # Idempotent DDL for ml_analysis.* tables (called on boot)
  db_writer.py           # Saves AnalysisResult + progress to PostgreSQL
  api.py                 # Flask blueprint: /api/analysis/* endpoints (OPS_KEY auth)
  Dockerfile             # nvidia/cuda:12.2, Python 3.11, FFmpeg, model weights
  deploy_aws.sh          # ECR + Batch infrastructure setup script
  test_e2e.sh            # End-to-end test script
  test_pipeline.py       # Unit test with synthetic video
  models/                # Pretrained weights (git-ignored, ~135MB total)
  test_videos/           # Test clips (git-ignored)
lambda/
  ml_trigger.py          # S3 ObjectCreated → create job row → submit Batch job
  deploy.sh              # Lambda deployment + S3 event + DLQ setup
```

**AWS Resources:**

| Resource | Name | Notes |
|---|---|---|
| ECR Repository | `ten-fifty5-ml-pipeline` | Lifecycle: keep 5 tagged, delete untagged after 1 day |
| Batch Compute Env | `ten-fifty5-ml-compute` | Spot G4dn.xlarge, 0–4 vCPUs |
| Batch Job Queue | `ten-fifty5-ml-queue` | Priority 1 |
| Batch Job Definition | `ten-fifty5-ml-pipeline` | 4 vCPU, 15GB RAM, 1 GPU, 2hr timeout |
| Lambda Function | `ten-fifty5-ml-trigger` | S3 videos/ prefix trigger |
| DLQ | `ten-fifty5-ml-trigger-dlq` | SQS, 14-day retention |
| CloudWatch Logs | `/aws/batch/ten-fifty5-ml-pipeline` | 30-day retention |

All resources tagged: `Project=TEN-FIFTY5`, `Environment=production`.

**Job Lifecycle:**

```
S3 upload to videos/{task_id}/file.mp4
  → Lambda creates ml_analysis.video_analysis_jobs row (status=queued)
  → Lambda submits AWS Batch job
  → Batch pulls Docker image from ECR, runs on Spot G4dn.xlarge
  → Pipeline stages: downloading → extracting_frames → detecting_court
    → tracking_ball → tracking_players → computing_analytics
    → generating_heatmaps → saving_results → complete
  → Results saved to ml_analysis.* tables
  → Heatmaps uploaded to S3: analysis/{job_id}/ball_heatmap.png, player_heatmap_{n}.png
  → Cost logged (G4dn.xlarge spot ≈ $0.16/hr)
```

Status: `queued` → `processing` → `complete` | `failed`

**Database Schema** (`ml_analysis.*`, managed by `db_schema.py`):
- `video_analysis_jobs` — one row per pipeline run (status, progress, video metadata, cost, heatmap S3 keys)
- `ball_detections` — per-frame ball positions (x, y, court coords, speed, bounce, in/out)
- `player_detections` — per-frame player bounding boxes + court coords
- `match_analytics` — aggregated stats per job (detection rate, bounces, rallies, serves, speeds)

**S3 Key Structure:**
- Input: `videos/{task_id}/{filename}.mp4`
- Heatmaps: `analysis/{job_id}/ball_heatmap.png`, `analysis/{job_id}/player_heatmap_0.png`, `analysis/{job_id}/player_heatmap_1.png`

**API Endpoints** (registered as `ml_analysis_bp` in `upload_app.py`, OPS_KEY auth):
- `GET /api/analysis/jobs/<job_id>` — full job status and metadata
- `GET /api/analysis/results/<match_id>` — analysis results by task_id (match_analytics + job data)
- `GET /api/analysis/heatmap/<job_id>/<type>` — presigned S3 URL for heatmap (1hr expiry). Types: `ball`, `player_0`, `player_1`
- `POST /api/analysis/retry/<job_id>` — reset failed/complete job, clear old detections, resubmit to Batch

**Models & weights:**

| Model | Architecture | Weights file | Source | Size |
|---|---|---|---|---|
| Ball tracker | TrackNet V2 (encoder-decoder CNN, 9→256ch) | `tracknet_v2.pt` | [yastrebksv/TrackNet](https://github.com/yastrebksv/TrackNet) (Google Drive) | 41MB |
| Player tracker | YOLOv8m (COCO pretrained) | `yolov8m.pt` | [ultralytics/assets v8.4.0](https://github.com/ultralytics/assets) | 50MB |
| Court detector | TrackNet-style CNN (3→15ch, 14 keypoints + center) | `court_keypoints.pth` | [yastrebksv/TennisCourtDetector](https://github.com/yastrebksv/TennisCourtDetector) (Google Drive) | 41MB |

To re-download weights: `python -c "from ultralytics import YOLO; YOLO('yolov8m.pt')"` for YOLO; use `gdown` for TrackNet/court weights (see Google Drive IDs in config.py comments).

**`AnalysisResult` data structure** (returned by `TennisAnalysisPipeline.process()`):
```python
@dataclass
class AnalysisResult:
    video_path: str
    video_metadata: VideoMetadata        # duration, fps, resolution, codec
    total_frames_processed: int
    processing_time_sec: float
    ms_per_frame: float
    court_detected: bool
    court_confidence: float              # 0.0–1.0
    court_used_fallback: bool            # True if Hough lines used instead of CNN
    ball_detections: List[BallDetection] # per-frame: x, y, court_x, court_y, speed_kmh, is_bounce, is_in
    player_detections: List[PlayerDetection]  # per-frame: player_id (0/1), bbox, center, court coords
    ball_detection_rate: float           # fraction of frames with ball found
    bounce_count: int
    bounces_in: int
    bounces_out: int
    max_speed_kmh: float
    avg_speed_kmh: float
    rally_count: int
    avg_rally_length: float              # bounces per rally
    serve_count: int
    first_serve_pct: float               # percentage
    player_count: int                    # distinct player IDs detected
    frame_errors: int
```

**Performance (CPU, 640x360 synthetic video):**
- ~5.5s per frame on CPU (TrackNet + CourtNet + YOLOv8m)
- Court detection runs every 30 frames (cached between)
- Player detection runs every 5 frames (reuses last bbox between)
- On GPU: expect 10–50x speedup (~100–500ms/frame)
- For production 2-hour videos: GPU is mandatory

**Known limitations:**
- Ball detection rate will be lower on real footage with fast-moving balls, camera motion, and occlusion. TrackNet V2 was trained on broadcast tennis; performance on amateur/phone footage is unvalidated.
- Player tracker assigns IDs by vertical position (bottom=player 0, top=player 1) which assumes a fixed camera angle. Moving/tilted cameras will break ID consistency.
- Court detector CNN is trained on standard tennis court views. Non-standard angles (side-on, close-up) may fail, triggering the Hough line fallback.
- Speed calculations assume a flat court plane. Ball height is not modelled, so speeds are 2D projections.
- No GPU auto-detection for mixed CPU/GPU setups — set device explicitly if needed.

### Other

- **`superset/`**: Optional Superset BI deployment config (Power BI is primary). Not in `render.yaml`.
- **`migrations/`**: One-off backfill SQL scripts. No automated migration framework — schema is managed idempotently via `db_init.py`.
