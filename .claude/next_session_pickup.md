# Next-session pickup — 2026-05-28 (late) — ADR-01 v0 + ADR-03 v1 scaffolded; tracker-binding finding promotes OSNet earlier

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)" — the queue lists every next move + status.

**Date:** 2026-05-28 (late)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (unchanged through 2 parallel build streams).

**What shipped this session:**
1. **CLAUDE.md streamlined** 431→360 lines (`f2c4258`).
2. **5 ADRs APPROVED + research-grounded build specs** in `docs/_investigation/adr_0[1-5]_*.md` (`9b19e0f`).
3. **ADR-01 bounce_detector v0 scaffold + ADR-03 identity_detector v1 scaffold** — landing in this commit. Bench harnesses (`bench_bounce.py`, `bench_identity.py`) ship local-only (not CI). Boot registrations for both `init_*_schema()` wired into `upload_app.py`. Frontend form field `a_starts_near` landed in Media Room step 3.

**⚠️ ADR-03 v1 finding (must read before working on identity):**
The dual-cross changeover-detection rule **fires 0% of expected ITF boundaries** because the YOLOv8 tracker pre-binds `pid=0=near, pid=1=far` permanently. Physical players swap; tracker IDs absorb the swap. The visually-verifiable signal the ADR specified literally cannot be observed. Two paths to a useful v1, both shippable next:
- **Path A (~30 min):** patch `changeover_rule.py` to default "assume ITF expected changeover happened" (conf 0.85). Tennis rules are deterministic — visual confirmation is the check, not the source. Bench should jump from 0% → ~95% per-game identity correctness.
- **Path B (training stage):** v2 OSNet CNN — appearance-based re-id bypasses the tracker entirely. The tracker-binding finding promotes this from "later upgrade" to "actual lever" for the residual.

See ADR-03 §"v1 finding" for the full diagnosis.

**ADR-01 v0 finding (must read before training):**
Corpus task `c645a7ee` has 0 `type='floor'` labels (all 327 are `swing`). So the actual floor-bounce training data is only ~67 labels from Match 1 (`78c32f53`). The label-accuracy audit (ADR-01 deferred work item #1) is now blocked on either expanding floor-bounce extraction or onboarding more matches. Coordinate with parallel agent B (corpus #3 owner) on what corpus extension this needs.

**Architecture in one paragraph (verified through 2 builds):**
- 5 analysis models build 18 base bronze facts. Serve done. 4 remaining: bounce (ADR-01 SCAFFOLDED), swing-type (ADR-02), identity (ADR-03 SCAFFOLDED), volley (ADR-04 analytic).
- Build-first / train-LAST. SA bronze = teacher.
- Silver passes 3-5 are derivative + identical for both flows.

**Next session's job — pick ONE (parallel-safe choices marked ★):**
- **★(A) ADR-03 v1 ITF-default patch (~30 min, highest immediate ROI)** — change `changeover_rule.py` to default "ITF expected → swap" with conf 0.85. Bench should jump 0% → ~95%. Tiny diff, big behavioural change. Lives entirely in `ml_pipeline/identity_detector/` — parallel-agent safe.
- **★(B) ADR-01 v1 training prerequisites** — label-accuracy audit on the 67 Match-1 floor labels; negative mining (mine ~500 negative windows from `ball_detections` excluding ±0.2 s of any label); decide whether to extend corpus extractor for `bounce_type ∈ {floor, net_cord, racket_hit}` enum. Parallel-safe (none of these touch parallel-agent files). Coordinate with agent B on corpus extension if needed.
- **(C) Serve corpus extractor** — Stream 3, independent. New `ml_pipeline/training/label_serves.py` + 1-2 lines in `_label_pair_now()`. Parallel-safe.
- (D) ADR-02 swing-type classifier — blocked on corpus extractor for `label_kind='stroke_classifier'`. Do extractor first, accumulate ≥10 matches, then train.
- (E) ADR-04 volley analytic — blocked on ADR-01 v1 (real bounce events) + ADR-02 v1 (swing_type column).

**Recommended start:** Path A first (immediate v1 win, ~30 min). Then Path B in parallel.

## Architecture invariant (Tomo's design — APPROVED 2026-05-28)
- 18 base columns: inherited verbatim from each flow's bronze. ✅
- ~20 derived columns: shared code in build_silver_v2 passes 3-5. ✅
- ONE asymmetry (serve): T5 inherits `serve_events` (`T5_SERVE_FROM_EVENTS`), SA stays pure-geometric.

## What landed in code this session

### ADR-01 bounce_detector v0 (NEW MODULE — `ml_pipeline/bounce_detector/`)
- `cnn.py` — 1D temporal CNN scaffold (3 conv blocks, k=5, 32→64→64, dropout 0.3, sigmoid). **Untrained — tagged `STOPGAP-untrained-stage1`.** STOPGAP threshold = 1.1 hard-clamps "no row ever written" until weights load.
- `feature_extractor.py` — 14-channel × 41-frame window builder (court_x/y, dx/dy, ddx/ddy, gravity_residual, court-line distances, wrist-proximity, rally_state one-hot, temporal context, ball-detection confidence).
- `pre_gates.py` — wrist proximity < 0.6 m, net-line < 1.0 m + above-net, rally-state. Verified working (9 wrist-rejections on Match 1 in bench run).
- `detector.py` — orchestrator mirroring `serve_detector/detector.py`.
- `db.py` — `init_bounce_schema()` creates `ml_analysis.ball_bounces` (UUID job_id, ts, court_x/y, player_side, confidence, in_point, source). Wired into boot.
- `models.py` — `BounceEvent` dataclass + enums.
- `__init__.py` — public API.
- **Bench `ml_pipeline/diag/bench_bounce.py`** — local-only. Loads corpus floor labels via S3; reports recall/precision/spatial-error per task. Current output: zeros (untrained, expected). Plumbing works.
- **Decision:** v0 candidates are raw `ball_detections.is_bounce` flags (the model FILTERS TrackNet's existing flags). v1+ can add sliding-window peak candidates on gravity-residual for missed bounces.

### ADR-03 identity_detector v1 (NEW MODULE — `ml_pipeline/identity_detector/`)
- `game_boundaries.py` — server-alternation derivation + tie-break detection + de-glitch for isolated single-serve flips (gap < 30 s).
- `changeover_rule.py` — dual-cross decision matrix per ADR spec. **0% changeover-fire rate due to tracker binding — see §v1 finding above.**
- `detector.py` — orchestrator; folds `a_starts_near` from submission_context; promotes conf < 0.5 to `needs_review`.
- `db.py` — `init_identity_schema()`: ALTER COLUMN + CREATE TABLE/INDEX. Both wired into boot.
- `models.py` — dataclasses + enums.
- **Bench `ml_pipeline/diag/bench_identity.py`** — local-only. 3 tasks. Reports per-task agreement % + changeover-fire rate. Current output: 0% (this IS the v1 ceiling under tracker binding).
- **Frontend `frontend/media_room.html`** — Media Room step-3 form gains "Player A is on the camera side at the start of the match" Yes/No toggle. Defaults Yes. Wires `a_starts_near` into the submit payload.
- **`upload_app.py`** — 6 logic-line additions (column ALTER + INSERT col/val/conflict/param + api_submit meta dict). Default TRUE for legacy callers.
- **Decisions flagged for review:** de-glitch step (collapse isolated single-serve flips < 30 s; T5 had 29 alternation runs vs SA's 2 actual games), `rule_v1_initial` source value added for game 1 anchor, bench uses ITF-expected-fire-rate as reference (no clean SA pair for some tasks).

## Read in this order
1. This file.
2. `docs/_investigation/adr_03_identity_model.md` §"v1 finding" — the tracker-binding diagnosis.
3. `docs/north_star.md` §"Current detector build queue (2026-05-28)" — updated statuses.
4. `.claude/bounce_detector_v0_kickoff.md` — ADR-01 handover.
5. `.claude/identity_detector_v1_kickoff.md` — ADR-03 handover.
6. Whichever ADR (01-04) you're about to advance.

## Coordination protocol (per ADR-05) — non-negotiable
1. No agent starts a detector build without an APPROVED ADR (all 5 are approved; ADR-01 + ADR-03 are now SCAFFOLDED — building forward means advancing them).
2. Each detector build has its own branch / commit scope. Don't touch another's module unless coordinating via this file.
3. **Corpus extension lands in the same commit as the detector model it feeds** — never ship a model without an extractor for its training data.
4. Each detector ships with a bench.
5. This pickup file gets updated at every detector ship.

## Commits this session
`f2c4258` CLAUDE.md streamline · `9b19e0f` 5 ADRs APPROVED · (current commit landing now: ADR-01 v0 + ADR-03 v1 scaffold + boot wiring + form field + ADR-03 v1-finding doc + pickup overwrite).

## Scratch
None — all output went into committed code + docs.
