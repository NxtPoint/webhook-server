# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-21 late evening (**WASB shipped to production end-to-end** — image pushed to both ECRs, eu-north-1 rev 47 + us-east-1 rev 29 active with `BALL_TRACKER=wasb` env var, image verified). Waiting on a fresh Batch upload to confirm production behaviour.

**TL;DR — where we are:**
- **WASB ball detector is LIVE in production Batch** (audit #3 SHIPPED). Image `sha256:8fe82a361023be8db4f50dd188bab74d12700740ed0d0c208d8c6458b94b34fa`. Pipeline.py picks between BallTracker and WASBBallTracker via `BALL_TRACKER` env var (default `tracknet_v2`); both job-defs now set `BALL_TRACKER=wasb`. Rollback = unset the env var on the job-def, no code change.
- **WASB validated on the regime that matters.** Ball-bench (commit `7100792`): WASB recovers 2/9 SA point 6 strokes vs TrackNetV2's 0/9. WASB's 11.76% post_filter_rate matches the documented 13% production coverage.
- **Phase 5c.0+5c.1 ready to flip.** `/ops/dual-submit-t5-backfill` endpoint shipped. Tomo to set `AUTO_DUAL_SUBMIT_T5=1` on Render's main API service.
- 🟡 **WASB Step 5 — fresh Batch upload verification pending.** Tomo to upload one `tennis_singles` match via Media Room. Agent runs the verification SQL in `/tmp/wasb_verify.sql` (or rewrites equivalents).

**Open admin items:**
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` — confirm SUCCEEDED + `ml_analysis.ball_detections WHERE job_id = '6a8a344f-...' AND source = 'roi_prod'` is non-zero. STILL OPEN from session start.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS) — terminate or keep for rollback.

Read in this order before doing anything else:

1. `.claude/next_session_pickup_ball_bench.md` — current state of the ball-bench thread, WASB-swap detail, verification SQL.
2. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST. Now includes `wasb_ball_tracker.py`, `wasb_hrnet.py`, `ball_tracker.py`, `config.py` (extended this session).
3. `.claude/strategy/infrastructure_audit_2026-05-20.md` — punch list. Audit #3 (WASB) shipped; #4 (ball-bench) DONE; #2 (silver-bench) and #5+ remain.
4. `docs/north_star.md` — macro plan. Phase 5 ball coverage closing — WASB win measured, production verification pending.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one (recommended order: 0 → 1 → 2 → 3 → 4):**

**Option 0: Verify WASB on a fresh Batch upload (Step 5).** Tomo uploads any `tennis_singles` match via Media Room → frontend or `aws batch submit-job` with `--job-definition ten-fifty5-ml-pipeline:47`. After SUCCEEDED, query `ml_analysis.ball_detections` for the new task_id and compare counts/coverage vs the TrackNetV2 baselines on 880dff02 (1983 detections, 162 bounces, 13% frame coverage). CloudWatch should show `Ball tracker: WASB (BALL_TRACKER=wasb)` in the job log. **Closes the WASB swap.**

**Option 1: Flip Phase 5c.0 (`AUTO_DUAL_SUBMIT_T5=1`).** 5-min Render env-var change. Verify by uploading one tennis_singles match and confirming two rows in `ml_analysis.video_analysis_jobs`. Then optionally trigger the backfill via `/ops/dual-submit-t5-backfill` (dry_run first).

**Option 2: NAT Gateway + EIP + re-lock Render Postgres.** Closes the security hole. 30-60 min VPC networking.

**Option 3: Silver-builder bench (audit #2).** Replicates the ball-bench pattern for the silver builder. ~1 session of code. Lower priority per bronze-first strategy.

**Option 4: Phase 5c.2 (pair-completion hook + corpus index).** ~4-6 hrs per `.claude/strategy/dual_submit_status_2026-05-20.md` §G3-G5. Needed before any actual TrackNetV3 retrain; but WASB shipping may reduce the urgency (per §6 risk #3).

**Strategic frame (Tomo's):** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. WASB shipping is the biggest single bronze quality move this quarter — verify it in production, then move to either dual-submit corpus (compound win over time) or close the NAT/Postgres security gap.

**Things NOT to do** (load-bearing):

- **Don't merge ball_tracker.py, wasb_ball_tracker.py, wasb_hrnet.py, config.py, pipeline.py, or Dockerfile changes without BATCH-SIDE CHANGE CHECKLIST.** Extended this session — see CLAUDE.md item #8 and `.claude/handover_t5.md` for the current file list.
- **Don't rollback WASB without first running the bench against TrackNetV2.** If WASB regresses on the production verification, the rollback is `aws batch update-job-definition` to clear `BALL_TRACKER` (or set to `tracknet_v2`), then the next job uses TrackNetV2. No image rebuild needed.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural, not a tuning problem).
- Don't use the §9 sequencing caveat to delay anything — silver-bench-specific (commit `0546278`).
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. One canonical bronze, distinguished by `source`.

---

## State at session end (2026-05-21 deep evening)

**`origin/main` at `4a39588` + any pending pickup updates.** Commits this session:

- `e487204` CLAUDE.md PowerShell + session-file hint + bench command
- `0d9c9ee` ball-tracker bench scaffolding
- `4867ccc` fixtures revised — focus Phase 5 win condition
- `0546278` §9 caveat narrowed + runbook keeps test_videos in rsync
- `989f80b` refresh next_session_pickup + add ball_bench pickup
- `98d20bf` Phase 5c.1: /ops/dual-submit-t5-backfill endpoint
- `d40bd7f` (parallel agent) GPU box migration 1a → 1b
- `d3abbfc` ball-bench initial baseline (v1 metric, lenient)
- `0e0a30e` (parallel agent) silver-builder bench design spec
- `5319ed7` ball-bench metric v2 (post-filter + coherence + tier breakdown)
- `7100792` v2 baseline — WASB wins on 880dff02 SA point 6
- `d2a3bd7` pickup refresh — WASB swap empirically justified
- `abbf81d` (parallel agent) SOP + session protocol
- `8c209e8` (parallel agent) session_protocol — opening/closing prompts
- `4a39588` **WASB swap shipped: env-gated drop-in + Dockerfile + BATCH-SIDE CHANGE CHECKLIST extension**

**Ball-bench baseline locked at `7100792`** — `ml_pipeline/diag/bench_ball_baseline.json`.

**Batch state — UPDATED THIS SESSION:**
- eu-north-1 `ten-fifty5-ml-pipeline:47` → `sha256:8fe82a361023be8db4f50dd188bab74d12700740ed0d0c208d8c6458b94b34fa` (with `BALL_TRACKER=wasb` env var, retryStrategy preserved)
- us-east-1 `ten-fifty5-ml-pipeline:29` → same digest, same env var
- Lambda submits by job-def name — new jobs auto-resolve to these revisions.
- Previous active revs (eu :46 / us :28) kept for rollback.

**Serve bench at session end:** `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Ball bench at session end (v2 metric, GPU run on Tesla T4):**

| fixture | tracker | post_rate | post_recall | coherence | tier note |
|---|---|---|---|---|---|
| 880dff02 | tracknet_v2 | 47.15% | **0.00%** (0/9) | 73.30% | 58% fallback noise |
| 880dff02 | wasb | 11.76% | **22.22%** (2/9) | 70.84% | — |
| a798eff0 | tracknet_v2 | 63.54% | 33.33% (1/3) | 78.80% | 67% fallback noise |
| a798eff0 | wasb | 22.07% | 33.33% (1/3) | 71.53% | — |

**End-to-end test (Tesla T4, BALL_TRACKER=wasb, 880dff02 SA point 6 window):**
- 287/405 frames yielded detect_frame output (71% raw)
- After interpolate_gaps + _filter_outliers: 39 detections retained
- detect_bounces found 1 bounce (no court_detector — limited to base path)
- assign_peak_flight_speeds, compute_speeds, log_diagnostics, reset all functional

**GPU dev box:** `i-0295d636f6bf957eb` (eu-north-1b), stopped. Old `i-0fb3983fa555c16e3` (1a) parked stopped.

**Render Postgres allowlist:** `0.0.0.0/0` (open admin item).

**In-flight Batch task:** `6a8a344f-93bb-49af-8456-88d81a5dd7e3` (Option A verification from prior session, status unknown at session end).

**Verification SQL for WASB Step 5** (also written to `/tmp/wasb_verify.sql` on the local machine):

```sql
-- Replace :tid with the new task_id from the upload.

SELECT job_id, status, video_duration_sec, total_frames, video_fps, court_detected
  FROM ml_analysis.video_analysis_jobs
 WHERE job_id = :tid;

SELECT count(*) AS total_detections,
       count(*) FILTER (WHERE is_bounce) AS bounces,
       count(*) FILTER (WHERE court_x IS NOT NULL) AS with_court_coords,
       max(speed_kmh) AS max_speed_kmh,
       round(avg(speed_kmh) FILTER (WHERE speed_kmh > 30)::numeric, 1) AS avg_real_shot_speed
  FROM ml_analysis.ball_detections
 WHERE job_id = :tid;

SELECT source, count(*), min(frame_idx), max(frame_idx)
  FROM ml_analysis.ball_detections
 WHERE job_id = :tid
 GROUP BY source;
```

CloudWatch should show `INFO ml_pipeline.pipeline: Ball tracker: WASB (BALL_TRACKER=wasb)` in the job log. If you see `Ball tracker: TrackNetV2`, the env var didn't reach the container — check `aws batch describe-job-definitions --region eu-north-1 --job-definitions ten-fifty5-ml-pipeline:47 --query 'jobDefinitions[0].containerProperties.environment'`.
