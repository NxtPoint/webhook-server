# Next-session pickup — 2026-05-28 — 5 ADRs APPROVED, build queue is live

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)" — the queue lists every next move, status, and dependency.

**Date:** 2026-05-28
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (unchanged).

**What shipped this session (architecture, no model code yet):**
1. **CLAUDE.md streamlined** 431→360 lines, ~43k→33k chars (`f2c4258`). All 11 "Things not to do" + load-bearing detail preserved; trimmed duplicate doc pointers + file-by-file lists Claude can grep.
2. **5 ADRs landed and APPROVED** in `docs/_investigation/adr_0[1-5]_*.md` — together they answer "what detector models to build, how to build each one, in what order, with what coordination protocol." Both architectural decisions AND research-grounded build specs (literature-cited algorithms / features / thresholds) are in each ADR.
3. **`docs/north_star.md` updated** with a "Current detector build queue" section pointing at ADR-05 as the single source of truth for the build sequence.

**Architecture in one paragraph (Tomo's mental model, verified this session):**
- 5 analysis models build the 18 base bronze facts. Serve done (`serve_detector`). 4 remaining: bounce (ADR-01), swing-type (ADR-02), identity (ADR-03), volley (ADR-04 — actually an analytic, not a model, but bronze-tier).
- Build-first / train-LAST: bring each to ~70-80% with standard models, then train to 90-95% via dual-submit corpus.
- SA bronze = teacher labels for the 18 (like-for-like).
- Silver passes 3-5 are derivative and identical for both flows.

**OPEN — TWO PARALLEL AGENTS in flight (do NOT touch their files):**
- **Parallel agent A** is shipping a memory-fix patch in `serve_detector/detector.py`, `serve_detector/pose_signal.py`, `stroke_detector/detector.py`, `stroke_detector/velocity_signal.py`, `build_silver_match_t5.py` (streamed keypoint loading + numpy float32 compaction; fits Render 512MB). Bench passes with the patch applied. Do not touch these files.
- **Parallel agent B** is landing corpus video #3 + optimising the dual-submit flow.

**Next session's job — pick ONE (parallel-safe choices marked ★):**
- **★(A) ADR-01 bounce model build** — Stream 1, biggest leverage, corpus has 488 ball_position labels already (do a label-accuracy audit first per the ADR spec). New module `ml_pipeline/bounce_detector/`. Independent of all parallel-agent files.
- **★(B) ADR-03 identity rule v1** — Stream 2, no training needed, ~1-2 day build. New module `ml_pipeline/identity_detector/`. Independent of all parallel-agent files.
- **(C) Serve corpus extractor + retrain** — Stream 3, parallel-safe. New `ml_pipeline/training/label_serves.py` paralleling `label_ball_positions.py` + 1-2 lines in `upload_app._label_pair_now()`. Doesn't block anything.
- (D) ADR-02 swing-type classifier — blocked on corpus extractor for `label_kind='stroke_classifier'` (do extractor first, then accumulate data, then train).
- (E) ADR-04 volley analytic — blocked on ADR-01 + ADR-02.

Recommended start: **A + B in parallel** (two agents) or **A first then B** (one agent).

## Architecture invariant (Tomo's design — APPROVED 2026-05-28)
- 18 base columns: inherited verbatim from each flow's bronze. ✅
- ~20 derived columns: shared code in build_silver_v2 passes 3-5 for both flows. ✅
- ONE asymmetry exists (serve): T5 inherits `serve_events` (overlay shipped 2026-05-27, `T5_SERVE_FROM_EVENTS`), SA stays pure-geometric. Fully-symmetric end state = ADR-04-style cleanup of `serve_d` (deferred).

## Read in this order
1. This file.
2. `docs/north_star.md` — RULES → "Current detector build queue (2026-05-28)" → 18-field build status table.
3. **`docs/_investigation/adr_05_detector_build_sequencing.md`** — the roadmap.
4. Whichever ADR (01-04) you're about to build.
5. `.claude/handover_t5.md` — ops / how-to-run / deploy.
6. `docs/_investigation/bronze_silver_18_audit.md` — the per-field audit that motivates the 5 ADRs.

## Coordination protocol (per ADR-05) — non-negotiable
1. No agent starts a detector build without an APPROVED ADR (all 5 are approved).
2. Each detector build has its own branch / commit scope. No agent touches another's module in the same session unless coordinating via this file.
3. **Corpus extension lands in the same commit as the detector model it feeds** — never ship a model without an extractor for its training data.
4. Each detector ships with a bench (`bench_bounce`, `bench_swing_type`, `bench_identity`).
5. This pickup file gets updated at every detector ship.

## Commits this session
`f2c4258` CLAUDE.md streamline; (ADR-01 to ADR-05 + north_star nav update + this pickup = single commit landing now).

## Scratch (gitignored, .claude/tmp/)
None this session — all output was architectural docs in `docs/_investigation/`.
