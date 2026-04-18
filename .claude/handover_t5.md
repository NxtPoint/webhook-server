# T5 ML Pipeline — Operational Handover

**Last updated:** 2026-04-18
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
| eu-north-1 | **30** | `sha256:dd6c4e1e24da563b409a461ccfb549d08c272a025982812fcfcde779ce905c3c` |
| us-east-1 | **19** | same |

Contents: player_tracker pose-gap fix (tier-500 net-zone, MIN_SELECTABLE_SCORE=500, pose_bonus=300), db_writer detection_source column.

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
| `diag/pose_gap_probe.py` | Local YOLOv8x-pose sampling to diagnose pose-coverage gaps |
| `diag/extract_local_poses.py` | Full-video local pose extraction → JSONL (dev-only) |

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

### P0 — Validate Apr 18 Batch deploy on `6421211e-6d2e-4d3f-a188-5ec5916c3703`

Just submitted (2026-04-18). Image contains player_tracker pose-gap fix. Expected result on completion (~47 min): pose coverage ≥ 80% every minute of the match (was 0% for minutes 1-5 on the pre-fix baseline).

Run after completion:
```bash
python -m ml_pipeline.harness eval-player 6421211e-6d2e-4d3f-a188-5ec5916c3703
python -m ml_pipeline.harness eval-serve 6421211e-6d2e-4d3f-a188-5ec5916c3703
```

Target on `eval-serve`: near-player recall ≥ 80%, overall precision ≥ 70%.

### P1 — Far-player serve detection (blocked on ball data)

Current: ~10% recall. Bronze TrackNet misses ~half of far-half serve bounces. Two paths:
- **Local ball extraction** mirroring the pose extractor — run TrackNet locally on the full video, augment `ml_analysis.ball_detections` with high-recall detections. ~1 day of work.
- **Retrain TrackNet** on dual-submit labels. Weeks of iteration.

### P2 — Umpire interference filter (Player 1 var_y=104)

Path-length filter in `pipeline.py` catches most but not all. Motion-persistence over 3-5 seconds would finish it. Non-blocking — mostly affects rally-shot attribution, not serves (since far-player serves are detected from bounces, not pose).

### P3 — Serve bucket calibration

T5 over-counts T bucket (40 vs 4), under-counts wide (16 vs 43). Pass-3 `serve_bucket_d` CASE in `build_silver_v2.py` — x thresholds need tuning against real MATCHI court geometry.

---

## Session log (reverse chronological)

### 2026-04-18 — Serve detector deployed

- Image `sha256:dd6c4e1e24da...` built, pushed to both ECR repos, registered (eu rev 30, us rev 19).
- New module `ml_pipeline/serve_detector/` (pose-first architecture per Silent Impact 2025 + TAL4Tennis + Springer 2024 literature).
- Three pipeline fixes in `player_tracker.py` close the pose-coverage gap (tier-500 net-zone, MIN_SELECTABLE_SCORE 1000→500, pose_bonus +300).
- `db_writer.py` adds `detection_source` column for future diagnostics.
- `harness.py` eval-serve command + `_do_ingest_t5` wiring.
- 9 component tests, all passing.
- Offline validation on 081e089c: near-player 12/14 TP (86%), overall 13/24 TP, 0.05 s mean ts error.
- Batch job `6421211e-...` submitted for post-deploy validation.

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
| Near-player serves missing | `serve_detector/pose_signal.py::find_serve_candidates` — tune cluster size / arm-extension threshold |
| Too many false-positive serves | `serve_detector/detector.py::_detect_bounce_based_serves_far` — tighten bounce-first gates |
| Rally state misclassifying | `serve_detector/rally_state.py::state_at` — adjust `idle_threshold_s` |
| Pipeline not producing pose | `player_tracker.py::_choose_two_players` — check tier assignment for mid-court |
| `ml_analysis.serve_events` missing | `serve_detector/schema.py::init_serve_events_schema` — auto-created on first use |
| Batch job uses old image | `aws batch describe-job-definitions` — confirm revision pinned to current digest |
| Ball speeds look wrong | `ball_tracker.py::assign_peak_flight_speeds` — p75 over 15-frame window logic |
| Court calibration fails | `camera_calibration.py::fit_calibration` — check RMS threshold 10 px |
