# Session 2026-05-20 (later) — Phase 5b characterised + round-1 staged

**Created:** 2026-05-20 evening by Claude session (continuation after `session_2026-05-20_phase3pt2_revert.md`).
**Outcome:** Phase 3 part 2 work is closed (see prior session doc). Phase 5b characterisation pass complete. A single conservative tuning change is staged on a branch but NOT yet pushed to Batch — DB was locked for v18 upgrade and the user wanted Phase 5b prepped for the next chat. Next chat reads the characterisation doc, runs the Batch dance, and measures the delta.

---

## TL;DR

- Phase 3 part 2 fully reverted (commit `de06d41`) — both pure-SQL approximations failed, both empirically. Documented in `session_2026-05-20_phase3pt2_revert.md`.
- **Phase 5b characterisation is complete and documented in `.claude/phase5b_ball_tracker_characterisation.md`.** Four-tier detector map, every Hough/threshold param with current value + tuning headroom + risk, eight prioritised candidate changes.
- **Branch `phase-5b/motion-threshold-reduce` is staged** with the single safest candidate (motion threshold 25 → 15 in `_detect_ball_frame_delta`, the actual "Hough fallback" per the original handover). Bench green locally. NOT yet built into the Batch image — next chat runs the Batch-side dance.
- Diagnostics are already enabled in prod (`BallTracker.log_diagnostics()` runs at end of every Batch job per `pipeline.py:292`) — next chat pulls the latest 880dff02 CloudWatch log to read the baseline tier breakdown.
- DB was being upgraded to v18 by user (1hr) during this session — no DB queries possible; the staged change is intentionally code-only.

---

## Commits on main this session (in order)

```
7818576 dashboards: polish pass 2 — week 1 reconciliation + designer critique  (parallel session)
4ae2b81 docs: correct main-API liveness endpoint (/__alive -> /healthz)
188a0d9 docs: 2026-05-20 PM — Phase 3 part 2 attempted + reverted, parked behind Phase 5
de06d41 Revert Phase 3 part 2 (v1 + v2 SQL approximations both flawed)
f0b104e build_silver_v2: Phase 3 part 2 v2 — anchor on point_number, not raw serve_d
34b0bdf docs: surface .claude/session_* + phase5_kickoff as live T5 entry points
00b8639 build_silver_v2: between-point filter for T5 silver (Phase 3 part 2)
e1ed1ff docs: 2026-05-20 T5 status review + .claude/ tracking correction  ← morning session start
```

## Active feature branch on origin (READY FOR BATCH DEPLOY)

```
phase-5b/motion-threshold-reduce  dace7ad
  ml_pipeline/ball_tracker.py            (motion threshold 25 → 15)
  .claude/phase5b_ball_tracker_characterisation.md  (NEW — required reading)
  .claude/phase5_kickoff.md              (cross-ref update)
```

NOT to be merged to main until the Batch-side dance runs and we have measurement evidence that it improves coverage on 880dff02 without breaking anything downstream. Per `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST": ball_tracker.py edits require Docker rebuild + dual-region ECR push (eu-north-1 + us-east-1) + new job-def revisions before Batch picks up the change.

## Uncommitted working-tree state (not blockers)

`CLAUDE.md` and `.gitignore` carry minor mods (CI section reformat into a bulleted list, and `.claude/tmp/` added to gitignore). Both are sensible standalone tidy-ups, applied externally (linter or concurrent session). Left uncommitted intentionally — user can commit them on main as a separate small "docs: tidy" commit at their convenience. They have no functional impact.

---

## Why round 1 is "motion threshold 25 → 15"

`_detect_ball_frame_delta` at `ball_tracker.py:498` was using `cv2.threshold(delta, 25, 255, BINARY)` as the very first gate of the frame-delta Hough fallback. Tennis balls on bright hard courts can produce inter-frame intensity diffs of only 15-25 — especially during the bright-on-bright case (ball lit, court lit). The threshold of 25 was excluding this class of motion before Hough's shape filter (radius 2-15, param2=5) could discriminate it.

Lowering to 15 admits the missing motion at the cost of admitting more lighting flicker. The Hough shape filter still rejects non-ball-shaped responses, so the cost is mostly in compute (more Hough candidates evaluated per frame) rather than false-positive ball detections.

**Predicted impact:** +5-15% coverage from frames where TrackNet failed AND the ball motion was dim. Predicted is a wide range because we don't yet have the diagnostic breakdown from the baseline run — that's the first action of next session.

**How to validate:** Pull `BallTracker.log_diagnostics()` output from the CloudWatch log of the next 880dff02 Batch job. Compare `delta_fallback_hits` count vs baseline. Compare `ml_analysis.ball_detections` row count on 880dff02 vs baseline ≈1983 rows (13% of 15300 frames).

**If round 1 helps:** merge `phase-5b/motion-threshold-reduce` to main, lock as the new baseline, proceed to round 2 from the candidate list in the characterisation doc.

**If round 1 doesn't help or regresses:** revert by closing the branch without merging (the Batch image still has the old threshold until you do the Docker push). Look at the diag output to figure out which tier is actually limiting coverage.

---

## What's NOT in this session

- **No DB queries run.** User reported DB locked for v18 upgrade for ~1hr. The characterisation is entirely code-reading + design.
- **No Batch deploy.** Branch is staged; the Docker rebuild + ECR push + job-def revisions sequence is for the next session.
- **No diagnostics fetched yet.** The baseline tier breakdown (% of frames in each of the four detector tiers) lives in CloudWatch from previous runs but wasn't pulled this session.

---

## Next session reads in this exact order

1. **`docs/north_star.md`** — macro plan. Phase 3 part 2 status is now "blocked by Phase 5"; Phase 5 is the active phase; sub-task 5b is the recommended start.
2. **`.claude/session_2026-05-20_phase5b_staged.md`** (THIS FILE) — what's already staged + branch name + the round-1 hypothesis.
3. **`.claude/phase5b_ball_tracker_characterisation.md`** — required reading. Four-tier detector map, every parameter, eight candidate changes, measurement-first workflow.
4. **`.claude/handover_t5.md`** — operational doc. **The "BATCH-SIDE CHANGE CHECKLIST" is mandatory** before any push to main for this work — ball_tracker.py is in the Batch image, so Docker rebuild + dual-region ECR push + new job-def revisions are required.
5. Optionally `session_2026-05-20_phase3pt2_revert.md` for context on why Phase 3 part 2 is parked.

---

## Then: the actual first move of next session

1. **Bench locally** — confirm a798eff0 20/24 + 880dff02 23/24 still locked: `.venv/Scripts/python -m ml_pipeline.diag.bench`
2. **Pull baseline diagnostics** from the latest 880dff02 Batch job's CloudWatch log. Record per-tier % in `phase5b_ball_tracker_characterisation.md` §"Tuning rounds" → "Round 0 — baseline."
3. **Check out the staged branch** and verify the diff:
   ```bash
   git fetch origin
   git checkout phase-5b/motion-threshold-reduce
   git diff main -- ml_pipeline/ball_tracker.py
   ```
4. **Run the BATCH-SIDE CHANGE CHECKLIST** from `.claude/handover_t5.md`:
   - ECR auth in both regions
   - `docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .`
   - Tag + push to both ECRs in parallel
   - Extract amd64 sub-manifest digest
   - Register new job-def revisions in eu-north-1 + us-east-1
5. **Ask Tomo to upload via Media Room** or trigger a rerun on 880dff02 task_id.
6. **Pull post-rerun diagnostics** + ball_detections row count. Compare to baseline.
7. **Record round 1 result** in the characterisation doc's "Tuning rounds" table.
8. **If round 1 improved coverage by ≥2× (target ≥25%):** merge branch to main, plan round 2 (the next candidate from the priority list, currently #2 = Tier 2 CC upper bound 200 → 300). If round 1 stayed flat or regressed: keep branch unmerged, re-read diagnostics, pick a different candidate first.

---

## Things to skip / don't redo

- **Don't re-attempt Phase 3 part 2.** Two SQL patterns failed; Pattern B inherits the same start-of-window limit. Blocked by Phase 5 ball coverage. See `session_2026-05-20_phase3pt2_revert.md`.
- **Don't tune Tier 1 Hough on the TrackNet heatmap** (`TRACKNET_HOUGH_*` in config.py) — already at maximum permissiveness (`param2=2`, radius 1-10).
- **Don't lower `TRACKNET_HEATMAP_THRESHOLD=127`.** Comment in config.py explicitly warns prior attempt at 100 broke ball detection.
- **Don't ship multi-parameter changes** in a single Batch round — couples cause and effect, breaks the measurement workflow.
- **Don't touch `ml_pipeline/training/visual_debug/`** — Tomo's instruction.
- **Don't widen `_baseline_zone` slack** or **relax `idle_threshold_s`** in `serve_detector/` — separately documented in `session_2026-05-20_review.md` (morning session) as previously-verified-failures.
- **Don't run pytest.** No test suite. The bench is the only regression gate.
