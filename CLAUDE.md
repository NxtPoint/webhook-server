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
| Locker Room | `gunicorn locker_room_app:app` | `locker_room_app.py` (serves `locker_room.html` as SPA, no DB) |

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
1. **Main app** polls SportAI status until complete
2. Main app POSTs to **ingest worker** `/ingest` (returns 202)
3. Ingest worker runs full pipeline: bronze ingest → silver build → video trim trigger → billing sync → PBI refresh
4. Main app separately polls task status; fires **Wix notify** only when `dashboard_ready=True` (ensures customer is notified only when data is viewable)

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
- Video upload to S3 + presigned URL generation
- SportAI job submission (`POST /api/statistics/tennis`) and status polling
- Webhook from SportAI on completion → auto-ingest via `ingest_bronze_strict`
- Silver layer build (`build_silver_point_detail`)
- Video trim trigger (`video_pipeline/video_trim_api.py`)
- Wix backend notification on completion
- Power BI dataset refresh

Registered blueprints: `coaches_api`, `members_api`, `subscriptions_api`, `usage_api`, `entitlements_api`, `client_api`, and `ingest_bronze` (mounted at root).

### Video Trim Pipeline

Fire-and-forget async flow:
1. **Main app** calls `trigger_video_trim(task_id)` in `video_pipeline/video_trim_api.py`
2. Builds EDL (Edit Decision List) from `silver.point_detail` via `build_video_timeline_from_silver()`
3. POSTs to the **video worker** service at `VIDEO_WORKER_BASE_URL/trim`
4. **Video worker** (`video_pipeline/video_worker_app.py`) accepts the request, spawns a detached subprocess, returns 202 immediately
5. Subprocess: downloads source from S3, FFmpeg re-encodes keep segments, concatenates, uploads `trimmed/{task_id}/review.mp4` to S3
6. Worker POSTs callback to `VIDEO_TRIM_CALLBACK_URL` with status + output S3 key

State is tracked in `bronze.submission_context.trim_status` (`queued` → `accepted` → `completed`/`failed`).

### Billing System

Credit-based usage tracking in the `billing` schema. Core files: `billing_service.py` (logic), `models_billing.py` (ORM), `billing_import_from_bronze.py` (sync pipeline).

Tables: `Account`, `Member`, `EntitlementGrant`, `EntitlementConsumption`. View: `billing.vw_customer_usage`.

Key patterns:
- 1 task = 1 match consumed (idempotent by `task_id` unique constraint)
- Entitlement grants are idempotent by `(account_id, source, plan_code, external_wix_id)`
- `billing_import_from_bronze.py` syncs completed tasks from `bronze.submission_context` into billing consumption records, auto-creating accounts from email + customer_name if missing
- `entitlements_api.py` gates uploads on remaining credit check

API blueprints: `subscriptions_api`, `usage_api`, `entitlements_api`.

### Client API, Locker Room & Players' Enclosure

`client_api.py` is the backend for the Locker Room dashboard and Players' Enclosure onboarding (client-facing SPAs). Uses separate auth: `X-Client-Key` header checked against `CLIENT_API_KEY` env var (not OPS_KEY). CORS headers manually injected for `/api/client/*` routes.

Key endpoints:
- `GET /api/client/matches` — list matches with stats, scores, trim status/footage keys
- `GET /api/client/players` — distinct player names for autocomplete
- `GET /api/client/matches/<task_id>` — point-level detail from silver
- `PATCH /api/client/matches/<task_id>` — update match metadata
- `POST /api/client/matches/<task_id>/reprocess` — rebuild silver via `build_silver_v2`
- `GET /api/client/profile` — primary member profile (name, surname, phone, UTR, dominant hand, country, area)
- `PATCH /api/client/profile` — update profile fields on `billing.member` (includes `profile_photo_url`)
- `GET /api/client/footage-url/<task_id>` — returns a time-limited S3 presigned URL for the trimmed match footage
- `GET /api/client/entitlements` — authoritative entitlement check (role, plan_active, credits_remaining, account_status, plans_page_url)
- `POST /api/client/register` — onboarding registration (creates/updates account + primary member with role)
- `POST /api/client/children` — add child member profiles under an account
- `GET /api/client/profile-photo-upload-url` — presigned S3 PUT URL for profile photo upload
- `GET /api/client/members` — list all active members on an account (primary + children)

**Locker Room dashboard** (`locker_room.html`) layout: header with player info + collapsible "My Details" profile editor → usage charts → latest match hero card with video player → historical match list with per-row footage playback.

**Players' Enclosure** (`players_enclosure.html`) is the member registration/onboarding page. Multi-step wizard: Welcome → Role Selection → Child Profiles (conditional) → Completion + Photo Upload. Served from `locker_room_app.py` at `/register`.

**Profile columns on `billing.member`**: `surname`, `phone`, `utr`, `dominant_hand`, `country`, `area`, `dob`, `skill_level`, `club_school`, `notes`, `profile_photo_url`. Added idempotently via `_ensure_member_profile_columns()` in `client_api.py` (runs on import). The match listing query also returns `trim_status`, `trim_output_s3_key`, and `trim_duration_s` from `bronze.submission_context`.

### Wix → HTML Data Handoff

The Players' Enclosure receives user identity data from Wix via two mechanisms (in priority order):
1. **postMessage** (preferred): Wix parent sends `{ type: 'wix-handoff', email, firstName, surname, wixMemberId }`. The page listens on `window.addEventListener('message', ...)`.
2. **URL params** (fallback): `?email=...&firstName=...&surname=...&wixMemberId=...&key=...&api=...`

Both mechanisms populate the same internal `WIX_DATA` object. If email, firstName, or surname are missing after a 2-second grace period, the page shows an error. The user is never asked to re-enter these fields.

### Entitlement System

#### What Exists

**Ops-level entitlement gate** (`entitlements_api.py`): `GET /api/entitlements/summary?email=` (OPS_KEY auth). Upserts into `billing.entitlements` table — a denormalized cache that joins account, member, subscription_state, grants, and consumption. Returns `can_upload`, `block_reason`, `can_view_dashboards`, `dashboard_block_reason`. Used for server-side upload gating.

**Subscription lifecycle** (`subscriptions_api.py`): Processes Wix subscription events (PLAN_PURCHASED, PLAN_CANCELLED, RECURRING_PAYMENT_CANCELLED). Writes to `billing.subscription_event_log` (idempotent by event hash) and `billing.subscription_state` (upserted per account). Monthly refill cron resets credits based on subscription allowance.

**Billing core** (`billing_service.py`, `models_billing.py`): Account/Member/EntitlementGrant/EntitlementConsumption ORM models. Credit math: remaining = active grants - consumption. `billing_import_from_bronze.py` syncs completed tasks into consumption.

**Members API** (`members_api.py`): `POST /api/billing/sync_account` (snapshot replacement from Wix), `POST /api/billing/member/upsert`, `POST /api/billing/member/deactivate`. OPS_KEY auth.

#### Client-Facing Entitlement Gate (NEW)

`GET /api/client/entitlements` (CLIENT_API_KEY auth) returns:
```json
{
  "role": "player_parent|coach",
  "plan_active": true,
  "credits_remaining": 5,
  "matches_granted": 10,
  "matches_consumed": 5,
  "account_status": "active|terminated",
  "plans_page_url": "https://www.tenfifty5.com/plans"
}
```

`plans_page_url` is read from `PLANS_PAGE_URL` env var (default: `https://www.tenfifty5.com/plans`).

#### Entitlement Matrix (Client-Side UX)

| Condition | Locker Room | Players' Enclosure |
|---|---|---|
| **Coach role** | Green notice: "Signed in as Coach — view only" | Allowed to complete onboarding |
| **No active plan** | Amber banner: "No active plan" + link to plans page | Amber banner with plans link |
| **Credits = 0, plan active** | Red banner: "Credits exhausted" + top-up link (dismissible) | N/A (onboarding page) |
| **Account terminated/suspended** | Full-screen blocking overlay with support email | Full-screen blocking overlay |
| **Active, credits > 0** | Normal view | Normal onboarding flow |

Server-side enforcement: `GET /api/entitlements/summary` (OPS_KEY) gates uploads via `can_upload` / `block_reason`. The client-facing `GET /api/client/entitlements` is for UX rendering only — security enforcement is server-side.

#### Assumptions

- `billing.subscription_state` and `billing.entitlements` tables are created by their respective API modules on first use (not in `db_init.py`).
- `media_room.html` is the Video Upload page (served at `/media-room` from the main app). Uses entitlement checks via `/api/client/entitlements` (same pattern as locker_room.html).
- Account "suspended" vs "terminated" both map to `account.active = false` (no separate suspended state in DB currently). The blocking overlay uses the same treatment for both.
- Roles are `player_parent` (covers both solo players and parents) and `coach`. The "Parent with Children" selection in Players' Enclosure stores `player_parent` as the DB role — the distinction is behavioral (child profiles are created).

### Media Room (`media_room.html`)

Video upload page replacing the Wix-based upload flow. Served at `GET /media-room` from the main webhook-server app (same-origin, no CORS needed for upload APIs). Self-contained iframe embed, same auth pattern as Locker Room (`?email=...&key=...&api=...`).

**4-step wizard flow:**
1. **Game Type Selection** — Singles (active), Technique Session / Doubles Training / Serve Practice (coming soon). Selection stored in state.
2. **Video Upload** — Chunked multipart upload directly to S3 via presigned URLs. 10 MB chunks with 3 retries + exponential backoff. Browser Wake Lock API prevents screen sleep on mobile (graceful fallback with warning). Resumable: upload state (uploadId, key, completed parts) persisted in `localStorage` with 24h expiry. On page load, checks for interrupted uploads and offers Resume/Discard. Progress shows: % bar, chunk counter, upload speed (MB/s), ETA.
3. **Match Details Form** — Exact same fields as Locker Room edit panel, writing to `bronze.submission_context`: `player_a_name` (dropdown from `/api/client/members` + `/api/client/players`), `player_a_utr`, `player_b_name`, `player_b_utr`, `match_date`, `location`, `first_server` (toggle: Server/Returner), `start_time` (seconds from video start), score (3 sets, A+B). Submit calls `POST /api/submit_s3_task` then PATCHes `first_server` to `player_a`/`player_b` format (matching Locker Room).
4. **SportAI Analysis Progress** — Polls `GET /upload/api/task-status?task_id=` every 5s. Shows progress bar, pipeline stage label, and scrollable dark terminal-style transaction log with timestamped entries. On completion: success card with optional Locker Room link (`?locker_room_url=` param). On failure: error card with reference code and retry button.

**Entitlement gate:** On page load, calls `/api/client/entitlements`. Blocks coaches (view-only message), no-plan users (link to plans page), and zero-credit users (link to buy more). Upload API endpoints also enforce server-side via `_upload_entitlement_gate(email)`.

**Future-proofing:** `getFormConfig(gameType)` stub function returns form configuration per game type. Currently only `singles` is implemented. New game types add a case returning their field config; the form renderer auto-generates the UI.

**New/modified endpoints:**
- `GET /media-room` — serves `media_room.html` (added to `upload_app.py`)
- `POST /upload/api/multipart/list-parts` — returns parts already uploaded for a multipart upload, used for reliable ETag retrieval and resume verification (added to `upload_app.py`)
- `GET /api/client/members` — returns active members on an account for player dropdowns (added to `client_api.py`)
- CORS extended to cover `/upload/api/*` and `/api/submit_s3_task` paths (modified in `upload_app.py`)

**No DB changes.** All fields written are existing `bronze.submission_context` columns.

### Admin UI (`ui_app.py`)

Flask Blueprint mounted at `/upload`. Provides:
- Sessions table with per-session ops (reconcile, repair, peek, delete)
- Read-only SQL runner
- Diagnostic route `/__which` to verify template paths

### Cron Jobs

- **`cron_capacity_sweep.py`**: Runs periodically. Detects stuck ingests, video trims, and PBI refreshes by checking timestamps against configurable thresholds (default: ingest 30m, trim 30m, PBI 10m). Marks them as `stale_timeout`/`failed`. Also sweeps stale PowerBI capacity leases.
- **`cron_monthly_refill.py`**: HTTP POST trigger that calls `/api/billing/cron/monthly_refill` on the main app.

### Auth Pattern

All ops endpoints use `OPS_KEY` checked via `X-Ops-Key` header or `Authorization: Bearer <key>` (never via query string to avoid log leakage). The video worker uses its own `VIDEO_WORKER_OPS_KEY`. The client API uses `CLIENT_API_KEY` via `X-Client-Key` header.

### Idempotency Patterns

- **Billing consumption**: unique constraint on `task_id`
- **Entitlement grants**: unique on `(account_id, source, plan_code, external_wix_id)`
- **Bronze ingest**: advisory locks on `task_id` to prevent concurrent ingests
- **Wix notify**: checks `wix_notified_at` before sending

### Required Environment Variables

Main service: `DATABASE_URL`, `OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `SPORT_AI_TOKEN`, `VIDEO_WORKER_BASE_URL`, `VIDEO_WORKER_OPS_KEY`, `VIDEO_TRIM_CALLBACK_URL`, `CLIENT_API_KEY`, `PLANS_PAGE_URL` (optional, default `https://www.tenfifty5.com/plans`)

Video worker: `VIDEO_WORKER_OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

### Diagnostics

- `GET /__alive` — liveness probe (from `probes.py`)
- `GET /ops/routes?key=<OPS_KEY>` — list all registered routes (auth required)
- `GET /ops/db-ping?key=<OPS_KEY>` — DB connectivity check

### Other

- **`superset/`**: Optional Superset BI deployment config (Power BI is primary). Not in `render.yaml`.
- **`migrations/`**: One-off backfill SQL scripts. No automated migration framework — schema is managed idempotently via `db_init.py`.
