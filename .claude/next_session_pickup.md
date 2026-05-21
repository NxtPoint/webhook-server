# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-22 (morning session — Phase 5c.2 + silver bench shipped)
**Phase active:** Phase 5 — Ball detection coverage. **5c.2 SHIPPED** (corpus pipeline foundation). **Silver bench SHIPPED** (snapshot + orchestrator). 5e WASB verification still pending (parallel-agent thread).
**Bench:** `a798eff0=20/24, 880dff02=23/24` — **green** (serve). Ball-bench v2 locked at `7100792`. Silver-bench has empty fixture set — first capture pending Render shell.
**What shipped last session:** Phase 5c.2 (`d7718e0`) — `_dual_submit_pair_complete_hook` + `ml_analysis.training_corpus` + `gold.vw_dual_submit_pairs` + `/ops/backfill-pair-labels`. Silver bench (`83e1ab7`) — `snapshot.py` + `bench.py` orchestrator, end-to-end schema init verified locally.
**What's blocked:** Nothing solo. Two Tomo-side actions for full activation: (1) `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` flip on Render for 5c.2; (2) first fixture capture from Render shell for silver bench.
**Next session's job:** Activate what's shipped (env flips + first-fixture capture) OR push forward on Phase 5c.3 (`harness build-corpus` subcommand — pure local, no fixtures yet but consumer code can land).

If the above is enough, stop reading this file and go.

If you need depth (inheriting a blocker, verifying a claim, picking next move), continue.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-22 morning (Phase 5c.2 + silver bench shipped; 5e WASB verification still parallel-agent thread).

**TL;DR — where we are:**
- **Phase 5c.2 shipped (`d7718e0`).** Pair-completion hook + corpus table + view + backfill endpoint. All dark behind `AUTO_LABEL_DUAL_SUBMIT_PAIRS=0` until flipped. Idempotent via UNIQUE constraint and double-check inside `_label_pair_now`. Backfill endpoint `/ops/backfill-pair-labels` ungated — explicit ops call retro-exports for completed pairs.
- **Silver bench shipped (`83e1ab7`).** `snapshot.py` captures bronze + ml_analysis rows for one task to gzipped SQL fixture; `bench.py` restores into local Docker Postgres + runs `build_silver_match_t5` + compares to baseline. Empty-state verified locally — bench DB spin-up creates all 24 expected tables (including the new 5c.2 `training_corpus`). Bootstrap playbook in spec §11.
- **WASB ball detector LIVE in production.** Phase 5e shipped 2026-05-21. Both ECRs hold the new image; both job-defs active with `BALL_TRACKER=wasb`. **Step 5 verification — owned by the parallel agent.**
- **Ball-tracker bench DONE.** v2 metric, locked baseline at `7100792`.
- **Phase 5c.0+5c.1 ready to flip.** `AUTO_DUAL_SUBMIT_T5=1` is the first flip; `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` is the second.

**Open admin items:**
- 5c.2 boot verification — once Render redeploys `83e1ab7`, confirm `ml_analysis.training_corpus` table exists and `gold.vw_dual_submit_pairs` view exists (SQL at §"5c.2 verification" below).
- Silver bench first-fixture capture — needs one Render-shell run. Playbook: `.claude/strategy/silver_bench_design_2026-05-21.md` §11.
- WASB Step 5 verification on task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` — parallel agent owns this; don't intervene unless asked.
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` — older in-flight from earlier in the day, still open.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).

Read in this order before doing anything else:

1. `.claude/strategy/silver_bench_design_2026-05-21.md` — silver bench design + §11 bootstrap playbook.
2. `.claude/strategy/dual_submit_status_2026-05-20.md` — Phase 5c.2 design (shipped) + 5c.3-5c.5 ahead.
3. `.claude/next_session_pickup_ball_bench.md` — ball-bench thread detail + WASB Step 5 verification SQL (parallel agent's thread).
4. `docs/north_star.md` — macro plan; Phase 5e SHIPPED on the ladder.
5. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST + new "Silver bench" subsection at line ~225.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one (recommended order: 1 → 2 → 3 → 4):**

**Option 1: Activate what's shipped.** Tomo-side actions: (a) flip `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render's main API service; (b) run snapshot for first silver-bench fixture from Render shell + upload to S3 + pull locally + bench; (c) run `/ops/backfill-pair-labels` to retro-seed the existing `8a5e0b5e/2c1ad953` pair. Each is <10 min once started. Closes both shipped builds to fully-live state.

**Option 2: Phase 5c.3 — `harness build-corpus` subcommand (~3-4 hr).** Pure local, no Render needed. Reads `ml_analysis.training_corpus`, pulls labels + videos from S3, assembles dataset for training. Spec at `.claude/strategy/dual_submit_status_2026-05-20.md` §4 Phase 5c.3 step 1. Future GPU box step 2 consumes the dataset. **Note:** consumer ships before producer fires meaningfully, so this is speculative until 5c.2 is activated + has rows.

**Option 3: Silver bench follow-ons (~2-3 hr).** (a) Row-level `--diff` flag — show WHICH silver rows changed when bench shows regression. (b) Practice-silver parallel bench — same shape, swap `build_silver_match_t5` for `build_silver_practice`. (c) CI integration per spec §6 — adds a job to `.github/workflows/bench.yml`. Defensive infra; depends on first fixture landing for real signal.

**Option 4: NAT Gateway + EIP + re-lock Render Postgres (~30-60 min).** Closes the `0.0.0.0/0` security hole. Higher risk infra — wants Tomo engaged.

**Strategic frame (Tomo's):** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. Phase 5c.2 is the training-data flywheel foundation. **Activation (Option 1) is the highest-leverage move because both shipped builds are otherwise dormant.** G10 (stroke-classifier hook) deferred to Phase 5c.5 alongside the GPU pipeline — the cv2/video/GPU dependencies make a hook in the Render API process the wrong architecture.

**Things NOT to do** (load-bearing):

- **Don't touch the WASB Step 5 verification thread.** Parallel agent owns it.
- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.** See CLAUDE.md item #8.
- **Don't rollback WASB without running the bench against TrackNetV2 first.** Rollback = `aws batch update-job-definition` clearing the `BALL_TRACKER` env var; previous revs (`:46`/`:28`) kept on standby.
- **Don't try to add the stroke-classifier hook (G10) to the Render API process.** `export_training_data.py` imports cv2, opens the video locally, runs optical flow — none of which works on Render. G10 belongs in Phase 5c.5 on the GPU box.
- **Don't change the `_dual_submit_pair_complete_hook` env-flag default to ON without an explicit go from Tomo.** Default-OFF is the safety; flip is a Render-side action.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural).
- Don't drop `test_videos/` from the GPU rsync.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. One canonical bronze, distinguished by `source`.

---

## State at session end (2026-05-22 morning)

**`origin/main` at `83e1ab7`.** Commits this session (most-recent first):

- `83e1ab7` silver bench: implement snapshot + orchestrator (steps 2+4 of the design)
- `4fba821` docs: pickup refresh — Phase 5c.2 shipped, next move = G10 stroke-classifier hook
- `d7718e0` phase 5c.2: pair-completion hook + ml_analysis.training_corpus

Previous session-end state (still relevant):

- `0aa5a79` session close (2026-05-21 deep eve): pickup refresh + north_star Phase 5e
- `5e3e746` silver bench: scaffolding + Docker Postgres lifecycle helper
- `afe4a56` docs: pickup refresh — WASB swap shipped to production (rev 47 / rev 29)
- `4a39588` WASB swap: env-gated drop-in for BallTracker (default still tracknet_v2)
- `8c209e8` (parallel agent) docs: session_protocol opening/closing prompts
- `abbf81d` (parallel agent) docs: SOP + session protocol guardrails
- `7100792` ball-bench v2 baseline: WASB wins on 880dff02 SA point 6 (0/9 -> 2/9)
- `5319ed7` ball-bench metric v2: post-filter + trajectory coherence + tier breakdown
- `98d20bf` phase 5c.1: add /ops/dual-submit-t5-backfill endpoint

**Ball-bench baseline locked at `7100792`** — `ml_pipeline/diag/bench_ball_baseline.json`.

**Batch state (unchanged):**
- eu-north-1 `ten-fifty5-ml-pipeline:47` → `sha256:8fe82a3...` (with `BALL_TRACKER=wasb`)
- us-east-1 `ten-fifty5-ml-pipeline:29` → same digest, same env var
- Previous active revs (eu :46 / us :28) kept for instant rollback

**Serve bench at session end:** `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Silver-builder bench:** snapshot + orchestrator implemented (commit `83e1ab7`). Schema init verified locally — creates all 24 expected tables on a fresh Docker Postgres. CLI: `python -m ml_pipeline.diag.bench_silver --setup|--status|--teardown|--task <TID8>|--update-baseline`. Snapshot CLI: `python -m ml_pipeline.diag.bench_silver.snapshot --task <TID>`. Empty fixture set — first capture pending.

**GPU dev box:** `i-0295d636f6bf957eb` (eu-north-1b), stopped. Old `i-0fb3983fa555c16e3` (1a) parked stopped.

**Render Postgres allowlist:** `0.0.0.0/0` (open admin item).

**In-flight Batch task:** `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` — WASB Step 5 verification, parallel-agent thread.

---

## Phase 5c.2 — verification (run after Render redeploys `83e1ab7`)

Boot-init creates these idempotently. Verify via `/ops/diag/sql`:

```sql
-- 1. Corpus table exists?
SELECT count(*) AS column_count
  FROM information_schema.columns
 WHERE table_schema = 'ml_analysis' AND table_name = 'training_corpus';
-- Expect: 11 (id, sa_task_id, t5_task_id, label_kind, label_s3_key,
--             video_s3_key, label_count, role_breakdown, created_at,
--             validated_at, used_in_models)

-- 2. Dual-submit view exists?
SELECT count(*) AS column_count
  FROM information_schema.columns
 WHERE table_schema = 'gold' AND table_name = 'vw_dual_submit_pairs';
-- Expect: 11

-- 3. Any existing pairs? (Won't have any until AUTO_DUAL_SUBMIT_T5=1 is flipped
--    AND a tennis_singles match is uploaded. The existing 8a5e0b5e_ball_positions.json
--    label exists locally but the pair structure won't exist in submission_context
--    until backfill or live submission.)
SELECT sa_task_id, t5_task_id, s3_key, pair_complete, paired_at
  FROM gold.vw_dual_submit_pairs
 WHERE pair_complete = TRUE
 ORDER BY paired_at DESC
 LIMIT 5;
```

Activation sequence:
1. Flip `AUTO_DUAL_SUBMIT_T5=1` on Render → next SA upload spawns a paired T5.
2. After T5 completes → confirm two `submission_context` rows for the same `s3_key`.
3. Flip `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render → next pair-completion fires the hook.
4. Confirm one row in `ml_analysis.training_corpus`.
5. `POST /ops/backfill-pair-labels` with `{"dry_run": true}` to find any pre-flip pairs needing retroactive labeling. Then `dry_run=false` to label them.

---

## Silver bench — first-fixture capture (run on Render shell)

Full playbook in `.claude/strategy/silver_bench_design_2026-05-21.md` §11. Quick version:

```bash
# On Render shell (in webhook-server's project dir):
python -m ml_pipeline.diag.bench_silver.snapshot \
    --task 880dff02-58bd-412c-9a29-5c5151004447

# Output: ml_pipeline/fixtures_silver/880dff02_bronze.sql.gz + _silver_baseline.json
# Upload to S3:
python -c "
import boto3
s3 = boto3.client('s3')
for f in ['880dff02_bronze.sql.gz', '880dff02_silver_baseline.json']:
    s3.upload_file(f'ml_pipeline/fixtures_silver/{f}',
                   'nextpoint-prod-uploads', f'fixtures/silver/{f}')
"

# Then locally:
aws s3 cp s3://nextpoint-prod-uploads/fixtures/silver/ \
          ml_pipeline/fixtures_silver/ --recursive
.venv/Scripts/python -m ml_pipeline.diag.bench_silver --setup
.venv/Scripts/python -m ml_pipeline.diag.bench_silver
# Expect: green for the fixture (silver builder is deterministic).
```

Once green, commit the baseline JSON (the `.sql.gz` stays gitignored):
```bash
git add ml_pipeline/fixtures_silver/880dff02_silver_baseline.json
git commit -m "silver bench: lock baseline from production capture"
```

Repeat for `a798eff0` (find full task_id by querying production for the most recent task that matches).

---

## WASB Step 5 — verification SQL (parallel-agent thread)

**This section is owned by the WASB-verification agent. Reproduced here only as a reference.** Do not touch unless the agent has signed off or Tomo says so.

Replace `:tid` with `'1d6feb3a-4624-47ae-b8f5-44246b6d0eb3'`.

```sql
-- 1. Did the pipeline run?
SELECT job_id, status, video_duration_sec, total_frames, video_fps, court_detected
  FROM ml_analysis.video_analysis_jobs
 WHERE job_id = :tid;

-- 2. Bronze ball detection counts — compare to TrackNetV2 baseline on
--    same video (880dff02): 1983 detections, 162 bounces, 13% coverage.
SELECT count(*) AS total_detections,
       count(*) FILTER (WHERE is_bounce) AS bounces,
       count(*) FILTER (WHERE court_x IS NOT NULL) AS with_court_coords,
       max(speed_kmh) AS max_speed_kmh,
       round(avg(speed_kmh) FILTER (WHERE speed_kmh > 30)::numeric, 1) AS avg_real_shot_speed
  FROM ml_analysis.ball_detections
 WHERE job_id = :tid;

-- 3. Coverage in the documented gap regimes (SA point 6 = 5599-6003 if same video).
SELECT count(*) FILTER (WHERE frame_idx BETWEEN 5599 AND 6003) AS in_sa_point_6_window,
       count(*) FILTER (WHERE frame_idx BETWEEN 7539 AND 9829) AS in_91s_gap_window,
       count(*) FILTER (WHERE frame_idx BETWEEN 5347 AND 6892) AS in_61s_gap_window
  FROM ml_analysis.ball_detections
 WHERE job_id = :tid;

-- 4. Source breakdown (roi_prod = Phase 5a ROI rows; default = main pass).
SELECT source, count(*), min(frame_idx), max(frame_idx)
  FROM ml_analysis.ball_detections
 WHERE job_id = :tid
 GROUP BY source;
```

CloudWatch confirm — search the job's log stream for:
```
INFO ml_pipeline.pipeline: Ball tracker: WASB (BALL_TRACKER=wasb)
```

**Rollback if WASB regresses:** point the queue back at rev 46 / 28 (no `BALL_TRACKER` env var). No image rebuild needed.
