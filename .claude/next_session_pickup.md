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

**Local `main` and `origin/main` in sync at `542dbed`** (or later if more docs land overnight). Bench locked at `a798eff0` 20/24 + `880dff02` 23/24 throughout. Zero detector-quality regression today.

Most-relevant recent commits on main (newest first; full log via `git log --oneline -20`):

- `542dbed` docs: copy Phase 5b characterisation + kickoff cross-ref to main
- `5816ebd` cleanup: finish PowerBI removal residue
- `e77f6a6` docs: harden CLAUDE.md + add canonical next-session handover
- `b36ffdb` docs: rename main API references webhook-server -> Sport AI - API call
- `585b2ad` cleanup: remove PowerBI + Superset (cost reduction)
- `9ebda55` docs: tidy — CI bullet list + .gitignore .claude/tmp/
- `d4d5b36` docs: session_2026-05-20_phase5b_staged — Phase 5b round-1 ready for Batch
- `188a0d9` docs: 2026-05-20 PM — Phase 3 part 2 attempted + reverted, parked behind Phase 5
- `de06d41` Revert Phase 3 part 2 (v1 + v2 SQL approximations both flawed)

What landed today on main (functional summary):

- Phase 3 part 2 fully reverted with empirical receipts in docs (v1 no-op cause, v2 wrong-rows-dropped cause)
- `docs/north_star.md` Phase 3 status flipped to "blocked by Phase 5" with per-attempt detail
- Four session docs in `.claude/`: morning `review`, `phase3pt2_revert`, `phase5b_staged`, this `next_session_pickup`
- Phase 5b characterisation doc (`phase5b_ball_tracker_characterisation.md`) on main alongside the kickoff cross-ref
- CLAUDE.md hardened: liveness endpoint corrected (`/__alive` → `/healthz`), env vars + ops/diag/sql + blueprints sections fleshed out
- PowerBI + Superset removed (cost reduction)
- Two stale phase-3 branches deleted from origin

**Branch `phase-5b/motion-threshold-reduce` (`dace7ad`) — READY FOR BATCH:**

- `ml_pipeline/ball_tracker.py` motion threshold 25 → 15 (single safest gain candidate)
- `.claude/phase5b_ball_tracker_characterisation.md` (also on main since `542dbed` — read from either location)
- `.claude/phase5_kickoff.md` cross-ref update (same — duplicated on main + branch; clean merge expected)

**Working tree at session end:** clean except `ml_pipeline/training/visual_debug/` (untracked, deliberately ignored — leftover local debug images).

The next chat has everything it needs to (a) bench-check, (b) pull baseline
diagnostics, (c) run the Batch dance, (d) measure the delta. That's a clean
Phase 5b round-1 cycle in one session.

Good luck with the DB upgrade. If the v18 upgrade resolves the connection
issue, great — if it doesn't, the IP allowlist diagnosis from earlier is
still the next thing to check.
