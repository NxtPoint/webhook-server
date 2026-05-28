"""
All tunable parameters for the ML tennis analysis pipeline.
Nothing is hardcoded in the main code — everything references this file.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ML_PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ML_PIPELINE_DIR, "models")

# Model weight files (relative to MODELS_DIR)
TRACKNET_WEIGHTS = os.path.join(MODELS_DIR, "tracknet_v2.pt")
YOLO_WEIGHTS = os.path.join(MODELS_DIR, "yolov8m.pt")
# Player detection: prefer the larger YOLOv8x-pose model (~133MB) for better
# small-object detection. Falls back to yolov8m-pose if x is missing.
YOLO_POSE_WEIGHTS = os.path.join(MODELS_DIR, "yolov8x-pose.pt")
YOLO_POSE_WEIGHTS_FALLBACK = os.path.join(MODELS_DIR, "yolov8m-pose.pt")
COURT_DETECTOR_WEIGHTS = os.path.join(MODELS_DIR, "court_keypoints.pth")

# ---------------------------------------------------------------------------
# Video preprocessing
# ---------------------------------------------------------------------------
FRAME_SAMPLE_FPS = 25          # Extract frames at this rate (match analysis)
FRAME_SAMPLE_FPS_PRACTICE = 10 # Lower FPS for practice — bounces still captured, 2.5x faster
SUPPORTED_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv")

# ---------------------------------------------------------------------------
# TrackNet (ball tracker)
# ---------------------------------------------------------------------------
TRACKNET_INPUT_WIDTH = 640
TRACKNET_INPUT_HEIGHT = 360
TRACKNET_NUM_INPUT_FRAMES = 3   # Sliding window of 3 consecutive frames (TrackNet V2)
TRACKNET_OUTPUT_CHANNELS = 256
TRACKNET_HEATMAP_THRESHOLD = 127  # Standard threshold (lowering to 100 broke ball detection)
TRACKNET_BGR2RGB = False           # DO NOT convert BGR→RGB. Empirically confirmed on run 33f952b9:
                                   # BGR→RGB dropped detection from 41% to 28% — this TrackNet V2
                                   # checkpoint was trained on BGR (cv2 convention). Keep False.

# TrackNet V3 (qaz812345/TrackNetV3) — NOT a drop-in V2 upgrade.
# V3 uses 8 input frames + a background median image = 27 input channels,
# a U-Net architecture with skip connections, and a separate rectification
# module for occluded trajectory repair. This is a fundamentally different
# model from V2 (3 frames, 9 channels, encoder-decoder without skips).
#
# Architecture is ported in ml_pipeline/tracknet_v3.py (TrackNetV3 class).
# BallTracker auto-selects V3 when tracknet_v3.pt exists in the models dir.
# V2 (tracknet_v2.pt) remains the default when V3 weights are absent.
#
# To activate V3:
#   1. Train or obtain V3 weights (qaz812345/TrackNetV3 training pipeline)
#   2. Place the .pt file at ml_pipeline/models/tracknet_v3.pt
#   3. No code changes needed — BallTracker detects and loads V3 automatically
TRACKNET_V3_WEIGHTS = os.path.join(MODELS_DIR, "tracknet_v3.pt")
TRACKNET_V3_NUM_INPUT_FRAMES = 8  # V3 uses 8-frame sliding window
TRACKNET_V3_IN_CHANNELS = 27      # 8 frames × 3 channels + 3 background = 27
# Number of frames sampled from the start of a video to compute the per-pixel
# median background for V3 input. More frames → more stable; 200 at 25 fps
# = ~8 s of warmup. During warmup the 8-frame buffer fills but detections are
# still returned (background is force-computed on the first full window if not
# yet ready).
TRACKNET_V3_BACKGROUND_WARMUP_FRAMES = 200
TRACKNET_HOUGH_DP = 1
TRACKNET_HOUGH_MIN_DIST = 1
TRACKNET_HOUGH_PARAM1 = 50
TRACKNET_HOUGH_PARAM2 = 2
TRACKNET_HOUGH_MIN_RADIUS = 1   # Allow smaller ball circles (serves/fast balls)
TRACKNET_HOUGH_MAX_RADIUS = 10  # Allow larger ball circles (slow/zoomed)
BALL_MAX_INTERPOLATION_GAP = 5   # Standard 5 frames
BALL_MAX_DIST_BETWEEN_FRAMES = 150
BALL_MAX_DIST_GAP = 150
# Number of consecutive detections that cohere with each other (each within
# BALL_MAX_DIST_BETWEEN_FRAMES of the previous) but are far from the current
# anchor needed to trigger a re-anchor in _filter_outliers. Without this, a
# bad early anchor freezes the filter chain — pre-fix, WASB on task 1d6feb3a
# kept rows only for frames 2-3329 of a 15,298-frame video. 4 is conservative
# enough that random outliers don't satisfy it but small enough to recover
# quickly after a real gap (ball re-acquired after going off-screen).
BALL_FILTER_REANCHOR_RUN = 4

# ---------------------------------------------------------------------------
# Ball tracker selection (env-controlled — see ml_pipeline.pipeline)
# ---------------------------------------------------------------------------
# Which ball detector to use in production. Default 'tracknet_v2' so unset
# environments behave like pre-2026-05-21 production. Set BALL_TRACKER=wasb
# on Render's main API service to flip to WASB (which benchmarked materially
# better on the documented coverage-gap regime — see
# ml_pipeline/diag/bench_ball_baseline.json, 880dff02 SA point 6: 2/9 vs 0/9
# strokes recovered, commit `7100792`).
#
# Valid values: 'tracknet_v2' (default), 'wasb'.
BALL_TRACKER = os.getenv("BALL_TRACKER", "tracknet_v2").strip().lower()

# ---------------------------------------------------------------------------
# GPU batching for the ball detector (Lever #2, docs/_investigation/
# t5_pipeline_speed.md). The ball model runs EVERY frame at batch=1, which
# leaves the Batch T4 badly underutilised on 512×288/640×360 inputs. Setting
# BALL_BATCH_SIZE>1 accumulates that many sliding-window inputs and runs ONE
# forward pass — same per-frame math (BatchNorm is eval/running-stats, conv is
# batch-element-independent), so outputs are identical on CPU and within
# fp-noise on GPU. Default 1 = current per-frame behaviour (zero-risk rollback,
# same pattern as the BALL_TRACKER env gate). Only the WASB tracker implements
# batching; TrackNet ignores it. 8 is a good T4 starting point.
BALL_BATCH_SIZE = max(1, int(os.getenv("BALL_BATCH_SIZE", "1")))

# ---------------------------------------------------------------------------
# GPU batching for the player detector (Lever #1 from
# docs/_investigation/batch_optimisation_plan.md). YOLOv8x-pose @ imgsz=1280
# is the dominant per-frame cost on Batch (~75-85% of the player stage budget)
# and runs at batch=1 — the T4 is grossly underutilised on a single 1080p
# frame. Setting PLAYER_BATCH_SIZE>1 accumulates that many DETECT frames
# (player tracker runs every PLAYER_DETECTION_INTERVAL=5 source frames, so
# N=8 detect-frames = 40 source frames of buffer) and runs ONE batched
# model.predict([f1..fN]) call. Same per-frame math (conv is batch-element-
# independent, BatchNorm is eval/running-stats, NMS is per-element post-hoc)
# so outputs match the per-frame path on CPU and within fp-noise on GPU —
# same equivalence as the ball-batching gate (BALL_BATCH_SIZE).
#
# SAHI stays per-frame in this lever (the SAHI skip A/B test depends on the
# per-frame full-YOLO output and SAHI's own tile-fan is a separate batching
# target — L2 in the optimisation plan). Default 1 = current per-frame
# behaviour (zero-risk rollback). Flip to 8 on the Batch job-def to activate.
PLAYER_BATCH_SIZE = max(1, int(os.getenv("PLAYER_BATCH_SIZE", "1")))

# ---------------------------------------------------------------------------
# Post-pipeline ROI extractor batching (Lever #4 from
# docs/_investigation/batch_optimisation_plan.md). The ROI passes
# (far-pose ViTPose + service-box TrackNet) account for ~25% of total
# Batch wall time on a long match (1.38h of 4.79h on `9378f2dd`). The
# dominant cost is the ~33,750 per-crop ViTPose calls @ batch=1 over a
# 45-min match. Batching N of them into ONE forward pass collapses that
# to ~33,750/N device round-trips.
#
# ROI_BATCH_SIZE: how many pose-input crops to accumulate before flushing
# one batched ViTPose call. Default 1 = current per-frame behaviour
# (zero-risk rollback). 16 is a good T4 starting point. The YOLO-det
# pre-pass that selects the person inside each ROI is also batched
# inside FarPoseProcessor — same buffer.
ROI_BATCH_SIZE = max(1, int(os.getenv("ROI_BATCH_SIZE", "1")))

# ROI_POSE_FP16: enable half-precision inference for the ViTPose model
# (and the YOLO-det pre-pass that runs on the ROI crop). T4 FP16 throughput
# is ~2× FP32. The full-frame YOLOv8x-pose pass in player_tracker already
# runs FP16 by default on cuda (see YOLO_CONFIDENCE comment) — ViTPose
# hadn't been opted in until this lever. Env-gated default OFF so the
# first deploy preserves FP32 outputs and we flip after benching the
# batched-FP16 detection counts on a known job.
ROI_POSE_FP16 = os.getenv("ROI_POSE_FP16", "0").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Court detector (ResNet50 keypoints)
# ---------------------------------------------------------------------------
COURT_INPUT_SIZE = 224             # ResNet50 expects 224x224
COURT_NUM_KEYPOINTS = 14           # 14 standard tennis court keypoints
COURT_DETECTION_INTERVAL = 30      # Run court detection every N frames (during calibration)
COURT_CALIBRATION_FRAMES = 300     # Number of frames to search for the best court detection.
                                   # After this, the best homography is LOCKED for the rest of
                                   # the video. Fixed indoor camera = court doesn't move.
COURT_DETECTION_INTERVAL_PRACTICE = 60  # Less frequent for practice (court is static)
COURT_CONFIDENCE_THRESHOLD = 0.5   # Below this → fall back to Hough lines
COURT_IMAGENET_MEAN = [0.485, 0.456, 0.406]
COURT_IMAGENET_STD = [0.229, 0.224, 0.225]

# Hough line fallback parameters
HOUGH_RHO = 1
HOUGH_THETA_DIVISOR = 180          # np.pi / HOUGH_THETA_DIVISOR
HOUGH_THRESHOLD = 100
HOUGH_MIN_LINE_LENGTH = 100
HOUGH_MAX_LINE_GAP = 10

# ---------------------------------------------------------------------------
# Tennis court real-world dimensions (metres, ITF standard)
# ---------------------------------------------------------------------------
COURT_LENGTH_M = 23.77             # Baseline to baseline
COURT_WIDTH_SINGLES_M = 8.23       # Singles sideline to sideline
COURT_WIDTH_DOUBLES_M = 10.97      # Doubles sideline to sideline
SERVICE_BOX_DEPTH_M = 6.40         # Net to service line
NET_HEIGHT_CENTER_M = 0.914        # Net height at centre
NET_HEIGHT_POST_M = 1.07           # Net height at posts

# Reference court keypoints (pixel coords in a canonical top-down view).
# These match the yastrebksv/TennisCourtDetector convention.
# Order: baseline-top-L, baseline-top-R, baseline-bot-L, baseline-bot-R,
#         singles-top-L, singles-bot-L, singles-top-R, singles-bot-R,
#         service-top-L, service-top-R, service-bot-L, service-bot-R,
#         center-service-top, center-service-bot
COURT_REFERENCE_KEYPOINTS = [
    (286, 561), (1379, 561),       # baseline top L, R
    (286, 2935), (1379, 2935),     # baseline bottom L, R
    (423, 561), (423, 2935),       # left inner line top, bottom
    (1242, 561), (1242, 2935),     # right inner line top, bottom
    (423, 1110), (1242, 1110),     # top inner line L, R
    (423, 2386), (1242, 2386),     # bottom inner line L, R
    (832, 1110), (832, 2386),      # middle line top, bottom
]

# ---------------------------------------------------------------------------
# Player tracker (YOLOv8)
# ---------------------------------------------------------------------------
YOLO_CONFIDENCE = 0.10             # Was 0.25. 2026-04-19: prod_pose_audit + replay_detect_frame diags
                                   # proved the near-player pose bbox Batch was missing in minutes 3-6
                                   # IS produced by local CPU YOLOv8x-pose AND scoring logic keeps it
                                   # when given the same frame (H2 ruled out). The drop only happens
                                   # inside the Batch container — leading theory is GPU FP16 inference
                                   # suppressing detections near the 0.25 threshold. Lowering to 0.10
                                   # lets borderline GPU outputs through; _choose_two_players tier
                                   # scoring + pixel-polygon gate still reject non-player noise
                                   # downstream. If false-positive rate explodes, revisit.
YOLO_COURT_CROP_CONFIDENCE = 0.15  # Lower threshold for the court-crop pass — distant players are small
                                   # and produce lower-confidence detections. Safe to be permissive here
                                   # because _choose_two_players span check filters non-players downstream.
YOLO_IMGSZ = 1280                  # Input resolution. Default 640 → too small for distant players. 1280 = 2x = 4x pixels per object
YOLO_COURT_CROP_INFERENCE = True   # Run a SECOND YOLO pass on the court-cropped+upscaled region (catches distant players)
YOLO_COURT_CROP_MARGIN_PX = 120    # Pixels of margin around court when cropping (was 80 — widened to
                                   # include more of the far baseline area where distant players stand)
PLAYER_OUTSIDE_COURT_MARGIN_PX = 9999 # DISABLED — court-area filter was rejecting real players when court
                                      # keypoints missed the far baseline (bbox only covered near court).
                                      # _choose_two_players court-geometry scoring + path-length filter
                                      # now handle non-player rejection. Was 120.

# ---------------------------------------------------------------------------
# SAHI tiled inference (small-object person detection)
# ---------------------------------------------------------------------------
# SAHI (Slicing Aided Hyper Inference) systematically slices the frame into
# overlapping tiles, runs YOLO on each tile, then merges results via NMS.
# Replaces the manual 3-pass approach (full frame + court crop + far baseline)
# with a principled method. Particularly effective for the far player (~30-40px).
SAHI_ENABLED = True                # Enable SAHI for player detection
SAHI_SLICE_HEIGHT = 640            # Tile height (was 416 — larger = fewer tiles)
SAHI_SLICE_WIDTH = 640             # Tile width (was 416)
SAHI_OVERLAP_RATIO = 0.15          # 15% overlap (was 20% — small stability tradeoff
                                   # for ~10% fewer tiles; edges are still well covered)
SAHI_CONFIDENCE = 0.15             # Low confidence for small distant players
SAHI_POSTPROCESS_TYPE = "NMS"      # Non-Maximum Suppression for dedup
SAHI_POSTPROCESS_MATCH_THRESHOLD = 0.5  # IoU threshold for NMS merge

# Debug frame export — saves a sampled frame with YOLO bboxes drawn on it
# every N detection frames. Uploaded to s3://{bucket}/debug/{job_id}/frame_*.jpg
# by __main__.py post-processing. Set to 0 to disable.
DEBUG_FRAME_INTERVAL = 1000
YOLO_PERSON_CLASS_ID = 0           # COCO class ID for 'person'
PLAYER_IOU_THRESHOLD = 0.2         # More lenient IoU matching (handles movement)
PLAYER_COURT_MARGIN_PX = 9999      # Effectively DISABLED — court bbox can be wrong, trust YOLO
PLAYER_DETECTION_INTERVAL = 5      # Was 3 — increased to 5 (200ms between detections at 25fps,
                                   # well within the window where a player's position barely
                                   # changes). Cuts detection work by 40%. Further increases
                                   # risk missing serve impact positions.
PLAYER_DETECTION_INTERVAL_PRACTICE = 10  # Less frequent for practice
# Identity stability (A2): bbox-to-prev matching needs two guards beyond
# raw IoU or a false-positive in one half can steal the OTHER half's pid.
PLAYER_MAX_CENTER_DRIFT_PX = 250   # Max pixel distance between prev and new
                                   # bbox centers for a match to count. A
                                   # detection halfway across the image cannot
                                   # inherit a pid just because its IoU with
                                   # the stale prev bbox happens to exceed
                                   # threshold (1080p frame: half-court span
                                   # in pixels is ~400-500 px; 250 covers a
                                   # player's typical motion in a 5-frame
                                   # window but rejects cross-court jumps).
PLAYER_TRACK_TIMEOUT_FRAMES = 30   # Drop prev_players[pid] if not refreshed
                                   # for this many frames (1.2s @ 25fps).
                                   # Stops a 10-second-old bbox from silently
                                   # matching a new false-positive detection.

# ---------------------------------------------------------------------------
# MOG2 background subtraction (far-player motion scoring)
# ---------------------------------------------------------------------------
# MOG2 separates moving objects (players) from static background (spectators).
# The foreground mask is used in _choose_two_players to prefer moving candidates
# in the far half — a player on court MOVES, a spectator in the stands does NOT.
MOG2_HISTORY = 200                 # frames of history for background model
MOG2_VAR_THRESHOLD = 50            # variance threshold for foreground detection
                                   # Higher = less sensitive (fewer false positives)
MOG2_DETECT_SHADOWS = False        # Don't detect shadows (adds complexity, no benefit here)
MOG2_LEARNING_RATE = 0.005         # How fast the background adapts. Lower = more stable
                                   # background model. 0.005 = ~200 frames to fully learn.
MOG2_MIN_MOTION_RATIO = 0.03      # Minimum fraction of bbox pixels that must be foreground
                                   # to consider a candidate as "moving". A moving player
                                   # typically has 5-15% foreground pixels; a seated spectator
                                   # has 0-1%. 3% is a safe threshold.
MOG2_MOTION_SCORE_WEIGHT = 1000   # Bonus added to far-half candidate score when motion is
                                   # above MOG2_MIN_MOTION_RATIO. Must dominate the y2-based
                                   # score (~0-1080) so a moving candidate always beats a
                                   # stationary one regardless of y2 position.

# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------
PROGRESS_LOG_INTERVAL = 100        # Log progress every N frames

# ---------------------------------------------------------------------------
# Bounce / speed detection
# ---------------------------------------------------------------------------
BOUNCE_VELOCITY_WINDOW = 5         # Standard 5 frames (shorter broke bounce detection)
BOUNCE_MIN_DIRECTION_CHANGE = 25   # Minimum frames of sustained direction change (rally split)
SPEED_SMOOTHING_WINDOW = 3         # Frames to average for speed calc
