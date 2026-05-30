# Match-4 optimization deploy — first prod run on rev 58 (2026-05-30)

**Status:** Run #1 (`540000b4`) SUCCEEDED with big speed/calibration wins + 2 env-triggered ROI bugs.
Clean re-run (`ab814d65`) in flight with the 2 levers rolled back via env (no rebuild).

This is the result of the runtime-optimization deploy cycle: one rebuild from `main` (image
`7f1998a5`, amd64 `sha256:1722156a…`) → job-defs **eu rev 58 / us rev 40**, all four new levers
ON (`PIPELINE_STAGE_OVERLAP=1`, `MOG2_DOWNSCALE=2`, `SAHI_BATCHED=1`, `ROI_BOUNCE_BATCH=8`) plus the
preserved ones (`YOLO_FP16`, `ROI_POSE_FP16`, `ROI_BATCH_SIZE=16`, `PLAYER_BATCH_SIZE=8`,
`BALL_BATCH_SIZE=8`, g5/A10G). Carries the calibration polish (`0ceec5b` lock-best + `8356237`
projection-quality select) + the fps fix.

## Headline results (run #1, job 540000b4, 45-min/60-fps match)

| Metric | rev 57 (prior clean) | **rev 58 (this run)** |
|---|---|---|
| Main loop ms/frame | 70.4 | **48.8** (1.44× faster) |
| Main loop wall | ~2h12 total | **58.5 min** (pipeline) / 72.5 min total |
| Court lock | radial 84% | **piecewise 88%, conf 0.93** |
| Player court_x coverage | — | **98.7%** |
| Ball court_x coverage | — | 66.1% (expected — balls leave court plane) |
| Est. cost | — | $0.19 |

**Calibration proven in prod on the optimized image** — the projection self-test correctly rejected
the early `cov=0%` degenerate candidates ("keep searching") then `LOCKED after 2940 frames`. The
lock-best + projection-quality polish works.

**CPU/GPU overlap CONFIRMED working** — `overlapped_hidden ≈ 8.5 ms/fr saved`, non-zero → cv2
releases the GIL on this base image (verify-gate #3 PASS). Only ~38% of MOG2 is hidden though (MOG2
compute ≈ GPU window), so MOG2 is still 23% of wall.

### Stage profile (FINAL, total=2935 s of the 3510 s pipeline)
| Stage | Share | ms/fr | Note |
|---|---|---|---|
| **player** | 43% | 17.6 | SAHI fires 97.5% of frames (skip 430/16898); SAHI-batched shrank it but still #1 |
| **ball** | 32% | 13.2 | WASB @ BALL_BATCH_SIZE=8 |
| **motion_mask** | 23% | 9.3 | residual after overlap hid ~38% of MOG2; MOG2_DOWNSCALE=2 active |
| court | 2% | 0.7 | one-shot lock, then cached |
| _unaccounted_ | ~16% | — | 3510 s pipeline − 2935 s stage-timed = 575 s (frame decode/IO?) — worth profiling |

## 🐞 Two env-triggered ROI bugs (both env-rollback tonight; code fix = daylight rebuild)

### 1. `ROI_POSE_FP16=1` → float16 crash → far-pose = 0 rows
```
roi_pose: ViTPose on cuda fp16=True batch_size=16
roi_unified: pose.feed raised at frame 176 (dropping pose pass, no rows written):
             array type dtype('float16') not supported
```
The fp16 ViTPose output (a float16 array) reaches a downstream op (cv2 or numpy) that rejects
float16. Non-fatal by design (pass dropped), but **all far-player pose was lost**.
- **Daylight fix** (`ml_pipeline/roi_extractors/pose.py`): cast the keypoint/array to float32
  (`np.asarray(x, dtype=np.float32)` / `.float()`) before the unsupported op. Keep the model in fp16.
- **Tonight (env, no rebuild):** `ROI_POSE_FP16=0`.
- **⚠️ Verify:** `ROI_POSE_FP16=1` was ALSO on rev 57 → far-pose may have been silently dropping
  there too (the drop is non-fatal). Check rev-57's `player_detections_roi` (far_vitpose) coverage.

### 2. `ROI_BOUNCE_BATCH=8` → CUDA OOM → ROI bounces = 0 rows
```
roi_bounces: ROI_BOUNCE_BATCH=8 → batched TrackNet-V2 forward ENABLED
roi_unified: bounce.feed raised at frame 553 (dropping bounce pass, no rows written):
             CUDA out of memory. Tried to allocate 900.00 MiB
```
The batched TrackNet forward (8 window-frames) on top of all resident models (YOLOv8x-pose, ViTPose,
WASB, TrackNet, court CNN) exhausts the A10G (24 GB). 900 MiB alloc failed → memory was already near
full. Non-fatal (pass dropped), but **all ROI-refined bounces were lost**.
- **Daylight fix** (`ml_pipeline/roi_extractors/bounces.py`): `torch.cuda.empty_cache()` before the
  ROI sweep; cap default batch (try 2–4); add a try-smaller-on-OOM fallback so it degrades gracefully
  instead of dropping the whole pass.
- **Tonight (env, no rebuild):** `ROI_BOUNCE_BATCH=1` (sequential, proven, no OOM — but ~25 min).

## Are we sub-1h? Close on the main loop, not yet on the clean total

- Main detection loop is **58.5 min** (48.8 ms/fr) — nearly there for detection alone.
- A **clean** run (with working ROI passes) needs the ROI bounce batching to work: at batch=1 the ROI
  bounce pass is ~25 min, which alone blows the budget. **Fixing the OOM (so batch=4–8 works) is the
  #1 lever for a sub-1h CLEAN run.**
- Trim/NVENC adds ~7 min serially after detection.

## Run #2 — CLEAN re-run (job `ab814d65`, env rollback `ROI_POSE_FP16=0` + `ROI_BOUNCE_BATCH=1`)
Both ROI passes succeeded this time → **complete bronze**:
- `roi_pose: wrote 47974 rows (far_vitpose)` (fp16=False) · `roi_bounces: 12144 rows (604 bounces)` (batch=1)
- Main loop identical: 48.8 ms/frame, 0 errors. Court `LOCKED piecewise 88%`, player court_x 98.7%.
- **BUT total = 9430 s (2h37m)** — the ROI sweep alone was **91 min** (pose 67 min fp32 + bounce 86 min @ batch=1). The rollback that made it *correct* made it *slow*. Est $0.41.

**This is the crux for sub-1h:** the main loop is ~58 min (near target), but a COMPLETE run is dominated by the ROI far-pose + bounce sweep — and the optimizations for that sweep are exactly the two bugs above. Run #1 (ROI crashed = ~free) was 72 min; run #2 (ROI works but unoptimized) was 157 min. A true optimized-clean run sits between (~58 main + ~40 optimized-ROI + 7 trim ≈ ~105 min).

**Honest sub-1h verdict:** NOT yet achieved for a complete 47-min-match run. Huge progress (main loop 70→49 ms/fr, 2h12→ the loop nearly halved, calibration proven, fps fix done, complete clean bronze produced), but sub-1h-CLEAN needs (a) the 2 ROI bug-fixes to re-enable fp16-pose + bounce-batching AND (b) further ROI cost reduction (far-pose sampling rate / larger batches / GPU memory headroom).

## ⚠️ Operational: overnight g5 capacity
Run #2 sat RUNNABLE ~8.5 h (submitted 22:15Z, started 06:43Z) waiting for a g5; the g4dn/Spot fallback in the queue did not pick it up. Check CE min-vCPU / capacity strategy for overnight runs.

## Optimization opportunities (ranked — for the daylight cycle)

1. **Fix ROI-bounce OOM → re-enable batching** (`bounces.py`): the gating lever for sub-1h-clean.
   Memory pressure is the theme — the A10G is near-full with all models resident.
2. **GPU memory audit**: which models can be unloaded between phases or share memory; safe fp16 where
   it doesn't crash. Unlocks every batching lever (ball, ROI).
3. **Player stage (43%)**: SAHI fires 97.5% of frames. `SAHI_SKIP_A_FAR_YMAX=8.0` (coded, default 5.0)
   skips more SAHI on closer cameras (not match-4-far, but helps the fleet). Deeper: YOLO-pose imgsz.
4. **MOG2 (23%, only 38% hidden)**: `MOG2_DOWNSCALE=4` halves compute again; or skip MOG2 on
   non-detect frames. Overlap can't hide more because MOG2 compute ≈ the GPU window it overlaps.
5. **Ball (32%)**: `BALL_BATCH_SIZE=16` if memory allows (blocked by #2).
6. **Trim/NVENC (~7 min serial)**: overlap with bronze-export, or skip during validation (it's the
   review video, not analysis).
7. **The ~575 s unaccounted main-loop overhead** (16%): profile frame decode/IO.

## Data location
- Run #1 bronze (main detections only, no ROI): `s3://nextpoint-prod-uploads/analysis/540000b4-…/bronze.json.gz`
  (ball=23796, player=62206). **NOT ingested** — manual Batch submit has no auto-ingest trigger;
  `ml_analysis` is empty for this job_id by design.
- Corpus task `ca475740` was **left untouched** (ran under fresh job_ids to avoid the append-on-rerun
  hazard: `ball_detections`/`player_detections` inserts have no delete/ON CONFLICT for a job_id).
