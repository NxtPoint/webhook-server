# Next-session pickup — 2026-07-22 — SportAI pipeline audit + data-coverage sprint

> **Two parallel threads in this repo.** This pickup covers the **SportAI (`tennis_singles`) business-analytics pipeline**, the focus of the last few sessions. The **T5 ML pipeline** thread is *parked at "bronze DEV complete, training is the incremental remainder"* — its handover is `.claude/handover_t5.md` and the T5 memories, unchanged by this sprint.

## ⚡ Executive summary (read first)

A deep audit of the **SportAI analytics pipeline** — JSON → bronze → silver → gold → dashboard. Full findings + method: **`docs/_investigation/pipeline_end_to_end_audit_2026-07-19.md`** (single source of truth for this work; read it before touching silver/gold).

**Headline: the pipeline's core is correct** — validated serve/point/game structure against the owner's own video (task `052786b4`: 18 points, 2 games 1-1, 26 serves, 1 double fault — reproduced exactly). The audit found real but *narrow* defects on a sound core, plus a lot of SportAI data we ingest but don't use.

**Bench is GREEN** (`ea1e500c=12/26, 880dff02=23/24`) — run `.venv/Scripts/python -m ml_pipeline.diag.bench` before any `serve_detector`/`build_silver_v2` change.

## What shipped this sprint (all on origin/main, safe/flag-gated)

1. **Video quality gate revived** (`upload_app.py`) — SportAI `/api/videos/check` now gates submission; a definitively-bad video is rejected (clean 400, no credit spent) *before* analysis. Fails open on infra errors. `VIDEO_QUALITY_CHECK_ENABLED` (default on). Honest limit: catches low res/fps, NOT poor camera angle.
2. **Raw-JSON archive + schema-drift alarm** (`raw_archive/`) — every ingest stores the whole payload to `s3://<bucket>/raw-json/<task>.json.gz`; a new top-level SportAI key logs **SCHEMA DRIFT** + ops email. `RAW_ARCHIVE_ENABLED` (default on). *Fixes the source-of-truth loss: past matches' JSON was unrecoverable (not stored + 1-hour URL expiry).*
3. **Bounce-recall via debug candidates** (`ingest_bronze.py`, `build_silver_v2.py` pass-2) — recovers extra floor bounces from `debug_data.ball_bounces` (conf≥0.6 + plausible + non-dup), delivered-preferred. **`BOUNCE_CANDIDATES_ENABLED=1` — ENABLED on the ingest worker** (⚠ also set it in the Render dashboard — render.yaml value changes may not auto-apply). Validated +2-3 clean plottable shots on recent matches; safe no-op on pre-2026-06-22 matches (they lack bounce confidence).
4. **`bounce_plausible_d`** silver column (pass-6) — flags impossible bounces so heatmaps can omit them. Populated; **not yet consumed by the frontend** (to-do).
5. **team_session near/far fix** (`ingest_bronze.py`) — `player_a_id`(near)/`player_b_id`(far) were NULL for every match (extractor assumed a dict; SportAI sends a list). Now populated with SportAI's ghost-free identity. Capture only; unused so far.
6. **Env-gated serve source** (`SILVER_SERVE_SOURCE`) — `auto` (SA flag + geometric fallback) is built and **video-validated 26/26 on 052786b4** but **NOT enabled** (prod = `geometric`, which has 1 phantom serve on that match but correct 18 points). Owner deferred enabling.
7. **devenv/** — disposable local Postgres + real-bronze seed + silver diff harness + JSON coverage/drift checker. See `devenv/README.md`.

## Local dev environment (how everything was validated)

- Docker Postgres `localhost:55433` (NOT :55432 = CourtFlow). `docker compose -f devenv/docker-compose.yml up -d`.
- Read-only prod role `tf_readonly` in **`devenv/.env.local`** (gitignored). **Drop the role when finished** (`DROP OWNED BY tf_readonly; DROP ROLE tf_readonly;`).
- `SEED_SOURCE_URL=$(cat devenv/.env.local) python -m devenv.seed_local --task <uuid>` → `python -m devenv.diff_silver --task <uuid> --save/--vs`.
- **Seeded reference matches:** `052786b4` (owner ground truth, 18pts), `079d2c62` (SA pair, messy 4-ghost), `0336b82b` (pathological). Raw JSONs for the first two in the session scratchpad + `s3://…/raw-json/`.

## Open defects to fix (from the audit — ranked)

- **P1 serve service-box test** (`build_silver_v2.py:945`) — only test is "within 1.6m of the net"; no service-box check → a long double fault can score as an ace. Fix = real box test (centre line 5.485 = `MID_X_DEFAULT`; service lines y=5.485/18.285). **Highest-value open item.**
- **P1 first-serve % inflated** — `'Double'` on both serve rows of a DF point removes the 1st from the denominator (52.9% vs true 50.0%). Fix = separate `double_fault_d` flag.
- **P1 service-line constants** `6.40/17.37`→`5.485/18.285`; `shot_phase_d` zones mis-defined.
- **P1 hollow ingest bills the customer** — zero-row ingest marked `completed`, credit consumed. Add a zero-count guard.
- **P1** NULL→0% rendering (`match_analysis.html` `pctW`); deleted matches on dashboards (`vw_player` lacks `deleted_at`); Serve Strategy double-count.
- Retracted after measurement: the coordinate-frame "P0" (code right — doubles frame [0,10.97]); ball_speed IS km/h; smash-as-serve can't fire.

## NEXT STEPS (owner-directed, in order)

1. **RALLY RECON** — the next focus, the analogue of tonight's serve recon. Validate against the owner's video on `052786b4`: **once a serve is in, ring-fence the rally = return → the point-ending shot (winner by hitter OR error by receiver).** Confirm point-ending-shot detection is correct. Also worth re-checking: did the serve validation surface anything reusable for rally (the `far` flag, `debug_data` per-swing signals).
2. **P1 serve service-box + first-serve-% fixes** — they rewrite historical numbers; validate before/after in devenv on `052786b4`, then rebuild historical silver.
3. Wire `bounce_plausible_d` into the heatmaps.
4. Athletics/fitness panel (easy win — data already in bronze.player).

## Data nuggets (Phase 2, mostly unused)

Coverage on the 11.6MB JSON: only `meta` + `debug_data` truly dropped; ~105 fields preserved-but-unused. Best untapped: `debug_data` per-swing signals (`far` = 100%-accurate near/far, serve_conf, nballs, 313-366 bounce candidates); `meta.video_info.fps` (dropped over a `meta`-vs-`metadata` key typo — the two-frame-spaces root); `highlights` (reel); player fitness; `team_sessions` near/far identity (now captured).

## Method that worked (keep doing this)

Measure-first, against ground truth. **Two proposals this sprint were refuted by the owner's video** (the coordinate-frame P0; the serve timing-gap phantom rule) — both caught *before* shipping because we validated in devenv. Never ship derived-logic changes without a before/after against `052786b4`.
