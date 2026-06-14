# TRAINING-BENCH HARNESS STATUS — the 5 facts (Tier-2 REFERENCE)

> **What this is:** the single map of "does every T5 fact have an enforceable,
> committed regression gate before a *train-all-5* run?" Point-in-time snapshot
> 2026-06-14. On conflict, `.claude/next_session_pickup.md` + the live
> `session_*.md` win. Companion: `.claude/training_environment.md` (trainer
> entrypoints), `docs/north_star.md` §"RULES OF THE GAME".
>
> **Enforceable = a plain re-run (no `--update-baseline`) loads the committed
> baseline, compares the headline aggregate, and exits non-zero on a negative
> delta** — the serve `bench.py` contract. Verified by running each bench this
> session.

## The 5 facts

| Fact | Label pipeline (script + `label_kind`) | Trainer (script → output `.pt`) | Dataset builder | Bench + baseline (enforceable?) | Data-ready notes |
|---|---|---|---|---|---|
| **Serve** | `training/label_serves.py` → `label_kind='serve'` | `serve_model.train` (coord MLP) → `models/serve_model_v1.pt` (env-gated `SERVE_MODEL_ENABLED`, default 0) | `serve_model/dataset.py` | `bench.py` → `bench_baseline.json` (`ea1e500c=12/26`, `880dff02=23/24`). **YES — committed + CI-gated** (`.github/workflows/bench.yml`); the only CI gate. Verified green this session. | The live detector is the pose-first heuristic; serve **signed off**. Far 0/12 on `ea1e500c` is upstream (far court_y NULL) — coverage/training territory, not a gate-tuning target (rule #5). |
| **Hit** | **borrows** `label_kind='ball_position'` — labels read STRAIGHT from SA `bronze.player_swing` (per-swing positional side, `ball_hit_location_y > 11.885` = near). No dedicated hit labeler. | `hit_model.train` (per-candidate coord MLP) → `models/hit_model_v1.pt` (gate NOT met — local/CPU only, not in Batch image) | `hit_model/dataset.py` (+ `candidates.py`, `features.py`) | `bench_hit.py` → `bench_baseline_hit.json` (NEAR gate 67% / FAR gate 19% / prec 54%; agg near_gate 667, far_gate 245, matched_any 1488). **YES — enforcement ADDED this session** (was write-only; now compares aggregate on plain run, exits 1 on regression). Verified green. | FAR attribution (19%) is THE blocker — training-gated on the sharp-far retrain (DoD #8). Corpus pairs via `ml_analysis.training_corpus` (`label_kind='ball_position'`). |
| **Bounce** | **borrows** `label_kind='ball_position'` — `training/label_ball_positions.py` exports SA `bronze.ball_bounce` (every ball event, `type` ∈ {swing, floor}); bench matches T5 emissions to the **floor**-type labels only. | `training.train_bounce_detector` (gravity-residual candidates → 1D-CNN) → `models/bounce_detector_v2_7match.pt` (deployed, Batch image) | `training/build_serve_bounce_dataset.py` | `bench_bounce.py` → `bench_baseline_bounce.json` — **NOW LOCKED** (was empty `{}` STOPGAP). agg rec **18.22%** / prec **23.26%** / over_x **0.783** (matched 137 / floor 752 / emit 589, 5 labelled tasks). **YES — enforcement ADDED this session** (aggregate compare, exits 1 on negative matched/precision delta). Verified: green=exit0, no-weights regression=exit1. | ⚠️ **The baseline is config-dependent.** Reproduce with `BOUNCE_CANDIDATE_MODE=gravity_residual` **and** `--weights-path models/bounce_detector_v2_7match.pt` **and** `--threshold 0.70` (the deployed config). Omitting either → STOPGAP random init → emit 0 → false all-zero regression. Recall is training-gated (sharp-far retrain). 3 zero-label corpus tasks excluded from the aggregate (`stored_rows_blind_to_scoring_population`). |
| **Identity** | **NONE** — rule-based v1 (changeover rule + game boundaries, `identity_detector/`). No `label_kind`, no corpus rows. | **NO TRAINER** (rule-based v1; OSNet/CNN re-id v2 is unbuilt and has no `label_kind` yet). | — | `bench_identity.py` → `bench_baseline_identity.json` — **NOW LOCKED** (was no baseline). Weighted changeover-fire agreement **100.0%** (n=14 ITF-expected changeovers, 3 tasks) vs ADR-03 floor 90%. **YES — enforcement ADDED this session** (compares weighted agreement, exits 1 below baseline or below floor). Verified green. | SA cross-reference via `silver.point_detail` (`model='sportai'`). The metric is internal-consistency (does the side flip at ITF-expected changeovers), not per-game side truth. v2 CNN re-id is the future upgrade. |
| **Swing** | owned by parallel build this session — see ADR-02 | owned by parallel build this session | owned by parallel build this session | **owned by parallel build this session — see ADR-02. DO NOT fill bench details here.** | `stroke_classifier/` in Batch image, `SWING_CLASSIFIER_ENABLED=0`; needs the 4th "other" class. |

## What changed this session (harness completeness)

- **bounce** baseline: `{}` STOPGAP → **locked** real values (rec 18.22% / prec 23.26% / over_x 0.783) in prod config. Added aggregate + regression gate to `bench_bounce.py`.
- **identity** baseline: none → **locked** (weighted agreement 100.0%, n=14). Added `--update-baseline` + regression gate to `bench_identity.py`.
- **hit** baseline existed but `bench_hit.py` was write-only (no compare). Added the aggregate regression gate so a plain re-run **enforces** (NEAR/FAR gate + matched_any).
- **serve** verified still green + CI-gated (no change).

All three new gates mirror the serve `bench.py` contract: load committed baseline → compare headline aggregate → exit non-zero on negative delta. `--task` narrows the population and intentionally **skips** the gate (baselines are locked on the full corpus / default task set).

## Residual gaps to "train all 5"

1. **Bounce/hit accuracy is train-last, gated on Tomo full-res uploads** (DoD #7/#8). The benches measure the floor; they don't move it. New sharp-far corpus data → retrain → re-`--update-baseline` (numbers go UP, the gate ratchets).
2. **Identity has no trainer/label pipeline** — the gate guards the rule-based v1 only. A trained v2 (CNN re-id) would need a new `label_kind` + corpus rows before it's a *trainable* fact; today it's a rule with a regression gate, which is the correct state for v1.
3. **Bounce bench is config-sensitive** (env + weights + threshold). The docstring + this row spell out the exact prod invocation; a bare `python -m ... bench_bounce` will FALSE-regress. Not CI-wired (local-only, by rule #9) — the operator must pass the prod config.
4. **None of hit/bounce/identity are CI-gated** — by design (rule #9: don't widen the CI glob; serve `bench.py` is the only CI gate). They are local pre-push gates run against the live prod DB (dev box IP allowlisted) + S3 corpus labels.
5. **Swing** — completed by the parallel build this session (ADR-02); not assessed here.
