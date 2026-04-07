# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Services and How to Run

This repo defines five Render services (see `render.yaml`). All are Python/Flask + Gunicorn:

| Service | Start command | Entry point |
|---|---|---|
| Main API (webhook-server) | `gunicorn wsgi:app` | `wsgi.py` → `upload_app.py` |
| Ingest worker | `gunicorn ingest_worker_app:app` | `ingest_worker_app.py` |
| Power BI service | `gunicorn powerbi_app:app` | `powerbi_app.py` |
| Video trim worker | Docker (see `Dockerfile.worker`) | `video_pipeline/video_worker_wsgi.py` → `video_pipeline/video_worker_app.py` |
| Locker Room | `gunicorn locker_room_app:app` | `locker_room_app.py` (serves HTML SPAs, no DB) |

The Locker Room service serves three pages:
- `GET /` → `locker_room.html` (dashboard)
- `GET /media-room` → `media_room.html` (video upload)
- `GET /register` → `players_enclosure.html` (onboarding wizard)

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

The primary service (~2800 lines). Responsibilities:
- S3 presigned URL generation (upload + get)
- SportAI job submission (`POST /api/statistics/tennis`) and status polling
- Video trim callback (`POST /internal/video_trim_complete`)
- Blueprint registration and CORS
- Wix backend notification on completion (legacy — will be removed when Wix is retired)

Registered blueprints: `coaches_api`, `members_api`, `subscriptions_api`, `usage_api`, `entitlements_api`, `client_api`, and `ingest_bronze` (mounted at root).

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

**Member profile columns** on `billing.member`: `surname`, `phone`, `utr`, `dominant_hand`, `country`, `area`, `dob`, `skill_level`, `club_school`, `notes`, `profile_photo_url`. Added idempotently via `_ensure_member_profile_columns()` in `client_api.py` (runs on import).

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
- `GET /api/client/footage-url/<task_id>` — time-limited S3 presigned URL for trimmed match footage
- `GET /api/client/entitlements` — entitlement check (role, plan_active, credits_remaining, account_status, plans_page_url). Handles missing `billing.subscription_state` table gracefully.
- `GET /api/client/members` — all active members on an account (full profile fields)
- `POST /api/client/members` — add a linked player (child or coach)
- `PATCH /api/client/members/<id>` — update a linked member's profile
- `DELETE /api/client/members/<id>` — soft-delete (sets `active=false`, preserves history)
- `POST /api/client/register` — onboarding registration
- `POST /api/client/children` — add child member profiles (Players' Enclosure onboarding)
- `GET /api/client/profile-photo-upload-url` — presigned S3 PUT URL for profile photo

### Locker Room (`locker_room.html`)

Dashboard SPA embedded as Wix iframe. Auth via URL params: `?email=...&key=...&api=...`.

**Page layout (top to bottom):**
1. **Header** — TEN-FIFTY5 logo, player name + surname, email, usage pill (remaining matches)
2. **My Details** (collapsible, collapsed by default) — editable profile: first name, surname, email (read-only), mobile, UTR, dominant hand, country, area
3. **Linked Players** (collapsible, collapsed by default) — cards for each non-primary member (children/coaches). Each card is individually collapsible with editable fields matching My Details. "Deactivate" soft-deletes (keeps history). "+ Add Player" inline form.
4. **Charts** — 70/30 grid: matches per month line chart | usage gauge
5. **Latest Match** — hero card inside a white block. Shows player names, date, location, score, key stats (points, games, aces, avg rally, duration). "Watch Footage" button opens modal HTML5 video player (or "Processing..." badge). Entire card clickable to open edit panel.
6. **Match History** — single white card block. Year headers → month headers (indented) → match rows (indented further). Years and months newest first. Matches within a month sort latest to oldest. Each row shows Player A vs Player B, date, location, status badge, score, play icon (footage), edit button.

**Edit panel** (slide-in from right): match stats grid, then editable fields — Player A (dropdown of active account members only), Player A UTR, Player B (free text), Player B UTR, match date, venue, "First Point: Player A was..." (Server/Returner toggle buttons matching Media Room), score (3 sets), start time offset. Save + Reprocess buttons.

**Video modal**: fullscreen overlay player, shared between hero card and match row play icons. Fetches presigned URL from `/api/client/footage-url/<task_id>`.

**Entitlement guards**: coach role shows view-only notice; exhausted credits show dismissible banner linking to plans page (`PLANS_PAGE_URL` env var).

**Design system**: all pages share the same CSS variables, Inter font, green/amber/red colour palette. Toggle buttons (`.toggle-group` / `.toggle-btn`) are identical between Locker Room and Media Room.

### Media Room (`media_room.html`)

Video upload page served at `GET /media-room`. Same auth pattern as Locker Room.

**4-step wizard flow:**
1. **Game Type Selection** — Singles (active), others coming soon
2. **Video Upload** — Chunked multipart upload to S3 via presigned URLs. 10 MB chunks, 3 retries + exponential backoff. Browser Wake Lock API. Resumable via `localStorage`.
3. **Match Details Form** — Player A dropdown from `/api/client/members`, same fields as Locker Room edit panel. First server as Server/Returner toggle. Submit calls `POST /api/submit_s3_task`.
4. **Analysis Progress** — Polls task status every 5s. Terminal-style transaction log. On completion: success card with Locker Room link.

**Entitlement gate**: calls `/api/client/entitlements` on load. Blocks coaches, no-plan users, zero-credit users.

### Players' Enclosure (`players_enclosure.html`)

Member registration/onboarding page. Multi-step wizard: Welcome → Role Selection → Child Profiles (conditional) → Completion + Photo Upload. Served at `/register`.

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

Ingest worker: same as main service plus `VIDEO_TRIM_CALLBACK_OPS_KEY` (must match main API's `OPS_KEY`)

Video worker: `VIDEO_WORKER_OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

### S3 CORS

The S3 bucket requires CORS configuration for cross-origin video playback and file uploads from the client-facing SPAs. AllowedOrigins must include: the Locker Room Render domain, tenfifty5.com variants, Wix editor/site domains.

### Diagnostics

- `GET /__alive` — liveness probe (from `probes.py`)
- `GET /ops/routes?key=<OPS_KEY>` — list all registered routes (auth required)
- `GET /ops/db-ping?key=<OPS_KEY>` — DB connectivity check

### Future: Post-Wix Cleanup

When Wix is retired, `upload_app.py` can be significantly simplified:
- Remove: Wix notify flow, old `/upload` HTML form routes, `_upload_entitlement_gate` Wix checks, Wix-specific admin ops
- Refactor: split monolith into app factory + `sportai_api.py` + `s3_helpers.py` + `trim_callback.py`
- Consolidate: `_ensure_submission_context_schema` DDL into `db_init.py`

### Other

- **`superset/`**: Optional Superset BI deployment config (Power BI is primary). Not in `render.yaml`.
- **`migrations/`**: One-off backfill SQL scripts. No automated migration framework — schema is managed idempotently via `db_init.py`.
