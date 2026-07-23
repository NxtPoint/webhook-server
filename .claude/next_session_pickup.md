# Next-session pickup — 2026-07-23 — rally recon + `debug_data` unlocked

> **Two parallel threads in this repo.** This pickup covers the **SportAI (`tennis_singles`) business-analytics pipeline**. The **T5 ML pipeline** thread is *parked at "bronze DEV complete, training is the incremental remainder"* — its handover is `.claude/handover_t5.md` and the T5 memories, untouched by this sprint.

## ⚡ Executive summary (read first)

Continuation of the SportAI audit. Full findings: **`docs/_investigation/pipeline_end_to_end_audit_2026-07-19.md`** — single source of truth, now ~1020 lines. Today added three sections: **RALLY RECON**, **RALLY RING-FENCE (exclude_d vs is_in_rally)**, **NEW MATCH c8b77210 + debug_data UNLOCKED**.

**Bench GREEN** (`ea1e500c=12/26, 880dff02=23/24`) — run `.venv/Scripts/python -m ml_pipeline.diag.bench` before any `serve_detector`/`build_silver_v2` change.

**Headline: a production outage was found and fixed, and `debug_data` is finally reachable.** Every SportAI match ingested after 2026-07-22 would have failed its silver build (`column "source" does not exist`) — a schema change went into `db_init.bronze_init()`, which is *not* the init function either service runs. Fixed. Separately, `BOUNCE_CANDIDATES_ENABLED` had been a **silent no-op in production** since it was "enabled"; now default-ON in code.

## Reference matches

| task | who | note |
|---|---|---|
| `052786b4` | Tomo v Jimbo Ma, 2026-07-19 | **owner-adjudicated on video** — the ground-truth reference. Protect from the orphan sweep. |
| `c8b77210` | Tomo v Jimbo Ma, 2026-07-23 | new; the only match with `debug_data` captured |
| `079d2c62` | Tomo v Jimbo Ma, 2026-06-16 | SA pair, messy 4-ghost |
| `0336b82b` | Erin v Jolanda Gericke, 2026-04-28 | real customer, badly tracked — every SportAI signal collapses on it |

## What shipped today (all on origin/main)

1. **`fix(bronze)` — `ball_bounce.source`/`confidence` added to `ingest_bronze._ensure_schema`** (`e7c71e7`). The live path. Unblocked the outage.
2. **`fix(ingest)` — `debug_data` captured** (`0132eb0`). Whole, unparsed, into `bronze.debug_event`. First time ever populated.
3. **`fix(silver)` — rally `gap_break` contiguity, DEFAULT OFF** (`259c779`), flag `SILVER_RALLY_CONTIGUITY`.
4. **bounce candidates default-ON** — code default flipped; `render.yaml`'s declared `"1"` never reached the service.
5. Three audit sections + two self-corrections recorded.

## Open, in priority order

1. **Promote `video_info.fps` to a bronze column.** `debug_data.video_info` has `fps 25.0 / total_frames 15300 / duration 612.0`. This is the fps lost to the `meta`-vs-`metadata` typo — the root of the recurring two-frame-spaces hazard. Cheapest high-value win available.
2. **Strip `video_info.video_source` on ingest** — it is a presigned S3 URL to the customer's raw video (7-day expiry) now persisted in `bronze.debug_event`. Do not persist signed URLs.
3. **Verify the bounce-candidate flip actually lands** — re-ingest any match and confirm `source='debug_candidate'` rows appear (198 expected on `c8b77210`). This has failed silently once already; do not assume.
4. **R6 (a missing bounce is scored as an error) — needs a new plan.** The cheap route is dead: `conf_ball_in`/`conf_ball_out` carry **no signal on 102 of 114 swings (89%)**. Remaining route is `ball_position` (image-space, populated in prod) projected via a homography fitted from the 168 paired `image_x/y`↔`court_x/y` bounce points — a real project with the T5 calibration-degeneracy risk. Scope it deliberately.
5. **`SILVER_RALLY_CONTIGUITY` stays OFF** until R6 is fixed. It is winner-neutral alone (fixes pt 17, breaks pt 15 — 2/3 either way); only rally length/membership is a strict gain. Ace guard also still owed (an undetected return becomes a fabricated ace) — but note **1 ace in 148 points** across three matches, so there is no live problem and no sample to design against.
6. **P1 serve service-box + first-serve-% fixes** — rewrite historical numbers; validate before/after in devenv on `052786b4`.
7. Per-match quality gate — `0336b82b` reports **0 winners in 112 points**, 6% in-rally, 28% ball-speed coverage, and nothing surfaces that its analytics are unreliable. `session_confidences` is the natural anchor.
8. Wire `bounce_plausible_d` into the heatmaps; athletics/fitness panel.

## Newly reachable signals (`debug_data`, per-swing, 114/114 unless noted)

`far` (SportAI's own near/far truth — relevant to the far-attribution problem gating T5) · `discarded` (its own ignore-flag, 5 here) · `serve_conf`/`sconf_*`/`serve_nn` (measure the geometric serve gate instead of arguing about it) · `is_in_rally`/`rally_start`/`rally_end` · `nballs` · `intercepting_player_id` · `ball_trajectory`.

## Method notes worth keeping

- **Measure post-filter, not pre-filter.** The recon's "empty 5–6s gap band" was an artifact of measuring rows *after* `exclude_d` had already removed them. Re-measured: 16 gaps in that bin.
- **Check whether the column is populated, not just whether rows exist.** `ball_position` reads all-NULL in devenv (GENERATED from a `data` blob the ingest strips) and is 100% populated in prod. Nearly reported the opposite.
- **devenv ≠ prod schema.** `bronze.ball_bounce` and `ball_position` both diverge. Verify schema-dependent findings against the prod read-only role.
- **This box's shell profile exports `DATABASE_URL` = `…:55432/courtflow_dev`.** Any script falling back to it silently hits CourtFlow. Pin the devenv URL explicitly.
- Four proposals were stopped by measurement this sprint: the coordinate-frame P0, the serve timing-gap rule, the ace guard, and `conf_ball_in/out` for R6.

## Local dev environment

- Docker Postgres `localhost:55433` (NOT `:55432` = CourtFlow). `docker compose -f devenv/docker-compose.yml up -d`.
- Read-only prod role `tf_readonly` in gitignored **`devenv/.env.local`**. **Still live — drop when the R6 work is done** (`DROP OWNED BY tf_readonly; DROP ROLE tf_readonly;`).
- Rebuild: `DATABASE_URL='postgresql+psycopg://tf:tf@localhost:55433/tf_dev' .venv/Scripts/python -c "import build_silver_v2 as b; print(b.build_silver_v2('<task>', replace=True))"`
- Re-ingest in prod: `POST /ops/ingest-task {"task_id":"…","mode":"worker"}` with `X-Ops-Key: $OPS_KEY` from the main-API Render shell. **`mode` must be `worker`** — `sync` runs on the main API, which lacks the bounce-candidate scope.
