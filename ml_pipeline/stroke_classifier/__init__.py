"""
Optical-flow stroke classifier for far-player stroke type inference.

The near player (~200-400px) has usable pose keypoints → classified in
build_silver_match_t5._infer_swing_type_from_keypoints().

The far player (~30-40px) is too small for pose. This module classifies
strokes from the *motion pattern* visible in optical flow around hit events.

Pipeline:
  1. During video processing, pipeline.py stores raw frames in memory
  2. After bounce detection, flow_extractor.py crops the far-player bbox
     ±5 frames around each hit and computes Farneback optical flow
  3. model.py classifies the 10-frame flow tensor → fh/bh/serve/volley/other
  4. Results stored in ml_analysis.player_detections.stroke_class column
  5. build_silver_match_t5 uses stroke_class when keypoints return "other"

Training: SportAI ground truth from dual-submit pairs provides free labels.
See train.py and export_training_data.py.
"""
