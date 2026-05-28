# Next-session pickup — 2026-05-28 (close 6) — L1+L4+L5 Batch perf + ADR-01 v1 trained + calibration fail-loud SHIPPED

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (close 6)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. Identity `100%` unchanged. `bench_swing_type` STOPGAP (no weights). **`bench_bounce` v1 trained but recall pool-limited (3-5%); baseline INTENTIONALLY NOT LOCKED — see Stream 3.**
**Match 4:** RECOVERED — corpus #4 landed all 3 kinds (664 ball / 397 stroke / 114 serve). **BUT calibration-corrupt: all 273 floor labels unusable for bronze-feature training until Match 4 is re-ingested post-calibration-fix.**

**What shipped this session — THREE parallel streams:**

### Stream 1: L1+L4+L5 Batch perf — SHIPPED + DEPLOYED + JOB-DEFS LIVE
- `024bb40` L1 player-stage GPU batching (PLAYER_BATCH_SIZE env, default 1)
- `b25b356` L4 ROI ViTPose batching + FP16 (ROI_BATCH_SIZE / ROI_POSE_FP16 env)
- `c660845` L5 NVENC GPU transcode (Dockerfile installs nvenc-enabled ffmpeg from BtbN; Python auto-falls back to libx264 if nvenc fails; TRANSCODE_CODEC env)
- Docker image rebuilt, pushed to eu-north-1 + us-east-1 ECR
- AMD64 sub-manifest digest: `sha256:9dfb66c33296a3daf73c945d6223be422966dacd6ff5a0e748e32dfbf4697b20`
- **Job-defs registered:** eu-north-1 rev **54**, us-east-1 rev **36** — both pin the new digest AND set `PLAYER_BATCH_SIZE=8 + ROI_BATCH_SIZE=16` env vars (ROI_POSE_FP16 deliberately NOT set; defer with L3).
- Equivalence verified locally: `.claude/tmp/test_player_batch_equiv.py` shows `_run_yolo_batch([f]*N)` per-frame-identical to N separate `_run_yolo(f)` calls (max box diff 0.000000).

### Stream 2: Calibration fail-loud safety net (C) — SHIPPED + DEPLOYED
- `eec1dae` Render-side fail-loud in `_do_ingest_t5`: if 0% of bronze rows have court_x populated (≥100 row threshold both sides), mark `ingest_error='calibration_degenerate_no_court_coords'`, set `last_status='failed_calibration'` + `ingest_finished_at=now()`, skip silver/serve/stroke/notify, return False. Also logs WARN when ball-court coverage <20% (currently 5 of 15 recent matches show 25-32%).
- Pure Render — auto-deployed; `/healthz` 200.
- Catches match-4-class catastrophic failures retroactively. Doesn't fix the root cause (degenerate homography locking); that's the Batch-side companion (A)+(B) — STILL TODO.

### Stream 3: ADR-01 v1 bounce_detector training infra + first trained weights + ceiling identified
- `a2bf4b8` `feat(t5): ADR-01 v1 bounce_detector training infra (dataset + trainer + bench --weights-path)` — 4 files, 871 insertions / 3 deletions.
  - `ml_pipeline/bounce_detector/dataset.py` — `build_manifest()` mines positives + 5× negatives. Positives use the audit's strict gate (is_bounce ±5 fr + ≤50 px) where it fires, fall back to SA `bounce_frame_est` otherwise.
  - `ml_pipeline/training/train_bounce_detector.py` — AdamW + cosine warmup + BCELoss + WeightedRandomSampler + per-epoch (loss / acc / prec / rec / F1 / PR-AUC) + early-stop on F1 or PR-AUC. Wrapped `{state_dict, meta}` save format.
  - `ml_pipeline/bounce_detector/cnn.py` — `load_weights()` accepts both bare state_dict AND wrapped trainer output.
  - `ml_pipeline/diag/bench_bounce.py` — `--weights-path` flag.
- v1 weights trained end-to-end on Match 1's 67 floor labels (50 epochs, early-stop epoch 6, val F1=0.40). Match 4's 273 floor labels deliberately excluded — calibration corruption (100% NULL `court_x`) poisons feature channels 0/1/7/8/9/10. **Bench at thresholds 0.3/0.4/0.5/0.55/0.7: recall 3-5% flat; precision climbs 1.2% → 8.3%.**

**The ceiling insight (the load-bearing finding from this session):** the detector's production candidate pool is `ball_detections WHERE is_bounce=TRUE` — TrackNet's velocity-reversal heuristic. Per `.claude/adr01_label_audit_2026-05-28.md`, only **6/67 (9%)** of SA floor labels have a matching is_bounce candidate within ±5 frames + ≤50 px. So even a perfect model on the existing candidate pool maxes out at ~9% recall. **More training data won't fix this** (the corpus could grow to 10,000 labels and recall would still hit the 9% wall). The right lever is **candidate generation**: swap the `is_bounce` filter in `_candidate_frames_from_raw_bounces` (currently `detector.py:177`) for a sliding-window peak detector on the gravity-residual feature (already computed in `feature_extractor.py` channel 6 — that's the most discriminative single feature per ADR-01, fitted against a parabolic prior). Expected after candidate-generation lift: recall ceiling 9% → ~50% (ball-coverage limited). THEN retraining is meaningful. Memory: [project_t5_may28_bounce_v1_candidate_ceiling](memory).

**Baseline NOT locked.** 3-5% recall would be a misleading floor that future work could trivially "beat" without actually improving anything. Lock the baseline after the candidate-generation work lands and we have honest numbers.

## Match 4 saga + recovery — full story for context

Match 4 (`ca475740-9e34-49c3-9b59-0194bfa37013`, Tomo vs Jimbo Ma, 47.9-min 1080p, 71915 frames) was the corpus #4 T5-side that started at 12:08 UTC and was supposed to complete in ~4h.

**Why it timed out:** Hit 6h Batch hard cap at 18:08 UTC (exit 137 SIGKILL, `statusReason: "Job attempt duration exceeded timeout"`). Three layered slow spots — exactly the L1/L4/L5 targets:
- Main loop: 3h 41m (184 ms/frame on 71915 frames — YOLOv8x-pose @ batch=1 on T4)
- ROI extract: 63 min (ViTPose @ batch=1)
- libx264 transcode: 30-40+ min on T4's 4 vCPUs, still running when killed

**Calibration degeneracy on top of that:** CNN returned 0 keypoints across 300-frame calibration window → Hough fallback locked a homography with H_diag `[21.43, 0.05]` (degenerate) that passed inliers/confidence gates. Every `to_court_coords()` projection returned None. **All 23,796 ball + 52,433 player rows had court_x=NULL.** `roi_pose: scanned 65243 sampled frames, 0 detections, 0 usable poses in 7736.8s` — 2h of GPU compute finding nothing because the projected ROI was 45×40 pixels.

**Recovery (in this session):**
1. Found `bronze_s3_key` was set BEFORE timeout (export_bronze_to_s3 wrote 4.5MB gz at 18:07:10, 1 min before kill). Bronze IS in S3.
2. UPDATE `vaj.status='complete'` (truthful — bronze IS complete; transcode died).
3. Backdated `vaj.updated_at` to 10 min ago so sweep cron's age gate passes.
4. Cron fired ≤5 min later → Render ingest read bronze.json.gz → DB populated → silver build ran (0 rows due to NULL court coords — see below) → stroke/serve/swing-type ran → SES email sent → **corpus auto-land hook fired and emitted all 3 kinds**.
5. Result: `training_corpus` for match 4 = `ball_position 664, stroke_classifier 397, serve 114` — exactly the predicted volumes. **ADR agent fully unblocked.**

**Silver=0 root cause investigated:** documented in `docs/_investigation/court_calibration_silent_degeneracy.md` (NEW). Match 4 is THE ONLY 0% case in the last 60 days of T5 matches, but 5 of 15 show sub-50% ball-court coverage. Trend is real on newer/longer videos. Memory: `project_t5_calibration_degeneracy.md`.

## What's NOT done (next-session work)

| # | Item | Owner | Effort | Notes |
|---|---|---|---|---|
| 1 | **VALIDATE L1+L4+L5 perf** on the next real T5 upload | next session | passive | The next user-uploaded T5 match lands on eu-north-1 rev 54 with `PLAYER_BATCH_SIZE=8 + ROI_BATCH_SIZE=16` active. Expected ms/frame to drop from ~183 → ~75-110 (conservative). Read `ms_per_frame` + `batch_duration_sec − processing_time_sec` from `ml_analysis.video_analysis_jobs`. Target full match ~2.5-3h vs match 4's 6h. |
| 2 | **L3 YOLO FP16** | next session | ~1h code + Docker rebuild | Code: flip `predict(half=True)` on full-frame YOLOv8x-pose + SAHI's YOLOv8m. **Needs its own bench cycle** — FP16 can shift detection counts near thresholds (`config.py:158` documents this). Validate on bench + a real long match before promoting. Trips BATCH-SIDE CHECKLIST. |
| 3 | **L7 G5.xlarge (A10G)** | next session | ~1-2h infra | No code. AWS Batch CE + Job Queue + JD updates. Quota check first (`aws service-quotas get-service-quota` for G-family vCPU in eu-north-1; if 0, request increase). G5.xlarge ~$1.006/h vs G4dn.xlarge $0.526/h but ~2× FP16 throughput so cost-per-job ~flat. Adds 2-3× hardware speedup on top of all software levers. |
| 4 | **Calibration fix (A) + (B)** | next session | ~2-3h with deploy | (A) H_diag sanity gate in `court_detector.py` — reject homographies where `\|H[0,0]\|` or `\|H[1,1]\|` outside `[0.1, 5.0]`. (B) post-lock projection self-test — project 4 court corners, reject if any falls outside frame. Batch-side, trips checklist. Bundle both in same Docker rebuild. Fix doc: `docs/_investigation/court_calibration_silent_degeneracy.md` §Fix options. |
| 5 | **Calibration (D) — CNN keypoints returning 0** | research | 1-3 days | Investigate why ResNet50 court-keypoint CNN returns 0 keypoints on this video's camera angle. Likely training-data gap. Defer until A/B/C are in. |
| 6 | **ADR-01 v1+: gravity-residual peak detector for candidate generation** | next session | ~2-4h | Render-side only — `bounce_detector/detector.py::_candidate_frames_from_raw_bounces` (line 177). Lifts recall ceiling from TrackNet's 9% bounce-flag coverage to the ~50% ball-coverage limit. Uses the gravity-residual feature already computed in `feature_extractor.py` channel 6 — sliding-window peak detection on it. Then **retrain v1** on the expanded positive set (most SA labels will now have a candidate). Per the bounce ceiling memo — **do not chase more training data first**; it can't move recall through the 9% wall. |
| 7 | **Re-ingest Match 4 post-calibration-fix** | next session | passive after #4 | `ca475740`'s 273 floor labels (audit's biggest haul) are corpus-landed but bronze-features-corrupt (NULL `court_x`). Once Fix (A)+(B) ships and we re-ingest Match 4 from the existing `bronze.s3_key`, those 273 labels become real training data and bounce v1 corpus jumps 67 → 340. Combined with #6 this is when bounce recall should jump 5% → 30-40%. |

## Coordination with other agent

ADR agent shipped + pushed in parallel this session:
- `a2bf4b8` ADR-01 v1 bounce_detector training infra (dataset + trainer + `bench --weights-path`)
- `8c6a1af` ADR-02 v1 swing-type model scaffold (model + dataset + detector + train + bench)
- Earlier (from close-5): `d4d6580` ADR-02 swing-type dataset builder

Their work is ADR detector-build / training. My work is Batch perf + Render-side safety net. **No file overlap.** All commits land cleanly on `main`.

## How to validate L1+L4+L5 when a fresh T5 match lands

```sql
SELECT task_id, total_frames, processing_time_sec, ms_per_frame,
       EXTRACT(EPOCH FROM (batch_end_at - batch_start_at))/60 AS batch_min
  FROM ml_analysis.video_analysis_jobs
 WHERE status = 'complete' AND batch_start_at > NOW() - INTERVAL '24 hours'
 ORDER BY batch_start_at DESC;
```

Look for: `ms_per_frame ≤ 110` (vs 184 baseline) AND `batch_min ≤ 180` (vs ~290 match 4). Bench MUST still be green — run `python -m ml_pipeline.diag.bench`.

CloudWatch grep for the new env-driven log lines:
- `player_sub ... batch_size=8` in `player_tracker._sub_seconds`
- `roi_pose: ViTPose on cuda fp16=False batch_size=16` (FP16 deliberately off; flip ROI_POSE_FP16=1 later if needed)
- `Complete: ... in X.Xs` from `_transcode_to_mp4` — expect <120s with NVENC working

If NVENC failed and fell back to libx264, log will say: `NVENC transcode failed ... falling back to libx264`. Doesn't break, but means the Dockerfile overlay didn't take.

## Files you'll touch on day 1

For L3: `ml_pipeline/player_tracker.py` (`predict(half=True)` flags). For L7: AWS console only. For (A)+(B): `ml_pipeline/court_detector.py`. All trip BATCH-SIDE CHECKLIST except L7.

## Background ADR work continues

`training_corpus` totals after match 4 lands:
- ball_position: **1,483** labels (819 + 664)
- stroke_classifier: **1,172** labels (775 + 397)
- serve: **232** labels (118 + 114)

ADR-02 wants ~2-3k swing labels for v1 training; we're at 1,172 — needs ~5-10 more matches. ADR-01 wants the floor labels for bounce training (411-684 today depending on re-submits). ADR-03 v1 already at 100% bench. ADR-04 still blocked on ADR-01 + ADR-02.

---

## Detailed deploy state — for reference

**Code:**
- `024bb40` L1, `b25b356` L4, `c660845` L5, `eec1dae` fix (C) — all pushed to `origin/main`.

**Docker image:**
- Manifest list: `sha256:5801adfb479d6a23c9092843137b3b2b6e3d5d896fbff8844482150109b2e68f`
- AMD64 sub-manifest: `sha256:9dfb66c33296a3daf73c945d6223be422966dacd6ff5a0e748e32dfbf4697b20`
- Tagged + pushed to both regions.

**Job-defs (BOTH register the new image AND env vars):**
- `ten-fifty5-ml-pipeline:54` (eu-north-1) — PLAYER_BATCH_SIZE=8, ROI_BATCH_SIZE=16, retryStrategy preserved
- `ten-fifty5-ml-pipeline:36` (us-east-1) — same

**Render:**
- Auto-deployed all four commits. `/healthz` 200. Ingest worker auto-deployed too.

**Bench gates:**
- Serve bench: a798eff0 20/24, 880dff02 23/24 — green pre-L1, green post-L1 (refactor preserves sync path), green post-L4, green post-L5.

**Match 4 final state (`ca475740-...`):**
- `vaj.status = complete`, `stage = transcoding` (DB shows the killed stage), `ms_per_frame = 191.7`
- `bronze.submission_context.last_status = completed`, `ingest_finished_at = 18:26:55`, `ses_notified_at = 18:26:55`, `session_id = ca475740-...`
- silver.point_detail = 0 (calibration degenerate)
- training_corpus: 3 kinds × 1 row × {664, 397, 114} labels

**This session's commits (newest first):**
```
eec1dae fix(t5/ingest): fail-loud on degenerate court calibration (no court coords)
c660845 perf(t5/batch): L5 NVENC GPU transcode (env-gated TRANSCODE_CODEC + Dockerfile ffmpeg overlay)
b25b356 perf(t5/batch): L4 ROI ViTPose batching + FP16 (env-gated ROI_BATCH_SIZE / ROI_POSE_FP16)
024bb40 perf(t5/batch): L1 player-stage GPU batching (env-gated PLAYER_BATCH_SIZE)
```
