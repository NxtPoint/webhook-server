# Next-session pickup — 2026-07-24 — second-match validation (Erin v Jolanda)

> **Two parallel threads.** This covers the **SportAI (`tennis_singles`)
> business-analytics pipeline**. The **T5 ML pipeline** is parked at "bronze DEV
> complete, training is the incremental remainder" (`.claude/handover_t5.md`).

## ⚡ Executive summary (read first)

The **silver-derivation correctness sprint is complete and shipped** — validated
18/18 point winners against video on `c8b77210` (Tomo v Jimbo Ma). Canonical
logic doc: **`docs/_investigation/silver_gold_filter_contract.md`** (bronze → 16
verbatim → derived → spine → gold filters). Audit closeout status lives at the
top of `docs/_investigation/pipeline_end_to_end_audit_2026-07-19.md` §"CLOSEOUT
STATUS".

**Bench must stay green** (`ea1e500c=12/26, 880dff02=23/24`):
`.venv/Scripts/python -m ml_pipeline.diag.bench`.

## THE JOB FOR TOMORROW — validate on a second, different match

18 points (one match) is thin. Everything is validated on the Tomo-v-Jimbo
footage only. **`0336b82b` = Erin v Jolanda Gericke — a genuinely different match
(different players, court, and it is badly tracked).** Run the same validation:

1. Seed into devenv (may already be seeded): `python -m devenv.seed_local --source-url "$RO" --task 0336b82b-15d9-4364-bc1e-c9a2b57b70e1`, then rebuild silver.
2. **It is the stress case:** 6% `is_in_rally` coverage, 28% ball-speed coverage, and it reported **0 winners in 112 points** before the sprint. Check whether the sprint's fixes (bounce honesty, contiguity, DF flag) hold up or break on bad tracking.
3. The goal is NOT 18/18 here — it's to find where the logic degrades on poor input, and decide what is a bronze-accuracy ceiling vs a silver bug. Expect the per-match quality-gate question (below) to become concrete.
4. If a second *clean* match ever gets uploaded, that is the better generality test — `0336b82b` is deliberately the hard one.

## Dashboard data layer — BUILT + validated (2026-07-23 eve), NOT wired

`silver_analytics/` (`1b5d2d6`) builds three new grains from bronze SportAI data
`build_silver_v2` never reads. Validated in devenv on `c8b77210`. **Does not touch
`point_detail`.** Roadmap: `.claude/plans/twinkly-seeking-bentley.md`.

- `silver.match_player_summary` — fitness (distance/sprint/activity, all SportAI
  pre-computed), shot mix, movement summary, near/far.
- `silver.player_movement_grid` — **pre-aggregated** 1m court occupancy grid
  (~150 rows/player, not ~3000 raw) — the heatmap source, performance-safe.
- `silver.match_quality` — ball/pose/swing/final confidence + reliability tier.

**Two open design decisions for tomorrow:**
1. **quality_tier thresholds need calibration.** `c8b77210` (our 18/18 match)
   reads **`medium`** because SportAI's `ball_conf` is only 0.30 — even our best
   match isn't "high". Calibrate the thresholds once `0336b82b` (the badly-tracked
   match) is built; it should read `low`.
2. **far-player heatmap orientation.** The grid stores raw `court_x/court_y`
   (silver stays faithful). Inverting the far player onto a canonical "own half"
   view is a gold/frontend job — decide the canonical frame when building.

**Not wired into prod ingest** — `build_all(engine, task_id)` runs standalone.
Wiring (ingest hook or ops endpoint) is a reviewed step for tomorrow; the tables
must exist in prod before the dashboards can read them. Momentum curves need NO
new table (gold view over `point_detail`).

## Reference matches

| task | who | note |
|---|---|---|
| `c8b77210` | Tomo v Jimbo Ma, 2026-07-23 | **primary reference — 18/18 vs video, debug_data captured.** Protect from orphan sweep. |
| `052786b4` / `079d2c62` | Tomo v Jimbo Ma | same footage, earlier SportAI runs (SportAI is nondeterministic) |
| `0336b82b` | **Erin v Jolanda**, 2026-04-28 | **tomorrow's target** — different match, badly tracked |

## Open audit P1s (NOT this sprint's cluster — verify each before fixing)

Ranked; all are billing/frontend/gold, outside the silver-derivation work just done.

1. **deuce/ad midline** (`build_silver_v2.py:653`) — splits deuce/ad on the
   drifting `AVG(ball_hit_location_x)` instead of the fixed centre mark 5.485
   (`MID_X_DEFAULT`). In-wheelhouse, low-risk, the top open silver item. Measure
   the deuce/ad delta on `c8b77210` before/after.
2. **hollow ingest bills the customer** — a zero-row ingest is marked `completed`
   and consumes a credit. Add a zero-count guard (upload/billing).
3. **NULL rendered as `0%`** across Match Analytics (frontend) — "not measured"
   shows as a real 0. Distinguish NULL from 0 in `match_analysis.html`.
4. **Serve Strategy totals double-count** — gold emits `points_played` per
   `(side,bucket,serve_try)`; the frontend re-keys on `side|bucket` and sums.
5. **soft-deleted matches never leave `vw_player`** (`gold_init.py:48-49`, no
   `deleted_at IS NULL`) — 11 downstream views inherit it.
6. **P2:** serve-speed KPIs average a partial sample (surface coverage);
   `_validate_rally_count` false-alarms in both directions (re-anchor or drop it).

## Per-match quality gate (the biggest untreated risk)

`0336b82b` publishes confidently wrong numbers (0 winners / 112 points) with
nothing flagging the analytics as unreliable. `bronze.session_confidences` +
the new `debug_data` per-swing confidences are the natural anchor for a
"this match's analytics are low-confidence" gate. Likely to surface hard when
tomorrow's run lands.

## What shipped in the silver-correctness sprint (all on main)

`81e48d9` swing-bounce honesty · `bfe7dd7` drop 4 dead columns (52 cols) ·
`e872a74` derived-column verification + rally_location_bounce NULL ·
`61f61e1` double_fault_d + event-spine model (first-serve % 52.9→50.0) ·
`3432ce6` exclude_d explicit across gold · `5f160b2` shot_phase service-line
constants + service-box investigation. Earlier same-day: rally contiguity
(default ON), debug_data capture, video_info/dbg_* promotion, 18/18 reconciled.

## Live flags (SportAI silver)

`SILVER_RALLY_CONTIGUITY` **default ON** (rally ends at first >5s break) ·
`SILVER_RALLY_IIR_MIN_COVERAGE=1.01` (is_in_rally escape OFF) ·
`BOUNCE_CANDIDATES_ENABLED` **default ON in code** · `SILVER_SERVE_SOURCE`
=`geometric`. All have env rollbacks.

## Local dev environment

- Docker Postgres `localhost:55433` (NOT `:55432` = CourtFlow).
  `docker compose -f devenv/docker-compose.yml up -d`.
- Read-only prod role `tf_readonly` in gitignored `devenv/.env.local`.
  **Still live — drop when done** (`DROP OWNED BY tf_readonly; DROP ROLE tf_readonly;`).
- ⚠ this box exports `DATABASE_URL` = `…:55432/courtflow_dev`; pin the devenv URL
  explicitly (`postgresql+psycopg://tf:tf@localhost:55433/tf_dev`) or scripts hit
  the wrong DB.
- Rebuild + 18/18 check pattern: filter-contract doc §Verification.
- Re-ingest in prod: `POST /ops/ingest-task {"task_id":"…","mode":"worker"}` with
  `X-Ops-Key: $OPS_KEY` from the main-API Render shell (`mode` must be `worker`).
