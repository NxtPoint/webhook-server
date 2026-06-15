"""T5 swing-type classifier — pose-occluded stroke type from optical flow.

Two coexisting implementations during the ADR-02 transition:

  v1 (pre-ADR-02, present from 2026-04-23 scaffold):
    - 5-class lightweight 3D-CNN, 64x48 input, far-player only
    - Files: model.py (StrokeFlowCNN, StrokeClassifier), flow_extractor.py,
      train.py, export_training_data.py
    - Status: UNTRAINED (weights file `models/stroke_classifier.pt` absent)
    - Production wiring: gated on weights existence in ml_pipeline/pipeline.py

  v2 (ADR-02, approved 2026-05-28) — LIVE, BATCH-side:
    - 4-class R(2+1)D-18 with handedness bit, 112x112 input, NEAR + FAR
    - Files: model_v2.py (SwingTypeR2plus1D, SwingTypeClassifierV2),
      dataset.py (SwingTypeDataset reads build_swing_type_dataset .pt files),
      inference_v2.py (classify_strokes_v2 entry point — runs in the Batch
      image from pipeline.py, writes ml_analysis.player_detections.stroke_class)
    - Status: ENABLED (SWING_CLASSIFIER_ENABLED default 1); swing bench LOCKED
      at macro-F1 0.7468. Silver projects stroke_class verbatim.
    - Trains via: python -m ml_pipeline.training.train_swing_type (GPU)
    - Benches via: python -m ml_pipeline.diag.bench_swing_type (GPU)
    - NOTE: the old Render-side detector_v2.py -> ml_analysis.swing_type_events
      path (no consumer) was removed 2026-06-15. stroke_class is the only swing
      output silver reads.

v1 imports are preserved for the pipeline.py weights-gated import path.
"""
# v1 surface — preserved for the pipeline.py weights-gated import path
from ml_pipeline.stroke_classifier.model import (
    NUM_CLASSES as V1_NUM_CLASSES,
    STROKE_CLASSES as V1_STROKE_CLASSES,
    STROKE_MODEL_WEIGHTS as V1_STROKE_MODEL_WEIGHTS,
    StrokeClassifier as V1StrokeClassifier,
    StrokeFlowCNN as V1StrokeFlowCNN,
)

# v2 surface (ADR-02)
from ml_pipeline.stroke_classifier.model_v2 import (
    CLASSES as V2_CLASSES,
    CLASS_TO_IDX as V2_CLASS_TO_IDX,
    MODEL_WEIGHTS_V2,
    NUM_CLASSES as V2_NUM_CLASSES,
    SwingTypeClassifierV2,
    SwingTypeR2plus1D,
)

__all__ = [
    # v1
    "V1StrokeClassifier", "V1StrokeFlowCNN",
    "V1_STROKE_CLASSES", "V1_NUM_CLASSES", "V1_STROKE_MODEL_WEIGHTS",
    # v2
    "SwingTypeR2plus1D", "SwingTypeClassifierV2",
    "V2_CLASSES", "V2_NUM_CLASSES", "V2_CLASS_TO_IDX", "MODEL_WEIGHTS_V2",
]
