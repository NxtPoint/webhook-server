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

The Locker Room service serves nine pages:
- `GET /` → `locker_room.html` (dashboard)
- `GET /media-room` → `media_room.html` (video upload)
- `GET /register` → `players_enclosure.html` (onboarding wizard)
- `GET /backoffice` → `backoffice.html` (admin dashboard)
- `GET /analytics` → `analytics.html` (Power BI embed)
- `GET /portal` → `portal.html` (unified nav shell — main entry point for Wix)
- `GET /pricing` → `pricing.html` (plans & pricing page)
- `GET /coach-accept` → `coach_accept.html` (coach invitation acceptance)

The main webhook-server also serves `/media-room`, `/backoffice`, `/analytics`, `/portal`, `/pricing`, and `/coach-accept` as same-origin backups for API access.

Note: The Locker Room service only installs `flask` + `gunicorn` (not full `requirements.txt`).

**Local dev:**
```bash
source .venv/Scripts/activate  # Windows bash
pip install -r requirements.txt
gunicorn wsgi:app --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 1800
gunicorn video_pipeline.video_worker_wsgi:app --bind 0.0.0.0:8001
```

**Manual integration smoke test** (requires live DB):
```bash
python video_pipeline/test_video_timeline.py
```

### Testing & Code Quality

No automated test suite, CI pipeline, or linter is configured. All testing is manual against the live Render database. Do not attempt to run `pytest`.

Schema DDL is split across multiple files: `db_init.py` (bronze tables, called on boot), `_ensure_member_profile_columns()` in `client_api.py` (billing columns, runs on import), `_ensure_submission_context_schema()` in `upload_app.py`, and `ensure_invite_token_column()` in `coach_invite/db.py`. These all use idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` or `CREATE TABLE IF NOT EXISTS` patterns.

## Architecture Overview

### Service Topology & Data Flow

On upload completion, the system follows this flow:
1. **Media Room** uploads video to S3, submits to SportAI via `POST /api/submit_s3_task`
2. **Main app** polls SportAI status until complete
3. Main app POSTs to **ingest worker** `/ingest` (returns 202)
4. Ingest worker runs full pipeline: bronze ingest → silver build → video trim trigger → billing sync → PBI refresh
5. Video worker trims footage, POSTs callback to `/internal/video_trim_complete` → `trim_status` = `completed`
6. **Customer notification**: SES email sent → customer sees "Your match analysis is ready"
7. **Locker Room** displays match data + trimmed footage playback

Key design: the ingest worker is self-contained — it does NOT import `upload_app.py`. It calls `ingest_bronze_strict()` directly from `ingest_bronze.py` (function call, not HTTP). Worker timeout is 3600s vs main app 1800s.

### T5 Pipeline (Serve Practice / Rally Practice)

Parallel analysis path for practice sessions, using the in-house T5 ML pipeline instead of SportAI. The routing is based on `gameType` sent from the Media Room frontend.

**Game type → pipeline routing:**

| Game Type | `sport_type` (DB) | Pipeline | Billing |
|---|---|---|---|
| Singles | `tennis_singles` | SportAI | 1 credit |
| Serve Practice | `serve_practice` | T5 (AWS Batch GPU) | Free |
| Rally Practice | `rally_practice` | T5 (AWS Batch GPU) | Free |
| Technique | TBD | TBD | TBD |
| Doubles | TBD | TBD | TBD |

**T5 flow** (same UX as SportAI, different backend):
1. **Media Room** user selects "Serve Practice" or "Rally Practice", uploads video to S3 (same `wix-uploads/` prefix)
2. `POST /api/submit_s3_task` with `gameType: "serve"` → `_t5_submit()` creates `ml_analysis.video_analysis_jobs` row + submits AWS Batch job
3. `GET /upload/api/task-status` detects `sport_type` in `T5_SPORT_TYPES` → `_t5_status()` polls `ml_analysis.video_analysis_jobs` (DB query, not external API)
4. Batch job completes → `_t5_status` returns `status=completed` + sentinel `result_url=t5://complete/{id}`
5. Auto-ingest detects sentinel URL → `_do_ingest_t5()` runs lightweight ingest: mark started, skip bronze/silver/billing/trim, trigger PBI refresh, mark done
6. Frontend shows completion

**Key differences from SportAI path:**
- Submit: `_t5_submit()` → AWS Batch (vs `_sportai_submit()` → external API)
- Status poll: DB query on `ml_analysis.video_analysis_jobs` (vs SportAI HTTP call)
- Cancel: `_t5_cancel()` → terminates Batch job (vs `_sportai_cancel()` → external API)
- Ingest: `_do_ingest_t5()` — lightweight, skips bronze/silver/billing/trim (data already in `ml_analysis.*`)
- Billing: no credit consumed for practice sessions
- Result URL resolution: `_resolve_result_url_for_task()` checks `sport_type` and uses `_t5_status()` for practice jobs (not SportAI)

**Cancel / unhappy path routing**: the cancel endpoint (`/upload/api/cancel-task`) and `_resolve_result_url_for_task()` both check `sport_type in T5_SPORT_TYPES` before deciding whether to call SportAI or T5 functions. All new endpoints that interact with analysis jobs must do the same check.

**Required env vars** (for T5 routing in upload_app.py): `BATCH_JOB_QUEUE` (default: `ten-fifty5-ml-queue`), `BATCH_JOB_DEF` (default: `ten-fifty5-ml-pipeline`)

**`sport_type` field**: stored on `bronze.submission_context.sport_type`. Determines which pipeline path is used for status polling and ingest. The `T5_SPORT_TYPES` set in `upload_app.py` controls routing.

**Strategic note**: serve + rally practice are designed so that combining them produces match-level analysis — if SportAI becomes unavailable, only the orchestration layer needs to change.

### Data Layers (PostgreSQL)

- **Bronze** (`bronze.*`): Raw SportAI JSON ingested verbatim. `db_init.py` owns schema creation (idempotent, called on boot). Key tables: `raw_result`, `submission_context`, `player_swing`, `rally`, `ball_position`, `ball_bounce`, `player_position`.
- **Silver** (`silver.*`): Structured/normalized data. `silver.point_detail` is the key match table consumed by the video timeline and client API. Built by `build_silver_v2.py` (5-pass SQL approach). `silver.practice_detail` is the practice table for serve/rally sessions. Built by `ml_pipeline/build_silver_practice.py` (3-pass: extract bounces → sequence detection → analytics). Legacy: `build_silver_point_detail.py` (Python-based, kept for reference).
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

### Silver Practice (`ml_pipeline/build_silver_practice.py`)

Silver builder for serve and rally practice data. Reads from `ml_analysis.ball_detections` + `ml_analysis.player_detections` (T5 bronze), writes to `silver.practice_detail`.

**3-pass approach:**
1. Extract bounces with court coordinates + nearest player position → insert rows
2. Sequence detection: serve practice = sequential numbering with deuce/ad alternation; rally practice = group bounces into rallies by frame gap, number shots within each
3. Analytics: placement zone (A/B/C/D quadrant), depth (Deep/Middle/Short), serve zone (Wide/Body/T), serve result (In/Fault), rally length and duration

Called from `_do_ingest_t5()` in `upload_app.py` after T5 Batch job completes. Schema managed by `ml_pipeline/db_schema.py` (idempotent).

### Main App (`upload_app.py`)

The primary service. Responsibilities:
- S3 presigned URL generation (single-part + multipart upload, GET)
- S3 multipart lifecycle: `initiate`, `presign-part`, `list-parts`, `complete`, `abort`
- SportAI job submission (`POST /api/statistics/tennis`) and T5 Batch submission — routed by `gameType`/`sport_type`
- Task status orchestration: auto-ingest trigger (SportAI or T5), PBI refresh polling, customer notification (SES + Wix)
- Video trim callback (`POST /internal/video_trim_complete`)
- CORS preflight handling (global `before_request` for OPTIONS on all client/upload paths)

Registered blueprints: `coaches_api`, `members_api`, `subscriptions_api`, `usage_api`, `entitlements_api`, `client_api`, `coach_accept` (from `coach_invite`), `ml_analysis_bp`, and `ingest_bronze` (mounted at root).

### Video Trim Pipeline

Fire-and-forget async flow:
1. **Ingest worker** calls `trigger_video_trim(task_id)` in `video_pipeline/video_trim_api.py`
2. Builds EDL from `silver.point_detail` via `build_video_timeline_from_silver()`
3. POSTs to the **video worker** service at `VIDEO_WORKER_BASE_URL/trim`
4. **Video worker** accepts, spawns detached subprocess, returns 202
5. Subprocess: downloads from S3 → FFmpeg re-encodes → uploads `trimmed/{task_id}/review.mp4`
6. Worker POSTs callback to `VIDEO_TRIM_CALLBACK_URL` with status + output S3 key

State tracked in `bronze.submission_context.trim_status` (`queued` → `accepted` → `completed`/`failed`).

### Billing System

Credit-based usage tracking in the `billing` schema. Core files: `billing_service.py` (logic), `models_billing.py` (ORM), `billing_import_from_bronze.py` (sync pipeline).

Tables: `Account`, `Member`, `EntitlementGrant`, `EntitlementConsumption`. Views: `billing.vw_customer_usage`.

Key patterns:
- 1 task = 1 match consumed (idempotent by `task_id` unique constraint)
- Entitlement grants are idempotent by `(account_id, source, plan_code, external_wix_id)`
- **Immediate credit grant on purchase**: `subscription_event()` calls `grant_entitlement()` when `PLAN_PURCHASED` + `ACTIVE`, so credits are available instantly (not delayed until monthly refill). Works for both recurring (`wix_subscription`) and PAYG (`wix_payg`) plans. Idempotent via `external_wix_id = "purchase:{order_id}:{account_id}"`.
- `billing_import_from_bronze.py` syncs completed tasks from `bronze.submission_context` into billing consumption records, auto-creating accounts from email + customer_name if missing
- `entitlements_api.py` gates uploads on remaining credit check
- **Upload gate**: allows upload if user has an active subscription OR remaining credits (PAYG users have no subscription but have credits)

**`billing.member` is the single source of truth for all customer/player/child/coach profile data.** Every client-facing page reads from and writes back to this one table. Match-level data (`player_a_name`, `player_b_name` etc.) is stored separately in `bronze.submission_context` as point-in-time snapshots — editing a player's name doesn't rewrite historical match records.

API blueprints: `subscriptions_api`, `usage_api`, `entitlements_api`.

### Client API (`client_api.py`)

Backend for all client-facing SPAs. Uses separate auth: `X-Client-Key` header checked against `CLIENT_API_KEY` env var (not OPS_KEY).

Key endpoints:
- `GET /api/client/matches` — list matches with stats, scores, trim status, footage keys
- `GET /api/client/players` — distinct player names for autocomplete
- `GET /api/client/matches/<task_id>` — point-level detail from silver
- `PATCH /api/client/matches/<task_id>` — update match metadata
- `POST /api/client/matches/<task_id>/reprocess` — rebuild silver via `build_silver_v2`
- `GET /api/client/profile` — primary member profile
- `PATCH /api/client/profile` — update profile fields on `billing.member`
- `GET /api/client/usage` — account usage summary
- `GET /api/client/footage-url/<task_id>` — time-limited S3 presigned URL for trimmed footage
- `GET /api/client/entitlements` — entitlement check (role, plan_active, credits_remaining, matches_granted, matches_consumed, account_status, subscription_status, plan_code, plan_type, current_period_end, plans_page_url)
- `GET /api/client/members` — all active members on an account
- `POST /api/client/members` — add a linked player
- `PATCH /api/client/members/<id>` — update a linked member
- `DELETE /api/client/members/<id>` — soft-delete (sets `active=false`)
- `POST /api/client/register` — onboarding registration
- `POST /api/client/children` — add child member profiles
- `GET /api/client/profile-photo-upload-url` — presigned S3 PUT URL for profile photo
- `GET /api/client/coaches` — list coach permissions for the account
- `POST /api/client/coach-invite` — invite a coach (creates permission + token + SES email)
- `POST /api/client/coach-revoke` — revoke a coach permission
- `GET /api/client/pbi-embed` — Power BI embed token (proxies to PBI service)
- `POST /api/client/pbi-heartbeat` — keep PBI capacity session alive
- `POST /api/client/pbi-session-end` — end PBI capacity session on page unload
- `GET /api/client/backoffice/pipeline` — admin: pipeline status table
- `GET /api/client/backoffice/customers` — admin: customer list with usage stats
- `GET /api/client/backoffice/kpis` — admin: KPI cards

Admin endpoints require email in `ADMIN_EMAILS` whitelist (hardcoded set in `client_api.py`): `info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`.

### Coach Invite Flow

Owner invites coaches from the Locker Room "Invite Coach" tab. Data stored in `billing.coaches_permission` table (columns: id, owner_account_id, coach_account_id, coach_email, status, active, invite_token, created_at, updated_at).

**Module**: `coach_invite/` — contains `db.py` (schema + token helpers), `email_sender.py` (AWS SES coach invite email), `video_complete_email.py` (AWS SES video completion email), `accept_page.py` (Flask blueprint).

**Server-to-server endpoints** (`coaches_api.py`, OPS_KEY auth):
- `POST /api/coaches/invite` — creates permission row (status=INVITED)
- `POST /api/coaches/accept` — sets status=ACCEPTED
- `POST /api/coaches/revoke` — sets status=REVOKED, clears invite_token

**Client-facing endpoints** (`client_api.py`, CLIENT_API_KEY auth):
- `GET /api/client/coaches` — list all coach permissions for the account
- `POST /api/client/coach-invite` — invite a coach: creates/reuses permission row, generates secure token (`secrets.token_urlsafe(32)`), sends invite email via AWS SES
- `POST /api/client/coach-revoke` — revoke a coach, clears invite_token

**Accept flow** (self-contained on Render, no Wix dependency):
- `GET /coach-accept?token=...` — serves `coach_accept.html` (standalone SPA)
- `POST /api/coaches/accept-token` — **public endpoint** (token IS the auth). Validates token against `billing.coaches_permission` (status=INVITED, active=true), sets status=ACCEPTED, clears token. Returns `coach_email` so the page can show which email to log in with.
- On success: shows confirmation with email login hint, auto-redirects to `https://www.ten-fifty5.com/portal` after 5 seconds.

**Idempotency**: re-inviting a previously revoked coach reuses the existing row (resets status to INVITED, generates new token, sends new email). Tokens are single-use (cleared on accept and revoke).

### Email System (AWS SES)

All transactional emails are sent via AWS SES using `boto3.client('ses')`. The `coach_invite/` package contains the email modules.

**Email types:**

| Email | Module | Trigger | Template |
|---|---|---|---|
| Coach invite | `coach_invite/email_sender.py` | `POST /api/client/coach-invite` | Branded HTML: "X has invited you to coach" + accept CTA button |
| Video complete | `coach_invite/video_complete_email.py` | Ingest step 7 + task-status auto-fire | Branded HTML: "Your match analysis is ready" + Portal CTA button |

**AWS SES setup:**
- **Region**: `eu-north-1` (Stockholm) — matches the Render deployment region
- **IAM user**: `nextpoint-uploader` — must have `AmazonSESFullAccess` policy (or `ses:SendEmail` + `ses:SendRawEmail`)
- **Verified identity**: domain `ten-fifty5.com` verified via DKIM (3 CNAME records in Wix DNS)
- **Sandbox**: must be promoted to production access to send to non-verified recipients

**Env vars:**
- `SES_FROM_EMAIL` — sender address (default: `noreply@ten-fifty5.com`). Domain must be verified in SES.
- `COACH_ACCEPT_BASE_URL` — base URL for accept links (default: `https://api.nextpointtennis.com`)
- `LOCKER_ROOM_BASE_URL` — CTA link in video completion email (default: `https://www.ten-fifty5.com/portal`)

Video completion emails are sent via AWS SES. Idempotent via `ses_notified_at` column on `bronze.submission_context`. The CTA button links to the portal (`LOCKER_ROOM_BASE_URL`).

### Locker Room (`locker_room.html`)

Dashboard SPA loaded inside the portal's inner iframe. Auth via URL params: `?email=...&key=...&api=...`.

**Header tabs:** Account (read-only stats), My Details (editable profile), Linked Players (member cards with add/edit/deactivate), Invite Coach (email input + coach list with status badges + revoke).

**Main sections:** Charts (matches per month + usage gauge), Latest Match (hero card), Match History (year → month → match rows), Edit Panel (slide-in), Video Modal (fullscreen player).

**Design system**: all pages share CSS variables, Inter font, green/amber/red colour palette. Toggle buttons (`.toggle-group` / `.toggle-btn`) are identical between Locker Room and Media Room.

### Media Room (`media_room.html`)

Video upload page. 4-step wizard: Game Type Selection → Video Upload (chunked multipart to S3) → Match Details Form → Analysis Progress (polls task-status). Auth via URL params. Entitlement gate on load.

### Pricing (`pricing.html`)

Plans & pricing page. Fetches entitlements on load and conditionally renders one of three views:
- **New plan selection** (player/parent with no active recurring subscription): shows monthly subscription plans + pay-as-you-go credit packs
- **Top-up only** (player/parent with active recurring subscription): shows only credit top-up packs with a note that plan changes are available after the current period ends
- **Coach view**: explains that coach access is free and managed by player accounts

On plan selection, sends `postMessage({ type: 'wix-checkout', planId })` up through portal to the Wix parent, which calls `checkout.startOnlinePurchase(planId)` via the Wix Pricing Plans API. Plan catalogue is configured as JS constants (`PLAYER_PLANS`, `TOPUP_PACKS`, `COACH_PLANS`) with `wixPlanId` fields — update these when Wix plan IDs change.

Status bar shows current plan, renewal date, and credit usage. All billing state reads come from `/api/client/entitlements`.

### Portal (`portal.html`)

Unified navigation shell — **the single frontend entry point**. Collapsible sidebar with navigation. Content pages load in an inner iframe with auth params forwarded.

**Hosting architecture**: The portal is embedded in a Wix page (`https://www.ten-fifty5.com/portal`) as an HTML iframe. Wix handles member authentication and passes identity data to the portal via URL params. All SPA pages (dashboard, upload, profile, analytics, pricing, backoffice) are rendered inside the portal's inner iframe. **Wix is no longer used for any page rendering** — only for member login, payment checkout (PayPal via Wix Pricing Plans API), and the coach accept landing page redirect.

**Wix page code** (in Wix Velo): fetches member identity via `wix-members-frontend`, reads `CLIENT_API_KEY` from Wix Secrets Manager via a backend web module (`backend/secrets.web.js`), builds the portal URL with auth params, and listens for `wix-checkout` postMessages to trigger `checkout.startOnlinePurchase()`.

**postMessage protocol** (portal ↔ child pages ↔ Wix):
- `{ type: 'portal-navigate', target: 'pricing' }` — child page requests portal navigation
- `{ type: 'wix-checkout', planId: '...' }` — pricing page → portal → Wix (triggers PayPal checkout)
- `{ type: 'wix-handoff', email, firstName, surname, wixMemberId }` — Wix → portal → child page (identity forwarding)

### Wix → HTML Data Handoff

Client-facing pages receive identity data from Wix via URL params passed through the portal:
`?email=...&firstName=...&surname=...&wixMemberId=...&key=...&api=...`

### Entitlement System

**Server-side gate** (`entitlements_api.py`): `GET /api/entitlements/summary?email=` (OPS_KEY auth). Returns `can_upload`, `block_reason`.

**Client-side gate** (`client_api.py`): `GET /api/client/entitlements` (CLIENT_API_KEY auth). Returns role, plan status, credits, plans page URL.

| Condition | Locker Room | Media Room |
|---|---|---|
| **Coach role** | Green notice: view only | Blocked with message |
| **No active plan** | Amber banner + plans link | Blocked with plans link |
| **Credits = 0** | Red dismissible banner | Blocked with top-up link |
| **Account terminated** | Full-screen overlay | Full-screen overlay |
| **Active, credits > 0** | Normal view | Normal upload flow |

### Auth Pattern

- **Ops endpoints**: `OPS_KEY` via `X-Ops-Key` header or `Authorization: Bearer <key>`
- **Video worker**: `VIDEO_WORKER_OPS_KEY` for worker auth, `VIDEO_TRIM_CALLBACK_OPS_KEY` for callback auth (must match main API's `OPS_KEY`)
- **Client API**: `CLIENT_API_KEY` via `X-Client-Key` header
- **Coach accept**: token-based (no API key — the invite token IS the auth)

### Idempotency Patterns

- **Billing consumption**: unique constraint on `task_id`
- **Entitlement grants**: unique on `(account_id, source, plan_code, external_wix_id)`
- **Bronze ingest**: advisory locks on `task_id` to prevent concurrent ingests
- **Customer notify**: checks `wix_notified_at` before sending (SES + Wix both use this gate)
- **Coach invite token**: unique partial index on `invite_token WHERE invite_token IS NOT NULL`

### Required Environment Variables

#### Main API (webhook-server)

**Required (service will fail without these):**

| Env Var | Source File(s) | Notes |
|---|---|---|
| `DATABASE_URL` | `db_init.py` | PostgreSQL connection string. Falls back to `POSTGRES_URL` then `DB_URL`. Normalized to `postgresql+psycopg://` |
| `OPS_KEY` | `upload_app.py`, `probes.py`, `entitlements_api.py`, `members_api.py`, `ingest_bronze.py`, `ui_app.py` | Ops auth key for server-to-server endpoints |
| `CLIENT_API_KEY` | `client_api.py` | Auth key for all client-facing `/api/client/*` endpoints |
| `S3_BUCKET` | `upload_app.py`, `client_api.py` | S3 bucket for uploads, profile photos, footage |
| `AWS_REGION` | `upload_app.py`, `client_api.py`, `coach_invite/email_sender.py`, `coach_invite/video_complete_email.py` | AWS region. Default: `us-east-1` |
| `AWS_ACCESS_KEY_ID` | implicit (boto3) | AWS credentials for S3 and SES |
| `AWS_SECRET_ACCESS_KEY` | implicit (boto3) | AWS credentials for S3 and SES |
| `SPORT_AI_TOKEN` | `upload_app.py` | SportAI API token. RuntimeError on SportAI submit if missing |
| `INGEST_WORKER_BASE_URL` | `upload_app.py` | URL of the ingest worker service |
| `INGEST_WORKER_OPS_KEY` | `upload_app.py` | Auth key for ingest worker calls |

**Required for video trim pipeline:**

| Env Var | Source File(s) | Notes |
|---|---|---|
| `VIDEO_WORKER_BASE_URL` | `upload_app.py`, `video_pipeline/video_trim_api.py` | URL of the video trim worker service |
| `VIDEO_WORKER_OPS_KEY` | `upload_app.py`, `video_pipeline/video_trim_api.py` | Auth key for video worker |
| `VIDEO_TRIM_CALLBACK_URL` | `video_pipeline/video_trim_api.py` | Callback URL for trim completion |
| `VIDEO_TRIM_CALLBACK_OPS_KEY` | `video_pipeline/video_trim_api.py` | Auth key for callback (must match main API's `OPS_KEY`) |

**Required for Power BI integration:**

| Env Var | Source File(s) | Notes |
|---|---|---|
| `POWERBI_SERVICE_BASE_URL` | `upload_app.py`, `client_api.py` | URL of the Power BI service |
| `POWERBI_SERVICE_OPS_KEY` | `upload_app.py`, `client_api.py` | Auth key for PBI service. Falls back to `OPS_KEY` |
| `PBI_TENANT_ID` | `powerbi_embed.py`, `azure_capacity.py` | Azure AD tenant ID |
| `PBI_CLIENT_ID` | `powerbi_embed.py`, `azure_capacity.py` | Azure AD app client ID |
| `PBI_CLIENT_SECRET` | `powerbi_embed.py`, `azure_capacity.py` | Azure AD app client secret |
| `PBI_WORKSPACE_ID` | `powerbi_embed.py` | Power BI workspace GUID |
| `PBI_REPORT_ID` | `powerbi_embed.py` | Power BI report GUID |
| `PBI_DATASET_ID` | `powerbi_embed.py` | Power BI dataset GUID |

**Optional (have sensible defaults):**

| Env Var | Default | Source File(s) | Notes |
|---|---|---|---|
| `SES_FROM_EMAIL` | `noreply@ten-fifty5.com` | `coach_invite/email_sender.py`, `coach_invite/video_complete_email.py` | SES sender address |
| `COACH_ACCEPT_BASE_URL` | `https://api.nextpointtennis.com` | `client_api.py` | Base URL for coach accept links |
| `LOCKER_ROOM_BASE_URL` | `https://www.ten-fifty5.com/portal` | `coach_invite/video_complete_email.py` | CTA link in video completion email |
| `PLANS_PAGE_URL` | `https://www.ten-fifty5.com/plans` | `client_api.py` | Plans page URL returned in entitlements |
| `SPORT_AI_BASE` | `https://api.sportai.com` | `upload_app.py` | SportAI API base URL |
| `SPORT_AI_SUBMIT_PATH` | `/api/statistics/tennis` | `upload_app.py` | SportAI submit endpoint path |
| `SPORT_AI_STATUS_PATH` | `/api/statistics/tennis/{task_id}/status` | `upload_app.py` | SportAI status endpoint path |
| `SPORT_AI_CANCEL_PATH` | `/api/tasks/{task_id}/cancel` | `upload_app.py` | SportAI cancel endpoint path |
| `AUTO_INGEST_ON_COMPLETE` | `1` | `upload_app.py` | Toggle auto-ingest on SportAI completion |
| `INGEST_REPLACE_EXISTING` | `1` | `upload_app.py` | Replace existing bronze data on re-ingest |
| `ENABLE_CORS` | `0` | `upload_app.py` | Enable CORS headers on API endpoints |
| `MAX_CONTENT_MB` | `150` | `upload_app.py` | Max upload size in MB |
| `MAX_UPLOAD_BYTES` | `20GB` | `upload_app.py` | Max multipart upload size |
| `MULTIPART_PART_SIZE_MB` | `25` | `upload_app.py` | Multipart chunk size |
| `S3_PREFIX` | `incoming` | `upload_app.py` | S3 key prefix for uploads |
| `S3_GET_EXPIRES` | `604800` (7 days) | `upload_app.py` | Presigned GET URL TTL in seconds |
| `INGEST_WORKER_TIMEOUT_S` | `10` | `upload_app.py` | HTTP timeout for ingest worker calls |
| `INGEST_STALE_AFTER_S` | `1800` | `upload_app.py` | Stale ingest detection threshold |
| `PBI_REFRESH_POLL_S` | `15` | `upload_app.py` | PBI refresh poll interval |
| `PBI_REFRESH_MAX_WAIT_S` | `1800` | `upload_app.py` | Max wait for PBI refresh completion |
| `PBI_REFRESH_TRIGGER_TIMEOUT_S` | `60` | `upload_app.py` | HTTP timeout for refresh trigger |
| `PBI_REFRESH_STATUS_TIMEOUT_S` | `60` | `upload_app.py` | HTTP timeout for refresh status check |
| `PBI_SUSPEND_AFTER_REFRESH` | `1` | `upload_app.py` | Suspend capacity after refresh completes |
| `BATCH_JOB_QUEUE` | `ten-fifty5-ml-queue` | `upload_app.py` | AWS Batch queue name (T5 pipeline) |
| `BATCH_JOB_DEF` | `ten-fifty5-ml-pipeline` | `upload_app.py` | AWS Batch job definition (T5 pipeline) |
| `BILLING_OPS_KEY` | falls back to `OPS_KEY` | `subscriptions_api.py`, `usage_api.py`, `coaches_api.py` | Billing-specific ops key |
| `VIDEO_WORKER_REQUEST_TIMEOUT_S` | `10` | `video_pipeline/video_trim_api.py` | HTTP timeout for video worker requests |

**Legacy (Wix transition — remove when Wix payment is retired):**

| Env Var | Source File(s) | Notes |
|---|---|---|
| `WIX_NOTIFY_UPLOAD_COMPLETE_URL` | `upload_app.py`, `ingest_worker_app.py` | Wix notify webhook URL |
| `RENDER_TO_WIX_OPS_KEY` | `upload_app.py`, `ingest_worker_app.py` | Wix notify auth key |
| `WIX_NOTIFY_TIMEOUT_S` | `upload_app.py`, `ingest_worker_app.py` | Default: `15` |
| `WIX_NOTIFY_RETRIES` | `upload_app.py`, `ingest_worker_app.py` | Default: `3` |

#### Ingest Worker

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `INGEST_WORKER_OPS_KEY` | — | **Required** (startup crash) | Auth for POST /ingest |
| `DATABASE_URL` | — | **Required** (via `db_init.py`) | — |
| `OPS_KEY` | `""` | Optional (fallback for PBI service key) | — |
| `POWERBI_SERVICE_BASE_URL` | `""` | Required for PBI refresh | — |
| `POWERBI_SERVICE_OPS_KEY` | falls back to `OPS_KEY` | Optional | — |
| `VIDEO_WORKER_BASE_URL` | `""` | Required for video trim | — |
| `VIDEO_WORKER_OPS_KEY` | `""` | Required for video trim | — |
| `INGEST_REPLACE_EXISTING` | `1` | Optional | — |
| `WIX_NOTIFY_UPLOAD_COMPLETE_URL` | `""` | Optional (legacy) | — |
| `RENDER_TO_WIX_OPS_KEY` | `""` | Optional (legacy) | — |
| `WIX_NOTIFY_TIMEOUT_S` | `15` | Optional | — |
| `WIX_NOTIFY_RETRIES` | `3` | Optional | — |

#### Power BI Service

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `OPS_KEY` | `""` | **Required** (auth fails without) | — |
| `DATABASE_URL` | — | **Required** (via `db_init.py`) | For session lease store |
| `PBI_TENANT_ID` | — | **Required** (RuntimeError) | Azure AD tenant |
| `PBI_CLIENT_ID` | — | **Required** (RuntimeError) | Azure AD app client ID |
| `PBI_CLIENT_SECRET` | — | **Required** (RuntimeError) | Azure AD app secret |
| `PBI_WORKSPACE_ID` | — | **Required** (RuntimeError) | Power BI workspace |
| `PBI_REPORT_ID` | `""` | Required unless fallback enabled | Report GUID |
| `PBI_DATASET_ID` | `""` | Required unless fallback enabled | Dataset GUID |
| `AZ_SUBSCRIPTION_ID` | — | **Required** (RuntimeError) | Azure subscription ID |
| `AZ_RESOURCE_GROUP` | — | **Required** (RuntimeError) | Azure resource group |
| `AZ_CAPACITY_NAME` | — | **Required** (RuntimeError) | Azure capacity name |
| `PBI_AUTOWARMUP_ON_EMBED` | `1` | Optional | Auto-resume capacity on embed |
| `PBI_DEBUG_ENDPOINTS` | `0` | Optional | Enable debug routes |
| `PBI_SESSION_LEASE_SECONDS` | `180` | Optional | Session lease duration (min 60) |
| `PBI_ALLOW_FALLBACK_ID_RESOLUTION` | `0` | Optional | Debug: auto-resolve IDs from API |
| `PBI_REQUIRE_RLS_IDENTITY` | `1` | Optional | Fail-closed RLS identity check |
| `PBI_HTTP_TIMEOUT_S` | `30` | Optional | HTTP timeout for PBI API calls |
| `PBI_SCOPE` | `https://analysis.windows.net/powerbi/api/.default` | Optional | OAuth scope |
| `AZ_CAPACITY_PROVIDER` | `Microsoft.PowerBIDedicated` | Optional | ARM resource provider |
| `AZ_API_VERSION` | `2021-01-01` | Optional | ARM API version |
| `AZ_HTTP_TIMEOUT_S` | `30` | Optional | HTTP timeout for ARM calls |
| `PORT` | `5000` | Optional | Service port |

#### Video Trim Worker (Docker)

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `VIDEO_WORKER_OPS_KEY` | — | **Required** (startup crash) | Worker auth |
| `S3_BUCKET` | `""` | **Required** | — |
| `AWS_REGION` | — | **Required** | — |
| `AWS_ACCESS_KEY_ID` | — | **Required** (implicit, boto3) | — |
| `AWS_SECRET_ACCESS_KEY` | — | **Required** (implicit, boto3) | — |
| `FFMPEG_BIN` | `ffmpeg` | Optional | Path to ffmpeg binary |
| `FFPROBE_BIN` | `ffprobe` | Optional | Path to ffprobe binary |
| `VIDEO_CRF` | `28` | Optional | FFmpeg CRF quality setting |
| `VIDEO_PRESET` | `veryfast` | Optional | FFmpeg encoding preset |
| `AUDIO_BITRATE` | `96k` | Optional | Audio bitrate |
| `MIN_KEEP_SEGMENT_S` | `0.25` | Optional | Minimum segment length |
| `FFMPEG_TIMEOUT_S` | `1800` | Optional | FFmpeg process timeout |
| `FFPROBE_TIMEOUT_S` | `60` | Optional | ffprobe timeout |
| `TRIM_MIN_DISK_FREE_MB` | `500` | Optional | Minimum free disk space |
| `VIDEO_TRIM_CALLBACK_TIMEOUT_S` | `20` | Optional | Callback HTTP timeout |
| `VIDEO_TRIM_CALLBACK_MAX_RETRIES` | `3` | Optional | Callback retry count |
| `VIDEO_TRIM_CALLBACK_RETRY_BASE_S` | `2.0` | Optional | Callback retry backoff base |
| `TRIM_LOG_DIR` | `/tmp/trim_logs` | Optional | Log directory |

#### Locker Room

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `PORT` | `5050` | Optional | Service port |

No other env vars — serves static HTML only, no DB or S3 access.

#### Cron Jobs

**`cron_capacity_sweep.py`:**

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `OPS_KEY` | — | **Required** (startup crash) | — |
| `DATABASE_URL` | — | **Required** | For DB queries |
| `RENDER_POWERBI_BASE_URL` | `""` | Optional | PBI service URL (sweep skipped if missing) |
| `PBI_REFRESH_STALE_S` | `600` (10 min) | Optional | Stuck PBI refresh threshold |
| `INGEST_STALE_S` | `1800` (30 min) | Optional | Stuck ingest threshold |
| `TRIM_STALE_S` | `1800` (30 min) | Optional | Stuck trim threshold |

**`cron_monthly_refill.py`:**

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `BILLING_OPS_KEY` or `OPS_KEY` | — | **Required** (one must be set) | Auth for refill API call |

#### Lambda (`lambda/ml_trigger.py`)

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `BATCH_JOB_QUEUE` | — | **Required** (KeyError) | AWS Batch queue |
| `BATCH_JOB_DEF` | — | **Required** (KeyError) | AWS Batch job definition |
| `DATABASE_URL` | — | **Required** (KeyError) | — |

#### ML Pipeline Docker (`ml_pipeline/__main__.py`)

| Env Var | Default | Required? | Notes |
|---|---|---|---|
| `S3_BUCKET` | — | **Required** in Batch mode (KeyError) | — |
| `DATABASE_URL` | — | **Required** (via `db_schema.py`) | — |
| `AWS_REGION` | `us-east-1` | Optional | — |
| `FFMPEG_BIN` | `ffmpeg` | Optional | For local transcode |

### S3 CORS

The S3 bucket (`nextpoint-prod-uploads`) requires CORS for browser-to-S3 multipart uploads (Media Room) and video playback (Locker Room):
- **AllowedMethods**: GET, PUT, POST, HEAD
- **AllowedHeaders**: `*`
- **ExposeHeaders**: `ETag` (required for multipart upload completion)
- **AllowedOrigins** must include: `https://locker-room-26kd.onrender.com`, ten-fifty5.com variants, Wix editor/site domains

### Cron Jobs (Render)

- **`cron_capacity_sweep.py`** — runs every few minutes. Sweeps stale PBI sessions (suspends capacity if idle), detects stuck PBI refreshes, stuck ingests, and stuck video trims.
- **`cron_monthly_refill.py`** — monthly billing entitlement refill. Calls `POST /api/billing/cron/monthly_refill` on the main API.

### Diagnostics

- `GET /__alive` — liveness probe (from `probes.py`)
- `GET /ops/routes?key=<OPS_KEY>` — list all registered routes
- `GET /ops/db-ping?key=<OPS_KEY>` — DB connectivity check

### Code Organisation

New features **must live in their own subdirectory** (not loose files in the repo root). Examples: `video_pipeline/`, `ml_pipeline/`, `coach_invite/`. Each directory should be a self-contained package with its own `__init__.py`. The repo root is for service entry points only (`*_app.py`, `wsgi.py`).

**Exception**: the Locker Room SPA files (`locker_room.html`, `media_room.html`, `portal.html`, `backoffice.html`, `analytics.html`, `pricing.html`, `coach_accept.html`, `players_enclosure.html`) live in the repo root because `locker_room_app.py` serves them with `send_file()` from the working directory.

**iOS iframe CSS rules**: All pages run inside a nested iframe (Wix → portal → page). On iOS Safari, `100vh` refers to the outer viewport, not the iframe. Portal uses `height: 100%` (not `vh`). All inner pages use `viewport-fit=cover` meta tag, `padding-bottom: 300px` on mobile, and no `min-height: 100vh` on body. Autofill inputs use `-webkit-box-shadow` override to replace browser blue with `var(--bg)`.

### ML Pipeline (`ml_pipeline/`)

ML inference pipeline for tennis video analysis. Supports both local dev mode and AWS Batch production mode (S3 input → GPU processing → PostgreSQL + S3 output).

**Run:**
```bash
pip install -r ml_pipeline/requirements.txt
python -m ml_pipeline <video_path>                          # local mode
python -m ml_pipeline --job-id <job_id> --s3-key <s3_key>  # AWS Batch mode
python -m ml_pipeline.test_pipeline                         # unit test
bash ml_pipeline/deploy_aws.sh                              # deploy infra
bash ml_pipeline/test_e2e.sh <video_path>                   # e2e test
```

**Docker build** (from repo root):
```bash
docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline .
```
Base image: `nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04`. PyTorch installed from `--extra-index-url https://download.pytorch.org/whl/cu121` to use system CUDA. `.dockerignore` excludes `.venv/`, `.git/`, etc. Model weights (`ml_pipeline/models/`, ~135MB, git-ignored) must be present on disk at build time.

**Architecture:** `config.py` (tunable params) → `video_preprocessor.py` (OpenCV frames) → `court_detector.py` (14 keypoints → homography) → `ball_tracker.py` (TrackNet V2) → `player_tracker.py` (YOLOv8) → `pipeline.py` (orchestrator) → `heatmaps.py` (matplotlib) → `db_writer.py` (PostgreSQL) → `api.py` (Flask blueprint).

**AWS Resources (eu-north-1):** ECR (`ten-fifty5-ml-pipeline`), Batch compute (`ten-fifty5-ml-compute`, Spot G4dn.xlarge, 0–4 vCPUs), Batch queue (`ten-fifty5-ml-queue`), Batch job def (`ten-fifty5-ml-pipeline`, 4 vCPU, 15GB RAM, 1 GPU, 2hr timeout), CloudWatch logs (`/aws/batch/ten-fifty5-ml-pipeline`, 30-day retention). IAM roles: `ten-fifty5-ml-instance-role` (EC2), `ten-fifty5-ml-job-role` (ECS tasks, S3 + CloudWatch access), `aws-ec2-spot-fleet-tagging-role` (Spot Fleet). All tagged `Project=TEN-FIFTY5`.

**Spot GPU quota**: AWS account needs "All G and VT Spot Instance Requests" quota >= 4 vCPUs in eu-north-1 (Service Quotas → Amazon EC2). Default is 0 — must be requested.

**Batch job post-processing** (`__main__.py`): after pipeline completes, the Batch job also: (1) transcodes source video to H.264 MP4 via FFmpeg, (2) uploads to `trimmed/{job_id}/practice.mp4`, (3) updates `bronze.submission_context.trim_output_s3_key`, (4) deletes raw source from S3. Both steps are non-fatal.

**`deploy_aws.sh` on Windows/Git Bash**: paths starting with `/` get mangled by MSYS2. Prefix commands with `MSYS_NO_PATHCONV=1` or run the Batch/CloudWatch steps manually.

**Database Schema** (`ml_analysis.*`, managed by `db_schema.py`): `video_analysis_jobs`, `ball_detections`, `player_detections`, `match_analytics`.

**API Endpoints** (OPS_KEY auth): `GET /api/analysis/jobs/<job_id>`, `GET /api/analysis/results/<match_id>`, `GET /api/analysis/heatmap/<job_id>/<type>`, `POST /api/analysis/retry/<job_id>`.

**Models:** TrackNet V2 (`tracknet_v2.pt`, 41MB), YOLOv8m (`yolov8m.pt`, 50MB), Court detector (`court_keypoints.pth`, 41MB). See `config.py` for download sources.

### Wix Remaining Dependencies

Wix pages have been retired. The portal (Render-hosted) is the sole frontend. **Wix is only used for:**
- **Member authentication**: Wix login + member identity passed to portal via URL params
- **Payment checkout**: `checkout.startOnlinePurchase(planId)` via Wix Pricing Plans API / PayPal
- **Subscription event webhook**: Wix fires `POST /api/billing/subscription/event` after payment

### Future: Full Wix Removal

When Wix is fully retired (own auth + own payments):
- Consolidate: `_ensure_submission_context_schema` DDL into `db_init.py`
- Drop: `wix_notified_at` / `wix_notify_status` / `wix_notify_error` columns (no longer written, historical only)
- Replace: Wix member auth with own auth (e.g., magic link or OAuth)
- Replace: Wix Pricing Plans with direct PayPal/Stripe integration
- Remove: `external_wix_id` references once own auth is in place

### Other

- **`superset/`**: Optional Superset BI deployment config (Power BI is primary). Not in `render.yaml`.
- **`migrations/`**: One-off backfill SQL scripts. No automated migration framework — schema is managed idempotently via `db_init.py`.
- **`_archive/`**: Deprecated/replaced code kept for reference.
- **`lambda/`**: AWS Lambda function source (e.g., S3 trigger for ML pipeline).
