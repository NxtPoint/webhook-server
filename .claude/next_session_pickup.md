# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session was 2026-05-20.

**TL;DR change since last handover:** Phase 5b is PARKED with empirical receipts. The staged motion-threshold change was tested and falsified locally (Tier 4 already saturated; lowering the threshold regresses post-`_filter_outliers` survival by 11.6%). The active sub-task is now **Phase 5a — finish the ROI bounce extractor**.

Read in this exact order before doing anything else:

1. `docs/north_star.md` — macro plan. Phase 5b status row reads "PARKED 2026-05-20" with the empirical detail inline. Phase 5a is marked ACTIVE.

2. `.claude/phase5a_kickoff.md` — **REQUIRED READING.** Full scoping of Phase 5a:
   - Architectural picture (the stub at `ml_pipeline/roi_extractors/bounces.py`, the working diag reference `ml_pipeline/diag/extract_roi_bounces.py`, the `extract_far_pose` precedent in `__main__.py:200-211`)
   - Recommended design (option (c) from the stub: bronze `ball_detections` as anchor source)
   - What to lift from the diag tool, what to drop (SA-truth dependency, CLI scaffolding)
   - Production wiring sketch in `__main__.py`
   - Three-stage validation plan (local sanity → Batch on 880dff02 → success criteria)
   - Open questions for next session
   - Things NOT to do

3. `.claude/phase5b_ball_tracker_characterisation.md` — receipts for the parking decision. The "Tuning rounds" table records Round 0 (CloudWatch baseline + local Tier-4 sweep falsifying the threshold change) and the reprioritised candidate list. Read if you're tempted to re-attempt 5b.

4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST section is mandatory. Edits to `ml_pipeline/roi_extractors/` or `__main__.py` require Docker rebuild + dual-region ECR push + new job-def revisions before Batch sees the change.

Then run the bench locally to confirm the floor is still locked:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions. If anything moved, stop and investigate before touching code.

If bench green, the first move is:

**A.** Confirm the bronze `ball_detections` anchor signal is usable. On `880dff02`, count how many bronze detections fall inside the service-box zone (court_x in [-1.5, COURT_WIDTH_DOUBLES+1.5], court_y in [FAR_SERVICE_LINE-1.5, NEAR_SERVICE_LINE+1.5]). If `>` ~50 candidates, option (c) is well-anchored; if fewer, fall back to option (b) or revisit.

    -- run via /ops/diag/sql endpoint (header auth)
    SELECT count(*) FROM ml_analysis.ball_detections
    WHERE job_id = '880dff02-58bd-412c-9a29-5c5151004447'
      AND court_x BETWEEN -1.5 AND 12.47
      AND court_y BETWEEN 3.985 AND 19.785;

**B.** Build the new `extract_far_bounces` in `ml_pipeline/roi_extractors/bounces.py`. Lift from the diag tool per the `phase5a_kickoff.md` "What to port" list. Skip SA-truth lookups and S3 video resolution (the prod pipeline already has `tmp_path` and `engine`).

**C.** Wire the call into `ml_pipeline/__main__.py` after step 2b (around line 211), inside a non-fatal `try/except` matching `extract_far_pose`'s pattern.

**D.** Stage 1 local validation: run a Python harness on `a798eff0_sa_video.mp4` (mocking the prod anchor source with the bench fixture's `ball_rows`). Sanity-check that some output rows land in service-box zone court coords and concentrate near SA serve timestamps.

**E.** Bench check, commit, push.

**F.** BATCH-SIDE CHANGE CHECKLIST (Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1). Ask Tomo to rerun `880dff02`.

**G.** Stage 2 measurement: query `ml_analysis.ball_detections_roi` for `880dff02` row count, plus per-window distribution. Re-run `reconcile_serves_strict` to confirm bench stays green and `audit_points_reconcile` to measure delta vs 0/17 baseline.

**Things NOT to do** (load-bearing — restating from `CLAUDE.md`, `docs/north_star.md`, and `phase5a_kickoff.md`):

- Don't re-attempt Phase 5b motion-threshold tuning. Round 0 receipts are conclusive; branch `phase-5b/motion-threshold-reduce` is retained on origin as falsified-hypothesis record. Don't merge.
- Don't widen the service-box zone to cover the whole court — the upsample-into-service-box trick IS the resolution gain.
- Don't ship a Batch round without the BATCH-SIDE CHANGE CHECKLIST. `roi_extractors/` edits are in-container.
- Don't ship without bench green.
- Don't skip the non-fatal try/except around the call site in `__main__.py`. 5a is additive; failure must not block silver/trim/notify.
- Don't re-attempt Phase 3 part 2 with any pure-SQL pattern. Same Phase 5 unblocker required.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD`.

---

## State at session end (2026-05-20)

**Local `main` at `d26e8cc`** (Round 0 findings commit), one or more ahead of `origin/main` if Tomo hasn't pushed yet. Push status depends on the close-of-session decision — see commit log via `git log --oneline -20`.

Recent commits on main (newest first; from before this session and through to today's close):

- `d26e8cc` phase 5b: Round 0 findings — staged change falsified, candidate list reprioritised
- `923280d` docs: refresh next_session_pickup state snapshot
- `1e7d286` dashboards: revert split tables — single H2H comparison w/ prominent headers
- `542dbed` docs: copy Phase 5b characterisation + kickoff cross-ref to main
- `e77f6a6` docs: harden CLAUDE.md + add canonical next-session handover
- `9ebda55` docs: tidy — CI bullet list + .gitignore .claude/tmp/
- `d4d5b36` docs: session_2026-05-20_phase5b_staged — Phase 5b round-1 ready for Batch
- `188a0d9` docs: 2026-05-20 PM — Phase 3 part 2 attempted + reverted, parked behind Phase 5
- `de06d41` Revert Phase 3 part 2 (v1 + v2 SQL approximations both flawed)

What this session contributed (the Phase 5b → 5a pivot):

- Bench confirmed locked on both fixtures at session start.
- Round 0 baseline diagnostics pulled from CloudWatch (eu-north-1 stream `ml-pipeline/default/1f5ceffac5de4e69806bdf61c2fdf4e0`, 2026-05-07 880dff02 run) — captured tier breakdown (tier1=36.1%, tier4=63.5%, none=63.5%, tier2_cc_rejected=0, tier3_argmax=0). Per-frame returns ~100%; persisted DB rows ~13%; 7.7× collapse is downstream of Tier 4.
- Local Tier-4-only sweep on `a798eff0` at threshold=25 vs 15 — falsified the staged change (post-filter survival 3205 → 2833, −11.6%; mean cluster length 118.7 → 69.1).
- Surrogate Option α experiment ran but inconclusive (construction artifacts + finding that `ball_rows` aren't strongly concentrated in rally windows even pre-filter — so "anchor on recent Tier-1" doesn't get the concentration boost the design assumed).
- Full BallTracker local validation aborted: 40-min CPU estimate was off by ~30× (actual ~21 hrs without GPU).
- `docs/north_star.md` Phase 5 detail updated: 5a is ACTIVE, 5b is PARKED with receipts inline.
- `.claude/phase5b_ball_tracker_characterisation.md` updated: Tuning rounds Round 0 + Round 1 (cancelled) populated; candidate list reprioritised (motion threshold + CC upper bound crossed off; source-aware filter + track-confirmation added as new top candidates if 5b ever resumes).
- `.claude/phase5a_kickoff.md` NEW — full Phase 5a scoping (this session's main deliverable for the next agent).
- `.claude/phase5_kickoff.md` updated: redirect from 5b to 5a.
- This file updated to reflect the pivot.

**Branch `phase-5b/motion-threshold-reduce`:** retained on origin as a falsified-hypothesis record. Do not merge. Do not reuse the branch name.

**Working tree at session end:** clean except `ml_pipeline/training/visual_debug/` (untracked, deliberately ignored) and potentially `frontend/match_analysis.html` if Tomo's parallel dashboard session left uncommitted work.

The next chat picks up Phase 5a from a fully-scoped kickoff doc, with bench locked and Round 0 receipts committed. That's a clean Phase 5a stage 1 (local sanity) in one session, with stage 2 (Batch validation) handed off afterwards.

Good luck with the v18 DB upgrade. The diag tools used today (`.claude/tmp/fetch_balltracker_diag.py` for CloudWatch, `.claude/tmp/phase5b_tier4_local_experiment.py` for the Tier-4 sweep) live under `.claude/tmp/` (gitignored) and are reusable for future BallTracker investigations if needed.
