# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-22 (evening — silver bench live end-to-end + Phase 5c.2 verified in prod)
**Phase active:** Phase 5 — Ball detection coverage. **5e VERIFIED IN PROD** + **5c.2 SHIPPED + schema live on Render** + **Silver bench live end-to-end (first fixture green)**.
**Bench:** Serve `a798eff0=20/24, 880dff02=23/24` — **green**. Ball-bench v2 locked at `7100792`. **Silver-bench `1d6feb3a` — green** (first fixture, 7 silver rows, 1 serve).
**What shipped last session:** Phase 5c.2 schema verified live on Render (training_corpus=11 cols, vw_dual_submit_pairs=12 cols). Silver bench first fixture captured + uploaded + locally restored + silver builder ran + matched baseline. Follow-up #2 (`source='main'`) code is in main but Batch-side, dormant until rebuild.
**What's blocked:** Nothing. Activation work + follow-ups all solo-runnable.
**Next session's job:** Pick (a) Phase 5c.2 activation (env flips + `/ops/backfill-pair-labels`), (b) follow-up #1 (`_filter_outliers` chain-rejection — the bug that limited `1d6feb3a` to 7 silver rows; now reproducible via silver bench), (c) capture `880dff02` as second silver-bench fixture, or (d) Batch deploy bundling follow-up #2 with any follow-up #1 change.

If the above is enough, stop reading this file and go.

If you need depth (inheriting a blocker, verifying a claim, picking next move), continue.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-22 evening (silver bench live + Phase 5c.2 verified live).

**TL;DR — where we are:**
- **Phase 5e WASB integration — VERIFIED IN PROD.** Batch task `1d6feb3a` ran end-to-end with WASB. 17 valid bounces, pipeline complete in 2,258s. Three follow-ups identified (none blocking).
- **Phase 5c.2 — SHIPPED + verified live on Render.** Pair-completion hook, `ml_analysis.training_corpus` (11 cols), `gold.vw_dual_submit_pairs` (12 cols), `/ops/backfill-pair-labels`. Both schema objects confirmed on prod via psql column-count check. All dark behind `AUTO_LABEL_DUAL_SUBMIT_PAIRS=0` until flipped.
- **Silver bench — LIVE END-TO-END.** `snapshot.py` + `bench.py` + first fixture `1d6feb3a` (1.5 MB gzipped). Full loop verified: Render snapshot → S3 → pull → docker postgres restore → `build_silver_match_t5` → compare baseline → OK. Baseline: 7 silver rows, 7 active, 1 serve (sparse — consistent with follow-up #1).
- **Follow-up #2 (`source='main'`) code in main but dormant.** `ml_pipeline/db_writer.py` change is Batch-side; needs Docker rebuild + ECR push + job-def revs to take effect.

**Architecture sanity check (verified 2026-05-22 evening):**
- T5 bronze lives in `ml_analysis.*` schema (`video_analysis_jobs`, `ball_detections`, `player_detections`, `serve_events`); SportAI bronze lives in `bronze.*` (`player_swing`, `rally`, `ball_bounce`, `ball_position`, `player_position`). Separate tables, both feed `silver.point_detail` distinguished by `model='t5'` vs `model='sportai'`.
- `bronze.submission_context.sport_type` is the routing key (`tennis_singles` / `tennis_singles_t5` / `serve_practice` / `rally_practice` / `technique_analysis`).
- `build_silver_match_t5.py` literally imports `pass3_point_context`, `pass4_zones_and_normalize`, `pass5_analytics` from `build_silver_v2.py` (line 1062-1067) — real code reuse, not duplication. T5 has its own Pass 1; Passes 3-5 are shared.
- **No `bronze.submission_context` column changes shipped this session.** Verified via `git diff 0aa5a79..52026a9 -- db_init.py upload_app.py` (no submission_context schema additions). The bench's `_EXTRA_SUBMISSION_CONTEXT_DDL` list only runs locally in the Docker bench DB and mirrors prod's existing columns — does not add new ones to prod.

**Three follow-ups from WASB verification:**

1. **`_filter_outliers` chain-rejection** in `ml_pipeline/ball_tracker.py` (copied to `wasb_ball_tracker.py`). WASB processed 15,298 frames but bronze only got rows for frames 2-3329. Filter chain stays stuck on an early reference. Now reproducible locally via silver bench (the `1d6feb3a` baseline of 7 rows is the artefact). Fix shape: re-anchor when N consecutive neighbours land near a rejected candidate.

2. **`source='main'` tag on main-pass writes.** Code is in main (commit `1c33607` — `ml_pipeline/db_writer.py` + `ml_pipeline/db_schema.py`) but Batch-side, dormant until Docker rebuild + ECR push + job-def revs. Bundle with #1 in the next Batch deploy.

3. **Second silver-bench fixture: `880dff02`.** Parity with serve bench. Same workflow as `1d6feb3a` capture; this time you have a real expected row count to compare against (vs the sparse `1d6feb3a`). Useful to validate the bench's regression-detection on a denser case.

**Open admin items:**
- Phase 5c.2 activation — flip `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render. Then `/ops/backfill-pair-labels {"dry_run": false, "limit": 1}` to seed.
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` — older in-flight, still open.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).

Read in this order before doing anything else:

1. `.claude/strategy/silver_bench_design_2026-05-21.md` — design + §11 bootstrap playbook (now executed).
2. `.claude/strategy/dual_submit_status_2026-05-20.md` — Phase 5c.2 design (shipped) + 5c.3-5c.5 ahead.
3. `docs/north_star.md` — macro plan; Phase 5e SHIPPED + VERIFIED.
4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST + silver-bench subsection.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench
    .venv/Scripts/python -m ml_pipeline.diag.bench_silver

Expect: serve bench `a798eff0` 20/24, `880dff02` 23/24; silver bench `1d6feb3a` OK.

**Next move — pick one (recommended order: 1 → 2 → 3 → 4):**

**Option 1: Phase 5c.2 activation** (Tomo-side, <10 min). Render dashboard env vars: `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1`. Then `curl -X POST /ops/backfill-pair-labels -d '{"dry_run": true}'` to enumerate eligible pairs, then `{"dry_run": false, "limit": 1}` to seed.

**Option 2: Follow-up #1 (`_filter_outliers` chain-rejection, ~1 session).** Now reproducible: edit `ml_pipeline/ball_tracker.py` + `wasb_ball_tracker.py`, run `python -m ml_pipeline.diag.bench_silver`, expect more silver rows on `1d6feb3a` once the chain-rejection is fixed. Design notes in `next_session_pickup.md` history. Bundle with follow-up #2 for a single Batch deploy.

**Option 3: Capture `880dff02` as second silver-bench fixture (~15 min Render-side + 5 min local).** Same workflow as `1d6feb3a`. Adds a denser regression target for the bench. Spec playbook in `.claude/strategy/silver_bench_design_2026-05-21.md` §11.

**Option 4: Phase 5c.3 `harness build-corpus` subcommand (~3-4 hr).** Pure local. Reads `ml_analysis.training_corpus`, pulls labels + videos from S3, assembles dataset. Spec at `.claude/strategy/dual_submit_status_2026-05-20.md` §4. **Note:** consumer ships before producer fires meaningfully — speculative until Phase 5c.2 is activated and has rows.

**Strategic frame (Tomo's):** silver derived from bronze; goal is bronze 100% correct and SA-aligned. T5 has its own bronze (`ml_analysis.*`); shared Passes 3-5 produce the unified silver. **Follow-up #1 directly improves bronze quality** — that's the highest-leverage post-5e move.

**Things NOT to do** (load-bearing):

- **Don't add new columns to `bronze.submission_context` in production** without explicit need and a documented reason. The bench's local DDL list is allowed to mirror prod, but adding new prod columns crosses an architectural boundary.
- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, `db_writer.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.** `db_writer.py` should be added to the explicit list in CLAUDE.md item #8 — caught during this session.
- **Don't rollback WASB without running the bench against TrackNetV2 first.** Rollback = `aws batch update-job-definition` clearing the `BALL_TRACKER` env var; previous revs (`:46`/`:28`) kept on standby.
- **Don't try to add the stroke-classifier hook (G10) to the Render API process.** `export_training_data.py` imports cv2 + opens the video locally + runs optical flow. G10 belongs in Phase 5c.5 on the GPU box.
- **Don't change the `_dual_submit_pair_complete_hook` env-flag default to ON without an explicit go from Tomo.**
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural).
- Don't drop `test_videos/` from the GPU rsync.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. T5 bronze in `ml_analysis.*`, SportAI in `bronze.*`, distinguished at the silver layer by `model` column.

---

## State at session end (2026-05-22 evening)

**`origin/main` at `52026a9`.** Commits this session (most-recent first):

- `52026a9` silver bench: first fixture (1d6feb3a) green end-to-end + restore fixes
- `9d37869` fix: snapshot baseline — outcome_d -> shot_outcome_d
- `1c33607` fix: snapshot UUID cast + follow-up #2 (source='main' on main-pass writes)
- `d3da6ef` phase 5c.2 fix: eager ml_analysis_init() on boot
- `0227918` docs: pickup + north_star refresh — 5c.2 + silver bench shipped, verified
- `83e1ab7` silver bench: implement snapshot + orchestrator (steps 2+4 of the design)
- `4fba821` docs: pickup refresh — Phase 5c.2 shipped
- `d7718e0` phase 5c.2: pair-completion hook + ml_analysis.training_corpus

Previous session-end state (still relevant):
- `379b173` WASB Step 5 verified (parallel agent)
- `0aa5a79` session close (2026-05-21 deep eve)
- `5e3e746` silver bench: scaffolding + Docker Postgres lifecycle helper
- `4a39588` WASB swap: env-gated drop-in for BallTracker
- `7100792` ball-bench v2 baseline

**Ball-bench baseline locked at `7100792`** — `ml_pipeline/diag/bench_ball_baseline.json`.
**Silver-bench baseline locked at `52026a9`** — `ml_pipeline/fixtures_silver/1d6feb3a_silver_baseline.json`. Sparse (7 rows) — captures the post-WASB chain-rejection state.

**Batch state (unchanged):**
- eu-north-1 `ten-fifty5-ml-pipeline:47` (`BALL_TRACKER=wasb`)
- us-east-1 `ten-fifty5-ml-pipeline:29` (same digest, same env var)
- Previous active revs (eu :46 / us :28) kept for instant rollback
- **db_writer.py source='main' change is in main but NOT in the deployed image yet** — needs Docker rebuild + ECR push + job-def revs.

**Serve bench at session end:** `a798eff0` 20/24, `880dff02` 23/24, no regressions.
**Silver bench at session end:** `1d6feb3a` OK (7 silver rows match baseline).

**GPU dev box:** `i-0295d636f6bf957eb` (eu-north-1b), stopped. Old `i-0fb3983fa555c16e3` (1a) parked stopped.

**Render Postgres allowlist:** `0.0.0.0/0` (open admin item).

---

## Phase 5c.2 activation playbook

In the Render dashboard for "Sport AI - API call":

1. Environment tab → add (or set):
   - `AUTO_DUAL_SUBMIT_T5` = `1`
   - `AUTO_LABEL_DUAL_SUBMIT_PAIRS` = `1`
2. Save → auto-redeploy.

Verify next SA upload spawns a paired T5:
```sql
-- From psql in Render shell after a fresh tennis_singles upload completes
SELECT task_id, sport_type, last_status, ingest_finished_at
  FROM bronze.submission_context
 WHERE s3_key = (SELECT s3_key FROM bronze.submission_context WHERE task_id = '<new_task_id>')
 ORDER BY created_at;
-- Expect two rows: sport_type='tennis_singles' + 'tennis_singles_t5'
```

After both complete, verify training_corpus row:
```sql
SELECT sa_task_id, t5_task_id, label_kind, label_count
  FROM ml_analysis.training_corpus
 ORDER BY created_at DESC LIMIT 5;
```

Backfill existing pairs (e.g. `8a5e0b5e/2c1ad953` if still present):
```bash
# Dry run first
curl -X POST https://api.nextpointtennis.com/ops/backfill-pair-labels \
     -H "X-Ops-Key: $OPS_KEY" -d '{"dry_run": true}'

# Then real run
curl -X POST https://api.nextpointtennis.com/ops/backfill-pair-labels \
     -H "X-Ops-Key: $OPS_KEY" -d '{"dry_run": false, "limit": 1}'
```

---

## Capture a second silver-bench fixture (e.g. 880dff02)

Same as `1d6feb3a` — see `.claude/strategy/silver_bench_design_2026-05-21.md` §11 for full playbook. Once green, commit the new `880dff02_silver_baseline.json` alongside `1d6feb3a_silver_baseline.json`.
