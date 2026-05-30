# Next-session pickup — 2026-05-30 (early AM) — RUNTIME OPTIMIZATION DEPLOYED ✅ big wins in prod; 2 ROI bugs to code-fix for sub-1h-CLEAN

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. `bench_calib` 4/4. bounce fps-fix validated (M4 0%→26.7%, M1 22.4%) — baseline NOT locked.
**What landed overnight:** the runtime-optimization deploy (job-defs **eu rev 58 / us rev 40**, image `7f1998a5` / amd64 `sha256:1722156a…`) RAN IN PROD on match 4. **Main loop 70.4→48.8 ms/frame**, **court locked piecewise 88% / player court_x 98.7%** (calibration polish proven in prod), **CPU/GPU overlap confirmed** (8.5 ms/fr hidden). Plus the **fps=60 bounce-corpus fix** (committed `f081bcd`+`d2d4182`, validated).
**What's NOT done:** TWO env-triggered ROI bugs dropped far-pose + ROI bounces in run #1. The **clean re-run** (`ab814d65`) with both levers rolled back via env (`ROI_POSE_FP16=0`+`ROI_BOUNCE_BATCH=1`) **SUCCEEDED** — complete bronze (far-pose 47974 rows, ROI bounces 12144/604, court 88%, 0 errors) — BUT total 2h37m because the rollback made the ROI sweep slow (pose 67m fp32 + bounce 86m @ batch=1).
**Honest sub-1h verdict:** NOT yet for a COMPLETE run. Main loop is ~58m (near target); the ROI far-pose+bounce sweep is the wall, and its optimizations ARE the 2 bugs. **Next session's job:** code-fix the 2 ROI bugs (`pose.py` fp16 cast + `bounces.py` OOM mgmt → re-enable fp16-pose + bounce-batching) → ONE daylight rebuild → re-run; that + further ROI cost reduction (far-pose sampling / memory headroom) is the sub-1h-CLEAN path. Full analysis: `docs/_investigation/match4_opt_run_2026-05-30.md`.

If that's enough, go. Depth below.

---

## ✅ The optimization deploy — what shipped and what it proved (run #1, job `540000b4`, rev 58)
All four new levers ON (`PIPELINE_STAGE_OVERLAP=1`, `MOG2_DOWNSCALE=2`, `SAHI_BATCHED=1`, `ROI_BOUNCE_BATCH=8`) + preserved (`YOLO_FP16`, `ROI_POSE_FP16`, `ROI_BATCH_SIZE=16`, `PLAYER_BATCH_SIZE=8`, `BALL_BATCH_SIZE=8`, g5/A10G). Carries calibration `0ceec5b`+`8356237` + fps fix.

- **Main loop 48.8 ms/frame** (was 70.4) = 58.5 min pipeline / **72.5 min total** (was ~2h12). 1.8× faster.
- **Court LOCKED piecewise 88%, conf 0.93** → bronze **player court_x 98.7%**, ball 66.1%. The self-test rejected the early `cov=0%` degenerate candidates then locked sound. **Calibration polish proven in prod.**
- **Overlap works**: `overlapped_hidden ≈ 8.5 ms/fr` (cv2 releases GIL on the base image — gate #3 PASS).
- Stage profile FINAL: player 43% (17.6 ms/fr), ball 32% (13.2), MOG2 23% (9.3, only ~38% hidden), court 2%, +~16% unaccounted overhead.

## 🐞 TWO ROI bugs (env-rolled-back for the clean re-run; CODE FIX = next rebuild)
1. **`ROI_POSE_FP16=1` → `array type dtype('float16') not supported`** at frame 176 → **far-pose 0 rows** (pass dropped, non-fatal). Fix in `ml_pipeline/roi_extractors/pose.py`: cast fp16 array → float32 before the cv2/numpy op (keep model fp16). **⚠️ was also ON in rev 57 — verify rev-57 far-pose wasn't silently dropping too.**
2. **`ROI_BOUNCE_BATCH=8` → CUDA OOM (900 MiB)** at frame 553 → **ROI bounces 0 rows** (dropped). A10G near-full with all models resident. Fix in `ml_pipeline/roi_extractors/bounces.py`: `torch.cuda.empty_cache()` before the sweep + cap default batch (try 2–4) + try-smaller-on-OOM fallback.

Tonight's clean re-run (`ab814d65`) overrode `ROI_POSE_FP16=0` + `ROI_BOUNCE_BATCH=1` (env only, no rebuild) to get a complete bronze. **Check its result first thing** (job below).

## Sub-1h status — close, not there for a CLEAN run
Main loop alone is 58.5 min. A clean run needs the ROI-bounce batching WORKING (at batch=1 the ROI bounce pass is ~25 min → blows the budget). **Fixing the OOM so batch=4–8 works is the #1 lever for sub-1h-clean.** Memory pressure is the theme — a GPU memory audit unlocks the batching levers. Ranked opportunities in the investigation doc.

## In-flight / jobs (NOT auto-ingested — manual Batch submits)
- Run #1 bronze (main only): `s3://nextpoint-prod-uploads/analysis/540000b4-…/bronze.json.gz` (ball=23796, player=62206). `ml_analysis` empty for it by design.
- Clean re-run **SUCCEEDED**: T5 `ab814d65-dbaf-4db9-8c6a-75bec136d4c8`, Batch `ad240ed2-…`. **Complete bronze** at `s3://nextpoint-prod-uploads/analysis/ab814d65-…/bronze.json.gz` — main + far-pose (47974) + ROI bounces (12144/604 bounces), court 88%/player 98.7%, 0 errors. This is a SUPERIOR match-4 bronze vs the corpus's current `ca475740` (84%). **NOT ingested** — manual submits have no auto-ingest + no `submission_context`, so `/ops/sweep-t5-orphans` won't pick it up. To land in `ml_analysis`/silver, trigger ingest manually. **Daylight option:** refresh the corpus match-4 entry from this better bronze → bounce re-bench (currently corpus uses `ca475740`; don't double-count match 4).
- **Corpus task `ca475740` was NOT touched** (ran under fresh job_ids — `ball/player_detections` inserts have no delete/ON CONFLICT, so reusing a job_id DOUBLES rows).

## ⚠️ Operational finding — overnight g5 capacity
Clean re-run sat RUNNABLE ~8.5 h (22:15Z→06:43Z) before a g5 freed up. The g4dn/Spot fallback in the queue didn't pick it up. Worth checking CE config / capacity strategy for overnight runs.

## Open items
| # | Item | Notes |
|---|---|---|
| 1 | **Code-fix the 2 ROI bugs → rebuild → re-run** | `pose.py` fp16 cast + `bounces.py` OOM mgmt. The sub-1h-CLEAN target. BATCH-SIDE CHECKLIST. |
| 2 | **Verify clean re-run `ab814d65`** | far-pose + ROI bounces present this time; court coverage ~88%+. |
| 3 | Ingest a clean run → `ml_analysis`/silver → optional corpus refresh + bounce re-bench | local; don't corrupt `ca475740`. |
| 4 | Lambda **function** code deploy (`update-function-code`) | the rev-58 job-def fixed the stored-command half; the Lambda path still needs deploy to restore DIRECT S3 uploads. |
| 5 | Optimization levers (ranked) | `docs/_investigation/match4_opt_run_2026-05-30.md` §"opportunities". |

## What I did NOT do overnight (by the rules)
- No Batch rebuild overnight (`feedback_overnight_branch_only`) — the ROI code fixes are documented + staged for daylight.
- Did not commit the Batch-side ROI fixes to `main` (they need daylight rebuild + validation).
- Did not refresh the corpus / retrain (bounce fps-fix train+bench already done; don't touch `ca475740`).

## Deploy state
- Image `7f1998a5` (amd64 `sha256:1722156a48577d9c3b852c285b3979cccf5590ef676316a27a4782e6e754aa38`), **job-defs eu rev 58 / us rev 40** (all-4 flags + eu stored-command fixed to args-only). Rev 57/39 superseded.
- g5 queue (g5→g4dn→Spot). Serve bench GREEN. `main` HEAD = `d2d4182`.

---
**END OF PICKUP**
