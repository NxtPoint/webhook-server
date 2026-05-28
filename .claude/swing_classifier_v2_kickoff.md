# ADR-02 v1 swing-type classifier scaffold — kickoff doc (2026-05-28)

## What's built
Mirrors the bounce_detector v0 pattern — STOPGAP-flagged scaffold for an unbuilt model.

**ml_pipeline/stroke_classifier/** (new files, ADR-02 v2):
- `model_v2.py` — `SwingTypeR2plus1D` (R(2+1)D-18, 2-channel optical-flow stem, handedness concat at penultimate FC) + `SwingTypeClassifierV2` wrapper with STOPGAP-no-weights behaviour
- `dataset.py` — PyTorch `SwingTypeDataset` reading from `build_swing_type_dataset` outputs; train/val/all split via manifest; hflip + dx-sign-flip + handedness-toggle + temporal-crop augmentation
- `detector_v2.py` — Render-side `detect_swing_types_for_task(task_id)` entry point: stroke_events → bbox-by-frame → 1080p video crop → optical-flow → batched predict → write `ml_analysis.swing_type_events`. STOPGAP-no-weights: returns `{"status": "stopgap"}` immediately; no S3 download; no DB writes.
- `db.py` — `ml_analysis.swing_type_events` schema (separate from `stroke_events` to preserve rule #8 ownership of `stroke_detector/` by the parallel agent)

**ml_pipeline/training/train_swing_type.py** — Training loop per ADR-02 §"Training recipe": AdamW + cosine warmup, label-smoothing OR focal loss, mixup α=0.2, WeightedRandomSampler oversamples minority class, early-stop on val macro-F1.

**ml_pipeline/diag/bench_swing_type.py** — Regression bench; `--bless` locks baseline; STOPGAP-no-weights returns `available=False` and exits 0.

**Modified:**
- `ml_pipeline/stroke_classifier/__init__.py` — exposes v2 API alongside legacy v1
- `upload_app.py` — boot init for `init_swing_type_schema()` + try/except-wrapped call to `detect_swing_types_for_task()` inside `_do_ingest_t5()` after stroke_detector

## What's NOT built (intentionally — needs corpus volume)
- **Trained weights** at `ml_pipeline/models/swing_classifier_v2.pt` — needs ~2-3k labelled hit-events (~5-10 more matches). Today: 368 training-ready hits across 3 matches.
- Bench baseline at `ml_pipeline/diag/bench_baseline_swing_type.json` — locks when first weights ship.
- Handedness inference — currently default-right. Future: query stroke_events forehand-side preference per player.

## Verification (already passed, this session)
- All modules import without torchvision needing network access
- `SwingTypeClassifierV2()` instantiates → `available=False` (STOPGAP)
- `predict_batch(fake_flow, fake_hand)` → `[]` (STOPGAP)
- `bench_swing_type.run_bench()` → STOPGAP report, exits 0
- Serve bench unchanged (a798eff0=20/24, 880dff02=23/24)
- Match 4 (ca475740) still at 78% roi_extract — when it lands, the new `detect_swing_types_for_task` runs as no-op and cannot break the ingest flow

## Next session needs (when corpus crosses ~2-3k labels)
```bash
python -m ml_pipeline.training.build_swing_type_dataset \
    --pairs 78c32f53 c645a7ee 9378f2dd ca475740 ...   # all corpus pairs
    --output ml_pipeline/training/datasets/swing_type_v2

python -m ml_pipeline.training.train_swing_type \
    --dataset-dir ml_pipeline/training/datasets/swing_type_v2 \
    --output ml_pipeline/models/swing_classifier_v2.pt \
    --epochs 50

python -m ml_pipeline.diag.bench_swing_type --bless
```

When weights ship, the existing detect_swing_types_for_task wiring flips from no-op to active; `ml_analysis.swing_type_events` starts populating; the silver pose-keypoint STOPGAP in `build_silver_match_t5._infer_swing_type_*` can be deleted (silver inherits bronze per rule #1).
