# T5 ML Pipeline — Operational Handover

**Last updated:** 2026-04-19
**Owner:** Tomo
**This is the single authoritative doc for T5.** CLAUDE.md now points here. Old handovers (`handover_t5_current.md`, `handover_serve_detector_build.md`) were folded in on 2026-04-18.

---

## Status

Pipeline is operational end-to-end: court calibration ✓, player identification ✓, ball tracking (near half only) ✓, **pose-first serve detection deployed**. Baseline reference `081e089c`: near-player serves hit 86% recall vs SportAI with 0.05 s mean timestamp error (validated offline against locally-extracted pose). Current Batch image `sha256:dd6c4e1e24da...c3c` — eu-north-1 revision 30, us-east-1 revision 19 (pushed 2026-04-18).

**Remaining gaps, in priority order:**

1. **Far-player serve detection** — ~10% recall. Bottleneck is bronze-side TrackNet missing ~half of far-half serve bounces. Next step: local ball extraction, or retrain TrackNet on dual-submit labels.
2. **Umpire interference in Player 1 slot** — `var_y=104` on reference task. Path-length filter catches most but not all. See A8 in session log.
3. **Serve bucket / side tuning** — serve_bucket_d over-counts T, under-counts wide. Pass 3 x-thresholds in `build_silver_v2.py` need calibration.

---

## Architecture at a glance

```
video.mp4 (S3)
      │
      ▼
┌──────────────────────────────────────────────────┐
│  ml_pipeline/ (AWS Batch, GPU)                   │
│  ┌──────────┐  ┌─────────┐  ┌────────┐          │
│  │ court_   │  │ ball_   │  │ player_│          │
│  │ detector │  │ tracker │  │ tracker│          │
│  └────┬─────┘  └────┬────┘  └───┬────┘          │
│       └─────────────┴───────────┘                │
│                     ▼                            │
│          ml_analysis.* (bronze)                  │
│          ball_detections, player_detections,     │
│          court_detections, video_analysis_jobs   │
└──────────────────────────────────────────────────┘
                     │
                     ▼  (Render webhook-server, _do_ingest_t5)
┌──────────────────────────────────────────────────┐
│  ml_pipeline/serve_detector/                     │
│  pose-first for near player                      │
│  bounce-first for far player                     │
│  rally-state gate                                │
│                     ▼                            │
│          ml_analysis.serve_events                │
└──────────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────┐
│  build_silver_match_t5.py                        │
│  (consumes serve_events + ml_analysis.*)         │
│                     ▼                            │
│          silver.point_detail  (model='t5')       │
└──────────────────────────────────────────────────┘
                     │
                     ▼
             gold.* views  →  API  →  dashboards
```

**Split of responsibilities:**

| Layer | Runs on | Writes | Iteration speed |
|---|---|---|---|
| ML detection (court/ball/player) | AWS Batch GPU | `ml_analysis.*` | ~47 min / run; needs Docker rebuild |
| Serve detection | Render main API | `ml_analysis.serve_events` | ~10 s / run; silver rerun |
| Silver build | Render main API | `silver.point_detail` | ~10 s / run |
| Gold views | Render main API (boot) | `gold.*` views | Instant |

---

## Running the pipeline

### Local dev setup (Windows)

```bash
cd C:/dev/webhook-server
source .venv/Scripts/activate
pip install -r ml_pipeline/requirements.txt
# DATABASE_URL points at the Render prod DB by default.
```

### Fresh Batch run on a new video

Preferred: upload via Media Room `/media-room`, gated to `tomo.stojakovic@gmail.com`. Auto-ingest fires on completion. ~47 min total.

Manual submit (CLI):
```bash
aws batch submit-job --region eu-north-1 \
  --job-name t5-<short-desc> \
  --job-queue ten-fifty5-ml-queue \
  --job-definition ten-fifty5-ml-pipeline:30 \
  --parameters s3_key=wix-uploads/<name>.mp4,job_id=<NEW_UUID>
```

On spot-capacity failure, failover to us-east-1 with `--job-definition ten-fifty5-ml-pipeline:19`.

### Re-run only silver (fast iteration, no Batch cost)

Use this when iterating on serve_detector or silver builder code:
```bash
python -m ml_pipeline.harness rerun-silver <task_id>
python -m ml_pipeline.harness reconcile 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb <task_id>
```

### Re-run full ingest (rebuild bronze too)

Only needed if bronze `ml_analysis.*` was cleared or if switching image:
```bash
python -m ml_pipeline.harness rerun-ingest <task_id>
```

---

## Validation

### Quick sanity pass

```bash
python -m ml_pipeline.harness validate <task_id>         # bronze + silver presence
python -m ml_pipeline.harness eval-court <task_id>       # court confidence, keypoint error
python -m ml_pipeline.harness eval-ball <task_id>        # detection rate, bounce count, speed
python -m ml_pipeline.harness eval-player <task_id>      # count, coord variance, path length
python -m ml_pipeline.harness eval-serve <task_id>       # precision/recall vs SportAI ground truth
```

`eval-serve` targets: precision ≥ 90%, recall ≥ 85%, mean ts error < 1 s.

### Full reconcile vs SportAI ground truth

```bash
python -m ml_pipeline.harness reconcile 4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb <task_id>
# Modes: --mode=summary|coverage|distributions|speed|rows (default: all)
```

### Visual — serve contact sheet

```bash
DATABASE_URL=... python -m ml_pipeline.diag.serve_viewer <task_id> \
    --video ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \
    --output ./diag_<tid>
```

Produces per-serve 3-frame strips (toss / contact / bounce), an overhead-not-serve contact sheet, and a combined contact sheet. Use for eyeball validation of detector events.

### Diagnostic — pose coverage probe

```bash
python -m ml_pipeline.diag.pose_gap_probe
```

Samples 20 frames spanning the match, runs YOLOv8x-pose locally, reports whether the model finds the near player with usable keypoints. Used to distinguish "pipeline bug" from "model limitation" when pose data is sparse.

### Diagnostic — prod pose audit (H1/H2/H3 discriminator)

```bash
DATABASE_URL=... python -m ml_pipeline.diag.prod_pose_audit \
    --task <task_uuid> --start-frame 4500 --end-frame 6000 --every 5
```

Sequential-read YOLOv8x-pose (matching Batch's `VideoPreprocessor.frames()` iteration exactly) vs `ml_analysis.player_detections` for the same frame_idx range. Also compares against `cap.set(POS_FRAMES, N)` seek-read on every sample to catch keyframe-seek mismatches (H3). Emits per-frame "interesting row" table, aggregate stats, and hypothesis verdict. See P0 below for interpretation.

### Offline serve-detector validation

For iteration without Batch rebuild, extract pose locally and run the detector in-memory:
```bash
python -m ml_pipeline.diag.extract_local_poses \
    ml_pipeline/test_videos/match_90ad59a8.mp4.mp4 \
    --output ml_pipeline/diag/local_poses_<tid>.jsonl --every 5
python -m ml_pipeline.serve_detector.validate_offline \
    ml_pipeline/diag/local_poses_<tid>.jsonl
```

### Unit tests

```bash
python -m ml_pipeline.serve_detector.tests.test_components
```

9 component tests (pose scoring, rally-state, ball toss, cluster peak picking).

---

## Docker & deploy

### Building the Batch image

Changes to anything that runs inside the Batch container (court/ball/player trackers, `pipeline.py`, `__main__.py`, `db_writer.py`) need a rebuild. Changes to `serve_detector/` or `build_silver_match_t5.py` do NOT — those run on Render which auto-deploys from `main`.

```bash
cd C:/dev/webhook-server
docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .
```

Image is ~5.9 GB. First build ~15-25 min, cached rebuilds ~1-3 min.

### Push to ECR (both regions)

```bash
ACCOUNT=696793787014

# eu-north-1 (primary)
aws ecr get-login-password --region eu-north-1 | \
  docker login --username AWS --password-stdin \
  $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com
docker tag ten-fifty5-ml-pipeline:latest \
  $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push \
  $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest

# us-east-1 (failover)
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
  $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com
docker tag ten-fifty5-ml-pipeline:latest \
  $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push \
  $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
```

Note the `sha256:...` digest from each `push` output — it's the manifest digest you'll pin in the Batch job def.

### Register new Batch job def revision

The existing job definition is pinned to a specific image digest — pushing `:latest` is NOT enough; you must register a new revision. Script (replace DIGEST):

```bash
DIGEST=sha256:<paste from push output>
for REGION in eu-north-1 us-east-1; do
  aws batch describe-job-definitions \
    --job-definition-name ten-fifty5-ml-pipeline \
    --status ACTIVE --region $REGION \
    --query 'jobDefinitions | sort_by(@, &revision) | [-1] | {jobDefinitionName: jobDefinitionName, type: type, containerProperties: containerProperties, retryStrategy: retryStrategy, platformCapabilities: platformCapabilities, propagateTags: propagateTags}' \
    > .claude/tmp_jobdef.json
  python -c "
import json
jd = json.load(open('.claude/tmp_jobdef.json'))
jd['containerProperties']['image'] = '696793787014.dkr.ecr.${REGION}.amazonaws.com/ten-fifty5-ml-pipeline@${DIGEST}'
for k in [k for k,v in list(jd.items()) if v is None]: jd.pop(k)
cp = jd.get('containerProperties', {})
for k in [k for k,v in list(cp.items()) if v is None]: cp.pop(k)
json.dump(jd, open('.claude/tmp_jobdef_new.json', 'w'), indent=2)
"
  aws batch register-job-definition --region $REGION \
    --cli-input-json file://.claude/tmp_jobdef_new.json
  rm .claude/tmp_jobdef.json .claude/tmp_jobdef_new.json
done
```

### Current deploy state

| Region | Revision | Image digest |
|---|---|---|
| eu-north-1 | **32** | `sha256:613c01376da7fdc631e7c5b5105bf202c3528ce9b61833526c8ecc432869d8ef` |
| us-east-1 | **21** | same |

Contents: rev 31 baseline (semantic-half assignment, tier-500 net-zone, etc.) + **YOLO_CONFIDENCE 0.25→0.10** (commit b66ad85) + **merged perf/sahi-skip-tighten B4+ skip rule** (merge commit 891b124, branch from 190fd62). This is the first image to combine both fixes.

Prior revs deprecated:
- rev 31 / 20 (`5798437b9ba01...`): semantic-half fix but conf=0.25 → suffered the density blocker.
- rev 30 / 19 (`dd6c4e1e24da...`): scoring fixes but NO semantic-half fix (ID-swap bug present).

### Quota note

On-demand G4dn vCPU quota is **zero** in both regions (confirmed 2026-04-15 via `VcpuLimitExceeded`). Prod is Spot-only despite on-demand being listed as fallback in the job queue. Manual region failover when Spot capacity is tight. Quota increase has been planned but not yet requested. Full playbook: `.claude/playbook_aws_batch_ondemand_fallback.md`.

---

## Training

All training workflows depend on dual-submit pairs: the same video processed by both SportAI (ground truth) and T5 (student). SportAI timestamps + labels become supervision for T5 detection.

### Dual-submit

```bash
python -m ml_pipeline.harness dual-submit <sportai_task_id>
```

Submits the original video to T5 Batch without re-uploading. Produces a second silver row set you can reconcile against.

### Stroke classifier (optical flow, far player)

Weights location: `ml_pipeline/models/stroke_classifier.pt` (auto-loaded by `StrokeClassifier` at pipeline runtime when present).

```bash
# Export training data from a clean dual-submit pair
python -m ml_pipeline.harness export-stroke-data \
    --sportai-task <sa_tid> --t5-task <t5_tid> \
    --video <path> --output <dir>

# Train
python -m ml_pipeline.harness train-stroke --data <dir> --epochs 50
```

Target accuracy: 75-85% on 200+ labelled examples (Mora CVPR-W 2017 pattern). Five clean dual-submit matches should suffice.

### TrackNet retraining

`ml_pipeline/training/` — dataset builder, trainer, label exporter:
```bash
# Export ball labels from T5 detections + SportAI hits
python -m ml_pipeline.harness export-ball-labels <task_id> <out.json>
python -m ml_pipeline.harness export-sportai-labels <task_id> <out.json>

# Extract training frames from video or S3
python -m ml_pipeline.harness extract-frames <video_or_s3> <out_dir> [--fps 25]

# Training runs inside training/train_tracknet.py — see its docstring.
```

**TrackNetV3** architecture is ported in `ml_pipeline/tracknet_v3.py`. Activates automatically when `ml_pipeline/models/tracknet_v3.pt` exists. Weights not yet trained — blocked on clean dual-submit data for the far-half ball problem.

### Training-bench (alignment analysis, not training)

```bash
python -m ml_pipeline.harness training-bench align <sa_tid> <t5_tid> [--window 1.0]
python -m ml_pipeline.harness training-bench serves <sa_tid> <t5_tid>
python -m ml_pipeline.harness training-bench features <sa_tid> <t5_tid>
python -m ml_pipeline.harness training-bench extract-serves <sa_tid> <t5_tid> [--csv PATH]
```

Matches events by timestamp, reports coverage/precision/recall per field, dumps raw rows for manual inspection.

---

## File index

### Detection pipeline (runs in Batch container)

| File | Purpose |
|---|---|
| `__main__.py` | Entry point — `python -m ml_pipeline --job-id X --s3-key Y` |
| `pipeline.py` | Orchestrates court → ball → motion → player per frame |
| `config.py` | All tunable constants (intervals, thresholds, court geometry) |
| `video_preprocessor.py` | Frame metadata + iterator |
| `court_detector.py` | CNN (14 keypoints) + Hough fallback + geometry validation + calibration lock |
| `camera_calibration.py` | Radial (Brown-Conrady k1/k2) + piecewise-homography lens calibration |
| `ball_tracker.py` | TrackNetV2 (9-channel) + frame-delta Hough fallback + 3-tier heatmap extraction |
| `tracknet_v3.py` | TrackNetV3 architecture port; activates when weights present |
| `player_tracker.py` | Multi-strategy detection (YOLOv8x-pose + SAHI + YOLOv8m-det) + 3-tier court-metre scoring |
| `heatmaps.py` | Rally / serve / bounce heatmap renderer |
| `bronze_export.py` | Write bronze JSON to S3 for archive |
| `db_schema.py` | DDL for `ml_analysis.*` tables |
| `db_writer.py` | Bulk-insert ball/player/job rows into `ml_analysis.*` |

### Serve detection (runs on Render, silver-build time)

| File | Purpose |
|---|---|
| `serve_detector/__init__.py` | Public API: `detect_serves_for_task`, `ServeEvent`, `SignalSource` |
| `serve_detector/models.py` | `ServeEvent` dataclass + `SignalSource` enum |
| `serve_detector/schema.py` | DDL for `ml_analysis.serve_events` (idempotent) |
| `serve_detector/pose_signal.py` | Silent Impact 2025 passive-arm scoring; cluster + peak selection |
| `serve_detector/rally_state.py` | HMM-style {pre_point, in_rally, between_points} state machine |
| `serve_detector/ball_toss.py` | Optional rising-ball confirmation (boosts conf, never rejects) |
| `serve_detector/detector.py` | Orchestrator — pose-first near, bounce-first far, signal fusion |
| `serve_detector/validate_offline.py` | In-memory runner against local pose JSONL (no DB writes) |
| `serve_detector/tests/test_components.py` | 9 component tests |

### Silver (T5 variant)

| File | Purpose |
|---|---|
| `build_silver_match_t5.py` | Match silver builder. Reads `ml_analysis.*` + `serve_events`, shares passes 3-5 with `build_silver_v2.py` (in repo root — used by SportAI too) |
| `build_silver_practice.py` | Practice silver builder (serve_practice + rally_practice). 3-pass SQL |

### Ingest / bronze

| File | Purpose |
|---|---|
| `bronze_ingest_t5.py` | Downloads gzipped JSON from S3 into `ml_analysis.*` |
| `api.py` | Flask blueprint — ops-key-protected ML job status + result S3 retrieval |

### Harness / test / validation

| File | Purpose |
|---|---|
| `harness.py` | Swiss-army CLI — validation, reconcile, rerun, training-bench, eval-*, export, training |
| `eval_store.py` | Persists eval run results to `ml_pipeline/eval_history.jsonl` |
| `recon_silver.py` | Lower-level reconcile logic used by `harness reconcile` |
| `training_bench.py` | Event alignment + feature analysis used by `harness training-bench` |
| `test_pipeline.py` | End-to-end pipeline smoke test on local video |

### Training (stroke classifier + TrackNet)

| File | Purpose |
|---|---|
| `training/export_labels.py` | Extract ball/SportAI labels from DB |
| `training/extract_frames.py` | Pull frames from video/S3 matching `ball_detections.frame_idx` |
| `training/tracknet_dataset.py` | PyTorch Dataset — 3-frame windows → Gaussian heatmap labels |
| `training/train_tracknet.py` | Freeze encoder, train decoder, BCELoss |
| `stroke_classifier/flow_extractor.py` | Farneback dense optical flow on bbox crops ±5 frames around hits |
| `stroke_classifier/model.py` | StrokeFlowCNN — ~50 k params 3D-CNN, 5-class |
| `stroke_classifier/train.py` | Training loop with augmentation |
| `stroke_classifier/export_training_data.py` | Aligns SportAI GT with T5 player_detections from dual-submit pairs |

### Diagnostics (dev tools)

| File | Purpose |
|---|---|
| `diag/serve_viewer.py` | Visual contact sheets — 3-frame strips per serve |
| `diag/pose_gap_probe.py` | Local YOLOv8x-pose sampling to diagnose pose-coverage gaps (uses seek — superseded for Batch-comparison by `prod_pose_audit`) |
| `diag/extract_local_poses.py` | Full-video local pose extraction → JSONL (dev-only; seek-based, see handover P0) |
| `diag/repro_pose_gap.py` | Apr 18 probe that ran detect_frame on seek-read frames; superseded by `prod_pose_audit` |
| `diag/prod_pose_audit.py` | Sequential-read YOLO vs `ml_analysis.player_detections` — discriminates H1/H2/H3 density-gap hypotheses |
| `diag/query_detections.py` | Standalone DB dump of player_detections rows for a task + frame window. Avoids heredoc quoting issues from Render shell |
| `diag/replay_detect_frame.py` | Replay `detect_frame()` locally on target frames with instrumented `_choose_two_players` that logs every candidate's tier/motion/baseline/bbox/pose/total score breakdown. Used to discriminate H1 (container-specific) from H2 (scoring bug) |

### Root-level touchpoints (not in ml_pipeline/)

| File | Purpose |
|---|---|
| `upload_app.py::_do_ingest_t5` | Orchestrates bronze → serve_detector → silver → trim → SES |
| `upload_app.py::_t5_submit` | Submits new T5 tasks to AWS Batch |
| `build_silver_v2.py` | Shared silver derivation (passes 3-5). T5's silver builder calls into this |
| `gold_init.py` | Gold views (`gold.vw_point` filters `model='t5'` for T5 runs) |
| `video_pipeline/video_trim_api.py` | Trim silver events to highlight video |

---

## Reference data

| Purpose | Task ID / path |
|---|---|
| **Baseline T5** (validated 2026-04-16) | `081e089c-f7b1-49ce-b51c-d623bcc60953` |
| **SportAI ground truth** | `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` (88 rows, 24 serves: 14 near + 10 far) |
| Reference video (S3) | `s3://nextpoint-prod-uploads/wix-uploads/1776237770_match.mp4` |
| Reference video (local) | `ml_pipeline/test_videos/match_90ad59a8.mp4.mp4` (50.8 MB) |
| Pre-serve-detector snapshot | 2026-04-16 handover table, pinned in `memory/project_t5_apr17_serve_detection_root_cause.md` |

---

## Known issues + next priorities

### P0 — Near-player density (DIAGNOSED — awaiting conf=0.10 rebuild verification)

**Status 2026-04-19 afternoon:** H2 ruled out, H1 confirmed by exclusion. Fix deployed in code (commit b66ad85, `YOLO_CONFIDENCE` 0.25→0.10). Verification task `6a9bce49-6a65-4d28-a0d1-42bab5f2fcee` running after Docker rebuild. See `memory/project_t5_apr19_density_blocker.md` for full diag trail.

**Rev 31 validation on `f181aaf7-6862-4364-bd03-7e92ff5346e9` (2026-04-19) — partial success, new blocker found.**

ID-swap fix validated:
- Player 0 correctly = near player in every minute (court_y 23.0-26.0)
- When Player 0 is detected, pose coverage is 92-100%

But serve detection still = 1/14 near, 0/10 far (F1 5.6% — same as rev 30). Why: near-player **detection rate itself** is ~2% of frames in minutes 3-6 (the most rally-active minutes):

| Minute | pid=0 detections / 1500 frames | % | Pose when detected |
|---|---|---|---|
| 0 | 648 | 43% | 99% |
| 1 | 124 | 8% | 92% |
| 2 | 169 | 11% | 96% |
| 3 | 29 | **2%** | 100% |
| 4 | 27 | **2%** | 48% |
| 5 | 27 | **2%** | 81% |
| 6 | 22 | **2%** | 77% |
| 7 | 197 | 13% | 100% |
| 8 | 77 | 5% | 86% |
| 9 | 261 | 17% | 96% |

Task: **find out why YOLOv8x-pose detects the near player in only ~2% of middle-minute frames** when a local `_run_yolo(frame)` call on the same video at the same frame indices produces strong pose output (verified in `ml_pipeline/diag/repro_pose_gap.py` Apr 18).

Three hypotheses to discriminate:
1. **Pipeline preprocessing differs** — `pipeline.py` or `player_tracker.detect_frame` may transform/crop the frame before `_run_yolo` in ways my isolated probe doesn't. Check: YOLO imgsz path, any letterbox/pad, any BGR/RGB conversion drift, SAHI's crop margin interaction.
2. **Semantic-half filter isn't seeing the pose-carrying bbox** — despite my Apr 18 `_assign_ids` rewrite taking biggest-area-per-half, something upstream may discard pose bboxes before `_assign_ids` is called. Check: after `_choose_two_players` runs, does the returned candidate list actually include the pose-carrying YOLO output? Add per-frame logging.
3. **cv2 seek vs sequential read** — my local probe seeks via `cap.set(CAP_PROP_POS_FRAMES, N)` which may land on a keyframe near N, not N itself. Batch reads sequentially. Same frame_idx may correspond to different actual frames. **Easy validation:** modify `repro_pose_gap.py` to read sequentially (not seek), compare output.

**First diag delivered 2026-04-19:** `ml_pipeline/diag/prod_pose_audit.py`. Iterates the video sequentially with VideoPreprocessor's exact fps-downsampling math (so yielded_idx here == Batch's `frame_idx` in `ml_analysis.player_detections`), runs YOLOv8x-pose at prod imgsz/conf, AND reads the same yielded_idx via `cap.set(POS_FRAMES, ...)` to directly test H3 (pixel-level comparison of the two reads). Queries `ml_analysis.player_detections` for the matching (task, frame) and classifies each sample as MATCH / LOCAL>DB / both-empty / DB>LOCAL.

Run:
```bash
DATABASE_URL=... python -m ml_pipeline.diag.prod_pose_audit \
    --task f181aaf7-6862-4364-bd03-7e92ff5346e9 \
    --start-frame 4500 --end-frame 6000 --every 5
```

Script prints per-frame table for "interesting" rows (gap or H3 diff), aggregate stats, and a hypothesis verdict. Detail JSON written to `ml_pipeline/diag/prod_pose_audit_<task8>.json` for follow-up analysis. ~300 YOLO runs at default stride; allow 5-10 min on CPU, ~1 min on GPU.

**Verdict interpretation:**
- H3 signal: `SEEK vs SEQUENTIAL pixel content differs` > 10% → the Apr 18 offline validation was comparing mismatched frames; redo offline eval with sequential iteration.
- H1/H2 signal: `LOCAL found near-pose, DB missing pid=0` > 30% → raw YOLO sees the player but Batch dropped it. Next step: replay `detect_frame()` locally with `court_corners` + `to_court_coords` pulled from `ml_analysis.court_detections` — if local detect_frame ALSO returns empty → H2 (scoring); else → H1 (Batch-container-specific, e.g. GPU nondeterminism or image/weights drift).
- Neither: local YOLO also misses the near player → genuine model-capability issue (motion blur / occlusion / camera pan), not a pipeline bug.

**Do NOT** tune pose-scoring rules until density is understood — pose rules are only relevant if we have enough samples to cluster.

### P1 — SAHI perf merge DEPLOYED (2026-04-19 autonomous session)

Merged `perf/sahi-skip-tighten` (commit 190fd62 → merge commit 891b124) into main. Docker rebuilt, pushed to both ECRs (digest `sha256:613c01376da7fdc631e7c5b5105bf202c3528ce9b61833526c8ecc432869d8ef`), registered as rev 32 (eu) / rev 21 (us). Verification run submitted: task `1e191bb5-a2da-400e-ab9d-c755587e859f`, Batch job `2a1a2c76-2924-4007-bdff-c0150e7c9b7e`.

Expected behaviour on verification run:
- Total runtime: ~35-40 min (vs 47 min before, ~600s saved from SAHI skip).
- PlayerTracker diag: `kept_0`, `kept_1`, `kept_2` within ±2% of 6a9bce49 (1.4%, 34%, 64.6%).
- `sahi_skipped` counter non-zero (was 0 in 6a9bce49 because skip rule wasn't deployed yet).
- Handler should not trigger auto-ingest on this task — direct Batch submit with a fresh UUID (no submission_context row), which is fine for CloudWatch-only verification.

### P2 — Far-player serve detection (still on the list, but blocked by P0 now)

Bronze TrackNet misses ~half of far-half serve bounces. Same plan as before (local ball extraction), but it's no longer the critical path — we need near-player detection density first.

### P3 — Serve bucket calibration

Cosmetic. Defer until P0 + P2 land.

### Realistic roadmap to 24/24 TP (revised 2026-04-19)

1. **P0 — near-player density fix (days)** → unblocks near-player serves → ~12/24 TP
2. **P1 — SAHI perf merge (hours)** → reduces Batch runtime 47→30 min but no accuracy change
3. **P2 — local ball extraction (1-2 days)** → unblocks far-player serves → ~20/24 TP
4. **P3 — bucket + edge tuning** → final polish → ~22-24/24 TP

Estimated: 1-2 weeks from now to 24/24. The density issue may turn out to be quick (hypothesis 3) or deep (hypothesis 1) — we won't know until we diagnose it.

---

## Session log (reverse chronological)

### 2026-04-19 afternoon (autonomous session) — SAHI merge verified: 27% faster, 5% kept_2 regression

Stretch goal run: task `9fe8c096-09b6-44f8-bceb-ab9185e24ca9` (Batch `7df43765-7718-4740-86f8-d849fd2f8845`) on rev 32 (conf=0.10 + SAHI skip merged). Ran 2069.7s vs 6a9bce49's 2842s — **27% faster, 12.7 min saved**. SAHI skip rate came in at **76.6%** (exceeding the handover's 57% projection).

PlayerTracker diagnostics comparison:

| metric | 6a9bce49 (pre-merge) | 9fe8c096 (merged) | delta |
|---|---|---|---|
| frames_yolo_ran | 3096 | 3060 | −1% |
| avg candidates/frame | 12.77 | 4.82 | −62% |
| kept_0 | 1.4% (44) | 0.0% (0) | −100% ✓ |
| kept_1_single_cand | 32.5% (1006) | 33.1% (1014) | +0.6% |
| kept_2 (both players) | 64.6% (2000) | **59.6% (1823)** | **−5.0%** |
| kept_1_span_fail | 1.5% (46) | 7.3% (223) | **+5.8%** |
| SAHI skip rate | 0% | 76.6% | — |

**The key regression**: 177 frames moved from `kept_2` to `kept_1_span_fail`. That's frames where both halves have candidates surviving _choose_two_players, but their pixel y-span is below the 378 px min — so the far candidate is dropped as a bench-sitter-style false positive. Before the merge, SAHI almost always contributed extra small-bbox candidates in the far half; with SAHI now skipped on 76.6% of frames (trigger: full-frame pose found in both halves), the real far-player bbox from SAHI is sometimes missing when YOLO full-frame misses it, and the best-scoring "far" candidate ends up being a mid-court artifact that the span check correctly rejects.

**This is a net win but not a zero-regression change**. Trade-off summary:
- ✓ Runtime: 47.4 → 34.5 min (27% reduction, great for iteration speed).
- ✓ Pre-existing `kept_0` gap closed: 1.4% → 0%.
- ✗ Far-player `kept_2` dropped 5% because fewer SAHI-sourced far candidates are available.

**Two paths for follow-up**:
1. **Accept the 5% regression** and consider it a fair cost for 27% runtime savings (and the 1.4% kept_0 win partially offsets). The impact is bounded to far-player detection.
2. **Tighten the SAHI skip rule** so it runs more often — e.g. require BOTH pose-spanning AND metric-far-baseline conditions (currently an OR). Would drop skip rate from 76.6% to maybe 40-50% (still a win over 0%), likely restoring kept_2 while keeping most perf gains.

Prod rev 32/21 (digest `613c01376da7fdc631e7c5b5105bf202c3528ce9b61833526c8ecc432869d8ef`) is currently deployed. If (2) becomes preferred, it's a config tweak + rebuild.

### 2026-04-19 afternoon (autonomous session) — conf=0.10 verified on Batch run 6a9bce49

Batch job `8bb77cf9` (task `6a9bce49-6a65-4d28-a0d1-42bab5f2fcee`) completed in 2842s (47 min). PlayerTracker final diagnostics from CloudWatch log stream `ml-pipeline/default/df19af458b8a444bb3a3b08eb3138db1`:

```
frames_yolo_ran: 3096
avg candidates/frame: 12.77
kept_2 (both players):  2000  (64.6%)
kept_1_single_cand:     1006  (32.5%)
kept_0 (nothing):         44  (1.4%)
kept_1_span_fail:         46  (1.5%)
```

The conf=0.10 fix landed decisively — 97%+ of YOLO-run frames now produce at least one valid player candidate vs f181aaf7's ~2% in rally-dense minutes. Early CloudWatch samples from minutes 0-1 showed **100% full>=1 frames** before CloudWatch ingestion lag cut off visibility.

Follow-up items noted during this session:
- Serve-recall number needs DB access to measure — Tomo runs `python -m ml_pipeline.harness eval-serve 6a9bce49-6a65-4d28-a0d1-42bab5f2fcee` + reconcile on Render.
- `pid=1` junk fallback bug found while querying f181aaf7: when `to_court_coords` returns None (projection failed — spectator outside calibrated bounds), `_choose_two_players` sets `score = motion_bonus` which can be 500, passing `MIN_SELECTABLE_SCORE = 500` and assigning pid=1 to whatever moves in the upper half. Clean fix: `score = 0.0` when `court_xy is None`. Deferred to not confound conf=0.10 verification.

### 2026-04-19 afternoon — Density gap diagnosed: H2 ruled out, H1 confirmed, conf=0.10 fix deployed

Three-diag sequence nailed down the cause of the near-player density gap on `f181aaf7`:

1. **`ml_pipeline/diag/prod_pose_audit.py --local-only`** — sequential-read YOLO on 300 target frames:
   - Local YOLO near-pose: **189/300 (63%)** frames have a pose-carrying near-half bbox.
   - Seek vs sequential pixel diff: **0/300** → **H3 ruled out** (video is 25fps source at 25fps target, no downsampling ambiguity).
   - 189 frames where local YOLO succeeds but DB stored nothing → proves pipeline dropped them.

2. **`ml_pipeline/diag/query_detections.py`** — raw DB dump around frames 4745-4800:
   - Only 3 rows across 56 frames. When pid=0 IS stored, it has pose. So Batch's scoring picks the pose bbox when anything at all comes through — the gap is "nothing comes through", not "wrong thing wins".
   - pid=1 junk fallback with NULL court coords confirms semantic-half `_assign_ids` accepting spectators as "far player" when real one isn't detected. Separate issue, lower priority.

3. **`ml_pipeline/diag/replay_detect_frame.py`** — full detect_frame() replay locally with instrumented `_choose_two_players`:
   - On frames 4750, 4780, 4800: local pipeline (YOLOv8x-pose + SAHI + court calibration + pixel-polygon gate) **KEPT the near player with pose on all 3 target frames**.
   - Scores 3941 / 2997 / 2968 — near pose bbox wins its half by ~600+ points.
   - Pixel-polygon gate never trips (pixel_dist +99, -36, -113 vs the -300 threshold).
   - **H2 definitively ruled out** — the scoring logic + pixel-polygon gate are correct.

**Verdict: H1 (Batch-container-specific).** Same code + weights + imgsz + conf threshold produces the bbox on local CPU FP32 but Batch GPU misses it. Leading theory: **GPU FP16 inference suppressing pose detections near the 0.25 YOLO_CONFIDENCE threshold**.

**Fix deployed (commit b66ad85):** `YOLO_CONFIDENCE` lowered 0.25 → 0.10. Tier-based scoring + pixel-polygon gate continue to filter non-player noise downstream. New Docker image must be built + pushed to both ECRs + registered as new Batch job def revision before verification task `6a9bce49-6a65-4d28-a0d1-42bab5f2fcee` runs with the fix active.

**If 6a9bce49 does NOT fix density:** threshold wasn't the cause — next move is CloudWatch `dedup_detail frame=XX full=Y` analysis to see raw GPU YOLO box counts, and an ultralytics version pin audit against the Docker image.

### 2026-04-19 morning — Rev 31 validated, new density blocker, SAHI branch ready

**Rev 31 validation on `f181aaf7-6862-4364-bd03-7e92ff5346e9`** (submitted by user 04:22 UTC, completed 05:14 UTC):
- ID-swap fix ✅ — Player 0 correctly = near player in every minute, pose coverage 92-100% when detected.
- Serve detection ❌ — still 1/14 near, 0/10 far. F1 = 5.6%, same as rev 30.
- Root cause: **near-player detection density** is ~2% of frames in minutes 3-6. Not a swap, not a scoring bug — YOLOv8x-pose just isn't finding the near player in most rally frames. See P0 for hypothesis set + first-step diag plan.
- Previous f3433ffc (Apr 18 submission) FAILED at 11% with "Host EC2 terminated" — Spot eviction. Retried as f181aaf7 this morning.

**SAHI optimization branch** (`perf/sahi-skip-tighten`, commit `190fd62`):
- Background agent delivered B4+ skip-rule tightening. 57% skip rate on 100-frame bench, 0 detection regressions.
- Rule: skip SAHI if EITHER (A) full-frame YOLOv8x-pose has pose-carrying candidates in both halves with size gates (near ≥40 px, far ≥20 px, ±5% midline dead zone), OR (B) any candidate projects via strict=False `to_court_coords` to court_y ∈ [-10, 5] m.
- Expected ~600s off 47-min runs. HELD on merge — see P1.

### 2026-04-18 evening — ID-swap root cause + rev 31 deploy

- Validated rev 30 run (`6421211e`). Result was disappointing: 1/24 TP, F1 5.6%, same pattern as pre-fix baseline.
- Dug in: Player 0 in minutes 1-4 had avg_court_y=0 (was supposed to be ~23.5 near baseline), bbox width 33 px (real near player is 131 px). Player 1 had court_y=23 and had pose. **IDs were swapped** during minutes 1-4.
- Root cause: old IoU-based `_assign_ids` had a swap-lock mode. When both players were lost for PLAYER_TRACK_TIMEOUT_FRAMES and re-init saw only the far player, the far player got pid=0 by "highest pixel-y first" (it was the only bbox). IoU matching locked the swap in for subsequent frames until another timeout.
- Fix: replaced IoU-based assignment with **semantic-half assignment** — pid=0 if bbox center cy > frame_height/2, else pid=1. Pure function, no state, no swap possible. Biggest bbox per half wins on collision.
- Verified locally against the same probe frames — all now correctly assign pid=0 to near player with pose.
- Built new image `sha256:5798437b9ba0...`, pushed to both ECRs, registered revision 31 (eu) / 20 (us).
- Batch job `f3433ffc-...` submitted 19:07 UTC on rev 31. Morning validation: see P0.

### 2026-04-18 morning/afternoon — Serve detector deployed (rev 30)

- Image `sha256:dd6c4e1e24da...` built, pushed, registered (eu rev 30, us rev 19). [Superseded by rev 31 — this rev had scoring fixes but NOT the ID-swap fix.]
- New module `ml_pipeline/serve_detector/` (pose-first architecture per Silent Impact 2025 + TAL4Tennis + Springer 2024 literature).
- Three scoring fixes in `player_tracker.py` for the net-zone tier (tier-500, MIN_SELECTABLE_SCORE 1000→500, pose_bonus +300).
- `db_writer.py` adds `detection_source` column for future diagnostics.
- `harness.py` eval-serve command + `_do_ingest_t5` wiring.
- 9 component tests, all passing.
- Offline validation on locally-extracted pose: near-player 12/14 TP (86%), overall 13/24 TP, 0.05 s mean ts error — proved the detector works, implied the problem was pose-data-starvation in DB, which turned out to be the ID-swap bug (caught in the evening dig above).

### 2026-04-17 — Root-cause dig

- Bronze ball bounces found to be 98% near-half biased → silver was attributing far-half bounces' hitter to the wrong player.
- Pose coverage gap identified: Player 0 had 0% keypoints during minutes 1-5 of baseline — full-frame YOLOv8x-pose was finding the near player mid-court but the tracker's cascade rejected those detections as tier-0 (off-court).
- Decision (user): full ground-up rebuild, pose-first, no silver touching.

### 2026-04-16 — Baseline `081e089c` = PASS

- Apr 15-16 work landed: lens calibration (radial, RMS 6.26 px), player ID fixes, A0 `strict=False`, ball speed unit fix (km/h), A3b p75-over-window.
- Silver validation passed for the first time. Reconcile showed remaining serve detection gap (17/24) — triggered the Apr 17 investigation.

### 2026-04-15 — Lens distortion breakthrough

- Radial calibration locked at RMS 6.26 px on MATCHI wide-angle footage. Court + player mapping ~95% correct. See `memory/project_t5_apr15_breakthrough.md`.

Earlier context (pre-calibration) is in `memory/project_t5_*.md` — kept for reference; don't re-read unless investigating historical regressions.

---

## Troubleshooting index

| Symptom | File / check |
|---|---|
| Player IDs swapped (Player 0 at far baseline, Player 1 at near) | `player_tracker.py::_assign_ids` — semantic-half assignment should prevent this since rev 31. If recurring, check whether `frame_height` is being passed correctly from `detect_frame` |
| Player 1 `var_y > 50` | Umpire interference — umpire at net (court_y≈11-12) sometimes wins far-slot. Path-length filter in `pipeline.py` catches most. See P2 |
| Near-player serves missing (after rev 31) | `serve_detector/pose_signal.py::find_serve_candidates` — tune cluster size / arm-extension threshold (30 px default) |
| Too many false-positive serves | `serve_detector/detector.py::_detect_bounce_based_serves_far` — tighten bounce-first gates |
| Rally state misclassifying | `serve_detector/rally_state.py::state_at` — adjust `idle_threshold_s` |
| Pipeline not producing pose | `player_tracker.py::_choose_two_players` — check tier assignment for mid-court (tier-500 added rev 30) |
| `ml_analysis.serve_events` missing | `serve_detector/schema.py::init_serve_events_schema` — auto-created on first use |
| Batch job uses old image | `aws batch describe-job-definitions` — confirm revision pinned to current digest |
| Ball speeds look wrong | `ball_tracker.py::assign_peak_flight_speeds` — p75 over 15-frame window logic |
| Court calibration fails | `camera_calibration.py::fit_calibration` — check RMS threshold 10 px |
| Far-player serves missing (after rev 31) | Bronze ball-bounce sparsity on far half. See P1 — needs local ball extraction or TrackNet retrain |
