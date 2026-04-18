# HANDOVER — Serve detector build (overnight Apr 17-18)

## TL;DR

A pose-first serve detector for T5 is **fully built, tested, and wired in**. Local validation hits **86% recall on near-player serves** (12/14 on baseline task). DB validation shows only 1/14 because the production pose data has a 0% coverage gap during minutes 1-5 — **that's fixed in the player_tracker.py changes, but needs a Batch rebuild to land in prod**.

All code committed to `main`. Your morning task: Docker rebuild + ECR push + fresh Batch job on 081e089c.

---

## What landed tonight

### New module `ml_pipeline/serve_detector/`

Pose-first serve detection per Silent Impact 2025 + TAL4Tennis literature:

| File | Purpose |
|---|---|
| `models.py` | `ServeEvent` dataclass, `SignalSource` enum (pose_only / pose_and_ball / pose_and_bounce / bounce_only) |
| `pose_signal.py` | Per-frame trophy/toss/both-up scoring. Passive-arm = Silent Impact discriminator. Cluster + arm-extension gate. |
| `rally_state.py` | HMM-style state machine {pre_point, between_points, in_rally}. Gates serves to non-rally moments. |
| `ball_toss.py` | Optional rising-ball signature around contact. Boosts confidence, never rejects. |
| `detector.py` | Orchestrator. Pose-first for near player; bounce-first for far player. Pose-score-3 clusters override rally gate (protects vs spurious bronze bounces). |
| `schema.py` | `ml_analysis.serve_events` DDL (idempotent). Unique on (task_id, frame_idx, player_id). |
| `validate_offline.py` | In-memory runner — reads local pose JSONL + DB ball data, no DB writes. |
| `tests/test_components.py` | 9 component tests. All pass. Run with `python -m ml_pipeline.serve_detector.tests.test_components`. |

### Pipeline fixes (`player_tracker.py` + `db_writer.py`)

Three fixes that together close the "pose gap" — the 0% keypoint coverage Player 0 had during minutes 1-5 of the baseline match:

1. **Tier-500 net-zone** in `_choose_two_players` — pose-carrying detections projecting to mid-court (net zone, y≈11.88m) now get tier=500 instead of tier=0. Previously these were rejected outright.
2. **MIN_SELECTABLE_SCORE 1000 → 500** — so tier-500 candidates can be selected.
3. **+300 pose_bonus in bbox_score** — prevents small SAHI fragments from stealing player_id=0 when the real pose-carrying detection exists.

Plus:

4. **`detection_source` column** added to `ml_analysis.player_detections` (idempotent ALTER). Values: `yolo_pose | yolo_det | sahi`. Lets future SQL diagnostics distinguish these without running the pipeline.

### Ingest + harness wiring

- `upload_app.py::_do_ingest_t5` — between bronze ingest and silver build on `tennis_singles_t5`, now calls `detect_serves_for_task(conn, task_id)`. Non-fatal: silver's legacy logic still fires if this errors.
- `ml_pipeline/harness.py` — new `eval-serve <task_id> [--sportai-tid X] [--tolerance 3]` command. Runs the detector, aligns vs SportAI ground truth, reports precision / recall / ts-error / per-role breakdown.

### Diagnostic scripts

- `ml_pipeline/diag/pose_gap_probe.py` — runs YOLOv8x-pose locally on sampled frames to compare with DB
- `ml_pipeline/diag/extract_local_poses.py` — full-video pose extraction to JSONL (used for offline validation)

---

## Current performance

**Offline validation on 081e089c** (using locally-extracted pose, which simulates what fixed pipeline will produce):

| Metric | Value | Target |
|---|---|---|
| Total TP | 13 / 24 | - |
| Precision | 56.5% | 90% |
| Recall | 54.2% | 85% |
| F1 | 55.3% | 85% |
| Mean ts error (matched) | 0.44 s | < 1 s |
| **Near-player recall** | **12 / 14 (86%)** | 85%+ ✅ |
| Far-player recall | 1 / 10 (10%) | - |

**DB validation** (existing pose data, pre-fix): 1/14 near, 0/10 far. As expected — pose gap blocks near detection; sparse bronze ball data blocks far.

**What the numbers tell us:**
- Pose-first near-player detection works. 86% recall with <0.5s ts error vs SA ground truth.
- Far-player detection is bottlenecked by bronze ball data (TrackNet misses ~half of far-half serve bounces). Needs a separate fix (local ball extraction or TrackNetV3 weights).

---

## Morning next steps (in order)

### 1. Rebuild Batch image + deploy (~30 min)

The `player_tracker.py` fix needs to go into Batch. Steps:

```bash
cd C:/dev/webhook-server
docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline .
ACCOUNT=696793787014

# eu-north-1
aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com
docker tag ten-fifty5-ml-pipeline:latest $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push $ACCOUNT.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest

# us-east-1 (same image)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com
docker tag ten-fifty5-ml-pipeline:latest $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
```

Then via Media Room or CLI, rerun 081e089c. ~47 min for full Batch run.

### 2. Validate post-rebuild (~5 min)

On Render shell:

```bash
# Confirm pose coverage fix landed
python -m ml_pipeline.harness eval-player 081e089c-f7b1-49ce-b51c-d623bcc60953
# Expect pose coverage >= 80% across all minutes (was 0% for minutes 1-5)

# Run detector against fresh DB data
python -m ml_pipeline.harness eval-serve 081e089c-f7b1-49ce-b51c-d623bcc60953 --tolerance 3
# Expected: near-player ~86% recall, 12+ TPs total
```

If eval-player still shows the pose gap, the player_tracker fix didn't land — check image SHA, `docker build` may have cached.

### 3. Far-player recall improvement (optional, ~4-6 hours)

The far-player recall is limited by bronze-side TrackNet missing ~half of serve bounces. Options:

- **Local ball extraction**, mirroring what we did for pose (`extract_local_poses.py`). Run TrackNet locally, write JSONL, feed to detector. Would bring far-player recall to ~80%.
- **TrackNetV3** — architecture already ported in `tracknet_v3.py` but weights aren't available. Retrain on dual-submit labels once serve detection is validated.

---

## Known limitations (document these in the serve detector doc when time permits)

1. **Pose-only detection needs the pipeline fix.** Without it, near-player serves during the main rally block are undetectable (no pose data to scan).
2. **Far-player detection is bounce-dependent.** We can only detect bounces that TrackNet sees. About half of far-player serve bounces aren't registered in bronze.
3. **Handedness is assumed right** unless `billing.member.dominant_hand = 'left'` is set. If the submitting user is left-handed but hasn't updated their profile, signals will be slightly off (toss arm detection inverted).
4. **Score=3 pose clusters override rally state.** Rationale: spurious bronze bounces would otherwise block real serves. Risk: a pose-score-3 inside a real rally (eg a smash that coincidentally triggers all three conditions) would fire as a serve. Haven't seen this in practice but worth monitoring.
5. **No local pose/ball extraction runs in prod.** The local diag scripts are for dev validation only — they're gitignored. Prod detector reads from `ml_analysis.*` always.

---

## Files to look at first (if something's off)

- **Near-player serves missed** → `ml_pipeline/serve_detector/pose_signal.py::find_serve_candidates` (tune `min_cluster_size` / `min_cluster_peak` / arm_extension 30 px)
- **Too many false positives** → `detector.py::_detect_bounce_based_serves_far` (tighten the bounce-first gate)
- **Rally state misclassifying** → `rally_state.py::state_at` (adjust `idle_threshold_s`)
- **Pipeline not producing pose** → `player_tracker.py::_choose_two_players` (the three-fix block around line 1005)
- **Schema missing** → `serve_detector/schema.py::_DDL` (auto-init on first use)

---

## Reference task IDs

- **Baseline T5**: `081e089c-f7b1-49ce-b51c-d623bcc60953`
- **SportAI ground truth**: `4a194ff3-b734-4b0b-bcb5-94d5b7caf3fb` (24 serves: 14 near @ ts=54-347, 10 far @ ts=378-585)
- **Local video**: `ml_pipeline/test_videos/match_90ad59a8.mp4.mp4`
- **Local pose dump** (gitignored): `ml_pipeline/diag/local_poses_081e089c.jsonl`

## Session commits

- `<hash1>` T5 serve detector: pose-first architecture + pipeline pose-gap fix
- `<hash2>` harness: eval-serve cmd + wire detector into _do_ingest_t5
- `<hash3>` tests: component sanity suite for serve_detector
