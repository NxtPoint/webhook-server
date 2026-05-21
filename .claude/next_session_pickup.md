# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-21 (deep evening session end)
**Phase active:** Phase 5 — Ball detection coverage. **5e (WASB) SHIPPED**, pending production verification on a fresh upload. 5c.0+5c.1 ready to flip.
**Bench:** `a798eff0=20/24, 880dff02=23/24` — **green**. Ball-bench v2 locked at `7100792`.
**What shipped last session:** WASB ball detector live in prod (eu-north-1 rev 47 / us-east-1 rev 29, image `sha256:8fe82a3…`, both with `BALL_TRACKER=wasb` env) + ball-tracker bench v2 + `/ops/dual-submit-t5-backfill` endpoint + silver-builder bench scaffolding.
**What's blocked:** WASB Step 5 production verification — Batch task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` in flight at session end. Tomo will signal when SUCCEEDED.
**Next session's job:** (1) verify `1d6feb3a` writes expected detections to `ml_analysis.ball_detections`, close 5e. Then pick from the punch list below — recommended: Phase 5c.2 (pair-completion hook + corpus index, ~4-6 hr).

If the above is enough, stop reading this file and go.

If you need depth (inheriting a blocker, verifying a claim, picking next move), continue.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-21 deep evening (WASB swap end-to-end shipped, ball-tracker bench locked, silver-bench scaffolding committed).

**TL;DR — where we are:**
- **WASB ball detector LIVE in production.** Phase 5e shipped 2026-05-21. Both ECRs hold the new image; both job-defs active with `BALL_TRACKER=wasb`. Lambda routes to the new revs by job-def name. Rollback = unset the env var on the job-def, no code change.
- **Ball-tracker bench DONE** (audit #4). v2 metric (post-filter + trajectory coherence + tier breakdown). Locked baseline at `7100792`. Any future ball-tracker edit benches in seconds.
- **Silver-builder bench scaffolding shipped** (audit #2 partial). Docker Postgres lifecycle helper verified end-to-end. Snapshot + orchestrator are STUBS — next session work.
- **Phase 5c.0+5c.1 ready to flip.** `/ops/dual-submit-t5-backfill` endpoint safety-reviewed and shipped. Tomo to set `AUTO_DUAL_SUBMIT_T5=1` on Render's main API service when convenient.

**Open admin items:**
- WASB Step 5 verification on task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` — pending Batch SUCCEEDED. Verification SQL in `next_session_pickup_ball_bench.md` §10 and below.
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` — older in-flight from earlier in the day, still open.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).

Read in this order before doing anything else:

1. `.claude/next_session_pickup_ball_bench.md` — ball-bench thread detail + verification SQL.
2. `.claude/strategy/silver_bench_design_2026-05-21.md` — silver bench design spec; what's left to build.
3. `.claude/strategy/dual_submit_status_2026-05-20.md` — Phase 5c.2+ design.
4. `docs/north_star.md` — macro plan; Phase 5e SHIPPED on the ladder now.
5. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST. Now includes `wasb_ball_tracker.py`, `wasb_hrnet.py`, `ball_tracker.py`, `config.py`.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one (recommended order: 0 → 1 → 2 → 3 → 4):**

**Option 0: Verify WASB Step 5 (5 min once Batch SUCCEEDS).** Task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3`. Run the verification SQL below + check CloudWatch for `Ball tracker: WASB (BALL_TRACKER=wasb)` in the job log. **Closes Phase 5e fully.**

**Option 1: Flip Phase 5c.0 + run 5c.1 backfill.** 5-min Render UI: set `AUTO_DUAL_SUBMIT_T5=1` on the main API service. Verify by uploading one tennis_singles match and confirming two rows in `ml_analysis.video_analysis_jobs`. Then trigger backfill via `/ops/dual-submit-t5-backfill` (dry_run first — see ops_runbook).

**Option 2: Phase 5c.2 — pair-completion hook + corpus index (~4-6 hr).** The next big bronze-strategic build. Adds `gold.vw_dual_submit_pairs` view + `ml_analysis.training_corpus` table + a hook in `_do_ingest_t5` end-of-flow. Full design at `.claude/strategy/dual_submit_status_2026-05-20.md` §4. Schema work + view + Python hook. Touches `db_init.py`, `gold_init.py`, `upload_app.py`. **Recommended for a fresh session — it's a meaty solo build.**

**Option 3: Silver-builder bench — finish steps 2+4 (~3-4 hr).** Implement `snapshot.py` (needs DATABASE_URL on Render shell) + bench orchestrator. Then capture first fixtures for 880dff02 + a798eff0. Builds on the Docker Postgres helper shipped this session.

**Option 4: NAT Gateway + EIP + re-lock Render Postgres (~30-60 min).** Closes the `0.0.0.0/0` security hole. Higher risk infra — wants Tomo engaged.

**Strategic frame (Tomo's):** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. WASB shipping was the biggest single bronze-quality move this quarter. Phase 5c.2 (training-data flywheel) is the natural compound on top.

**Things NOT to do** (load-bearing):

- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.** Extended this session. See CLAUDE.md item #8.
- **Don't rollback WASB without running the bench against TrackNetV2 first.** If WASB regresses in production, rollback = `aws batch update-job-definition` clearing the `BALL_TRACKER` env var; previous revs (`:46`/`:28`) kept on standby. No image rebuild needed.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural).
- Don't use the §9 sequencing caveat to delay anything — silver-bench-specific (commit `0546278`).
- Don't drop `test_videos/` from the GPU rsync. Broke the first ball-bench run; runbook keeps it on purpose now.
- Don't add an S3 URI fallback to `replay_ball.py` until needed (build-when-needed).
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. One canonical bronze, distinguished by `source`.

---

## State at session end (2026-05-21 deep evening)

**`origin/main` at `5e3e746`.** Commits this session (most-recent first; all on origin):

- `5e3e746` silver bench: scaffolding + Docker Postgres lifecycle helper
- `afe4a56` docs: pickup refresh — WASB swap shipped to production (rev 47 / rev 29)
- `4a39588` WASB swap: env-gated drop-in for BallTracker (default still tracknet_v2)
- `8c209e8` (parallel agent) docs: session_protocol opening/closing prompts
- `abbf81d` (parallel agent) docs: SOP + session protocol guardrails
- `d2a3bd7` docs: pickup refresh — WASB swap (#3) empirically justified
- `7100792` ball-bench v2 baseline: WASB wins on 880dff02 SA point 6 (0/9 -> 2/9)
- `5319ed7` ball-bench metric v2: post-filter + trajectory coherence + tier breakdown
- `0546278` docs: narrow §9 caveat to silver-only + runbook keeps test_videos in rsync
- `989f80b` docs: refresh next_session_pickup + add ball_bench pickup detail
- `4867ccc` fixtures_ball: revise a798eff0 + add 880dff02 — focus Phase 5 win condition
- `0d9c9ee` ball-tracker bench scaffolding
- `98d20bf` phase 5c.1: add /ops/dual-submit-t5-backfill endpoint
- `e487204` docs: CLAUDE.md PowerShell + session-file hint + bench command
- (plus parallel-agent commits `1a0f8b3`, `d40bd7f`, `0e0a30e` for doc + GPU box migration + silver-bench design spec)

**Ball-bench baseline locked at `7100792`** — `ml_pipeline/diag/bench_ball_baseline.json`.

**Batch state — UPDATED THIS SESSION:**
- eu-north-1 `ten-fifty5-ml-pipeline:47` → `sha256:8fe82a361023be8db4f50dd188bab74d12700740ed0d0c208d8c6458b94b34fa` (with `BALL_TRACKER=wasb`)
- us-east-1 `ten-fifty5-ml-pipeline:29` → same digest, same env var
- Previous active revs (eu :46 / us :28) kept for instant rollback
- Lambda submits by job-def name — new jobs auto-resolve to these revs

**Serve bench at session end:** `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Ball bench at session end (v2 metric, GPU run on Tesla T4):**

| fixture | tracker | post_rate | post_recall | coherence | tier note |
|---|---|---|---|---|---|
| 880dff02 | tracknet_v2 | 47.15% | **0.00%** (0/9) | 73.30% | 58% fallback noise |
| 880dff02 | wasb | 11.76% | **22.22%** (2/9) | 70.84% | — |
| a798eff0 | tracknet_v2 | 63.54% | 33.33% (1/3) | 78.80% | 67% fallback noise |
| a798eff0 | wasb | 22.07% | 33.33% (1/3) | 71.53% | — |

**Silver-builder bench:** scaffolding committed (commit `5e3e746`). Docker Postgres helper end-to-end verified. CLI: `python -m ml_pipeline.diag.bench_silver --status|--setup|--teardown`. Snapshot + orchestrator are STUBS — next session work.

**GPU dev box:** `i-0295d636f6bf957eb` (eu-north-1b), stopped. Old `i-0fb3983fa555c16e3` (1a) parked stopped.

**Render Postgres allowlist:** `0.0.0.0/0` (open admin item).

**In-flight Batch task:** `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` (WASB Step 5 verification, kicked off late this session).

---

## WASB Step 5 — verification SQL (run when `1d6feb3a` SUCCEEDS)

```sql
-- Replace :tid with '1d6feb3a-4624-47ae-b8f5-44246b6d0eb3'.

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
