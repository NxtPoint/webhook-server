# DATA-INVENTORY.md

> **Purpose.** Catalogue every place customer / match / subscription data lives today, and **which copy is the source of truth**. Audience: Tomo + future Claude sessions, for planning the Render-native single-source-of-truth migration. Companions: [`ARCHITECTURE.md`](ARCHITECTURE.md), [`WIX-DEPENDENCY.md`](WIX-DEPENDENCY.md).
>
> **Freshness.** 2026-06-16 from code. Tables marked *(inferred)* were deduced from query usage rather than an explicit `CREATE TABLE` in the files read — verify against the live DB before relying on exact columns. Line numbers drift; file is the anchor.

---

## 1. Where data lives — the short version

| Store | Owner | What's in it | Source of truth for |
|---|---|---|---|
| **Postgres `bronze.*`** | us (Render DB) | Raw SportAI JSON + per-task match context | Match metadata, video location, ingest/trim status |
| **Postgres `silver.*`** | us | One row per shot (analytics) | Point-level analytics, outcomes, serve/return classification |
| **Postgres `gold.*`** | us | Thin per-chart views | Client-facing dashboard shapes (derived only) |
| **Postgres `ml_analysis.*`** | us | T5 detections, job tracking, training corpus | T5 ML facts, GPU job state |
| **Postgres `billing.*`** | us | Accounts, members, entitlements, sub state | Customer profiles, credits/usage, coach links |
| **AWS S3** | us | Uploaded + trimmed video, ML outputs, training labels, profile photos | Video + binary artefacts |
| **Clerk** (external) | Clerk | Login identity, password, session JWT, social/OAuth | **Auth identity** (LIVE 2026-06-17; `auth_v2` verifies the JWT, maps to `core.user`) |
| **PayPal** (direct) | PayPal | Card/payment records, plan catalogue, subscription billing | Payment instruments + charges (we never see these); plan catalogue mirrored in `paypal_billing/catalog.json` |
| ~~**Wix Studio**~~ (RETIRED 2026-06-17) | — | Was auth + Pricing Plans + subscription webhook | **Inert; rollback only** (`PAYPAL_ENABLED=0` / legacy `CLIENT_API_KEY`). See `WIX-DEPENDENCY.md` |

**The split that matters now:** our DB owns *everything about the analysis, the customer profile, billing state, and (since the Clerk cutover) the identity mapping in `core.user`*. The **external front door is Clerk (identity) + PayPal (payment)**; the **authoritative subscription state is our `billing.subscription_state`**, written one-way by the PayPal webhook. Wix is retired (rollback only). See `WIX-DEPENDENCY.md`.

---

## 2. Bronze — raw ingest (`db_init.py`)

| Table | Holds | Notes |
|---|---|---|
| `bronze.raw_result` | JSONB snapshot of SportAI payloads (`payload_json`/`payload_gzip`, sha256) | Verbatim audit copy |
| `bronze.submission_context` | **Per-task orchestration + match metadata** | The master control row — see below |
| `bronze.player` | Per-player metadata (activity score, distance, swing counts, heatmaps) | From SportAI |
| `bronze.player_swing` | One row per swing event (serve flag, swing_type, hit/impact coords, rally bounds) | |
| `bronze.rally` | Rally groupings (start/end/length) | |
| `bronze.ball_position` | Per-frame ball x/y (GENERATED STORED from JSON) | indexed `(task_id, timestamp)` |
| `bronze.ball_bounce` | Bounce events (court_x/y GENERATED, image coords) | |
| `bronze.player_position` | Per-frame player court position | |
| `bronze.session_confidences` | Tracking + court-detection confidence | |
| `bronze.team_session` | Player count + A/B player ids | |
| `bronze.session`, `thumbnail`, `highlight`, `bounce_heatmap` | Registry + assets | |
| `bronze.unmatched_field`, `debug_event` | Unmapped / debug data | |

**`bronze.submission_context` — the load-bearing table.** Source of truth for match metadata + pipeline state. Key columns:
- Customer identity: `email`, `customer_name`
- Match context: `sport_type`, `match_date`, `start_time`, `location`
- Player identity (point-in-time snapshot, **not** linked to `billing.member`): `player_a_name`, `player_b_name`, `player_a_utr`, `player_b_utr`
- Score: `player_{a,b}_set{1,2,3}_games`, `first_server` (`S`/`R`/`player_a`/`player_b` mapping override)
- Video: `s3_bucket`, `s3_key` (original), `trim_output_s3_key`, `video_url`, `share_url`
- Pipeline state: `ingest_started_at`, `ingest_finished_at`, `ingest_error`, `last_status`, `last_result_url`, `session_id`
- Trim: `trim_status` (`queued`/`accepted`/`completed`/`failed`), `trim_requested_at`, `trim_finished_at`, `trim_error`
- Notify: `ses_notified_at`, `ses_notify_error`
- **Soft-delete: `deleted_at`** (workers abort at 4 gates if set; never cascades to `billing.*`)

> **T5 nuance:** for T5, the *true* bronze is `ml_analysis.*`; `build_silver_match_t5.py` Pass 1 projects it into the bronze base facts. SportAI bronze is ingested verbatim from JSON.

---

## 3. Silver — analytical truth (`build_silver_v2.py`)

- **`silver.point_detail`** — one row per shot. Source of truth for point-level analytics. Built by a 5-pass SQL pipeline (insert from `player_swing` → bounce coords → serve/point/game structure + exclusions → zone classification + normalisation → analytics). Key fields: serve zones, rally locations (A–D), stroke type, aggression/depth, outcome (Winner/Error/In), serve try (1st/2nd/Double), ace/DF, `exclude_d` (warmup/replay/gap filter), and a **`model` column (`'sportai'` vs `'t5'`)** so both pipelines coexist in one table.
- **`silver.practice_detail`** — practice equivalent (3-pass, `build_silver_practice.py`).
- **`silver.technique_*`** — technique-analysis silver.

Architectural rule: **silver owns analytics; T5 silver inherits bronze base facts 100% verbatim (hit-driven)**. Don't aggregate in Python/JS if a view can do it.

---

## 4. Gold — presentation views (`gold_init.py`, `db_init.py`)

Thin views, one per chart/widget, aggregating silver into the exact dashboard shape. **Derived only — never a source of truth.** Examples:
- `gold.vw_player`, `gold.vw_point` — base dimension/fact views (player A/B assignment, flattened points)
- `gold.match_kpi`, `gold.match_serve_breakdown`, `gold.match_return_breakdown`, `gold.match_rally_breakdown`, `gold.match_rally_length`, `gold.match_shot_placement`
- `gold.vw_client_match_summary` — client match-list sidebar (filters `deleted_at IS NULL`, `sport_type='tennis_singles'`)

Recreated on every boot (`DROP+CREATE` in one transaction). Consumed by `client_api.py` thin passthroughs + the LLM coach.

---

## 5. `ml_analysis.*` — T5 ML pipeline (`ml_pipeline/db_schema.py`)

| Table | Holds | Source of truth for |
|---|---|---|
| `ml_analysis.video_analysis_jobs` | One row per T5 job: status, stage, video metadata (fps/dims/codec), perf (`ms_per_frame`), court detection, output S3 keys, `batch_job_id`/`arn`, `estimated_cost_usd` | T5 job state + GPU cost tracking |
| `ml_analysis.ball_detections` | Per-frame ball (x/y, court coords, speed, `is_bounce`, `source`) | T5 ball facts |
| `ml_analysis.player_detections` | Per-frame player bbox + center + court coords + `keypoints` (JSONB) + `stroke_class` | T5 player/pose facts |
| `ml_analysis.match_analytics` | Aggregated per-job stats | T5 match aggregates |
| `ml_analysis.training_corpus` | Dual-submit label index (SA↔T5 pairs, label kind, S3 key, counts) | Training-label provenance; UNIQUE `(sa_task_id, t5_task_id, label_kind)` |

> High-volume, frame-level data. Note: Batch direct-writes to `ml_analysis.*` are overwritten by the Render re-ingest (DELETE+COPY from the S3 JSON export) — new bronze facts must be added to **both** the export and the T5 ingest.

---

## 6. `billing.*` — accounts, profiles, money

> Some DDL is created lazily (on first webhook/endpoint); a few tables are *(inferred)* from query usage. Verify columns against live DB.

### Identity
- **`billing.account`** — one per email. `email` (UNIQUE), `primary_full_name`, **`external_wix_id`** (Wix member UUID), `currency_code`, `active`. **Source of truth for the customer account + email↔account mapping.**
- **`billing.member`** — many per account (primary + children + coaches). `full_name`, `role` (`player_parent`/`coach`), `is_primary`, plus profile: `surname`, `phone`, `utr`, `dominant_hand`, `country`, `area`, `profile_photo_url`, and **child fields: `dob` (DATE), `skill_level`, `club_school`, `notes`**. **Source of truth for player/child/coach profile data.** ⚠️ Stores minors' DOB + profile — see compliance risk in `ARCHITECTURE.md` §6.2.

### Entitlements / credits
- **`billing.entitlement_grant`** — credits added (`source`: `wix_subscription`/`wix_payg`/`manual_adjustment`/`signup_bonus`, `matches_granted`, `techniques_granted`, validity). Idempotent UNIQUE `(account_id, source, plan_code, external_wix_id)`.
- **`billing.entitlement_consumption`** — credits used. **One per match, idempotent on `task_id` (UNIQUE).** Source of truth for usage.
- **`billing.vw_customer_usage`** — computed granted/consumed/remaining per account.
- **`billing.entitlements`** *(cache)* — upserted summary on each `/api/entitlements/summary` call (can_upload, block_reason, remaining counts). Derived cache, not truth.

### Subscription state (fed by the PayPal webhook; Wix webhook = rollback)
- **`billing.subscription_state`** *(inferred)* — `account_id` (UNIQUE), `plan_id` (PayPal Billing-Plan id, or legacy Wix UUID), `plan_code`, `plan_type` (`recurring`/`payg`), `matches_granted`, `status` (ACTIVE/CANCELLED/EXPIRED), period dates, **`billing_provider`** (`wix`/`paypal` — the monthly cron refills only `wix`), **`provider_subscription_id`** (PayPal `I-…` for cancel). **Our authoritative copy of subscription state — a one-way mirror written by the PayPal webhook** (see below).
- **`billing.subscription_event_log`** *(inferred)* — audit log of PayPal + Wix webhooks, dedup by `event_id` (sha256).
- **`billing.monthly_refill_log`** *(inferred)* — idempotency for the refill cron, UNIQUE `(account_id, year_month)`.

### Coach access
- **`billing.coaches_permission`** — owner→coach grants (`coach_email`, `status` INVITED/ACCEPTED/REVOKED, `invite_token`). UNIQUE `(owner_account_id, coach_email)`. Token is the auth for accept.

---

## 7. S3 — files & blobs

Bucket = `S3_BUCKET` env (prod: `nextpoint-prod-uploads`); region `AWS_REGION`. Read access via 7-day presigned URLs.

| Content | Key pattern | Tracked in |
|---|---|---|
| Original upload | varies | `bronze.submission_context.s3_key` |
| Trimmed match video | `trimmed/{task_id}/review.mp4` | `submission_context.trim_output_s3_key` |
| Trimmed practice video | `trimmed/{job_id}/practice.mp4` | (practice) — survives original deletion |
| Bronze export JSON | varies | `ml_analysis.video_analysis_jobs.bronze_s3_key` |
| Ball / player heatmaps | varies | `video_analysis_jobs.ball_heatmap_s3_key`, `player_heatmap_s3_keys` (JSONB) |
| Training labels | `training/labels/{t5_task_id}_*.json` | `ml_analysis.training_corpus.label_s3_key` |
| Profile photo | varies (presigned PUT) | `billing.member.profile_photo_url` |

> **Retention note:** original uploads are deleted post-trim (the `s3_key` 404s); the frame-aligned full video survives as `trimmed/<task>/practice.mp4`.

---

## 8. Externally-held data (Clerk / PayPal; Wix retired 2026-06-17)

| Data | Held by | We hold | Sync direction | Truth |
|---|---|---|---|---|
| Login credentials / password | Clerk | ❌ | — | **Clerk** |
| Auth session / JWT | Clerk | verified per-request (`auth_v2`) | Clerk → us (Bearer JWT) | **Clerk** |
| Email | Clerk (login) | `billing.account.email` + `core.user`/`core.account` | Clerk → us (signup + JWT claim) | **Clerk** login master; our copy authoritative for billing |
| First/last name | entered at signup | `billing.member.full_name`/`surname` | user → us | **us** (editable our side) |
| `wixMemberId` | — (legacy) | `billing.account.external_wix_id` | — | **inert** (Wix retired) |
| Plan catalogue (prices, intervals) | PayPal | `paypal_billing/plans.py` + `catalog.json` (live ids) | us → PayPal (catalog script) | **us** — `plans.py` is canonical; legacy Wix UUIDs in `pricing.html` = fallback only |
| Payment / card records | PayPal | ❌ | — | **PayPal** (we never see card data) |
| Full subscription ledger/history | PayPal | current state in `billing.subscription_state` | PayPal → us (webhook) | **PayPal** (full history); our DB = authoritative current state |

Our profile-only fields (UTR, dominant hand, country, children, coach permissions, all match/analysis data) have **no external copy** — we are already the source of truth for those.

---

## 9. Source-of-truth quick map

| Data category | Source of truth | Table(s) |
|---|---|---|
| Auth identity / login | **Clerk** | (external) → mapped to `core.user.auth_provider_uid` |
| Payment | **PayPal (direct)** | (external); catalogue in `paypal_billing/` |
| Current subscription state | **us** (PayPal-webhook-fed) | `billing.subscription_state` |
| Customer account | us | `billing.account` (email UNIQUE) |
| Player/child/coach profile | us | `billing.member` |
| Credits / usage | us | `entitlement_grant` + `entitlement_consumption` (task_id UNIQUE) |
| Coach access | us | `billing.coaches_permission` |
| Match metadata + video location | us | `bronze.submission_context` |
| Point-level analytics | us | `silver.point_detail` |
| Client dashboards | us (derived) | `gold.*` |
| T5 ML facts | us | `ml_analysis.*` |
| Video + binaries | us | S3 |

---

## 10. Sensitive data (PII / minors / financial)

- **PII:** `account.email`, `member.{full_name, surname, phone, profile_photo_url}`, `submission_context.{email, customer_name, player_a_name, player_b_name}`.
- **Minors:** `member.dob`, `skill_level`, `club_school`, `notes` (child profiles) + **video + biometric pose keypoints** (`ml_analysis.player_detections.keypoints`). ⚠️ No consent/age-gate/retention logic in code (confirmed none formal today).
- **Financial:** `entitlement_grant.matches_granted`, `subscription_state.status`+periods, `video_analysis_jobs.estimated_cost_usd`. Card data never touches our systems (PayPal).

---

## 11. Cannot determine from code

1. What Clerk stores internally about members (full identity profile, auth tokens, session/event history).
2. PayPal payment ledger + reconciliation (we hold current subscription state only). Legacy Wix internals matter only for the retained rollback path.
3. Whether any analytics warehouse / CDP / support tool is wired (none found here).
4. SES email *template* content (only trigger points are in-repo).
5. S3 bucket policy, encryption-at-rest, lifecycle rules.
6. GDPR/COPPA retention + erasure process (none in code).
7. Exact DDL for the lazily-created/`(inferred)` billing tables — verify on live DB.
