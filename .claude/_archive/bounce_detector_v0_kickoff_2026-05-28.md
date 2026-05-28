# bounce_detector v0 — scaffold landed (2026-05-28)

ADR: `docs/_investigation/adr_01_bounce_model_architecture.md`

## What's built (v0 scaffold only)

- New module `ml_pipeline/bounce_detector/`:
  - `models.py` — `BounceEvent` dataclass + `SignalSource` / `PlayerSide` enums.
  - `db.py` — `init_bounce_schema()` creates `ml_analysis.ball_bounces` (idempotent) per ADR schema.
  - `cnn.py` — `BounceCNNWrapper` around a 3-block 1D temporal CNN (k=5, channels 32→64→64, dropout 0.3, sigmoid head). `load_weights()` handles weights-absent gracefully — tagged `# STOPGAP-untrained-stage1`.
  - `feature_extractor.py` — 14-channel × 41-frame window builder (every channel from the ADR table).
  - `pre_gates.py` — wrist proximity (<0.6 m), net-line (<1.0 m + above-net), rally-state.
  - `detector.py` — orchestrator. Mirrors `serve_detector/detector.py`: `_load_ball_rows` / `_load_wrist_positions` / `_load_rally_states_by_frame` → pre-gates → features → CNN → 0.15 s NMS → persist. STOPGAP forces threshold to 1.1 → zero rows written until trained weights land.
- New bench `ml_pipeline/diag/bench_bounce.py` — local-only, not CI-gated. Pulls labels from `ml_analysis.training_corpus` (488 ball_position labels across 2 tasks), filters to `type='floor'` only, reports per-task recall / precision / spatial error.
- Baseline stub `ml_pipeline/diag/bench_baseline_bounce.json` — empty until v1 lands.

## What's NOT built (deferred to next session)

1. **Label-accuracy audit** — verify the 488 corpus labels are themselves <1 m accurate before training against them. ADR §"Training data assessment" gate-3.
2. **Negative mining** — sample ~5-10× negative windows from `ball_detections` excluding ±0.2 s of any label, plus hand-labelled FPs from current high-confidence T5 bounces in implausible positions.
3. **`bounce_type` enum extension** — add `{floor, net_cord, racket_hit}` to schema + a few hundred hand-labelled FPs, so the model learns to discriminate at the head (not just pre-gates).
4. **Training script** — `ml_pipeline/training/train_bounce_detector.py` that consumes the corpus + negative mines + bounce_type labels, trains the CNN, dumps `ml_pipeline/models/bounce_detector_v1.pt`.
5. **Ingest-flow integration** — wire `detect_bounces(task_id)` into `_do_ingest_t5()` in `upload_app.py` AFTER `serve_detector` runs (serve_events is a pre-gate input). Out of v0 scope — parent session lands this once weights exist.
6. **`init_bounce_schema()` registration in `upload_app.py`** — parent session follow-up; mirror the on-boot init pattern used for serve_events / coach views.
7. **Silver consumption** — silver builders eventually read from `ml_analysis.ball_bounces` instead of `ball_detections.is_bounce` (parallel agent territory; ADR-04 cleanup).

## How to run locally

```powershell
# Bench (untrained — expect ~0% recall, all-zero rows, just confirms plumbing).
.venv\Scripts\python -m ml_pipeline.diag.bench_bounce

# Single task:
.venv\Scripts\python -m ml_pipeline.diag.bench_bounce --task 78c32f53-5580-4a88-a4e7-7506e59b2b52

# Once weights exist:
.venv\Scripts\python -m ml_pipeline.diag.bench_bounce --threshold 0.55 --update-baseline
```

## Path to v1 (next session)

Order: label audit (1) → negative mining (2) → bounce_type schema add (3) → training script (4) → run training → lock baseline → integrate into ingest (5+6). Each step is one focused commit. The serve bench MUST stay green throughout (CI gate, CLAUDE.md rule #9).

Estimated time to v1: roughly 1-2 working days, dominated by step 3 (negative mining + bounce_type hand-labelling).
