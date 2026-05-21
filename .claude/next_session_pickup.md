# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-22 (early morning session — Phase 5c.2 landed)
**Phase active:** Phase 5 — Ball detection coverage. **5c.2 SHIPPED** (pair-completion hook + corpus). 5e WASB verification still pending (parallel-agent thread).
**Bench:** `a798eff0=20/24, 880dff02=23/24` — **green**. Ball-bench v2 locked at `7100792`.
**What shipped last session:** Phase 5c.2 — `_dual_submit_pair_complete_hook` + `ml_analysis.training_corpus` + `gold.vw_dual_submit_pairs` + `POST /ops/backfill-pair-labels`. All gated by `AUTO_LABEL_DUAL_SUBMIT_PAIRS=0` (default OFF) so code ships dark. Commit `d7718e0`.
**What's blocked:** Nothing for my thread. WASB Step 5 verification on Batch task `1d6feb3a-...` is the parallel agent's open thread — don't touch it.
**Next session's job:** Pick from the punch list — recommended next solo build is **Phase 5c.2 follow-up: stroke-classifier hook (G10)** OR **silver-builder bench steps 2+4 (Option 3)**.

If the above is enough, stop reading this file and go.

If you need depth (inheriting a blocker, verifying a claim, picking next move), continue.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-22 early morning (Phase 5c.2 shipped + ad-hoc /init review of CLAUDE.md).

**TL;DR — where we are:**
- **Phase 5c.2 shipped (`d7718e0`).** Pair-completion hook + corpus table + view + backfill endpoint. All dark (env flag off) until Tomo flips it. Idempotent via UNIQUE constraint and double idempotency check inside `_label_pair_now`.
- **WASB ball detector LIVE in production.** Phase 5e shipped 2026-05-21. Both ECRs hold the new image; both job-defs active with `BALL_TRACKER=wasb`. Lambda routes to new revs by job-def name. Rollback = unset the env var on the job-def, no code change. **Step 5 verification — owned by the parallel agent.**
- **Ball-tracker bench DONE** (audit #4). v2 metric (post-filter + trajectory coherence + tier breakdown). Locked baseline at `7100792`. Any future ball-tracker edit benches in seconds.
- **Silver-builder bench scaffolding shipped** (audit #2 partial). Docker Postgres lifecycle helper verified end-to-end. Snapshot + orchestrator are STUBS — next session work.
- **Phase 5c.0+5c.1 ready to flip.** `/ops/dual-submit-t5-backfill` endpoint safety-reviewed and shipped. Tomo to set `AUTO_DUAL_SUBMIT_T5=1` on Render's main API service when convenient. **`AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` is the second flip** that switches on the new 5c.2 hook.

**Open admin items:**
- WASB Step 5 verification on task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` — parallel agent owns this; don't intervene unless asked. SQL block + rollback playbook is reproduced at the bottom of this file.
- 5c.2 boot verification — once Render redeploys, confirm `ml_analysis.training_corpus` table exists and `gold.vw_dual_submit_pairs` view exists (SQL at §"5c.2 verification" below).
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` — older in-flight from earlier in the day, still open.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).

Read in this order before doing anything else:

1. `.claude/strategy/dual_submit_status_2026-05-20.md` — Phase 5c.2 design (shipped) + 5c.3-5c.5 ahead.
2. `.claude/next_session_pickup_ball_bench.md` — ball-bench thread detail + WASB Step 5 verification SQL (parallel agent's thread).
3. `.claude/strategy/silver_bench_design_2026-05-21.md` — silver bench design spec; what's left to build.
4. `docs/north_star.md` — macro plan; Phase 5e SHIPPED on the ladder now.
5. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST. Now includes `wasb_ball_tracker.py`, `wasb_hrnet.py`, `ball_tracker.py`, `config.py`.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one (recommended order: 1 → 2 → 3 → 4):**

**Option 1: Phase 5c.2 follow-up — stroke-classifier hook (G10, ~2 hr).** Extend `_label_pair_now` with a second `label_kind='stroke_classifier'` branch that calls `ml_pipeline/stroke_classifier/export_training_data.py` (refactor needed — currently CLI-only). Same UNIQUE-constraint idempotency. Compounds directly on the 5c.2 framework. Unblocks the far-player stroke classifier (auto-memory `project_far_player_stroke_research.md` flags this as "awaiting dual-submit training data").

**Option 2: Flip Phase 5c.0 + 5c.1 + 5c.2 + run backfills.** Three Render env-var flips: `AUTO_DUAL_SUBMIT_T5=1`, `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1`. Verify by uploading one tennis_singles match → two rows in `ml_analysis.video_analysis_jobs` → after both complete, one row in `ml_analysis.training_corpus`. Then trigger `/ops/dual-submit-t5-backfill` + `/ops/backfill-pair-labels` (dry_run first — see ops_runbook).

**Option 3: Silver-builder bench — finish steps 2+4 (~3-4 hr).** Implement `snapshot.py` (needs DATABASE_URL on Render shell) + bench orchestrator. Then capture first fixtures for 880dff02 + a798eff0. Builds on the Docker Postgres helper shipped in `5e3e746`.

**Option 4: NAT Gateway + EIP + re-lock Render Postgres (~30-60 min).** Closes the `0.0.0.0/0` security hole. Higher risk infra — wants Tomo engaged.

**Strategic frame (Tomo's):** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. WASB shipping was the biggest single bronze-quality move this quarter. **Phase 5c.2 is the training-data flywheel foundation; G10 (stroke-classifier hook) is the natural compound on top.**

**Things NOT to do** (load-bearing):

- **Don't touch the WASB Step 5 verification thread.** Parallel agent owns it.
- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.** See CLAUDE.md item #8.
- **Don't rollback WASB without running the bench against TrackNetV2 first.** Rollback = `aws batch update-job-definition` clearing the `BALL_TRACKER` env var; previous revs (`:46`/`:28`) kept on standby. No image rebuild needed.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural).
- Don't use the §9 sequencing caveat to delay anything — silver-bench-specific (commit `0546278`).
- Don't drop `test_videos/` from the GPU rsync. Broke the first ball-bench run; runbook keeps it on purpose now.
- Don't add an S3 URI fallback to `replay_ball.py` until needed (build-when-needed).
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. One canonical bronze, distinguished by `source`.
- **Don't change the `_dual_submit_pair_complete_hook` env-flag default to ON without an explicit go from Tomo.** Default-OFF is the safety; flip is a Render-side action.

---

## State at session end (2026-05-22 early morning)

**`origin/main` at `d7718e0`.** Commits this session (most-recent first):

- `d7718e0` phase 5c.2: pair-completion hook + ml_analysis.training_corpus

Previous session-end state (still relevant):

- `0aa5a79` session close (2026-05-21 deep eve): pickup refresh + north_star Phase 5e
- `5e3e746` silver bench: scaffolding + Docker Postgres lifecycle helper
- `afe4a56` docs: pickup refresh — WASB swap shipped to production (rev 47 / rev 29)
- `4a39588` WASB swap: env-gated drop-in for BallTracker (default still tracknet_v2)
- `8c209e8` (parallel agent) docs: session_protocol opening/closing prompts
- `abbf81d` (parallel agent) docs: SOP + session protocol guardrails
- `d2a3bd7` docs: pickup refresh — WASB swap (#3) empirically justified
- `7100792` ball-bench v2 baseline: WASB wins on 880dff02 SA point 6 (0/9 -> 2/9)
- `5319ed7` ball-bench metric v2: post-filter + trajectory coherence + tier breakdown
- `98d20bf` phase 5c.1: add /ops/dual-submit-t5-backfill endpoint

**Ball-bench baseline locked at `7100792`** — `ml_pipeline/diag/bench_ball_baseline.json`.

**Batch state (unchanged from previous session):**
- eu-north-1 `ten-fifty5-ml-pipeline:47` → `sha256:8fe82a361023be8db4f50dd188bab74d12700740ed0d0c208d8c6458b94b34fa` (with `BALL_TRACKER=wasb`)
- us-east-1 `ten-fifty5-ml-pipeline:29` → same digest, same env var
- Previous active revs (eu :46 / us :28) kept for instant rollback

**Serve bench at session end:** `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Silver-builder bench:** scaffolding committed (commit `5e3e746`). Snapshot + orchestrator still STUBS.

**GPU dev box:** `i-0295d636f6bf957eb` (eu-north-1b), stopped. Old `i-0fb3983fa555c16e3` (1a) parked stopped.

**Render Postgres allowlist:** `0.0.0.0/0` (open admin item).

**In-flight Batch task:** `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` — WASB Step 5 verification, parallel-agent thread.

---

## Phase 5c.2 — verification (run after Render redeploys `d7718e0`)

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

-- 3. Existing pair? (8a5e0b5e_ball_positions.json was labelled locally;
--    once /ops/backfill-pair-labels runs, this should produce one row.)
SELECT sa_task_id, t5_task_id, s3_key, pair_complete, paired_at
  FROM gold.vw_dual_submit_pairs
 WHERE pair_complete = TRUE
 ORDER BY paired_at DESC
 LIMIT 5;
```

If everything checks out, the next action is:
1. `POST /ops/backfill-pair-labels` with `{"dry_run": true}` — list eligible pairs.
2. Same with `{"dry_run": false, "limit": 1}` — label one to smoke-test the S3 write.
3. Once verified, lift the limit. The existing labelled `8a5e0b5e` pair will be the first row.
4. **Then** flip `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` so future pairs auto-label at end of `_do_ingest_t5`.

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

If you see `Ball tracker: TrackNetV2 (BALL_TRACKER=...)`, the env var didn't reach the container — check `aws batch describe-job-definitions --region eu-north-1 --job-definitions ten-fifty5-ml-pipeline:47 --query 'jobDefinitions[0].containerProperties.environment'` and confirm `BALL_TRACKER=wasb` is present.

**Rollback if WASB regresses:**
```bash
# Strip BALL_TRACKER env var from the rev and re-register (creates a new rev pointing back to TrackNet)
# OR — simpler — point the queue back at rev 46 / 28 (which doesn't have the env var):
aws batch describe-job-definitions --region eu-north-1 --job-definitions ten-fifty5-ml-pipeline:46 --query 'jobDefinitions[0].status'
# Re-register the old digest as a new rev if needed, OR Lambda submits by name → latest active wins,
# so register-job-definition with the OLD image digest + no BALL_TRACKER env to roll back.
```
