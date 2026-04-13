# HANDOVER — T5 ML Pipeline (continuing from Apr 11-13 session)

## Read CLAUDE.md first — the T5 section at the bottom is the primary reference.

═══════════════════════════════════════════════════════════════════════
## WHAT WAS ACCOMPLISHED (Apr 11-13)
═══════════════════════════════════════════════════════════════════════

### Ball Detection: 7% → ~99.5%
- Fixed `*255` heatmap wrap bug in `ball_tracker.py:_postprocess_heatmap`
  (was `(feature_map * 255).astype(uint8)` — modular overflow destroyed signal)
- Added frame-delta Hough fallback: when TrackNet returns nothing (63.5%
  of frames), compute `cv2.absdiff` between consecutive frames + HoughCircles
- TrackNet: 36.1% detection, Delta fallback: 63.5%, Combined: ~99.5%
- BGR→RGB tested and REJECTED — model is BGR-trained (confirmed empirically,
  BGR→RGB dropped detection from 41% to 28%)
- Speed clamped at 250 km/h (was 806 km/h from position glitches)
- **CAVEAT**: delta fallback quality is UNVALIDATED — may detect player
  movement, racket swings, shadows, not just the ball. Downstream silver
  build will reveal if these are usable detections.

### Player Detection: Partially Solved
- Bench sitter/umpire: SOLVED — span check + court-geometry filtering
- Near player: SOLVED — always detected, never falsely filtered
- Detection-only model: BREAKTHROUGH — `yolov8m.pt` (not pose) for Pass 3
  detects people in far baseline area that `yolov8x-pose` missed entirely
  (pose NMS suppresses small detections where keypoints can't be resolved)
- **FAR PLAYER ON-COURT: UNSOLVED** — the detection-only model finds ~12
  candidates per frame in the far area, but scoring consistently picks
  spectators behind the baseline over the actual player ON the court.
  Multiple scoring approaches tried and failed:
  1. Midline distance (picks furthest = spectator behind baseline)
  2. Three-tier court_bbox (court_bbox is truncated, both in same tier)
  3. Feet-on-court y2 scoring (spectator y2 and player y2 too similar)

### Court Detection
- Calibration lock implemented (300 frames calibration, then locked forever)
- But homography still mostly fails on this camera angle
- This is the ROOT CAUSE of the far-player problem — without reliable
  court boundaries, we can't distinguish "on court" from "behind court"

### Infrastructure
- Auto-dual-submit: `AUTO_DUAL_SUBMIT_T5=1` env var (submission_context
  bug fixed, threaded call, idempotent)
- Manual dual-submit: `POST /ops/dual-submit-t5` + `harness dual-submit`
- Training bench: `harness training-bench align/serves/features/extract-serves`
- Ball + player diagnostics: automatic in CloudWatch every run
- Detection-only model loaded as `self._det_model` in PlayerTracker

═══════════════════════════════════════════════════════════════════════
## WHAT IS NOT WORKING (priority order)
═══════════════════════════════════════════════════════════════════════

### #1 — FAR PLAYER IDENTIFICATION
  The detection-only model FINDS people in the far area (~12 candidates
  per frame at conf=0.05). But the scoring picks the wrong one. The
  actual far player on the court and spectators behind the baseline
  are indistinguishable by pixel position alone when the court_bbox
  is unreliable.

  RESEARCH RECOMMENDATIONS NOT YET IMPLEMENTED:
  1. MOG2 background subtraction — detect moving objects on static court
  2. SAHI tiled inference — proper small-object detection framework
  3. Hough-lines court homography — more robust than CNN keypoints
  
  RECOMMENDED APPROACH: Background subtraction (MOG2) is the most
  promising. A player MOVES during play; spectators sit still. Use
  `cv2.createBackgroundSubtractorMOG2()` to find moving blobs in the
  far baseline area, then match YOLO detections to moving blobs.
  Players = detection + movement. Spectators = detection + no movement.

### #2 — SERVE DETECTION: 0 serves detected (SportAI gets 24)
  T5 reconciliation shows 63 rows, 0 points, 0 games, 2 raw serves
  (0 in serve_d). Direct consequence of:
  - Far player not identified → player assignments wrong
  - Ball detection quality unknown (delta fallback may be noisy)
  - Court_y offset still present (~5m systematic error)

### #3 — BALL DELTA FALLBACK QUALITY
  9,707 delta fallback detections — but are they the BALL or other
  moving things (players, rackets, shadows)? Need to validate by
  running silver build and checking if bounce/serve detection improves
  or gets worse with the extra detections.

### #4 — COURT HOMOGRAPHY
  Still failing most frames. Calibration lock helps (uses the best
  detection from first 300 frames) but the best detection itself
  may have only 4 inliers with mediocre accuracy. The 5m court_y
  offset persists.

### #5 — STROKE CLASSIFICATION
  T5 classifies most strokes as "Other" or "Volley" vs SportAI's
  correct Serve/Forehand/Backhand. Swing type inference from pose
  keypoints needs work, but this is downstream of player detection.

═══════════════════════════════════════════════════════════════════════
## RESEARCH RECOMMENDATIONS (from deep research agent)
═══════════════════════════════════════════════════════════════════════

  Priority order. Items 1-3 are implemented. Items 4-8 are NOT.

  1. ✅ Detection-only model for far player (yolov8m) — DONE
  2. ❌ MOG2 background subtraction for far player — HIGH PRIORITY
  3. ❌ SAHI tiled inference — medium priority (detection-only may suffice)
  4. ✅ Court calibration lock — DONE  
  5. ❌ TrackNetV3 upgrade (5-frame context) — medium priority
  6. ✅ Frame-delta ball fallback — DONE (quality unvalidated)
  7. ❌ Fine-tune TrackNet on own footage — long term
  8. ❌ Hough-lines court homography — medium priority

  Key insight from research: YOLOv8-pose SUPPRESSES detections below
  ~60-80px because pose NMS requires resolvable keypoints. Detection-
  only model floor is ~20-30px. ALWAYS use detection-only for small
  targets. This was the breakthrough for far-baseline detection.

═══════════════════════════════════════════════════════════════════════
## USER'S RECOMMENDED APPROACH (3 steps)
═══════════════════════════════════════════════════════════════════════

  Step 1: IMPLEMENT ALL 8 RESEARCH RECOMMENDATIONS
    Get the setup perfectly correct for what we're trying to do.
    Especially: MOG2 background subtraction, SAHI, Hough-lines court,
    TrackNetV3. Focus on far player identification + court accuracy.

  Step 2: SET UP THE BEST TRAINING SYSTEM
    Research what the best way is to test and train ML models for
    tennis video analysis. Use the dual-submit infrastructure +
    training bench to compare SportAI vs T5 systematically.
    Label training data from SportAI ground truth.

  Step 3: ITERATE AND IMPROVE DATA QUALITY
    Use the training system to systematically improve each component:
    ball detection, player detection, serve detection, stroke
    classification. Train custom models on own footage.

═══════════════════════════════════════════════════════════════════════
## REFERENCE TASKS
═══════════════════════════════════════════════════════════════════════

  SportAI ground truth:  4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb
                         88 rows, 17 points, 2 games, 24 serves

  T5 latest (0e929b55):  0e929b55-3a4d-4f50-8058-16a782ef8b87
                         63 rows, 0 points, 0 games, 0 serves
                         (Same video as SportAI — use for reconciliation)

  Reconcile command:
    python -m ml_pipeline.harness reconcile 4a194ff3 0e929b55

  Training bench:
    python -m ml_pipeline.harness training-bench serves 4a194ff3 0e929b55

═══════════════════════════════════════════════════════════════════════
## HARD LESSONS — DO NOT REPEAT
═══════════════════════════════════════════════════════════════════════

  LESSON 1: YOLOv8-pose suppresses small person detections (<60px).
  Use detection-only model (yolov8m) for any target under 60px.

  LESSON 2: BGR→RGB conversion HURTS this TrackNet V2 checkpoint.
  Confirmed empirically: detection dropped 41% → 28% with RGB. Keep BGR.

  LESSON 3: Court_bbox from keypoint model is unreliable for far court.
  The keypoint CNN misses far baseline keypoints → truncated bbox →
  can't use bbox for "inside court" checks. Need Hough-lines approach.

  LESSON 4: Midline-distance scoring picks spectators over players.
  Spectators behind baseline have MORE midline distance than players
  ON the court. Don't use midline distance as primary ranking signal.

  LESSON 5: Feet-on-court (y2) scoring doesn't cleanly separate players
  from spectators. Their y2 values are too similar at this camera angle.

  LESSON 6: The frame-delta ball fallback produces high detection count
  (~63.5% of frames) but quality is UNVALIDATED. Don't assume these are
  all ball detections — they may include player movement, racket swings.

  LESSON 7: Docker rebuild + ECR push is the deployment path for
  ml_pipeline changes. Git push to main deploys to Render (main API,
  ingest worker) but NOT to the ML Batch containers.

═══════════════════════════════════════════════════════════════════════
## DOCKER BUILD & DEPLOY
═══════════════════════════════════════════════════════════════════════

  docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline .
  ACCOUNT=696793787014
  # eu-north-1
  aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com
  docker tag ten-fifty5-ml-pipeline:latest $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
  docker push $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
  # us-east-1
  aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com
  docker tag ten-fifty5-ml-pipeline:latest $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
  docker push $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest

═══════════════════════════════════════════════════════════════════════
## CLOUDWATCH LOG MONITORING
═══════════════════════════════════════════════════════════════════════

  Log group: /aws/batch/ten-fifty5-ml-pipeline (eu-north-1)

  Find recent streams:
    MSYS_NO_PATHCONV=1 aws logs describe-log-streams \
      --log-group-name "/aws/batch/ten-fifty5-ml-pipeline" \
      --region eu-north-1 --order-by LastEventTime --descending --limit 5

  Pull diagnostics (replace LOG_STREAM):
    MSYS_NO_PATHCONV=1 aws logs get-log-events \
      --log-group-name "/aws/batch/ten-fifty5-ml-pipeline" \
      --log-stream-name "LOG_STREAM" \
      --region eu-north-1 --limit 200 --output text | \
      grep -iE "BallTracker|PlayerTracker|filter_stationary|KEEP|REJECT"

  The user is technical, hands-on, tests via debug frames in S3 + Render
  shell. Be terse. Don't time-estimate. Read code before suggesting
  changes. When stuck, ask for diagnostic data rather than guessing.
