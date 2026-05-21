# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-21 evening (ball-bench scaffolding + Phase 5c.1 endpoint + GPU box AZ migration + **WASB win measured: 2/9 vs 0/9 SA point 6 strokes**).

**TL;DR — where we are:**
- **Ball-tracker bench is fully shipped with metric v2** (post-filter + trajectory coherence + tier breakdown). Production-aligned: WASB's 11.76% post_filter_rate on 880dff02 matches the documented 13% bronze coverage almost exactly.
- **WASB wins on the regime that matters.** On 880dff02 SA point 6 (the canonical "TrackNetV2 finds zero balls" 9-stroke rally per `docs/_investigation/may07_sa_point6_gap.md`): WASB recovers **2 of 9** strokes; TrackNetV2 recovers **0 of 9**. WASB's lower raw det_rate is because TrackNetV2's 4-tier output is 58-67% motion-fallback noise.
- **Bench infrastructure ready for next iteration.** Both fixtures committed, baseline locked at `7100792`. Any future ball-tracker edit can be benched in seconds.
- **Phase 5c.0+5c.1 ready to flip.** `/ops/dual-submit-t5-backfill` endpoint shipped (`98d20bf`); safety-reviewed and ready. You just need to set `AUTO_DUAL_SUBMIT_T5=1` on Render's main API service when ready.

**Open admin items:**
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` — confirm SUCCEEDED + `ml_analysis.ball_detections WHERE job_id = '6a8a344f-...' AND source = 'roi_prod'` is non-zero.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS) — terminate or keep for rollback.

Read in this order before doing anything else:

1. `.claude/next_session_pickup_ball_bench.md` — current state of the ball-bench thread + WASB-swap decision rule.
2. `.claude/strategy/infrastructure_audit_2026-05-20.md` — punch list. Audit #3 (WASB integration) is now empirically justified by the bench; #4 (ball-tracker bench) is DONE.
3. `docs/north_star.md` — macro plan. Phase 5 ball coverage still the bottleneck; WASB is the next concrete win.
4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST is required for the WASB ship.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one (recommended order: 1 → 2 → 3 → 4):**

**Option 1: Plan + ship the WASB production swap (audit #3).** Bench validated WASB wins on coverage gaps. The swap = wire `WASBBallTracker` into `ml_pipeline/pipeline.py` (replacing or alongside `BallTracker`), Docker rebuild + dual-region ECR push + new job-def revisions per BATCH-SIDE CHANGE CHECKLIST, then verify on a fresh Batch upload that bronze ball coverage improves. ~1-2 sessions. **The highest-leverage next move on the bronze-quality axis.**

**Option 2: Flip Phase 5c.0 (AUTO_DUAL_SUBMIT_T5=1).** 5-min Render env-var change. Verify by uploading one tennis_singles match and confirming two rows in `ml_analysis.video_analysis_jobs`. Then optionally trigger the backfill via `/ops/dual-submit-t5-backfill` (dry_run first).

**Option 3: NAT Gateway + EIP + re-lock Render Postgres.** Closes the security hole. 30-60 min VPC networking.

**Option 4: Silver-builder bench (audit #2).** Now that ball-bench is the working template, replicating the pattern for silver is straightforward. Lower priority per the bronze-first strategy but it's the next-best leverage item after WASB ships.

**Strategic frame (Tomo's):** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. WASB swap directly improves bronze quality on the documented failure mode (SA point 6 zero-coverage gaps). This is the move.

**Things NOT to do** (load-bearing):

- Don't ship WASB without the BATCH-SIDE CHANGE CHECKLIST — `ml_pipeline/wasb_ball_tracker.py`, `wasb_hrnet.py`, and any change to `pipeline.py` are in-container. Docker rebuild + dual-region ECR push + new job-def revisions are required.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (the motion-fallback noise is structural, not a tuning problem).
- Don't use the §9 sequencing caveat to delay anything — it's silver-bench-specific (commit `0546278`).
- Don't drop test_videos/ from the GPU rsync (broke the first ball-bench run).
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. One canonical bronze, distinguished by `source`.

---

## State at session end (2026-05-21 late evening)

**`origin/main` at `7100792`.** Commits this session, all on origin:

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

**Ball-bench baseline locked at `7100792`** — see `ml_pipeline/diag/bench_ball_baseline.json`.

**Batch state (unchanged):**
- eu-north-1 `ten-fifty5-ml-pipeline:46` → `sha256:87435dbfd…`
- us-east-1 `ten-fifty5-ml-pipeline:28` → `sha256:87435dbfd…`

**Serve bench at session end:** `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Ball bench at session end (v2 metric, GPU run on Tesla T4):**

| fixture | tracker | post_rate | post_recall | coherence | tier note |
|---|---|---|---|---|---|
| 880dff02 | tracknet_v2 | 47.15% | **0.00%** (0/9) | 73.30% | 58% fallback noise |
| 880dff02 | wasb | 11.76% | **22.22%** (2/9) | 70.84% | — |
| a798eff0 | tracknet_v2 | 63.54% | 33.33% (1/3) | 78.80% | 67% fallback noise |
| a798eff0 | wasb | 22.07% | 33.33% (1/3) | 71.53% | — |

**GPU dev box:** `i-0295d636f6bf957eb` (eu-north-1b), stopped. Old `i-0fb3983fa555c16e3` (1a) parked stopped.

**Render Postgres allowlist:** `0.0.0.0/0` (open admin item).

**In-flight Batch task:** `6a8a344f-93bb-49af-8456-88d81a5dd7e3` (Option A verification, status unknown at session end).
