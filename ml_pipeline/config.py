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

# TrackNet V3 — drop-in upgrade with 5-frame context window.
# 5 frames gives more temporal context for ball trajectory prediction,
# especially for fast serves and occluded frames. Auto-detected by
# BallTracker based on which weights file exists.
TRACKNET_V3_WEIGHTS = os.path.join(MODELS_DIR, "tracknet_v3.pt")
TRACKNET_V3_NUM_INPUT_FRAMES = 5  # 5-frame sliding window
TRACKNET_HOUGH_DP = 1
TRACKNET_HOUGH_MIN_DIST = 1
TRACKNET_HOUGH_PARAM1 = 50
TRACKNET_HOUGH_PARAM2 = 2
TRACKNET_HOUGH_MIN_RADIUS = 1   # Allow smaller ball circles (serves/fast balls)
TRACKNET_HOUGH_MAX_RADIUS = 10  # Allow larger ball circles (slow/zoomed)
BALL_MAX_INTERPOLATION_GAP = 5   # Standard 5 frames
BALL_MAX_DIST_BETWEEN_FRAMES = 150
BALL_MAX_DIST_GAP = 150

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
YOLO_CONFIDENCE = 0.25             # Sane production value with YOLOv8x-pose (bigger model = more confident)
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

# Debug frame export — saves a sampled frame with YOLO bboxes drawn on it
# every N detection frames. Uploaded to s3://{bucket}/debug/{job_id}/frame_*.jpg
# by __main__.py post-processing. Set to 0 to disable.
DEBUG_FRAME_INTERVAL = 1000
YOLO_PERSON_CLASS_ID = 0           # COCO class ID for 'person'
PLAYER_IOU_THRESHOLD = 0.2         # More lenient IoU matching (handles movement)
PLAYER_COURT_MARGIN_PX = 9999      # Effectively DISABLED — court bbox can be wrong, trust YOLO
PLAYER_DETECTION_INTERVAL = 3      # Run YOLO more often for stable tracking
PLAYER_DETECTION_INTERVAL_PRACTICE = 10  # Less frequent for practice

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
