# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session was 2026-05-20.

**TL;DR change since last handover:** Phase 5b is **PARKED** with empirical receipts. The staged motion-threshold change (`phase-5b/motion-threshold-reduce` branch) was tested locally and falsified: Tier 4 is already saturated at ~100% per-frame return rate; lowering the threshold doesn't help and actually *regresses* post-`_filter_outliers` survival by 11.6% on `a798eff0`. The dominant filter is `_filter_outliers` (150px gate) downstream, not the Tier-4 threshold upstream. Source-aware filter alternative (Option α) was inconclusive in surrogate testing, and clean validation requires GPU (CPU run is ~21 hrs, not viable).

**The active sub-task is now Phase 5a — finish the ROI bounce extractor.** Additive coverage source rather than tuning the saturated BallTracker. Fully scoped in `.claude/phase5a_kickoff.md`; ready to implement.

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

**A.** Confirm the bronze `ball_detections` anchor signal is usable. On `880dff02`, count + frame-distribution of bronze detections inside the service-box zone (court_x in `[-1.5, 12.47]` = doubles ±1.5m; court_y in `[3.985, 19.785]` = far-service-line−1.5 to near-service-line+1.5):

    -- run via /ops/diag/sql endpoint (header auth)
    SELECT count(*)                                              AS total,
           count(*) FILTER (WHERE is_bounce)                     AS bounces,
           min(frame_idx)                                        AS first_frame,
           max(frame_idx)                                        AS last_frame,
           count(DISTINCT frame_idx / 250)                       AS distinct_10s_buckets
    FROM ml_analysis.ball_detections
    WHERE job_id = '880dff02-58bd-412c-9a29-5c5151004447'
      AND court_x BETWEEN -1.5 AND 12.47
      AND court_y BETWEEN 3.985 AND 19.785;

The decision: if `total` ≥ a few dozen AND `distinct_10s_buckets` ≈ rally count (24 serves → ~12-18 distinct buckets after merging), option (c) is well-anchored. If `total` is in the single digits, fall back to option (b) (use `ml_analysis.serve_events` from the Render-side serve_detector as anchors — that's where pose timestamps live). Read it; don't over-fit a threshold without seeing the data.

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

**`origin/main` at `590d43b`** — fully pushed. Bench locked at `a798eff0` 20/24 + `880dff02` 23/24 throughout the session. Zero detector-quality regression.

Most-relevant recent commits on main (newest first; full log via `git log --oneline -20`):

- `590d43b` phase 5: park 5b, promote 5a — session 2026-05-20 pivot
- `833bca4` dashboards: audit pass — Y axis off everywhere, chart-grid widths uniform
- `d26e8cc` phase 5b: Round 0 findings — staged change falsified, candidate list reprioritised
- `923280d` docs: refresh next_session_pickup state snapshot
- `542dbed` docs: copy Phase 5b characterisation + kickoff cross-ref to main
- `e77f6a6` docs: harden CLAUDE.md + add canonical next-session handover
- `9ebda55` docs: tidy — CI bullet list + .gitignore .claude/tmp/
- `d4d5b36` docs: session_2026-05-20_phase5b_staged — Phase 5b round-1 ready for Batch
- `188a0d9` docs: 2026-05-20 PM — Phase 3 part 2 attempted + reverted, parked behind Phase 5
- `de06d41` Revert Phase 3 part 2 (v1 + v2 SQL approximations both flawed)

What this session contributed (the Phase 5b → 5a pivot):

- Bench confirmed locked on both fixtures at session start.
- Round 0 baseline diagnostics pulled from CloudWatch (eu-north-1 stream `ml-pipeline/default/1f5ceffac5de4e69806bdf61c2fdf4e0`, 2026-05-07 `880dff02` run) — captured tier breakdown (`tier1_hough=36.1%`, `tier2_cc=0.4%`, `tier4_delta=63.5%`, `tier2_cc_rejected=0`, `tier3_argmax=0`). Per-frame returns ~100%; persisted DB rows ~13%; 7.7× collapse is downstream of Tier 4.
- Local Tier-4-only sweep on `a798eff0` at threshold=25 vs threshold=15 — falsified the staged change (post-`_filter_outliers` survival 3205 → 2833, **−11.6%**; mean cluster length 118.7 → 69.1).
- Surrogate Option α experiment (source-aware filter) inconclusive — construction artifacts + finding that `ball_rows` aren't strongly concentrated in rally windows even pre-filter (Tier 1 fires across the whole match, so "anchor on recent Tier-1" doesn't get the concentration boost the design assumed).
- Full BallTracker local validation aborted: 40-min CPU estimate was off by ~30× (actual ~21 hrs without local GPU).
- `docs/north_star.md` Phase 5 detail updated: 5a is **ACTIVE 2026-05-20**, 5b is **PARKED 2026-05-20** with receipts inline.
- `.claude/phase5b_ball_tracker_characterisation.md` updated: "Tuning rounds" Round 0 + Round 1 (cancelled) populated; candidate list reprioritised (motion threshold + CC upper bound crossed off; source-aware filter + track-confirmation added as new top candidates if 5b ever resumes).
- `.claude/phase5a_kickoff.md` **NEW** — full Phase 5a scoping (this session's main deliverable for the next agent).
- `.claude/phase5_kickoff.md` updated: redirect from 5b to 5a.
- This file rewritten for the 5a-first plan.

**Branch `phase-5b/motion-threshold-reduce`:** retained on origin as a falsified-hypothesis record. **Do not merge. Do not reuse the branch name.**

**Working tree at session end:** clean except `ml_pipeline/training/visual_debug/` (untracked, deliberately ignored) and potentially `frontend/match_analysis.html` if Tomo's parallel dashboard session left uncommitted work.

**Reusable scratch artefacts** (under `.claude/tmp/`, gitignored, kept locally):

- `fetch_balltracker_diag.py` — pulls `BallTracker.log_diagnostics()` from the most-recent Batch CloudWatch stream matching a task_id prefix. Reusable for any future ball-detector investigation. Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python .claude/tmp/fetch_balltracker_diag.py`.
- `phase5b_tier4_local_experiment.py` — pure Tier-4 sweep on a local video at two thresholds, computes `_filter_outliers` survival. Reusable for any Tier-4-only candidate (kernel size, post-blur threshold, Hough radius). ~3-4 min on CPU.
- `phase5b_option_alpha_experiment.py` + `phase5b_option_alpha_real.py` — Option α validation harnesses. The `_real` variant runs against per-frame source-labeled data (requires GPU or overnight CPU run to produce `.claude/tmp/phase5b_real_detections.pkl`); the non-`_real` version is the surrogate that ran today.
- `phase5b_round0_baseline.md` + `phase5b_round0_findings.md` — write-ups of today's Round 0 receipts in prose.

The next chat picks up Phase 5a from a fully-scoped kickoff doc, with bench locked and Round 0 receipts committed + pushed. A clean Phase 5a stage-1 cycle (local sanity, no Batch) is achievable in one session; stage 2 (Batch validation) is a hand-off to a follow-up session.
