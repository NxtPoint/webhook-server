# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here — what to read first

Pick the closest match and jump there before reading the rest of this file:

> **Snapshots vs. ground truth.** Specific numbers, job-def revisions, bench baselines, and "current phase" notes in this file are point-in-time snapshots and drift. On any conflict, `.claude/next_session_pickup.md` + the live `.claude/session_*.md` in `git status` win.

- **Any session, any task** → `.claude/next_session_pickup.md` (current state + read-order for the next move). **Overwrite it at session end** so the next session inherits cleanly. A modified `.claude/session_*.md` in `git status` is the live thread for deep detail.
- **Routine ops** ("when X happens, do Y") → `.claude/sop.md`. Render deploys, Batch container deploys, bench discipline, phase transitions, GPU box experiments, prod SQL diag, plus the short list of actions that genuinely require Tomo.
- **Session boot / close checklists** → `.claude/session_protocol.md`. Run boot in the first 5 min; close before declaring done.
- **Doc tier system + lifecycle** → `.claude/docs_hygiene.md`. Five tiers (TRUTH / REFERENCE / STRATEGY / HISTORICAL / MEMORY) + when NOT to write a new doc.
- **T5 ML pipeline / serve detector / Batch / silver_t5** → **first** `docs/north_star.md` §"★ RULES OF THE GAME" (bronze = single source of truth; silver inherits 100% / does no work; one-model-per-fact; build-first / train-last; keep-it-clean). Then the macro plan in the rest of `docs/north_star.md`; how to run/validate/ship in `.claude/handover_t5.md` (read "NEXT SESSION" + "TEST HARNESS"). 18-base-field audit: `docs/_investigation/bronze_silver_18_audit.md`. **Run `bench` before any `ml_pipeline/serve_detector/` edit.**
- **Dashboards / gold views / endpoint mapping** → `docs/dashboards.md`.
- **Public marketing site / blog / SEO / backlinks** → `docs/seo_marketing_migration.md` (architecture + cutover — DONE 2026-06-15: `www`→Render marketing, Wix app→its wixstudio URL) + `docs/seo_backlink_kit.md` (off-page/backlinks). **Publish a blog post**: drop a `<slug>.md` (frontmatter: title/description/date, optional `image: /blog/images/<file>` for a hero + index thumbnail) in `frontend/blog/_posts/`, run `.venv/Scripts/python build_blog.py`, then commit + push. No more Wix blog.
- **Business rules / account model / credits / entitlements / soft-delete / share + referrals + pricing-pivot** → `docs/business.md` (canonical for *how the product behaves*).
- **Growth / CRM / admin cockpit / event + page tracking / feedback+NPS / consent / canonical `core.*` DB / de-Wix auth + payment** → `marketing_crm/STATUS.md` (the living hymn sheet — start here), the canonical model in `core_db/` (code: `models.py` / `schema.py` / `repositories/`) with the design rationale in root-level `DB-SCHEMA-PROPOSAL.md`, and the root system-map docs `ARCHITECTURE.md` / `DATA-INVENTORY.md` / `WIX-DEPENDENCY.md` / `AUTH-MIGRATION-PLAN.md`. **Fresh session picking up auth or payment → `HANDOVER.md`** (read-order + kickoff prompts). Most `marketing_crm` features are now **always-on** (de-gated 2026-06-17 — cockpit/consent/feedback/tracking/core_api register unconditionally; `crm_sync` self-gates on HubSpot/Klaviyo key presence). `AUTH_V2_ENABLED` + `PAYPAL_ENABLED` keep their flags for rollback. **Lane guard:** `.githooks/pre-commit` blocks code commits unless `CLAUDE_CODE=1` (docs commit freely) — keeps the Cowork/content lane out of code.
- **Pricing tier numerics / plan IDs / marketing copy** → `docs/pricing_strategy.md`.
- **Billing implementation** (file map, entry points, flows) → `docs/billing.md`. Behaviour → `docs/business.md`.
- **Module-level orientation** → `<module>/README.md` first. READMEs exist for: `coach_invite/`, `tennis_coach/`, `support_bot/`, `technique/`, `video_pipeline/`, `cleanup/`, `lambda/`, `migrations/`, `frontend/`.
- **Ops endpoints / `/ops/*` reference** → `docs/ops_runbook.md`.
- **Environment variables (any service)** → `docs/env_vars.md`.
- **Technique pipeline** → `docs/technique.md` + `technique/README.md`.
- **Support bot** → `docs/support_bot.md` + `support_bot/README.md`.

## Things not to do (load-bearing)

These look reasonable but will burn future sessions. Each is an explicit decision.

1. **Don't run `pytest` or add it as a dependency.** No suite exists; testing is manual against the live Render DB. The only regression gate is `python -m ml_pipeline.diag.bench` (mandatory before any `serve_detector` push). A few `python -m` scripts are git-tracked but are *not* a suite (`serve_detector/tests/test_components.py` is a pure-logic check; `ml_pipeline/test_pipeline.py` and `test_e2e.sh` need a gitignored `test_videos/` dir or full AWS). Extend `bench`/`bench_ball`/`bench_silver` instead of growing a pytest suite.
2. **Don't aggregate in Python or JavaScript if a gold view can do it.** SQL views own aggregation, Python is a thin passthrough, frontend is pure rendering. Adding `groupby` / `reduce` in `client_api.py` or a chart file means you skipped the right layer — extend or add a `gold.*` view.
3. **Don't import `upload_app` from the ingest worker.** The worker is deliberately self-contained (calls `ingest_bronze_strict()` directly). Importing the main app pulls in Flask boot side-effects and breaks the worker timeout split (3600s vs 1800s).
4. **Don't `DELETE FROM billing.*` on match delete.** Matches are billable events — the consumption record stays. Match delete is soft-delete only via `submission_context.deleted_at`; workers honour this at four gates. See `cleanup/orphan_sweep.py`.
5. **Don't push T5 `serve_detector` changes without `bench` green.** Two fixtures are locked in `ml_pipeline/diag/bench_baseline.json`: `ea1e500c`=12/26 (CI-gated — the only fixture in `fixtures_ci/`; rev-72 clean coordinates, SA truth `ba4812be` 26 serves) and `880dff02`=23/24 (local-only; warp-era, guards the legacy `is_bounce` path). `python -m ml_pipeline.diag.bench` checks both; CI checks just `ea1e500c`. Three prior silent regressions motivated this rule. The far 0/12 on `ea1e500c` is upstream (far court_y NULL in serve windows; ROI sweep is rally-gated past serves) — gate-tuning to chase it backfires; it's coverage + training territory.
6. **Don't add ops endpoints with query-string `?key=` auth.** `_guard()` in `upload_app.py` rejects it to keep `OPS_KEY` out of access logs. Header-only (`X-Ops-Key` or `Authorization: Bearer`).
7. **Don't ask the user to rerun an ingest before `git push`.** Render deploys from `origin/main`; the Render shell would otherwise execute stale code.
8. **Don't merge a T5 detector branch without the Batch-side change check.** Bench green ≠ Batch in sync. Any diff against a path the Batch image bundles — **canonical list = the `COPY` lines in `ml_pipeline/Dockerfile`** (`roi_extractors/`, `serve_detector/`, `stroke_classifier/`, `bounce_detector/`, `models/`, `pipeline.py`, `__main__.py`, `config.py`, `db_writer.py`, …) plus the Dockerfile and `ml_pipeline/requirements.txt` themselves — requires Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1 before rerun. Full checklist: `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST". **When adding a new Batch-side module, add its `COPY` line in the same commit** — `__main__.py` wraps stages in try/except, so a missing COPY skips the stage silently. Origin: Phase 1 shipped 2026-05-07 with `extract_far_pose` only on Render; `db_writer.py` joined 2026-05-22; `bounce_detector/` was caught missing 2026-06-05.
9. **Don't skip, relax, or work around the bench CI check.** A red bench is a real regression — `bench.yml` replays the CI fixture against the locked baseline. Revert, reproduce locally with `python -m ml_pipeline.diag.bench`, ship a fix that turns it green. Weakening the gate (lowering baseline, narrowing trigger globs, removing the workflow) is never the right move — the silent slip from `0cb645a` is exactly why the harness exists.
10. **Don't auto-spawn a task without a paired server-side trigger.** Browser-polling ingest gates (like `/upload/api/task-status`) only fire when a user has the page open. Auto-spawned tasks have no browser → ingest never starts and the task sits in `queued` forever. Every auto-spawn must be paired with a cron, webhook, or sweep endpoint — `/ops/sweep-t5-orphans` was added for exactly this gap.
11. **Don't change T5 silver row-generation (or chase SportAI reconciliation in silver) until the 18 bronze base fields align with SportAI in `ml_analysis.*`.** The T5 "bronze" is `ml_analysis.*`; `build_silver_match_t5.py` Pass 1 is the bronze→base-fact projection that must reconcile, and passes 3-5 are silver analytics on top. Reconciliation gaps (e.g. the Forehand undercount) are **bronze accuracy** problems — far-player pose coverage, bounce/ball coordinate accuracy, A/B identity — not silver-derivation problems. We proved this on 2026-05-25 when pivoting Pass 1 to stroke-driven row generation overshot (the stroke detector's hitter attribution is perspective-biased to the near player). **UPDATE 2026-06-14 (Tomo):** the silver ROW ARCHITECTURE is now settled = HIT-DRIVEN (one row per stroke event = one shot; bounce is an attribute — `docs/north_star.md` §"SILVER ROW ARCHITECTURE"), and `T5_STROKE_DRIVEN_SILVER` now **DEFAULTS ON** (the hit-driven path is live). The "wait until bronze is right" hold was lifted because T5 silver is not consumed by prod — the architecture is correct *now*, accuracy fills in at training (build-first/train-last). What's still gated on bronze accuracy is not the *architecture* but the *numbers* (far attribution ~19% gate per `bench_hit`; sharp-far retrain, DoD #8). Rollback: `T5_STROKE_DRIVEN_SILVER=0`. The rule's spirit still holds for SportAI reconciliation: don't chase SA parity with silver heuristics — fix bronze. See `docs/north_star.md` §"Bronze-first" + §"DEFINITION OF DONE" and `docs/_investigation/far_player_accuracy.md`. **UPDATE 2026-06-16:** bronze deterministic DEV is now COMPLETE — every clean code fix is shipped; the residual numbers (stroke WHEN/WHO recall, bounce recall, swing-type accuracy, far position) are strictly training/data, validated on the reference pair (SA `079d2c62` ↔ T5 `375198f5`) and reconciled per RULE 6. Receipts: `.claude/audit_bronze_build_2026-06-16.md`.
12. **Don't use feature branches.** Commit and `git push` directly to `main`, every time — Render deploys from `origin/main` and this repo's whole workflow assumes a single line of history. The one exception is overnight/unsupervised Batch-side work, which is branch-only by deliberate policy (small blast radius — see auto-memory `feedback_overnight_branch_only`).

## Services and how to run

Python 3.12 / Flask + Gunicorn, deployed on Render (see `render.yaml`):

| Service | Start command | Entry point |
|---|---|---|
| **Sport AI - API call** (main API, `api.nextpointtennis.com`) | `gunicorn wsgi:app` | `wsgi.py` → `upload_app.py` |
| **Ingest worker** | `gunicorn ingest_worker_app:app` | `ingest_worker_app.py` |
| **Video trim worker** | Docker (`Dockerfile.worker`) | `video_pipeline/video_worker_wsgi.py` |
| **Locker Room** (static) | `gunicorn locker_room_app:app` | `locker_room_app.py` |

The main service is `name: webhook-server` in `render.yaml` (legacy slug) but Render UI/billing shows **"Sport AI - API call"** — prefer the display name in conversation. The Locker Room service serves HTML SPAs from `frontend/` via `send_file()` (Flask + gunicorn only, no DB); the main API also serves them as same-origin backups for iframe API access.

> **Footgun:** the root-level `marketing_app.py` is **not wired into `render.yaml`** and does **not** deploy. The live marketing site is served host-switched by `locker_room_app.py` — edit that, not `marketing_app.py`.

**Public marketing site is served by the Locker Room service, host-switched** (commit `1a1b5fc` — deliberately no second Render service). In `locker_room_app.py`, `_is_marketing_host()` checks `request.host` against `MARKETING_HOSTS` (`www.ten-fifty5.com` / `ten-fifty5.com`, extendable via the `MARKETING_HOSTS` env var). On a marketing host, `/` → `home.html` and `/pricing` → `pricing_public.html`; on every other host (the locker-room `onrender.com` URL) those paths are the unchanged app pages. Marketing-only paths (`/overview`, `/coaching`, `/academies`, `/contact-us`, `/blog`, `/post/<slug>`, generated `/robots.txt` + `/sitemap.xml`) are pure additions. The point is native, fully-crawlable HTML (no Wix iframe/JS) so the indexed URLs carry their rankings over. **LIVE since 2026-06-15**: `www` + apex `ten-fifty5.com` point at this service; the Wix app (login/portal/checkout) moved to its free Wix Studio URL `https://info5945780.wixstudio.com/online-tennis-analyt` (the `my.` subdomain was abandoned — Wix Studio refuses plain subdomains). `marketing_app.py` is a **standalone variant of the same site, not wired into `render.yaml`** — `locker_room_app.py` is the deployed path; don't edit `marketing_app.py` expecting it to ship. Background: `docs/seo_marketing_migration.md`, memory `project_marketing_site_render_migration`.

**Blog is statically generated** by `build_blog.py` (dependency-free, no framework): drop `frontend/blog/_posts/<slug>.md` with `title`/`description`/`date` frontmatter (optional `image: /blog/images/<file>` → hero + index thumbnail; served via the `/blog/images/<f>` route), run `.venv/Scripts/python build_blog.py`, commit the generated `frontend/blog/*.html`. Each post gets the shared nav + footer, Article + BreadcrumbList JSON-LD, Open Graph tags (its own hero as the OG card), a canonical at `/post/<slug>`, and is auto-added to the generated sitemap. Markdown supports `##`–`####` headings, lists, `**bold**`, `*italics*`, links, and pipe tables.

**Marketing assets + polish (2026-06-15):** all 6 marketing pages + the blog share one sticky centered top-nav (current page highlighted) + footer, unified 1200px width, WCAG-AA contrast, and a skip-to-content link. Brand favicon (`/favicon.svg|.ico|.png`, `/apple-touch-icon.png`), per-page social cards (`/og/<file>`, 1200×630), and a **branded 404** (`frontend/404.html` via the `locker_room_app.py` errorhandler — HTML for browsers, JSON for `/api`·`/ops`). The design system is duplicated per file, so site-wide nav/colour/width changes are N-file edits. Full reference: `docs/seo_marketing_migration.md` + `frontend/README.md`.

**Shell** — default is PowerShell (use `$null` not `/dev/null`, `$env:VAR` not `$VAR`, backtick for line continuation, `if ($?) { B }` not `A && B`). Bash also available via the Bash tool.

**`python` invocation (Windows)** — there is no project `python` on PATH; always invoke the venv interpreter explicitly: `.venv\Scripts\python -m ml_pipeline.diag.bench` (PowerShell) or `.venv/Scripts/python -m …` (Git Bash). The bare `python -m …` forms shown throughout the T5 sections assume this venv is already activated.

**Local dev** (Windows / Win 11):
```bash
# Git Bash:
source .venv/Scripts/activate
# PowerShell:
.venv\Scripts\Activate.ps1
# then (either shell):
pip install -r requirements.txt
gunicorn wsgi:app --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 1800
```

**Deploy:** Render auto-deploys all four services on push to `origin/main`. Always `git push` *before* asking the user to rerun an ingest — otherwise the shell runs stale code (rule #7).

### Testing & CI

No automated test suite, no linter. Functional testing is manual against the live Render DB. Do not run `pytest`.

**The only CI check** is `.github/workflows/bench.yml` (the entire `.github/` surface). It runs `python -m ml_pipeline.diag.bench` and triggers on push to `main` and PRs touching:

- `ml_pipeline/serve_detector/**`
- `ml_pipeline/diag/{bench.py,replay_serves.py,bench_baseline.json,requirements-bench.txt}`
- `ml_pipeline/fixtures_ci/**`
- `build_silver_v2.py`
- `.github/workflows/bench.yml`

Only these paths gate CI — `bench_ball*` / `bench_silver*` are local-only and deliberately *not* triggers. Don't widen or narrow this glob set (rule #9).

Replays the committed CI fixture (`ml_pipeline/fixtures_ci/ea1e500c.pkl.gz`) against the locked baseline (`bench_baseline.json`, `ea1e500c`=12/26 — that fixture only; the file also locks `880dff02`=23/24 for the local `bench` run). Exits non-zero on any negative delta. Sub-second; no DB, no AWS, no weights. Details: `.claude/handover_t5.md` §"TEST HARNESS".

### Schema management

No migration framework. Schema is managed idempotently across multiple `_init` / `_ensure_*` functions (grep `CREATE TABLE IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS` to find them). Bronze tables boot via `db_init.py::bronze_init()`; gold views recreate on every boot via `gold_init.py`, `tennis_coach/coach_views.py`, and `technique/gold_technique.py`.

**Gold view recreation wraps the entire DROP+CREATE loop in a single transaction.** Postgres DDL is transactional and takes AccessExclusiveLock on each view → concurrent readers block until COMMIT and then see the new views atomically. A single view failure rolls back the whole batch (we keep the previous working set rather than a half-applied mix).

---

## Architecture overview

### Data layers (medallion)

```
bronze.*  →  silver.*  →  gold.*  →  API  →  Dashboards + LLM Coach
  raw        analytical    thin          thin        rendering /
 ingest      point-level   per-chart     pass-       LLM context
             (fact)        views         through
```

**Bronze** (`bronze.*`): raw SportAI JSON ingested verbatim. `db_init.py` owns schema. Key tables: `raw_result`, `submission_context`, `player_swing`, `rally`, `ball_position`, `ball_bounce`, `player_position`.

**Silver** (`silver.*`): single source of truth for match-level analytics.
- `silver.point_detail` — one row per shot. Derived: serve zones (`serve_side_d`, `serve_bucket_d`), rally locations (A-D), aggression (Attack/Neutral/Defence), depth (Deep/Middle/Short), stroke (Forehand/Backhand/Serve/Volley/Slice/Overhead/Other), outcome (Winner/Error/In), serve try (1st/2nd/Double), ace/DF detection, normalised coordinates. Built by `build_silver_v2.py` (5-pass SQL). `model` column distinguishes `'sportai'` vs `'t5'` so both pipelines coexist.
- `silver.practice_detail` — practice equivalent. Built by `ml_pipeline/build_silver_practice.py` (3-pass).

**Gold** (`gold.*`): presentation layer. Thin views — one per chart or one per widget — that aggregate silver into exactly the shape the frontend needs. Same views feed dashboards and LLM coach. Full catalogue: `docs/dashboards.md`.

**Architecture rule** (rule #2): SQL views own aggregation. Python API endpoints are thin passthroughs. Frontend is pure rendering. Never aggregate in Python/JS if a view can do it once.

### Silver V2 (`build_silver_v2.py`)

Current prod implementation. 5-pass SQL pipeline:
1. Insert from `player_swing` (core fields)
2. Update from `ball_bounce` (bounce coordinates)
3. Serve detection + point/game structure + exclusions
4. Zone classification + coordinate normalization
5. Analytics (serve buckets, stroke, rally_length, aggression, depth)

Court geometry constants live in `SPORT_CONFIG` at the top. T5 silver builders (`ml_pipeline/build_silver_match_t5.py` and `build_silver_practice.py`) call passes 3-5 directly to share the derivation logic.

### Service topology

Media Room uploads video to S3 → `POST /api/submit_s3_task` → main app routes by `sport_type`:
- **SportAI** (`tennis_singles`): async submit → poll → delegate to ingest worker → bronze → silver → trim → SES
- **T5** (`*_practice`, `tennis_singles_t5`): AWS Batch → sentinel `t5://complete/{id}` → in-process `_do_ingest_t5` → bronze (from `ml_analysis`) → silver → trim → notify
- **Technique** (`technique_analysis`): single background thread → external API → bronze → silver → trim → notify (no auto-ingest routing, no sentinel URL)

The ingest worker is **self-contained** — does NOT import `upload_app.py`, calls `ingest_bronze_strict()` directly (rule #3). Worker timeout 3600s vs main app 1800s.

### Main app (`upload_app.py`)

S3 presigned URLs + multipart, sport-routed submission, task-status orchestration, auto-ingest, video-trim callback, SES notify, CORS preflight for `/api/client/*`. Blueprints: grep `app.register_blueprint`.

**On-boot init order** (each try/except-wrapped so one failure can't kill the service; order matters because later steps may read earlier views):
1. `gold_init_presentation()` — `gold.vw_player`, `vw_point`, `match_*`, `player_performance`
2. legacy `gold_init()` — `gold.vw_client_match_summary` (feeds `/api/client/matches` sidebar; will be replaced by `gold.match_kpi`)
3. `init_tennis_coach()` + register `coach_bp` — `gold.coach_*` views + `tennis_coach.coach_cache`
4. `init_support_bot()` + register `support_bp` — `support_bot.conversations` + `faq_cache`
5. register `cleanup.orphan_sweep_bp` — `POST /ops/orphan-sweep`
6. register `diag_sql.diag_sql_bp` — `POST /ops/diag/sql`
7. `technique_bronze_init()` + `ensure_silver_schema()` + `init_technique_gold_views()`
8. `init_auth_v2(app)` — Clerk JWT verifier boot hook (logs state; the actual dual-mode auth is in `client_api._guard()` / `resolve_principal`)
9. always-on `marketing_crm` + `core_api` registrations (de-gated 2026-06-17): cockpit (`+ init_cockpit_views()`), consent, feedback, tracking beacon, `core_api.core_bp` — each registers unconditionally now (no `*_ENABLED` flag)

### Video trim pipeline

Fire-and-forget async: ingest worker (match) or `_do_ingest_t5` (practice) calls `trigger_video_trim(task_id)` → loads silver, builds EDL → POSTs to video worker → worker spawns detached subprocess → downloads from S3 → FFmpeg re-encodes → uploads `trimmed/{task_id}/review.mp4` → callback updates `bronze.submission_context.trim_status`.

For practice the trim source is `trim_output_s3_key` (the ML-produced `practice.mp4`), not the deleted original.

---

## Subsystems

### Dashboards & gold views
Custom ECharts + canvas SPAs (`match_analysis.html`, `practice.html`) backed by thin gold views. Match dashboard has 4 modules: Match Analytics, Placement Heatmaps, Player Performance, AI Coach. Practice is the reference design for new dashboards. Full catalogue + endpoint mapping + LLM Coach data flow: `docs/dashboards.md`. LLM Coach design: `docs/llm_coach_design.md`.

### Billing (`billing.*`)
Credit-based usage tracking. Core: `billing_service.py`, `models_billing.py`, `billing_import_from_bronze.py`. Tables: `Account`, `Member`, `EntitlementGrant`, `EntitlementConsumption`. View: `billing.vw_customer_usage`. Blueprints: `subscriptions_api`, `usage_api`, `entitlements_api`.

Key patterns:
- 1 task = 1 match consumed (idempotent via `task_id` unique constraint)
- Entitlement grants idempotent via `(account_id, source, plan_code, external_wix_id)`
- **Immediate credit grant on purchase**: `subscription_event()` → `grant_entitlement()` instantly on `PLAN_PURCHASED + ACTIVE`
- `billing_import_from_bronze.py` syncs completed tasks into consumption records, auto-creating accounts
- `entitlements_api.py` gates uploads (allows if active subscription OR remaining credits)

**`billing.member` is the single source of truth** for customer/player/child/coach profile data. Match-level `player_a_name` / `player_b_name` stored separately in `bronze.submission_context` as point-in-time snapshots.

### Coach invite
Owner invites coaches from the Locker Room "Invite Coach" tab. Data in `billing.coaches_permission`. Module: `coach_invite/` (`db.py`, `email_sender.py`, `video_complete_email.py`, `accept_page.py`).

- Client endpoints (`client_api.py`): `GET /api/client/coaches`, `POST /api/client/coach-invite` (creates row + token + SES email), `POST /api/client/coach-revoke` (clears token).
- Accept flow: `GET /coach-accept?token=…` → `coach_accept.html` → `POST /api/coaches/accept-token` (**token IS the auth**; validates against `billing.coaches_permission`, sets ACCEPTED, clears token, redirects to portal).
- Idempotent: re-inviting a revoked coach reuses the row (status → INVITED, new token, new email). Tokens single-use.

### Email (AWS SES)
Two emails (both in `coach_invite/`): coach invite (on `POST /api/client/coach-invite`) and video complete (ingest step 7 + task-status auto-fire, idempotent via `ses_notified_at`).

SES region `eu-north-1` (Stockholm, matches Render). IAM user `nextpoint-uploader` needs `ses:SendEmail` / `ses:SendRawEmail`. Domain `ten-fifty5.com` verified via DKIM. Must be out of sandbox to send to unverified recipients. Env: `SES_FROM_EMAIL` (default `noreply@ten-fifty5.com`), `COACH_ACCEPT_BASE_URL` (default `https://api.nextpointtennis.com`), `LOCKER_ROOM_BASE_URL` (default `https://www.ten-fifty5.com/portal`).

### Support bot (`support_bot/` + `frontend/support.html`)
Portal chat using Claude Haiku 4.5. FAQ-only (answers strictly from `support_bot/faq.md`), forced tool-use for structured output, auto-escalates account-specific questions to `info@ten-fifty5.com` via SES. Surface: `GET /help`. API: `/api/support/{ask,feedback,escalate,health}` under `X-Client-Key` auth. Kill switch: `SUPPORT_BOT_ENABLED=false`. FAQ is the load-bearing artefact (5 seeded, ~30 planned). Full reference: `docs/support_bot.md`.

### Client API (`client_api.py`)
Auth: `X-Client-Key` header. Admin endpoints additionally require email in `ADMIN_EMAILS` (hardcoded: `info@ten-fifty5.com`, `tomo.stojakovic@gmail.com`). Surface: customer-facing dashboard data + profile / entitlements / members / matches / footage URLs + `/backoffice/*` admin endpoints. Dashboard endpoints: `docs/dashboards.md`. Full list: grep `@.*\.route` in `client_api.py`.

### Growth / CRM stack (`marketing_crm/` + `core_db/` + `core_api/`)
The de-Wix growth + canonical-data layer. **Now always-on** (de-gated 2026-06-17 — cockpit/consent/feedback/tracking/core_api register unconditionally; `crm_sync` self-gates on HubSpot/Klaviyo keys). Living status doc: `marketing_crm/STATUS.md` (start here). `marketing_crm/` sub-packages: `backoffice` (admin cockpit), `consent` (consent capture + biometric/parental modals), `privacy` (policy + decisions), `tracking` (page-view beacon + event tracking → `core.*`), `feedback` (in-app feedback + NPS), `klaviyo` / `crm_sync` (CRM flows), `outreach`, `contracts`. The canonical DB is `core_db/` (`models.py` / `schema.py` / `repositories/`, schema `core.*` — customers/users/subs/matches/usage/feedback/consent), surfaced over HTTP by `core_api/` (`/api/core/*`, registered in boot, dual-mode auth). `core.user` carries `auth_provider` + `auth_provider_uid` — **now LIVE for Clerk** (the de-Wix auth target, shipped). Design rationale: root `DB-SCHEMA-PROPOSAL.md`; system maps: `ARCHITECTURE.md` / `DATA-INVENTORY.md` / `WIX-DEPENDENCY.md` / `AUTH-MIGRATION-PLAN.md`; auth/payment read-order: `HANDOVER.md`.

### Locker Room SPAs (`frontend/`)
All auth via URL params forwarded through the portal: `?email=&firstName=&surname=&wixMemberId=&key=&api=`.

**Design system**: shared CSS variables, Inter font, green/amber/red palette, `.toggle-group` / `.toggle-btn` buttons, ECharts helpers (`eBar`, `eStackedBar`, `ePie`, `eGauge`) defined identically in every file.

Pages:
- `/` Locker Room — dashboard, header tabs (Account / My Details / Linked Players / Invite Coach), charts (matches per month, usage gauge), match history.
- `/media-room` — 4-step upload wizard (game type → upload → details → progress). Game types: Singles (SportAI, prod), Singles T5 / Serve / Rally / Technique (dev-only, gated to `tomo.stojakovic@gmail.com`).
- `/pricing` — fetches entitlements, renders new-plan / top-up-only / coach view. Sends `postMessage({type:'wix-checkout', planId})` to Wix for PayPal checkout.
- `/portal` — **entry point**. Collapsible sidebar, inner iframe per child page. **Standalone on Render, reached top-level from `/login` (Clerk)** — NOT embedded in a Wix iframe (the Wix auth handoff was removed 2026-06-17). Nav: Dashboard (`/dashboard`), Upload Match, My Profile, Analytics (Match Analytics, Placement Heatmaps), Plans & Pricing (`/plans`), Backoffice (admin), **Business Cockpit (admin → `/cockpit`)**, Practice (WIP). Auth is dual-mode via `TFAuth` (`/auth_client.js`): a Clerk session (live path) or the legacy `?email=&key=` URL params (fallback).
- `/login` — **Clerk sign-in/sign-up** (Google + email). The live login door; on success forwards to `/portal`. Served by the locker-room service; renders a "being set up" notice if `AUTH_V2_ENABLED!=1`.
- `/dashboard`, `/plans` — dedicated, NON host-switched routes for the dashboard + pricing SPAs (the portal nav loads these, because `/` and `/pricing` serve the marketing pages on a marketing host).
- `/practice`, `/match-analysis` — analytics SPAs (see `docs/dashboards.md`).
- Public marketing (served host-switched by the Locker Room service — see Services table): `/` (`home.html`), `/overview` (`how_it_works.html`), `/pricing` (`pricing_public.html` on a marketing host), `/coaching` (`for_coaches.html`), `/academies` (`for_academies.html`), `/contact-us` (`contact.html`), `/blog`, `/post/<slug>`. Blog HTML is generated by `build_blog.py` from `frontend/blog/_posts/*.md`.

**Wix dependencies — ALL MIGRATED 2026-06-16 (kept only as rollback fallbacks):**
1. Member authentication → **Clerk** (`auth_v2/`, dual-mode) — see the AUTH CUTOVER note below.
2. Payment checkout → **direct PayPal** (`paypal_billing/`, LIVE) — see the PAYMENT CUTOVER note below.
3. Subscription event webhook → **PayPal webhook** (`/api/billing/paypal/webhook`) feeds the same `apply_subscription_event` grant path; the Wix `/api/billing/subscription/event` endpoint stays for the fallback.

Since 2026-06-15 the Wix site (the above three) lives at its **free Wix Studio URL** `https://info5945780.wixstudio.com/online-tennis-analyt` — `www`/apex now serve the native Render marketing site. Wix flags a cosmetic "domain points away from Wix" warning in Domains — ignore it; never click "Try Again" (reverts `www` to Wix).

**AUTH CUTOVER — LIVE (de-Wix auth, Phases 0-3 done; Phase 4 = drop the shared key, pending):** marketing "Log in / Start Free" CTAs point at **`/login`** (Clerk sign-in, served by locker-room), and **Clerk is the only login door** — the Wix `postMessage` auth handoff was **removed from `portal.html` + `players_enclosure.html` (2026-06-17)**. New/returning users authenticate via **Clerk** (Google/email) → standalone Render `/portal`. Server side: `auth_v2/` verifies the Clerk JWT and the client APIs accept it **alongside** the legacy `CLIENT_API_KEY` (now a pure fallback across `client_api`/cockpit/consent/feedback/`support_bot`/`tennis_coach`/`core_api` — Phase 4 deletes it). Frontend: every logged-in SPA uses the shared `TFAuth` helper (`frontend/auth_client.js`, served at `/auth_client.js`, auto-injected by `_html()`) — **Clerk loads once in the portal (top frame); child iframes relay a fresh token per request via `postMessage`** (auth-once, no per-page re-init). **Clerk PRODUCTION instance is LIVE: `clerk.ten-fifty5.com` (`pk_live_…`, own Google OAuth, custom-domain DNS verified).** Env: `AUTH_V2_ENABLED=1` (both services), `AUTH_ISSUER`/`AUTH_JWKS_URL`=`clerk.ten-fifty5.com`, `CLERK_PUBLISHABLE_KEY`=`pk_live_…` (locker-room). Full plan: `AUTH-MIGRATION-PLAN.md`; status: `marketing_crm/STATUS.md`.

**PAYMENT CUTOVER — LIVE 2026-06-16 (direct PayPal):** `/pricing` now renders **native PayPal buttons** (PayPal JS SDK — Subscriptions API for recurring, Orders API for PAYG top-ups), NOT the Wix `postMessage` checkout. Module: `paypal_billing/` (`plans.py` + committed `catalog.json` with the live Product/Billing-Plan ids, `client.py` REST client, `webhook.py` receiver + checkout/cancel/config endpoints, dark `register(app)`). The webhook (`/api/billing/paypal/webhook`) verifies PayPal's signature → **refetches** the resource from PayPal → maps to the SHARED `subscriptions_api.apply_subscription_event(provider='paypal')` — **no duplicated billing logic**; `billing.*` only (core mirror deferred). Grant model = money-received: recurring on `PAYMENT.SALE.COMPLETED` (`valid_to`=next billing → no rollover), PAYG on capture (never expires); `subscription_state.billing_provider='paypal'` fences PayPal subs out of the Wix monthly-refill cron. Checkout endpoints use `client_api._guard` (Clerk JWT **or** legacy key) so they work on both the Wix-embedded and standalone Clerk portals. Gated by `PAYPAL_ENABLED=1` + `PAYPAL_ENV=live` (+ `PAYPAL_CLIENT_ID`/`SECRET`/`WEBHOOK_ID` dashboard secrets). **Rollback:** `PAYPAL_ENABLED=0` → instant Wix-checkout fallback, no deploy. Proven end-to-end on sandbox + a real live purchase. Runbook: `paypal_billing/README.md`. **Env-var gotcha:** a `render.yaml` value change (e.g. `PAYPAL_ENV`) may not auto-apply on push — set critical flips in the Render dashboard too and verify via `/api/billing/paypal/config`.

**iOS iframe CSS**: child pages run inside the portal → page iframe (the portal itself is now standalone top-level on Render, not nested in Wix). Use `height: 100%` (not `vh`), `viewport-fit=cover` meta tag, `padding-bottom: 300px` on mobile.

---

## Auth, idempotency, env vars, S3 CORS

### Auth
- **Ops**: `OPS_KEY` via `X-Ops-Key` header or `Authorization: Bearer <key>` (never query string — rule #6)
- **Video worker**: `VIDEO_WORKER_OPS_KEY` (worker auth), `VIDEO_TRIM_CALLBACK_OPS_KEY` (callback auth, must match main API `OPS_KEY`)
- **Client API**: dual-mode (de-Wix, 2026-06-17) — a per-user **Clerk JWT** via `Authorization: Bearer <token>` (verified by `auth_v2/`, email derived server-side) **OR** the legacy shared `CLIENT_API_KEY` via `X-Client-Key` + `?email` (fallback, slated for removal). Same dual-mode guard in `client_api`, cockpit, consent, feedback, `support_bot`, `tennis_coach`, `core_api`.
- **Coach accept**: token-based (the invite token IS the auth)

### Idempotency
- Billing consumption: unique on `task_id`
- Entitlement grants: unique on `(account_id, source, plan_code, external_wix_id)`
- Bronze ingest: advisory locks on `task_id`
- Customer notify: checks `wix_notified_at` / `ses_notified_at` before send
- Coach invite: unique partial index `WHERE invite_token IS NOT NULL`
- Gold views: `DROP VIEW IF EXISTS` + `CREATE VIEW` on every boot
- Coach cache: unique `(task_id, email, prompt_key)`

### Env vars
Full matrix (main API + workers + crons + Lambda + ML pipeline Docker): `docs/env_vars.md`.

Main API quick reference: `DATABASE_URL`, `OPS_KEY`, `CLIENT_API_KEY`, `ANTHROPIC_API_KEY`, `S3_BUCKET`, `AWS_REGION`, AWS keys, `SPORT_AI_TOKEN`, worker-pair URLs/keys (`INGEST_WORKER_*`, `VIDEO_WORKER_*`, `VIDEO_TRIM_CALLBACK_*`). Plus two operational tunables at the top of `upload_app.py`: `MAX_CONTENT_MB` (default 150, sets Flask's `MAX_CONTENT_LENGTH`) and `ENABLE_CORS` (default 0; the per-path `CORS_PATHS` allowlist runs independently of this flag).

### S3 CORS
Bucket `nextpoint-prod-uploads`. AllowedMethods GET/PUT/POST/HEAD; AllowedHeaders `*`; **ExposeHeaders `ETag` (required for multipart completion)**; AllowedOrigins include `https://locker-room-26kd.onrender.com`, `https://api.nextpointtennis.com`, ten-fifty5.com variants, Wix editor/site domains.

---

## Diagnostics & ops

All `/ops/*` use header-only auth (`X-Ops-Key: <OPS_KEY>` or `Authorization: Bearer <OPS_KEY>`). Query-string `?key=` is deliberately rejected by `_guard()` to keep `OPS_KEY` out of access logs.

- `GET /healthz` — liveness (main API, no auth, returns "OK"). The Locker Room service has its own `/__alive` at `locker_room_app.py:113`; the main API does NOT serve `/__alive`.
- `GET /ops/routes` — list registered routes
- `GET /ops/db-ping` — DB connectivity
- `POST /ops/compact-storage` — `VACUUM (FULL, ANALYZE)` over bronze/silver/ml_analysis tables, returns per-table `before/after/freed` bytes. Optional `{"only": ["schema.table", …]}` to scope. Takes ACCESS EXCLUSIVE per table — low-traffic only.
- `POST /ops/orphan-sweep` — soft-delete cascade mop-up. Two passes: (1) child rows of deleted `submission_context`, (2) true orphans with no `submission_context`. `{"dry_run": true}` reports counts; `{"include_orphans": false}` skips pass 2. Never touches `billing.*`. (`cleanup/orphan_sweep.py`)
- `POST /ops/sweep-t5-orphans` — fires `_start_ingest_background` for `tennis_singles_t5` tasks where `ingest_started_at IS NULL` but Batch is `complete`. Plugs the polling-gate gap (rule #10). `{"dry_run": true, "limit": 50, "min_age_minutes": 5}`. Idempotent via the inner ingest gate + `training_corpus` UNIQUE. Cron runs every 5 min via `cron_sweep_t5_orphans.py`.
- `POST /ops/diag/sql` — read-only SELECT runner for autonomous diagnostics. `{"sql": "...", "limit": 100}`. `sqlparse`-enforced single-statement `SELECT`/`WITH...SELECT`; keyword denylist + `statement_timeout=5s`. (`diag_sql/sql_endpoint.py`. Full constraints + curl: `docs/ops_runbook.md`.)

**Workers respect `submission_context.deleted_at`** — both `ingest_worker_app.py::_do_ingest` and `upload_app.py::_do_ingest_t5` check at four gates (`pre_start`, `pre_bronze`, `pre_silver`, `pre_trim`) and abort cleanly without re-populating bronze if a delete races with an in-flight ingest.

---

## Code organisation

New features **must live in their own subdirectory** with `__init__.py` (e.g. `video_pipeline/`, `ml_pipeline/`, `coach_invite/`, `tennis_coach/`, `cleanup/`). Repo root is for service entry points (`*_app.py`, `wsgi.py`, `gold_init.py`, `db_init.py`) and legacy top-level Flask blueprints.

**Blueprints registered on the main API** (grep `app.register_blueprint` for the wiring):

Always registered:
- `client_api.py` — `/api/client/*`, CLIENT_API_KEY auth. Customer-facing API (dashboard endpoints in `docs/dashboards.md`).
- `coaches_api.py` — `/api/coaches/*`, OPS_KEY auth. Server-to-server coach permission management over `billing.coaches_permission`; called internally by `client_api.py` coach endpoints.
- `members_api.py` — members CRUD.
- `subscriptions_api.py`, `usage_api.py`, `entitlements_api.py` — billing surface.
- `coach_invite.accept_bp` — `GET /coach-accept` + `POST /api/coaches/accept-token` (token IS the auth).
- `ingest_bronze` (no prefix) — bronze ingest HTTP surface from `ingest_bronze.py`.
- `ui_app.py` — **legacy** admin UI at `/upload/*`, OPS_KEY auth. Bronze/silver inspection via `render_template_string`. Not used by any SPA (`backoffice.html` is the real admin UI) — retained for shell/debug only.

Try/except-wrapped (failure is logged, service still boots):
- `tennis_coach.coach_bp` — LLM coach endpoints.
- `support_bot.support_bp` — `/api/support/*`.
- `cleanup.orphan_sweep_bp` — `POST /ops/orphan-sweep`.
- `diag_sql.diag_sql_bp` — `POST /ops/diag/sql`.
- `ml_pipeline.api.ml_analysis_bp` — local-only; import fails on Render (no `cv2` / `torch`). Dev diagnostics only, never serves prod.

Always-on (de-gated 2026-06-17 — `register()` registers unconditionally; each route keeps its own admin/auth gate):
- `marketing_crm` stack — registered in `upload_app.py` boot: `backoffice` (cockpit, `+ init_cockpit_views()`), `feedback`, `consent`, `tracking` page beacon. See `marketing_crm/STATUS.md`.
- `core_api.core_bp` — `/api/core/*`, thin HTTP layer over `core_db` repositories. **Wired into `upload_app.py` boot (2026-06-17)**; dual-mode auth (Clerk JWT via `resolve_principal`, or `CORE_API_KEY`/`CLIENT_API_KEY`). The canonical `core.*` surface.
- `auth_v2` — not a blueprint; `init_auth_v2(app)` boot hook + `resolve_principal` consumed by `client_api._guard()` and the other dual-mode guards.

**Cron scripts** (root-level, invoked by Render Cron Jobs, not blueprints):
- `cron_capacity_sweep.py` — periodic billing/capacity sweep (see `docs/billing.md`, `docs/env_vars.md`).
- `cron_monthly_refill.py` — monthly entitlement refill for active subscriptions.
- `cron_sweep_t5_orphans.py` — every 5 min; fires `POST /ops/sweep-t5-orphans` (pairs with rule #10).

**Ignorable root directories** (present on disk, not part of runtime):
- `diag_081e089c/`, `data/` — local investigation snapshots / scratch dumps (often gitignored).
- `static/`, `templates/` — Flask defaults; actual SPAs live under `frontend/`, inspection templates inlined in `ui_app.py`.

`frontend/` contains all SPA HTML; served by `locker_room_app.py` and (same-origin backups) `upload_app.py` via a `_html(name)` helper that resolves an absolute path under `frontend/`.

---

## T5 ML pipeline (`ml_pipeline/`)

In-house tennis video analysis. Runs on AWS Batch GPU (G5.xlarge/A10G primary, G4dn.xlarge / Spot fallback) for detection; runs on Render for serve detection + silver build. Handles `tennis_singles_t5`, `serve_practice`, `rally_practice`. Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`.

**Status 2026-06-16: BRONZE DETERMINISTIC DEV COMPLETE.** Every clean code fix is shipped; bronze emits all 18 base facts and silver inherits them 100% verbatim (hit-driven). The remaining gaps (stroke WHEN/WHO recall, bounce recall, swing-type accuracy, far-player position, per-shot ball_speed) are **training/data only** — the build-first/train-last endpoint is reached. Training is the final phase and accrues incrementally (free dual-submit corpus + GPU train env built). Validated on the reference pair SA `079d2c62` ↔ T5 `375198f5`; full receipts in `.claude/audit_bronze_build_2026-06-16.md`. Reconciliation method = RULE 6 (`docs/north_star.md`).

**Session start**: see the Start Here pointer (`docs/north_star.md` §"★ RULES OF THE GAME" first, then `.claude/handover_t5.md`). Run `.venv/Scripts/python -m ml_pipeline.diag.bench` to confirm the floor (ea1e500c=12/26, 880dff02=23/24) before touching code. The `.claude/` folder is **tracked in git** except per-run artefacts (`debug_frames_*/`, `eval_*.txt`, `reconcile_*.txt`, `run_status_*.md`) and the scratch `.claude/tmp/` and `.claude/worktrees/` dirs.

### Data flow

```
video.mp4 → Batch (court/ball/player detection) → ml_analysis.*
          → Render (serve_detector) → ml_analysis.serve_events
          → Render (build_silver_match_t5) → silver.point_detail (model='t5')
          → gold.* views → dashboards
```

Both T5 and SportAI share passes 3-5 in `build_silver_v2.py`. The serve detector is a separate pose-first module (`ml_pipeline/serve_detector/`, per Silent Impact 2025 + TAL4Tennis literature) that emits ServeEvent rows the silver builder consumes.

### Key directories

| Dir | Purpose |
|---|---|
| `ml_pipeline/` | Core detection (court, ball, player), `db_writer.py` (Batch-side writes to `ml_analysis.*`, `source='main'`), harness, evals |
| `ml_pipeline/serve_detector/` | Pose-first serve detection + rally state machine + schema. Consumes CNN `ball_bounces` with legacy `is_bounce` fallback (`05fe85d`). **2026-06-16: far-POSE serve path RETIRED in prod** via `SERVE_FAR_POSE_ENABLED=0` (`render.yaml`); code default stays ON so the CI bench (fixtures carry no model candidates) stays green. The trained `model_far` + near-pose cover the same real far serves — retiring far-pose dropped serves 55→28 vs SA 24 with **zero recall loss** (18/24), precision 33%→60%. Rollback: env=1. See RULE 6 + `.claude/audit_bronze_build_2026-06-16.md`. |
| `ml_pipeline/serve_model/` | Serve model v1 (`61b677b`) — far-serve candidate anchors + MLP scorer, ADR-01 bounce-recipe port. **LIVE and default-on**: Batch infer stage `serve_candidates` runs (`__main__.py`, `SERVE_MODEL_STAGE=1`), Render `serve_detector` merges `model_far` additively (`SERVE_MODEL_ENABLED` default 1, `detector.py:544`); validated end-to-end far 3/12→7/12 (rev 73, 2026-06-06). Local/CPU train via `python -m ml_pipeline.serve_model.train`. Rollback: either env=0 (no rebuild). Further gains = free training. |
| `ml_pipeline/stroke_detector/` | Velocity-signal stroke detection (`detector.py`, `velocity_signal.py`, `schema.py`) → `ml_analysis.stroke_events`. Home of the near-side swing-path precision gate (`9a4ab0a`). **Live heuristic detector** — distinct from `stroke_classifier/` (the trained swing-type CNN, proven on `375198f5`). |
| `ml_pipeline/hit_model/` | Per-candidate ball-hit model — B2 of the stroke arc (`c06a198`), serve-model recipe replayed (candidates/features/dataset/model/train). Classifies each ball-trajectory-discontinuity candidate as hit/bounce/noise; trains ~3 min CPU → `models/hit_model_v1.pt`. **Gate NOT met — local/CPU only, not in the Batch image.** Detection head beats the heuristic on precision (2.5×) and near-side pid-strict (24/51 vs 13/51); bottleneck is WHO attribution (far 6/51) polluted by bounce-candidates wearing hit labels. Labels = SA `player_swing` via corpus pairs, positional per-swing side (SA `player_id` is a PERSON and swaps ends at changeovers — see `.claude/next_session_pickup.md`). |
| `ml_pipeline/stroke_classifier/` | Optical-flow R(2+1)D swing-type classifier (ADR-02 v2, both players) → bronze `stroke_class`. **In the Batch image and ENABLED** (`SWING_CLASSIFIER_ENABLED` default 1, `pipeline.py:646`; rev-80 job-def `=1`) — now a 4-class model (the `other` class was added); swing bench LOCKED at macro-F1 0.7468 (GPU, `bench_swing_type`). Silver projects `stroke_class` verbatim (NULL falls back to the literal `"other"` sentinel — no heuristic). ✓ **PROVEN on real upload `375198f5`** (257 `stroke_class` rows: fh87/bh77/oh57/other36, 2026-06-16) — the "unproven" caveat is resolved. Rollback: `SWING_CLASSIFIER_ENABLED=0` (no rebuild). |
| `ml_pipeline/bounce_detector/` | CNN bounce bronze model (ADR-01 v2 — gravity-residual candidates → CNN scorer) → `ml_analysis.ball_bounces`. Runs Batch-side from `__main__.py` **before** the ROI sweep (match flow only) — its output rally-gates the far-pose ROI sweep (`328d3b8`); weights `models/bounce_detector_v2_7match.pt`. Trips rule #8. |
| `ml_pipeline/identity_detector/` | A/B player-identity detection (ADR-03 — changeover rule + game boundaries). Render-side (schema init on main-app boot), not in the Batch image. |
| `ml_pipeline/roi_extractors/` | Batch-side ROI extractors — `pose.py` (far-player ViTPose → `player_detections_roi`, wired in `ead857a`) + `bounces.py`. Trips the BATCH-SIDE CHANGE CHECKLIST (rule #8). |
| `ml_pipeline/point_structure/` | `point_boundaries.py` — point/game structure derivation. **Not used by the silver builders** (they derive point/game structure via `build_silver_v2` pass-3 SQL); imported only by `diag/audit_points.py`. Keep for that diag. |
| `ml_pipeline/ground_truth/` | Hand-labelled reference data backing the bench/eval harnesses |
| `ml_pipeline/training/` | TrackNet fine-tuning on dual-submit labels (`visual_debug/` is leftover local debug images, untracked — don't read or edit) |
| `ml_pipeline/diag/` | Dev tools — the bench family (`bench` / `bench_ball` / `bench_silver` are the load-bearing three; plus `bench_hit` (locked hit-model accuracy gate, `bench_baseline_hit.json` — NEAR/FAR/precision), `bench_bounce`, `bench_calib`, `bench_identity`, `bench_lens`, `bench_swing_type`, `bench_finetuned`), serve viewer, pose probe, plus `recon_line` (line-level SA-active vs T5-active reconciliation scorecard, ~12 fields, ~1s — the RULE 6 reconciliation tool) |
| `ml_pipeline/fixtures*/` | Locked bench fixtures (`fixtures_ci`, `fixtures_ball`, `fixtures_silver`, `fixtures_calib` — one dir per bench, plus a bare `fixtures/`) with `*_baseline.json` siblings in `diag/` |

Weights in `ml_pipeline/models/` (git-ignored, Batch-bundled via the Dockerfile `models/` COPY): TrackNet V2, YOLOv8x/m-pose, YOLOv8m, court_keypoints.pth, `bounce_detector_v2_7match.pt` (144KB), `swing_classifier_v2.pt` (125MB).

### Most-used commands

Full catalogue in `.claude/handover_t5.md`. The ones that come up constantly:

```bash
python -m ml_pipeline.diag.bench                        # serve detector regression (CI-gated; mandatory pre-push)
python -m ml_pipeline.diag.bench_ball                   # ball-tracker regression (tracknet_v2 + wasb; local-only)
python -m ml_pipeline.diag.bench_finetuned --weights-path <path>  # ball-bench against fine-tuned weights (Phase 5c.4)
python -m ml_pipeline.diag.bench_silver                 # silver-builder regression (local Docker Postgres; run --setup once to seed)
python -m ml_pipeline.harness validate <task_id>        # bronze + silver sanity
python -m ml_pipeline.harness eval-serve <task_id>      # pose-first serve detector vs SA
python -m ml_pipeline.harness reconcile <sa_tid> <t5_tid>
python -m ml_pipeline.diag.recon_line <t5_tid> --sa <sa_tid>  # line-level SA-active vs T5-active reconciliation (RULE 6)
python -m ml_pipeline.harness rerun-silver <task_id>    # fast — no Batch needed
python -m ml_pipeline.harness build-corpus              # dataset from ml_analysis.training_corpus (Phase 5c.3); --task <id>, --upload-s3
python -m ml_pipeline.harness verify-corpus-row <task_id>
python -m ml_pipeline.diag.serve_viewer <task_id> --video <path>
```

### Compute reality (2026-05-27)

On-demand G-family GPU is **available and prioritised** — eu-north-1 queue order 1 = `ten-fifty5-ml-ce-eu-ondemand` (EC2); Spot CE `ten-fifty5-ml-compute` is order-2 fallback (confirmed via job `9378f2dd`). The earlier "Spot-only / on-demand quota = 0 (2026-04-15)" reality is **stale** — quota was raised. Prioritise Europe + on-demand for long runs so they aren't Spot-eviction-exposed. Cross-region failover + Spot fallback playbook: `.claude/playbook_aws_batch_ondemand_fallback.md`.

---

## Technique analysis (`technique/`)

Biomechanics stroke analysis via external SportAI Technique API. Dev-only (gated to `tomo.stojakovic@gmail.com`). Sport type: `technique_analysis`. Synchronous streaming — a single background thread in `upload_app.py::_technique_run_pipeline()` does download → API call → bronze → silver → trim copy → SES notify, end-to-end. Full reference: `docs/technique.md`.

---

## Other

- **`docs/`**: feature design + reference. Active set listed in Start Here. Subdirs: `_investigation/` (deep-dive diagnoses, e.g. `far_player_accuracy.md` cited by rule #11), `sql/` (canonical diag queries, e.g. `reconcile_serves.sql`), `_archive/` (superseded).
- **`migrations/`**: one-off backfill SQL scripts. No migration framework — schema is idempotent via `_init` / `_ensure_*` functions.
- **Archived code/docs** live under `docs/_archive/` (superseded docs) and `.claude/_archive/` (old handover/session docs) — there is no root-level `_archive/`. Reference only; don't read unless chasing a specific historical regression.
- **`lambda/`**: AWS Lambda source (e.g., S3 trigger for ML pipeline).
- **`.claude/`**: handover docs + AWS Batch playbooks (tracked in git, see Start Here); per-run artefacts gitignored. Subdirs: `infrastructure/`, `research/`, `strategy/`, `serve_ground_truth/`, plus gitignored `tmp/` and `worktrees/`.
- **Auto-memory** (per-project, indexed by `MEMORY.md`, loaded into every conversation): historical T5 context (`project_t5_*.md`), user/feedback rules, feature-launch records. Check for "why did we decide X" before re-deriving from code. Local to the machine, not in git.
