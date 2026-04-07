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

### Client API & Locker Room

`client_api.py` is the backend for the Locker Room dashboard (client-facing SPA). Uses separate auth: `X-Client-Key` header checked against `CLIENT_API_KEY` env var (not OPS_KEY). CORS headers manually injected for `/api/client/*` routes.

Key endpoints:
- `GET /api/client/matches` — list matches with stats, scores, trim status/footage keys
- `GET /api/client/players` — distinct player names for autocomplete
- `GET /api/client/matches/<task_id>` — point-level detail from silver
- `PATCH /api/client/matches/<task_id>` — update match metadata
- `POST /api/client/matches/<task_id>/reprocess` — rebuild silver via `build_silver_v2`
- `GET /api/client/profile` — primary member profile (name, surname, phone, UTR, dominant hand, country, area)
- `PATCH /api/client/profile` — update profile fields on `billing.member`
- `GET /api/client/footage-url/<task_id>` — returns a time-limited S3 presigned URL for the trimmed match footage

**Locker Room dashboard** (`locker_room.html`) layout: header with player info + collapsible "My Details" profile editor → usage charts → latest match hero card with video player → historical match list with per-row footage playback. Entitlement guards: coach role hides upload CTAs; exhausted credits show a dismissible banner linking to `/plans`.

**Profile columns on `billing.member`**: `surname`, `phone`, `utr`, `dominant_hand`, `country`, `area`. Added idempotently via `_ensure_member_profile_columns()` in `client_api.py` (runs on import). The match listing query also returns `trim_status`, `trim_output_s3_key`, and `trim_duration_s` from `bronze.submission_context`.

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

Main service: `DATABASE_URL`, `OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `SPORT_AI_TOKEN`, `VIDEO_WORKER_BASE_URL`, `VIDEO_WORKER_OPS_KEY`, `VIDEO_TRIM_CALLBACK_URL`, `CLIENT_API_KEY`

Video worker: `VIDEO_WORKER_OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

### Diagnostics

- `GET /__alive` — liveness probe (from `probes.py`)
- `GET /__routes` — list all registered routes
- `GET /ops/db-ping?key=<OPS_KEY>` — DB connectivity check
- `GET /upload/__which` — confirm which template file Flask resolves

### Other

- **`superset/`**: Optional Superset BI deployment config (Power BI is primary). Not in `render.yaml`.
- **`migrations/`**: One-off backfill SQL scripts. No automated migration framework — schema is managed idempotently via `db_init.py`.
