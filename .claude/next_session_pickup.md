# Next-session pickup — 2026-07-23 — rally RECONCILED 18/18 in production

> **Two parallel threads in this repo.** This pickup covers the **SportAI (`tennis_singles`) business-analytics pipeline**. The **T5 ML pipeline** thread is *parked at "bronze DEV complete, training is the incremental remainder"* — `.claude/handover_t5.md`, untouched by this sprint.

## ⚡ Executive summary (read first)

**Every point winner on `c8b77210` matches the owner's video — 18/18, verified against PRODUCTION.** Baseline at the start of the session was 15/18. Full findings: **`docs/_investigation/pipeline_end_to_end_audit_2026-07-19.md`** (~1115 lines); today's sections are RALLY RECON, RALLY RING-FENCE, NEW MATCH + debug_data, and **18/18 — RALLY RECONCILED**.

**Bench GREEN** (`ea1e500c=12/26, 880dff02=23/24`).

### What got it there
1. **Rally contiguity** (`SILVER_RALLY_CONTIGUITY`, **default ON**) — points 11 + 15. Rally ends at the first >5s break instead of re-anchoring onto post-point activity. Also makes results *reproducible*: the legacy rule returned different winners on the same footage per SportAI run (3 of 4 adjudicated points flipped).
2. **`is_in_rally` escape REMOVED** (`SILVER_RALLY_IIR_MIN_COVERAGE` default 1.01 = off) — point 16 on `052786b4`. It re-admitted a shot 6.7s after the point ended. Its INCLUDE direction was never video-validated.
3. **Bounce-candidate recovery** (`BOUNCE_CANDIDATES_ENABLED`, **default ON in code**) — point 16 on `c8b77210`. 131 bounces recovered; one decided a point.

### The correction that matters
**R6 is materially reduced by a feature we already had.** The earlier "R6 has no cheap fix" conclusion (after `conf_ball_in/out` came back empty on 89% of swings) was too pessimistic. Point 16 is a controlled A/B on identical footage: bounce present → Winner (correct); bounce NULL → fabricated Error (wrong). The bounce was in `debug_data` all along at conf 0.60, passing every filter — it never reached bronze because the env var never reached the service.

### What 18/18 is NOT
One match, 18 points. **Point winners only** — rally length, stroke, zones, aggression, depth, serve placement were never reconciled. The seeded set holds **two distinct matches** (this one ×3 runs + `0336b82b`). `0336b82b` still reports **0 winners in 112 points** with nothing flagging it as unreliable.

## Reference matches

| task | who | note |
|---|---|---|
| `052786b4` | Tomo v Jimbo Ma, 2026-07-19 | **owner-adjudicated on video** — the ground-truth reference. Protect from the orphan sweep. |
| `c8b77210` | Tomo v Jimbo Ma, 2026-07-23 | new; the only match with `debug_data` captured |
| `079d2c62` | Tomo v Jimbo Ma, 2026-06-16 | SA pair, messy 4-ghost |
| `0336b82b` | Erin v Jolanda Gericke, 2026-04-28 | real customer, badly tracked — every SportAI signal collapses on it |

## What shipped today (all on origin/main)

1. **`e7c71e7` fix(bronze)** — `ball_bounce.source`/`confidence` added to `ingest_bronze._ensure_schema`, the init path that actually runs. Unblocked a production outage: every SportAI match ingested after 2026-07-22 was failing its silver build.
2. **`0132eb0` fix(ingest)** — `debug_data` captured whole into `bronze.debug_event`. Empty for every match ever ingested before today.
3. **`259c779` fix(silver)** — rally `gap_break` contiguity (shipped default OFF).
4. **`7b3b011` feat(bounce)** — candidate recovery default ON in code; `render.yaml`'s declared `"1"` never reached the service.
5. **`20b8711` feat(silver)** — contiguity flipped to default ON on the reproducibility evidence.
6. **`9120f32` fix(silver)** — `is_in_rally` escape disabled; it caused point 16 on `052786b4`.

## Open, in priority order

1. **Promote `video_info.fps` to a bronze column.** `debug_data.video_info` has `fps 25.0 / total_frames 15300 / duration 612.0`. This is the fps lost to the `meta`-vs-`metadata` typo — root of the recurring two-frame-spaces hazard. Cheapest high-value win available.
2. **Strip `video_info.video_source` on ingest** — a presigned S3 URL to the customer's raw video (7-day expiry) now persisted in `bronze.debug_event`. Do not persist signed URLs.
3. **Reconcile the fields 18/18 did NOT cover** — rally length, stroke type, zones, aggression, depth, serve placement. Point winners are validated; nothing else on that row is.
4. **Per-match quality gate.** `0336b82b` (a real customer match) reports **0 winners in 112 points**, 6% in-rally coverage, 28% ball-speed coverage, and nothing surfaces that its analytics are unreliable. `session_confidences` is the natural anchor. This is the largest untreated defect.
5. **R6 residual.** Materially reduced by candidate recovery but not solved: a bounce no candidate covers still fabricates an `Error`, and NULL→Error still cannot distinguish "netted" from "not tracked". The `ball_position` + homography route (168 paired image↔court points per match) remains available if the residual proves costly — scope deliberately, it carries the T5 calibration-degeneracy risk.
6. **P1 serve service-box + first-serve-% fixes** — rewrite historical numbers; validate before/after in devenv.
7. **Backfill.** Existing matches were ingested before candidate recovery worked and before contiguity; their silver is stale on both counts. Decide whether to re-ingest historical tasks (SportAI's re-fetch URL expires 1h after ingest, so old matches may only be rebuildable from `raw-json/` archives, which start 2026-07-22).
8. Ace guard (an undetected return becomes a fabricated ace) — **1 ace in 148 points**, so no live problem and no sample to design against. Wire `bounce_plausible_d` into the heatmaps; athletics/fitness panel.

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
