# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous sessions: 2026-05-21 PM (Phase 5a Stage 2 + Option A delivered), 2026-05-21 evening (ball-tracker bench scaffolding built, awaiting GPU baseline run).

**TL;DR — where we are:**
- **Phase 5a is fully shipped end-to-end** (from prior session): ROI bounces write into canonical `ml_analysis.ball_detections` with `source='roi_prod'`. eu-north-1 rev 46 / us-east-1 rev 28 both pinned to `sha256:87435dbfd…`.
- **Ball-tracker bench scaffolding committed** (this session). Audit item #4 — required gate for the WASB swap in #3. Mirror-of-`bench.py` architecture: `replay_ball.py` runs one tracker on one fixture, `bench_ball.py` orchestrates all (fixture × tracker) pairs vs `bench_ball_baseline.json`, `snapshot_task_ball.py` bootstraps fixtures from SA truth.
- **Two fixtures committed:** `a798eff0.json` (regression guard for serve-bench compatibility — 3 windows / 938 frames) and `880dff02.json` (Phase 5 progress guard — 3 windows / 1105 frames, including the canonical SA point 6 [5599, 6003] where TrackNetV2 finds zero balls).
- **Baseline NOT YET LOCKED** — needs GPU run on `t5-dev-gpu` box. Workflow + decision rule documented in `.claude/next_session_pickup_ball_bench.md`.
- **CLAUDE.md updated** to make this file the canonical "first read" for every session, overwritten at session end.

**Open admin items:**
- Render Postgres still open to `0.0.0.0/0` (opened 2026-05-21 to unblock Batch). Re-lock to home IP `105.214.8.31/32` OR build the NAT-Gateway-with-EIP proper fix. See `feedback_render_postgres_ip_allowlist`.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` uploaded 2026-05-21 evening — Tomo to confirm SUCCEEDED + verify `ml_analysis.ball_detections WHERE job_id = '6a8a344f-...' AND source = 'roi_prod'` is non-zero.

Read in this order before doing anything else:

1. `.claude/next_session_pickup_ball_bench.md` — **REQUIRED if continuing the ball-bench thread.** Current state, GPU workflow, decision rule, things-not-to-do specific to this work.
2. `.claude/session_2026-05-21_phase5a_stage2.md` — full Phase 5a / Option A story (deep detail behind the current state).
3. `docs/north_star.md` — macro plan. Phase 5a done; Phase 5 ball coverage still the bottleneck.
4. `.claude/strategy/infrastructure_audit_2026-05-20.md` — prioritised roadmap. §9 caveat now narrowed to silver bench only (commit `0546278`).
5. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST still required for future Batch deploys.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one (recommended order: 1 → 2 → 3 → 4):**

**Option 1: Run the ball-tracker bench on the GPU box.** Lock baseline for TrackNetV2 + WASB across both fixtures. Decide WASB swap. ~1 session including baseline lock + WASB run + analysis. Full workflow in `.claude/next_session_pickup_ball_bench.md`. **This is the in-flight thread — finish it first before starting anything else.**

**Option 2: Verify Option A Batch run.** Once `6a8a344f-93bb-49af-8456-88d81a5dd7e3` SUCCEEDS, confirm `source='roi_prod'` rows landed in canonical bronze. Proves the new image (rev 46/28) writes correctly. ~5 min.

**Option 3: Silver-builder bench (audit #2).** Highest-leverage audit item by isolated measure, but lower priority than #4 → #3 for the bronze-first strategy. ~1 session. The §9 caveat in the audit was the gate — Phase 5a has landed, so this can baseline now.

**Option 4: NAT Gateway + static EIP for Batch + re-lock Render Postgres.** The proper security fix. 30-60 min VPC networking task. Removes the `0.0.0.0/0` hole.

**Strategic frame (Tomo's):** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. Prioritise items that directly attack bronze quality (WASB swap, dual-submit, ball-tracker bench as the safety net) over items that improve silver-builder iteration.

**Things NOT to do** (load-bearing — restated from `CLAUDE.md`, `docs/north_star.md`, and the feedback memories):

- Don't re-attempt Phase 5b motion-threshold tuning. Receipts conclusive; branch `phase-5b/motion-threshold-reduce` retained as falsified-hypothesis record.
- Don't create parallel bronze tables. One canonical bronze, distinguished by `source`. See `feedback_t5_single_canonical_bronze`.
- Don't use §9 sequencing caveat to delay ball-bench baseline (caveat is silver-bench-specific — clarified in commit `0546278`).
- Don't drop the `test_videos/` include from the GPU rsync. The first bench attempt broke for this reason; the runbook keeps it on purpose now.
- Don't add an S3 URI fallback to `replay_ball.py` until we feel real pain from the local-path coupling — build-when-needed.
- Don't ship a Batch round without the BATCH-SIDE CHANGE CHECKLIST.
- Don't ship without bench green (`python -m ml_pipeline.diag.bench`).
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD`.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys. See `feedback_agent_handles_deploys`.

---

## State at session end (2026-05-21 evening)

**`origin/main` at `0546278`.** 4 new commits this session, all on origin:

- `e487204` docs: CLAUDE.md — PowerShell shell note, session-file pickup hint, bench in T5 commands
- `0d9c9ee` ball-tracker bench scaffolding: replay + bench + snapshot tools + a798eff0 fixture
- `4867ccc` fixtures_ball: revise a798eff0 + add 880dff02 — focus Phase 5 win condition
- `0546278` docs: narrow §9 caveat to silver-only + runbook keeps test_videos in rsync

**Batch state (unchanged from prior session):**
- eu-north-1 job-def `ten-fifty5-ml-pipeline:46` → image `@sha256:87435dbfd…`
- us-east-1 job-def `ten-fifty5-ml-pipeline:28` → image `@sha256:87435dbfd…`

**Bench at session end:** `commit=e487204`, a798eff0=20/24, 880dff02=23/24, no regressions. (Re-ran after CLAUDE.md edits as the first action of this session.)

**Ball-tracker bench at session end:** scaffolding ready, fixtures committed, **baseline NOT YET LOCKED**. Needs GPU box run per `.claude/next_session_pickup_ball_bench.md`.

**GPU dev box:** stopped. Start command in `.claude/infrastructure/gpu_dev_box_runbook.md`.

**Working tree at session end:** clean except `ml_pipeline/training/visual_debug/` (untracked, deliberately ignored per CLAUDE.md "Things not to do").

**Render Postgres allowlist:** `0.0.0.0/0` (still — open admin item).

**In-flight Batch task:** `6a8a344f-93bb-49af-8456-88d81a5dd7e3` uploaded during this session for Option A verification. Status at session end: unknown to agent (Tomo self-serves from frontend dashboard).
