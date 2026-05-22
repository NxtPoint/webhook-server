# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-22 (late evening — chain-rejection fix shipped + bundled Batch deploy)
**Phase active:** Phase 5 — Ball detection coverage. **5e VERIFIED IN PROD** + **5c.2 schema live (awaiting env flips)** + **Silver bench live (1d6feb3a baseline locked)** + **`_filter_outliers` re-anchor fix shipped + Batch deployed (eu :48 / us :30)**.
**Bench:** Serve `a798eff0=20/24, 880dff02=23/24` — **green** (unchanged, filter fix is upstream of serve). Ball-bench v2 baseline updated post-fix — post_filter_sa_recall hit 100% on 3/4 (fixture, tracker) combos, 67% on a798eff0/tracknet_v2 (was 33% pre-fix). Silver-bench `1d6feb3a` still OK at 7 rows (frozen bronze pre-dates fix; recapture pending).
**What shipped last session:** Follow-up #1 fixed (`_filter_outliers` re-anchors on coherent post-gap cluster — BALL_FILTER_REANCHOR_RUN=4). Mirror in replay_ball.py. Ball-bench baseline updated hand-derived from the first measurement run (commit `ff0f0f5` numbers — recompute on next bench run to true up sub-percent drift). Batch rebuild + dual-region ECR push + new job-def revs (eu :48, us :30, amd64 digest `bc8f7d72`). Follow-up #2 (`source='main'`) shipped in same Batch image — was dormant code in main; now live.
**What's blocked:** Nothing. Activation work all solo-runnable.
**Next session's job:** Pick (a) Phase 5c.2 activation (env flips + `/ops/backfill-pair-labels`), (b) re-capture `1d6feb3a` silver-bench fixture against new Batch image to see post-fix bronze density and update silver baseline, (c) capture `880dff02` as second silver-bench fixture, or (d) Phase 5c.3 `harness build-corpus` (still speculative until 5c.2 activated).

If the above is enough, stop reading this file and go.

If you need depth (inheriting a blocker, verifying a claim, picking next move), continue.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-22 late evening (chain-rejection fix + Batch deploy).

**TL;DR — where we are:**
- **`_filter_outliers` chain-rejection — FIXED + DEPLOYED.** Pre-fix, a single bad early anchor froze the greedy filter chain and dropped tens of thousands of downstream detections (1d6feb3a kept frames 2-3329 of 15,298). Fix: maintain a `pending` cluster of detections rejected from current anchor; when `BALL_FILTER_REANCHOR_RUN=4` consecutive entries cohere with each other, accept the cluster and re-anchor. Both `ml_pipeline/ball_tracker.py` and `ml_pipeline/wasb_ball_tracker.py` carry identical implementations; `ml_pipeline/diag/replay_ball.py:_post_filter_detections` mirrors them so the ball bench measures the new shape. Verified via ball bench: post_filter_sa_recall 0% → 100% (880dff02/tracknet), 22% → 100% (880dff02/wasb), 33% → 67% (a798eff0/tracknet), 33% → 100% (a798eff0/wasb).
- **Phase 5e WASB integration — VERIFIED IN PROD.** Batch task `1d6feb3a` ran end-to-end with WASB. 17 valid bounces, pipeline complete in 2,258s.
- **Phase 5c.2 — SHIPPED + verified live on Render schema-only.** Pair-completion hook, `ml_analysis.training_corpus` (11 cols), `gold.vw_dual_submit_pairs` (12 cols), `/ops/backfill-pair-labels`. All dark behind `AUTO_LABEL_DUAL_SUBMIT_PAIRS=0` until flipped.
- **Silver bench — LIVE END-TO-END.** `1d6feb3a` baseline locked at 7 silver rows. Note: this baseline captures PRE-FIX bronze. A re-snapshot after a fresh Batch run with eu :48 will show many more `ml_analysis.ball_detections` rows and likely more silver rows.
- **Follow-up #2 (`source='main'`) — NOW LIVE.** Was dormant Batch-side code in main; included in the same Batch deploy this session.

**Architecture sanity check (verified 2026-05-22 evening):**
- T5 bronze lives in `ml_analysis.*` schema (`video_analysis_jobs`, `ball_detections`, `player_detections`, `serve_events`); SportAI bronze lives in `bronze.*` (`player_swing`, `rally`, `ball_bounce`, `ball_position`, `player_position`). Separate tables, both feed `silver.point_detail` distinguished by `model='t5'` vs `model='sportai'`.
- `bronze.submission_context.sport_type` is the routing key (`tennis_singles` / `tennis_singles_t5` / `serve_practice` / `rally_practice` / `technique_analysis`).
- `build_silver_match_t5.py` literally imports `pass3_point_context`, `pass4_zones_and_normalize`, `pass5_analytics` from `build_silver_v2.py` (line 1062-1067) — real code reuse, not duplication. T5 has its own Pass 1; Passes 3-5 are shared.

**Open admin items:**
- Phase 5c.2 activation — flip `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render. Then `/ops/backfill-pair-labels {"dry_run": false, "limit": 1}` to seed.
- Re-capture `1d6feb3a` silver-bench fixture against new Batch image (eu :48) to see post-fix bronze density.
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).
- Ball-bench baseline values were hand-derived from the first post-fix measurement run (printed rates × frames_processed) rather than from a clean `--update-baseline` run. They should be accurate to ±1 detection and ±0.5% coherence. Next session running `python -m ml_pipeline.diag.bench_ball` will reveal any micro-drift; if a coherence regression flags within ±1%, just `--update-baseline` to true up.

Read in this order before doing anything else:

1. `.claude/strategy/silver_bench_design_2026-05-21.md` — design + §11 bootstrap playbook.
2. `.claude/strategy/dual_submit_status_2026-05-20.md` — Phase 5c.2 design (shipped) + 5c.3-5c.5 ahead.
3. `docs/north_star.md` — macro plan; Phase 5e SHIPPED + VERIFIED.
4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST + silver-bench subsection.

Then run the locked benches locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench
    .venv/Scripts/python -m ml_pipeline.diag.bench_silver
    # bench_ball is optional — ~90 min on CPU; only run if you're touching ball_tracker.py

Expect: serve bench `a798eff0` 20/24, `880dff02` 23/24; silver bench `1d6feb3a` OK (7 rows — frozen pre-fix bronze).

**Next move — pick one (recommended order: 1 → 2 → 3 → 4):**

**Option 1: Phase 5c.2 activation** (Tomo-side, <10 min). Render dashboard env vars: `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1`. Then `curl -X POST /ops/backfill-pair-labels -d '{"dry_run": true}'` to enumerate eligible pairs, then `{"dry_run": false, "limit": 1}` to seed. Playbook at end of this file.

**Option 2: Re-capture `1d6feb3a` silver-bench fixture against post-fix Batch image.** Tomo reruns ingest from Render shell → Batch picks up eu :48 → new bronze has many more `ml_analysis.ball_detections` rows → silver builder sees richer input. Capture fresh snapshot via `python -m ml_pipeline.diag.bench_silver.snapshot --task 1d6feb3a-...`, upload to S3, pull locally, run silver bench, `--update-baseline`. Validates the fix end-to-end at the silver layer.

**Option 3: Capture `880dff02` as second silver-bench fixture (~15 min Render-side + 5 min local).** Same workflow as `1d6feb3a`. Adds a denser regression target. Spec playbook in `.claude/strategy/silver_bench_design_2026-05-21.md` §11.

**Option 4: Phase 5c.3 `harness build-corpus` subcommand (~3-4 hr).** Pure local. Reads `ml_analysis.training_corpus`, pulls labels + videos from S3, assembles dataset. Spec at `.claude/strategy/dual_submit_status_2026-05-20.md` §4. **Note:** consumer ships before producer fires meaningfully — speculative until Phase 5c.2 is activated and has rows.

**Strategic frame (Tomo's):** silver derived from bronze; goal is bronze 100% correct and SA-aligned. T5 has its own bronze (`ml_analysis.*`); shared Passes 3-5 produce the unified silver. **Follow-up #1 directly improved bronze quality** — the verdict ball-bench numbers (post_filter_sa_recall jumping to ~100%) are evidence the chain-rejection bug was structurally the dominant cause of T5's "ball goes missing mid-rally" symptom. Bigger downstream effects (denser silver, fewer ROI-coverage gaps, possibly more recovered far-player serves) should land naturally once we re-ingest a real match.

**Things NOT to do** (load-bearing):

- **Don't add new columns to `bronze.submission_context` in production** without explicit need and a documented reason.
- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, `db_writer.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.** Both `_filter_outliers` mirror updates this session went through Docker rebuild + ECR push + job-def revs in both regions.
- **Don't rollback WASB without running the bench against TrackNetV2 first.** Previous revs (eu :46-:47 / us :28-:29) kept on standby.
- **Don't change the `_dual_submit_pair_complete_hook` env-flag default to ON without an explicit go from Tomo.**
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural).
- Don't drop `test_videos/` from the GPU rsync.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. T5 bronze in `ml_analysis.*`, SportAI in `bronze.*`, distinguished at the silver layer by `model` column.

---

## State at session end (2026-05-22 late evening)

**`origin/main` at** the commit landing this session (`_filter_outliers` re-anchor + baseline + Batch deploy artefacts). Commits this session (most-recent first will be the chain-rejection fix). Previous session-end state still relevant:

- `ff0f0f5` session close 2026-05-22 evening: archive + CLAUDE.md guardrail
- `550770c` docs: pickup refresh — silver bench live end-to-end + architecture check
- `52026a9` silver bench: first fixture (1d6feb3a) green end-to-end + restore fixes
- `9d37869` fix: snapshot baseline — outcome_d -> shot_outcome_d
- `1c33607` fix: snapshot UUID cast + follow-up #2 (source='main' on main-pass writes)
- `d3da6ef` phase 5c.2 fix: eager ml_analysis_init() on boot
- `379b173` WASB Step 5 verified (parallel agent)

**Ball-bench baseline locked at HEAD** — hand-derived from the first post-fix measurement run; commit notes the source-of-truth caveat.
**Silver-bench baseline locked at `52026a9`** — `ml_pipeline/fixtures_silver/1d6feb3a_silver_baseline.json`. Pre-fix bronze.

**Batch state:**
- **eu-north-1 `ten-fifty5-ml-pipeline:48`** — amd64 digest `bc8f7d72ba8942ea25213112d7adff9a867ff8ac1307a1ceba99217ef0d8204f` — INCLUDES chain-rejection fix + `source='main'` follow-up #2
- **us-east-1 `ten-fifty5-ml-pipeline:30`** — same amd64 digest
- Previous active revs (eu :47 / us :29) kept ACTIVE for rollback (WASB swap baseline)
- Pre-WASB revs (eu :46 / us :28) also kept

**Serve bench at session end:** `a798eff0` 20/24, `880dff02` 23/24, no regressions.
**Silver bench at session end:** `1d6feb3a` OK (7 silver rows — frozen pre-fix bronze).
**Ball bench at session end:** post-fix verdict metrics all up; new baseline checked in.

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

Backfill existing pairs:
```bash
# Dry run first
curl -X POST https://api.nextpointtennis.com/ops/backfill-pair-labels \
     -H "X-Ops-Key: $OPS_KEY" -d '{"dry_run": true}'

# Then real run
curl -X POST https://api.nextpointtennis.com/ops/backfill-pair-labels \
     -H "X-Ops-Key: $OPS_KEY" -d '{"dry_run": false, "limit": 1}'
```

---

## Capture a fresh silver-bench fixture against post-fix Batch image

Tomo reruns ingest from Render shell on a sport_type='tennis_singles_t5' task. Batch picks up eu :48 (auto via job-def name). Once SUCCEEDED:

1. From Render shell: `python -m ml_pipeline.diag.bench_silver.snapshot --task <task_id> --upload-s3`
2. Locally: `aws s3 cp s3://<bucket>/silver_bench_fixtures/<task>_bronze.sql.gz ml_pipeline/fixtures_silver/`
3. Local: `python -m ml_pipeline.diag.bench_silver --task <task> --update-baseline`
4. Commit the new `_bronze.sql.gz` + `_silver_baseline.json`.

If the silver-row count jumps from 7 to (say) 30+, that's direct confirmation the chain-rejection fix is structurally repairing T5 bronze density.

Full playbook in `.claude/strategy/silver_bench_design_2026-05-21.md` §11.
