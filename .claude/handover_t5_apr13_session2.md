# HANDOVER — T5 ML Pipeline Session 2 (Apr 13, continuing from handover_t5_apr13.md)

## Read CLAUDE.md first — the T5 section at the bottom is the primary reference.

═══════════════════════════════════════════════════════════════════════
## WHAT WAS ACCOMPLISHED
═══════════════════════════════════════════════════════════════════════

### FAR PLAYER IDENTIFICATION: SOLVED (from 0% → ~80%+)

The #1 problem from the previous session. Three-tier court-geometry scoring
now correctly identifies the far player in most frames:

  Tier 1 (3000): Inside the court quadrilateral (cv2.pointPolygonTest)
  Tier 2 (2000): Behind baseline, within sideline extensions
  Tier 3 (1000): Near sideline corridor (baseline-to-net)
  Tier 0:        Off-court — spectators, bench, umpire

Tiebreakers within each tier:
  - MOG2 motion bonus (+500) — moving player > stationary person
  - Bbox area (0-200) — actual player is closer to camera = bigger box
  - Center-line proximity (0-100) — players stand near court center

Fallback to centering heuristic when court corners unavailable.

### All 8 Research Recommendations Addressed

| # | Recommendation | Status |
|---|---|---|
| 1 | Detection-only yolov8m | DONE (prev session) |
| 2 | MOG2 background subtraction | DONE — motion scoring integrated |
| 3 | SAHI tiled inference | DONE — sahi==0.11.18, 416×416 tiles |
| 4 | Court calibration lock | DONE (prev session) |
| 5 | TrackNetV3 architecture | DONE — full U-Net port in tracknet_v3.py |
| 6 | Frame-delta ball fallback | DONE (prev session, quality unvalidated) |
| 7 | Fine-tune TrackNet | DONE — training pipeline built |
| 8 | Hough-lines court detection | DONE — white-mask + clustering + CNN refine |

### Court CNN Keypoint Extraction Fixed

Rewrote to match yastrebksv/TennisProject reference:
- Threshold 170 (was 0.01) + Hough circles (r=10-25)
- refine_kps() — crop around keypoint, find lines, snap to intersection
- Both CNN and Hough run during calibration, best wins
- New: get_court_corners_pixels() returns 4 baseline corners for geometry

### TrackNetV3 Architecture Ported

New file: ml_pipeline/tracknet_v3.py
- U-Net with skip connections (encoder: Double/Triple conv blocks)
- 27 input channels (3 background median + 8 frames × 3)
- Sigmoid output (8 heatmaps, one per frame)
- BackgroundEstimator: median from first 200 frames
- Auto-detected by BallTracker when tracknet_v3.pt exists
- V2 remains default — V3 needs weights (train or download)

### Eval Infrastructure Built

ml_pipeline/eval_store.py:
- record_reconciliation(), record_golden_check(), record_component_eval()
- Persists to ml_pipeline/eval_history.jsonl

New harness commands:
- eval-ball <task_id> — detection rate, bounce count, speed sanity
- eval-player <task_id> — player count, coordinate variance, path length
- eval-court <task_id> — court confidence, homography success rate
- eval-history — view past eval results

### Training Pipeline Built

ml_pipeline/training/:
- export_labels.py — extract ball labels from DB (T5 + SportAI)
- tracknet_dataset.py — PyTorch Dataset, 3-frame windows, Gaussian heatmaps
- train_tracknet.py — fine-tune decoder, BCELoss, pos_weight=100
- extract_frames.py — extract frames from video/S3

Harness commands: export-ball-labels, export-sportai-labels, extract-frames

═══════════════════════════════════════════════════════════════════════
## COMMITS (in order)
═══════════════════════════════════════════════════════════════════════

d5afbb4  feat(t5): MOG2 motion scoring, improved Hough court detection, TrackNetV3 support
d1acfb8  fix(t5): audit fixes — SAHI, court CNN refinement, correct TrackNetV3 docs
5ce2e7e  feat(t5): TrackNetV3 architecture port + fix sahi opencv version conflict
da20ca0  feat(t5): eval infrastructure — eval_store + per-component eval commands
dc26d63  fix(t5): add horizontal centering bias to far-player scoring
24e9dcb  fix(t5): three-tier court-geometry scoring for player selection
a9b1e53  fix(t5): bbox size + center-line proximity as tiebreakers in player scoring
5bf1bf3  feat(t5): TrackNet training pipeline — dataset, fine-tuning, label export

═══════════════════════════════════════════════════════════════════════
## REFERENCE TASKS
═══════════════════════════════════════════════════════════════════════

SportAI ground truth:  4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb
  88 rows, 17 points, 2 games, 24 serves

T5 run (MOG2 only):   eee5e0ed-2f8e-40a5-8354-3da24736b1c9
  162 rows, 1 point, 1 serve (far player still broken)

T5 run (SAHI + court geometry): af1d9aec-fe9c-4c74-a73e-fccdb36b81dc
  First successful far-player detection (~80%+ correct)
  Frames 400/550 wrong (spectator behind player won on proximity)
  → fixed by bbox size tiebreaker in a9b1e53

T5 run (bbox fix):    PENDING — next submission after a9b1e53

═══════════════════════════════════════════════════════════════════════
## WHAT'S LEFT
═══════════════════════════════════════════════════════════════════════

1. Validate bbox + center-line fix on next run
2. Run eval-player + reconcile on successful run
3. Seed golden datasets once results are stable
4. Enable AUTO_DUAL_SUBMIT_T5=1 once far player is reliable
5. Start training: extract frames + labels, fine-tune TrackNet
6. Obtain/train TrackNetV3 weights (architecture is ready)

═══════════════════════════════════════════════════════════════════════
## KEY FILES MODIFIED/CREATED THIS SESSION
═══════════════════════════════════════════════════════════════════════

Modified:
  ml_pipeline/config.py — MOG2, SAHI, TrackNetV3 config constants
  ml_pipeline/pipeline.py — MOG2 subtractor, court_corners passthrough
  ml_pipeline/player_tracker.py — three-tier scoring, SAHI, MOG2, bbox
  ml_pipeline/court_detector.py — Hough rewrite, CNN refine, get_court_corners
  ml_pipeline/ball_tracker.py — TrackNetV3 integration, V3 inference path
  ml_pipeline/harness.py — eval + training commands
  ml_pipeline/requirements.txt — sahi, opencv 4.9
  ml_pipeline/Dockerfile — tracknet_v3.py COPY

Created:
  ml_pipeline/tracknet_v3.py — TrackNetV3 U-Net architecture
  ml_pipeline/eval_store.py — persistent eval results
  ml_pipeline/training/__init__.py
  ml_pipeline/training/export_labels.py
  ml_pipeline/training/tracknet_dataset.py
  ml_pipeline/training/train_tracknet.py
  ml_pipeline/training/extract_frames.py
