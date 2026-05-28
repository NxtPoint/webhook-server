# Next-session pickup — 2026-05-28 (close 5) — ADR-02 v1 model SCAFFOLD shipped, awaits corpus volume

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (close 5)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. Identity `100%` unchanged. New `bench_swing_type` ships local-only in STOPGAP mode (returns `available=False` until weights ship).

**What shipped this session — ADR-02 v1 model scaffold (Option G from close-4 pickup):**

Mirrors bounce_detector v0 pattern — STOPGAP-flagged scaffold ready for the moment training data crosses ~2-3k labels.

- `ml_pipeline/stroke_classifier/model_v2.py` — R(2+1)D-18 + 2-channel optical-flow stem + handedness concat (`SwingTypeR2plus1D`); `SwingTypeClassifierV2` wrapper with STOPGAP-no-weights behaviour
- `ml_pipeline/stroke_classifier/dataset.py` — PyTorch `SwingTypeDataset` reading `build_swing_type_dataset` outputs; train/val/all split + ADR-02 augmentation (hflip + dx-sign-flip + handedness-toggle + temporal crop)
- `ml_pipeline/stroke_classifier/detector_v2.py` — Render-side `detect_swing_types_for_task` entry point per ADR-02 Q4-A; stroke_events → bboxes → 1080p video crop → optical flow → batched predict → write `ml_analysis.swing_type_events`. Exception-safe (never raises, returns dict)
- `ml_pipeline/stroke_classifier/db.py` — `ml_analysis.swing_type_events` schema (separate table per ADR-02 Q6 — preserves rule #8 ownership of `stroke_detector/`)
- `ml_pipeline/training/train_swing_type.py` — Training loop: AdamW + cosine warmup + label-smoothing/focal loss + mixup + WeightedRandomSampler + early-stop on macro-F1
- `ml_pipeline/diag/bench_swing_type.py` — Regression bench with `--bless` baseline lock; local-only (CI gate per ADR-02 v1 only after weights ship)
- `upload_app.py` — boot init for `init_swing_type_schema()` + try/except `detect_swing_types_for_task()` call in `_do_ingest_t5` after stroke_detector. Cannot break ingest flow.
- `ml_pipeline/stroke_classifier/__init__.py` — exposes v2 API alongside legacy v1
- `.claude/swing_classifier_v2_kickoff.md` — handover doc

**Verified live:** all imports clean; `SwingTypeClassifierV2()` → `available=False` (STOPGAP); `predict_batch()` → `[]`; bench runs `available=False` and exits 0; `ml_analysis.swing_type_events` table created on prod DB. Serve bench unchanged.

**Match 4 SAFETY confirmed:** the new `detect_swing_types_for_task` runs as no-op when weights are absent. Match 4's eventual Render ingest hook will log `swing-type classifier: {'status': 'stopgap'}` and proceed cleanly to silver build. No risk to the running job.

**Two batch-optimisation commits piggyback on this push** (other agent's L1 + L4 work, env-gated, source-only — no production effect until Docker rebuild + ECR push):
- `024bb40` perf(t5/batch): L1 player-stage GPU batching (env-gated PLAYER_BATCH_SIZE)
- `b25b356` perf(t5/batch): L4 ROI ViTPose batching + FP16 (env-gated ROI_BATCH_SIZE / ROI_POSE_FP16)

## Corpus state (unchanged from close-4)

| Pair | T5 | SA | ball_position | stroke_classifier | serve |
|---|---|---|---|---|---|
| Match 1 (Tomo / Rivonia) | 78c32f53 | 0d0514df | 161 ✅ | 94 ✅ | 25 ✅ |
| Match 2 (Erin / ccj)     | c645a7ee | ee12d918 | 327 ✅ | 341 ✅ | 46 ✅ |
| Match 3 (Dejan / ccj)    | 9378f2dd | 2f355924 | 331 ✅ | 340 ✅ | 47 ✅ |
| Corpus 4 (Tomo / Rivonia) | ca475740 *(T5 78%, roi_extract — last checked 15:58 UTC)* | 3922af92 ✅ | pending hook | pending hook | pending hook |
| **Totals (today)** |  |  | 3 rows / 819 | 3 rows / 775 | 3 rows / 118 |

ADR-02 v1 dataset (built last session): 368 training-ready hits / 16-frame Farneback flow / 3 matches. Below 2-3k volume target. Will rebuild + grow once Corpus 4 lands.

## Honest status of the 5 facts (post-this-session)

| Fact | Status | Honest read |
|---|---|---|
| serve | **DEV CEILING** ✅; corpus extractor LIVE (Stream 3) | 118 labels + ~114 from Corpus 4 ≈ 232. Need ~500+ for receiver-FP training. |
| bounce (ADR-01) | **v0 SCAFFOLDED — UNTRAINED** | STOPGAP threshold 1.1. 411 floor labels reachable. |
| swing_type (ADR-02) | **v1 MODEL SCAFFOLD + DATASET + TRAINING + BENCH SHIPPED** | All plumbing in place; STOPGAP-no-weights. Need ~2-3k labels (~5-10 more matches) to train. |
| identity (ADR-03) | **v1 SHIPPED at 100% bench** ✅ | v2 OSNet planned. |
| volley (ADR-04) | **Not built — by design** | Blocked on bounce + swing-type. |

**Three of the 5 facts are now at "scaffolded + ready for training" or "shipped":** identity (DONE), swing-type (SCAFFOLD READY), bounce (SCAFFOLD READY). The whole architecture pattern is now consistent and proven.

## Honest re-ordered roadmap

1. **Wait for Corpus 4 T5 to finish** — corpus rows auto-land via hook (all 3 kinds).
2. **(Optional, Tomo's call)** Re-submit 2 unpaired Tomo-Rivonia matches → +411 floor + ~500 swing + ~50 serve.
3. **Train ADR-01 bounce_detector v1** on accumulated floor labels — HIGHEST IMPACT next move (recipe: `.claude/adr01_label_audit_2026-05-28.md`).
4. **Accumulate swing-type corpus to ~2-3k labels** (5-10 more matches), then train R(2+1)D-18 via `train_swing_type.py`.
5. **Accumulate serve corpus to ~500+ labels**, then retrain serve_detector for receiver-FP.
6. **ADR-04 volley analytic drops out** — ~30 lines.
7. *(Maybe)* ADR-03 v2 OSNet.

## Next session's job — pick ONE (parallel-safe ★)

- **★(A) ADR-01 v1 training** — IFF Corpus 4 has landed AND ideally 2 unpaired Rivonia matches re-submitted. **HIGHEST IMPACT** (unblocks bounce from STOPGAP zero). ~2-3 hr. Recipe in `.claude/adr01_label_audit_2026-05-28.md`.
- **★(F) Batch runtime optimisation — finish the deploy** — L1 + L4 are committed but the BATCH-SIDE CHECKLIST hasn't run (Docker rebuild + ECR push + job-def revisions). Daylight-only. ~5 hr. Plan: `docs/_investigation/batch_optimisation_plan.md`. The two committed levers stack to ~25-55% speedup once activated via env vars.
- **★(C) ADR-03 v2 OSNet** — not urgent.
- (D) ADR-04 volley — still blocked on (A).
- (E) Hand-label net-cord / racket-hit FPs for ADR-01 bounce_type enum — deferred until v1 bench tells us pre-gates aren't enough.

**Recommended:** (A) if Corpus 4 has landed by then; (F) if Tomo wants the user-pain ceiling fixed in daylight.

## Coordination protocol (per ADR-05) — unchanged

1. No agent starts a detector build without an APPROVED ADR.
2. Each detector build has its own commit scope.
3. **Corpus extension lands in the same commit as the detector model it feeds.**
4. Each detector ships with a bench.
5. This pickup file gets updated at every detector ship.

## Commits this session
- *(this commit)* `feat(t5): ADR-02 v1 swing-type model scaffold (model + dataset + detector + train + bench + boot wiring)` — 8 new/modified files + kickoff doc + pickup overwrite.

(Pushed alongside: `024bb40` L1 player batching, `b25b356` L4 ROI batching + FP16 — both from the parallel batch-optimisation agent, env-gated.)

## Memory ceiling reference (unchanged)

| Stage | Peak heap |
|---|---|
| Bronze ingest (streaming ijson) | ~15 MB |
| serve_detector | ~75 MB |
| stroke_detector | ~53 MB |
| silver build | ~79 MB |

## Runtime ceiling reference (unchanged from close-3)

| Phase | Wall time (44-min match) | Status |
|---|---|---|
| AWS Batch | ~4.79 h | L1 + L4 committed, awaiting deploy |
| Render ingest | ~3 min 39 sec | Solved |
| Target | <1 h | Plan: `docs/_investigation/batch_optimisation_plan.md` |

## Read in this order
0. **`docs/north_star.md` §"★ RULES OF THE GAME"** — non-negotiable.
1. This file.
2. `docs/north_star.md` §"Current detector build queue (2026-05-28)".
3. `.claude/adr01_label_audit_2026-05-28.md` — for Task (A) ADR-01 work.
4. `.claude/swing_classifier_v2_kickoff.md` — for context on the scaffold shipped this session.
5. `docs/_investigation/batch_optimisation_plan.md` — for Task (F) Batch runtime work.

## Constraints (unchanged)

- Don't touch parallel-agent files: `serve_detector/`, `stroke_detector/`, `build_silver_match_t5.py`, `ball_tracker.py`, `wasb_*.py`, `roi_extractors/`, or any file in BATCH-SIDE CHECKLIST per rule #8 (unless doing Task F).
- No pytest. No `?key=` query-string auth. Pull-rebase before push.
- Always commit to main (no feature branches).

## Scratch
None. All output went into committed code + docs.
