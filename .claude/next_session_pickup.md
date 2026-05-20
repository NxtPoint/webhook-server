# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session was 2026-05-20.

Read in this exact order before doing anything else:

1. `docs/north_star.md` — macro plan. Phase 3 part 2 is now "blocked by Phase 5"
   with empirical evidence. Phase 5b is the active sub-task.

2. `.claude/session_2026-05-20_phase5b_staged.md` — what's already staged.
   Branch `phase-5b/motion-threshold-reduce` holds a conservative single-
   parameter change (motion threshold 25 → 15 in `_detect_ball_frame_delta`).
   NOT yet in the Batch image.

3. `.claude/phase5b_ball_tracker_characterisation.md` (lives on the branch
   above — `git checkout` the branch to read it). Required reading. Four-tier
   detector map, every Hough/threshold parameter with current value +
   tuning headroom + risk, eight prioritised candidate changes with
   predicted impact.

4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST section is
   mandatory. `ball_tracker.py` edits require Docker rebuild + dual-region
   ECR push + new job-def revisions before Batch sees the change.

Then run the bench locally to confirm the floor is still locked:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions. If anything
moved, stop and investigate before touching code.

If bench green, the first move is:

**A.** Pull baseline diagnostics from the latest `880dff02` Batch CloudWatch
log. `BallTracker.log_diagnostics()` runs at the end of every job via
`pipeline.py:292`. Record the per-tier % (`tier1_hough` / `tier2_cc` /
`tier2_cc_rejected` / `tier3_argmax` / `none_returned` / `delta_fallback_hits`)
in the characterisation doc's "Tuning rounds → Round 0" row.

**B.** Check out the staged branch + run the BATCH-SIDE CHANGE CHECKLIST:

    git fetch origin
    git checkout phase-5b/motion-threshold-reduce
    git diff main -- ml_pipeline/ball_tracker.py

Then Docker rebuild + dual-region ECR push + new job-def revisions.

**C.** Ask me to rerun `880dff02` (or upload again). Then pull post-rerun
diagnostics + `ml_analysis.ball_detections` row count vs baseline ≈1983 rows
(13% of 15300 frames). Target: ≥25% coverage as round-1 success criterion.
If hit, merge the branch + plan round 2 (next candidate per characterisation
doc priority list). If not, revisit.

**Things NOT to do** (load-bearing — restating from `CLAUDE.md` and the docs):

- Don't tune Tier 1 Hough (`TRACKNET_HOUGH_*` in `config.py`). `param2=2`
  is already maxed permissive; the problem isn't there.
- Don't lower `TRACKNET_HEATMAP_THRESHOLD=127` — prior attempt at 100
  broke detection (comment in `config.py`).
- Don't ship multi-parameter changes in one Batch round — couples cause
  and effect.
- Don't re-attempt Phase 3 part 2 with any pure-SQL pattern. Both v1 and
  v2 flawed for documented structural reasons.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't push `serve_detector` changes without bench green.
- Don't merge a Batch-touching branch without the BATCH-SIDE checklist.

---

## State at session end (2026-05-20)

**Local `main` at `9ebda55` — one commit ahead of `origin/main` (not yet pushed).**

Recent commits on main (newest first):

- `9ebda55` docs: tidy — CI bullet list + .gitignore .claude/tmp/  *(local only)*
- `d4d5b36` docs: session_2026-05-20_phase5b_staged — Phase 5b round-1 ready for Batch
- `7818576` dashboards: polish pass 2 — week 1 reconciliation + designer critique
- `4ae2b81` docs: correct main-API liveness endpoint (/__alive -> /healthz)
- `188a0d9` docs: 2026-05-20 PM — Phase 3 part 2 attempted + reverted, parked behind Phase 5
- `de06d41` Revert Phase 3 part 2 (v1 + v2 SQL approximations both flawed)

What landed today on main:

- Phase 3 part 2 fully reverted with empirical receipts in docs
- `docs/north_star.md` updated to reflect "Phase 3 part 2 blocked by Phase 5"
- Three session docs in `.claude/`: morning review, `phase3pt2_revert`, `phase5b_staged`
- `CLAUDE.md` doc bug (`/__alive` → `/healthz`) fixed
- `CLAUDE.md` CI section reformatted as a bullet list + T5 silver builders named explicitly
- `.gitignore` ignores `.claude/tmp/`
- Two stale phase-3 branches deleted from origin

**Branch `phase-5b/motion-threshold-reduce` (`dace7ad`) — READY FOR BATCH:**

- `ml_pipeline/ball_tracker.py` motion threshold 25 → 15 (single safest gain candidate)
- `.claude/phase5b_ball_tracker_characterisation.md` (NEW) — eight prioritised
  tuning candidates, measurement workflow
- `.claude/phase5_kickoff.md` cross-ref update

**Working tree:** clean except `ml_pipeline/training/visual_debug/` (untracked,
deliberately ignored — leftover local debug images).

**Bench locked at `a798eff0` 20/24 + `880dff02` 23/24 throughout. Zero
detector-quality regression today.**

The next chat has everything it needs to (a) bench-check, (b) pull baseline
diagnostics, (c) run the Batch dance, (d) measure the delta. That's a clean
Phase 5b round-1 cycle in one session.

Good luck with the DB upgrade. If the v18 upgrade resolves the connection
issue, great — if it doesn't, the IP allowlist diagnosis from earlier is
still the next thing to check.
