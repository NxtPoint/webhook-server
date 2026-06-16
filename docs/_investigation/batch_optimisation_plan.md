# Batch GPU pipeline — sub-1-hour optimisation plan (player-stage focus)

> **SUPERSEDED 2026-06-03 by docs/_investigation/t5_runtime_backlog.md. Levers L1/L3/L4/L5/L7 shipped (rev 59). Frozen historical.**

**Tier:** REFERENCE / investigation
**Dated:** 2026-05-28
**Status:** PLAN — to be executed during Tomo's daylight hours, one commit per lever, bench-gated
**Supersedes (partially):** `docs/_investigation/t5_pipeline_speed.md` — that doc's static analysis was correct, but two of its top levers have since been pulled and the empirical result reframes which lever to chase next.
**Goal:** 45-min match (~67k frames @ 25 fps) in **< 1 h** wall time. **~3.5× speedup needed** without regressing detection quality.

---

## TL;DR

- **Current:** 183.2 ms/frame on the dominant 67k-frame regime (job `9378f2dd`, 27 May 26). The main per-frame loop eats ~3.41 h of the ~4.79 h Batch wall time; the rest is ROI ViTPose/TrackNet + transcode/upload.
- **Target:** ≤ 53.7 ms/frame *and* trim ~30 min off ROI/transcode → walltime ≤ 3 600 s on a 67k-frame match.
- **Dominant bottleneck:** the player stage — **YOLOv8x-pose @ imgsz=1280 + SAHI tiled inference, both running batch=1**. Ball batching (5317c50) was flat because ball was never the bottleneck — it was already off-budget compared to a 133 MB pose model run on a tile-fan of YOLOv8m every 5th frame. **Player-stage batching + SAHI rationalisation is the highest-ROI lever still on the table.**

---

## 1. Profile — empirical evidence from completed Batch runs

Pulled live from `ml_analysis.video_analysis_jobs` (`status='complete'`, ordered by `batch_end_at DESC`). All eu-north-1, fixed image, default `BALL_BATCH_SIZE=1` (the 27 May ball-batching commit is committed but the env flag has not been flipped).

### Long-match (67 k-frame) regime — the target

| Job (task_id prefix) | Date | total_frames | processing_sec | batch_sec | **ms/frame** | ROI+transcode (batch−proc) |
|---|---|---:|---:|---:|---:|---:|
| `9378f2dd` | 27 May 26 | 66 937 | 12 268.3 | 17 242.9 | **183.28** | **4 974.6 s** (1.38 h) |
| `c645a7ee` | 27 May 26 | 66 937 | 12 255.5 | 17 503.7 | **183.09** | 5 248.2 s (1.46 h) |

→ ms/frame is **stable at 183 ms** across re-runs of the same long match. Main loop is 71 % of wall, ROI + transcode + S3 is 29 %.

### Short-match (15.3 k-frame) regime — the bench fixture

| Job | Date | ms/frame |
|---|---|---:|
| `78c32f53` | 22 May 26 | 145.6 |
| `1d6feb3a` | 21 May 26 | 147.6 |
| `880dff02` | 7 May 26  | 167.5 |
| `a798eff0` | 27 Apr 26 | 168.4 |
| `6a9bce49` | 19 Apr 26 | 185.8 |
| `081e089c` | 16 Apr 26 | 187.8 |
| `fd623ed2` | 15 Apr 26 | 209.8 |

→ Short matches sit lower (no fully-warm MOG2 yet, fewer crowd-heavy frames, fewer SAHI-eligible frames). The headroom we need lives in the **long-match** regime.

### Target arithmetic

To hit < 1 h on the 67 k-frame match we need either:
- ms/frame ≤ **53.7 ms** (3.4× faster main loop), and roughly halve the post-main-loop ROI+transcode/upload (1.38 h → ~25 min), **or**
- An asymmetric mix: heavier cut to ROI than to main loop, e.g. ms/frame ≈ 75 (2.45×) + ROI/transcode ≈ 12 min.

Both are achievable on paper if Levers 1, 2, 3 below land.

### Cost per minute of video (sanity, not a goal)

`9378f2dd`: $0.756 / 44.6 min ≈ **$0.017/video-min** on Spot. On-demand will be ~3-4× this — still negligible against the value of dual-submit corpus throughput.

---

## 2. Per-stage profile — where the 183 ms goes

The pipeline **already instruments per-stage timings** (`pipeline.py::_log_stage_timings`) and a **player sub-breakdown** (`full_yolo`, `sahi`, `choose2`, `other`) — these print every 100 frames to CloudWatch. They're not stored in the DB. Pulling one of those log lines from any recent job is the single fastest way to ground the table below.

In the absence of a fresh log retrieval this session, the static profile (verified from code) is:

| Stage | What runs | Cadence | Est. per-detect-frame cost | Share of 183 ms/frame |
|---|---|---|---|---|
| **court** | ResNet50 keypoints | Every 30 fr for first 300 fr, then **homography LOCKED** (`court_detector` returns cached) | ~0 ms after frame 300 | **<1 %** — not a bottleneck |
| **ball** | WASB HRNet or TrackNet V2 (env-gated) at 512×288 / 640×360 | **every frame**, batch=1 (5317c50 committed, env-flag NOT flipped) | ~10-15 ms | ~6-8 % |
| **motion_mask** | MOG2 CPU bg-subtract | every frame | ~2-3 ms | ~1-2 % |
| **player** | YOLOv8x-pose @ imgsz=1280 full-frame + SAHI tile-fan (`SAHI_ENABLED=True`, 640×640 tiles, 15 % overlap, YOLOv8m per tile) + `_choose_two_players` scoring | **every `PLAYER_DETECTION_INTERVAL = 5` frames** (cached on the other 4) | YOLOv8x-pose full-frame: ~60-80 ms. SAHI when it runs (court-ROI only): ~200-300 ms over tile-fan. `_choose_two_players` + dedup + filters: ~10-20 ms. | **~75-85 %** — **the dominant cost** |
| `postprocess` (one-shot) | interpolate gaps, detect bounces, compute speeds, optional stroke classifier flow pass | once at end | ~30-90 s total | absorbed in `processing_time_sec` |

### Outside the per-frame loop (the other 1.38 h on a long match)

| Section | Where | What | Order-of-magnitude cost |
|---|---|---|---|
| **ROI ViTPose far-pose** | `roi_extractors/pose.py` via `roi_extractors/unified.py` | Sample every 2nd frame (12.5 fps eff), YOLOv8m det → ViTPose-Base, **batch=1**, rally-gated | ~30-50 min on a 45-min match |
| **ROI bounce TrackNet** | `roi_extractors/bounces.py` via the same unified driver | ±2.5 s windows around each bounce, TrackNet on a service-box crop, model loaded once (Bug 2 fix, commit `2c461e9`) | ~5-15 min on a 45-min match |
| Heatmaps + S3 PUTs | `__main__.py` | matplotlib renders + small uploads | ~30-60 s |
| Debug-frame upload | `__main__.py` | hundreds of JPEGs per job | ~30-90 s |
| **MP4 transcode** | `__main__.py::_transcode_to_mp4` | libx264 ultrafast CRF 28 720p over the full source | **~5-15 min** on a 45-min source (ffmpeg, **CPU-only on the T4 host** — no NVENC wired in) |
| S3 source delete + cost row | `__main__.py` | trivial | ~1 s |

### Critical structural finding (still holds)

**Every detector runs batch=1**: TrackNet, WASB, YOLOv8x-pose (full frame), YOLOv8m (each SAHI tile), ViTPose. The T4 (TFLOPS FP16 ~65) is grossly underutilised on small inputs like 512×288 or 640×640 tiles at batch=1.

### Instrumentation gap

`processing_time_sec` and `ms_per_frame` are stored on `video_analysis_jobs`, but **per-stage shares are not**. The values printed every 100 frames via `_log_stage_timings` (and the `player_sub` line every 100 frames) live only in CloudWatch logs. Recommended: persist the final stage-timing tuple (`court_ms`, `ball_ms`, `player_ms`, `full_yolo_ms`, `sahi_ms`, `choose2_ms`, `sahi_run_count`, `sahi_skip_count`) onto the job row — every future optimisation is gated on knowing the split, and right now we read it by SSHing into Batch logs.

---

## 3. Ranked levers

Ranked by **(speedup × inverse-accuracy-risk × inverse-effort)**. Every Batch-side change trips **rule #8 — BATCH-SIDE CHECKLIST** (Docker rebuild + dual-region ECR push to eu-north-1 + us-east-1 + new job-definition revisions). All deploys daylight-only (memory `feedback_overnight_branch_only.md`), one lever per commit (memory `feedback_always_main_branch.md`), `bench`/`bench_ball`/`bench_silver` green before push (rule #9).

| # | Lever | Mechanism | Est. speedup | Quality risk | Bench gates | Checklist trip? | Deploy effort |
|---|---|---|---|---|---|---|---|
| **L1** | **Player-stage GPU batching** — accumulate N=8-16 detect frames in the main loop and run YOLOv8x-pose in one `predict(list_of_frames)` call. Since `PLAYER_DETECTION_INTERVAL=5`, 8 batched detect-frames = 40 source frames of buffer. Mirror the WASB ball-batching pattern (5317c50). Add a `flush()` at end of loop. | Full-frame YOLOv8x-pose at imgsz=1280 batch=1 leaves a T4 ~25-35 % busy on a single 1080p frame. Batching 8 → 1 forward pass with the same kernels, expected **2-3× speedup on the YOLO portion**, which is ~40-60 % of player-stage cost. Net: **~25-40 % main-loop speedup** if SAHI cadence stays the same. | **None on outputs** — `predict()` is per-frame independent (Ultralytics handles list input natively), conv math is batch-element-independent, BatchNorm is running-stats. Same as the ball-batching equivalence proof. | `bench` (serve) + `bench_ball` (no change expected) + `bench_silver` | YES (`pipeline.py` + `player_tracker.py` + maybe `config.py`) | Med — buffer logic needs to keep MOG2/court/ball per-frame sequential while only the player call defers |
| **L2** | **SAHI rationalisation** — three sub-levers, each independent:<br>(a) **Tighten skip rule A** so it fires more often when both players are pose-confirmed in the full-frame pass. Today the rule is already there but `_sahi_run_count` vs `_sahi_skip_count` is currently the only signal. Pull a CloudWatch log line, see actual hit rate. If <50 % skip, tighten predicates with metric far-baseline acceptance.<br>(b) **Drop SAHI overlap to 10 %** (from 15 %) and **enlarge tiles to 768** (from 640) — fewer tiles per frame, each individually still resolves the ~30-40 px far player.<br>(c) **GPU-batch the tile-fan** — `_run_sahi` currently calls SAHI which loops tiles sequentially. Batching all tiles of one frame into a single YOLOv8m forward pass is a true batch=N parallel call. | When SAHI runs, it costs ~200-300 ms per frame — the largest single op in the pipeline. Skipping more (a) or running fewer/wider tiles in parallel (b+c) is multiplicative with L1. Expect **~15-30 % main-loop speedup** stacking with L1 if SAHI hit rate is currently 30-60 %. | **Med-High.** SAHI exists to catch the ~30-40 px far player (YOLOv8x-pose suppresses sub-60 px because keypoint NMS can't resolve them). Cutting SAHI risks far-player coverage — which `docs/north_star.md` already flags as the field with the lowest precision/recall ceiling. **(a) is the safest of the three** because the skip predicate by construction only fires when full-frame pose has already found both players. **(b) reduces tile count but each tile resolves the same player.** **(c) is pure parallelisation, same outputs.** | `bench` + reconcile far-player coverage on `9378f2dd` / `c645a7ee` (both have SA pairs) | YES (`config.py`, `player_tracker.py`) | Low (config flips) to Med (tile batching) |
| **L3** | **YOLO FP16 inference** — Ultralytics `predict(half=True)` on the player full-frame and SAHI tiles. ViTPose probably also benefits. | T4 FP16 is ~2× FP32 throughput. Stacks with L1+L2 — pure kernel-level cut, no schedule change. Expect **~25-40 % additional speedup on the YOLO portion**, ~15-25 % main-loop net. | **Med.** `config.py` line 158 documents that lowering `YOLO_CONFIDENCE` to 0.10 was a *response* to a "GPU FP16 suppressing detections near 0.25 threshold" episode. FP16 was already on at that time — but the symptom was specifically suppression at default confidence. With the 0.10 floor already in place, FP16 should be stable, but **re-verify by re-running the bench fixtures with timing logs**. | `bench` (serve detector inputs unchanged) + reconcile player coverage on `9378f2dd` | YES (config) | Low (one-line flag) but **must validate** |
| **L4** | **ROI ViTPose batching + FP16** — `roi_extractors/pose.py` calls ViTPose per cropped frame. Batch 8-16 crops, half precision. ROI bounce TrackNet similar. | ROI passes account for ~25 % of total wall time (1.38 h of 4.79 h on `9378f2dd`). Batching the 12.5 fps × ~45 min ≈ 33 750 ViTPose calls into batches of 16 → 2 100 forward passes. Expect **ROI section ~3-5× faster → ~50-65 min off the long match** (combined ~30 min if Lever 5 lands too). | **None** — same models, same crops, same outputs. | `bench_silver` (validates downstream serve_events / silver), plus a manual sanity check that far_pose row count on a known job is unchanged | YES (`roi_extractors/pose.py`, `roi_extractors/bounces.py`, `roi_extractors/unified.py`) | Med — batching crops of varying size needs padding or per-bucket batching |
| **L5** | **NVENC transcode** — replace `libx264 ultrafast` with `h264_nvenc` (G4dn has the NVIDIA Video Codec SDK; the T4 has NVENC silicon). | libx264 ultrafast on a 44-min 1080p → 720p source is ~5-15 min CPU-bound. NVENC at p4/p6 preset is **typically 5-10× faster than libx264 ultrafast** while producing visually equivalent CRF-equivalent output. Saves ~5-10 min per long match. | **Low** — review video, not analytic data; we're not gating any silver/gold on the trimmed MP4. NVENC's quality vs libx264 ultrafast is well-documented as comparable or better at the same bitrate. | None (visual review only) | YES (`__main__.py::_transcode_to_mp4` + `Dockerfile` if `ffmpeg` build lacks NVENC) — also need to confirm the bundled ffmpeg has `--enable-nvenc` | Low (one ffmpeg arg + Docker check) |
| **L6** | **Bump `PLAYER_DETECTION_INTERVAL` 5 → 7** — run YOLO+SAHI on fewer frames. | Linear in player-stage frequency; **~28 % reduction** in player-stage calls. Stacks with L1-L3. | **Med.** Config comment warns further increases "risk missing serve impact positions" — hitter attribution + serve impact bbox already weak fields. Worth measuring on the bench but **do not stack with L2 (b/c)** without validating each independently. | `bench` (serve detector reads player bboxes at serve impact) + reconcile on `9378f2dd` | YES (config) | Low (one-line config) |
| **L7** | **Instance type swap G4dn.xlarge (T4) → G5.xlarge (A10G)** — Ampere, 24 GB, ~2-3× FP16 throughput. | Pure hardware win on top of all software levers. Compare $/hour: G4dn.xlarge on-demand ~$0.526, G5.xlarge on-demand ~$1.006. If software levers deliver 2.5× speedup, G5 doubles that to ~5× for ~2× $/hr → net cost roughly flat, wall time halved. | **None** (newer GPU, same models). | `bench_ball` + `bench` (same fixtures, new compute env) | NO code change — Compute Environment + Job Queue + Job Definition update on the AWS Batch side | Low (CloudFormation / console) but requires quota verification |
| **L8** | **Decode-and-discard fix in `video_preprocessor.frames()`** — currently decodes 30 fps source then drops 5 frames to hit 25 fps target (`cap.read()` every source frame, sampling accumulator on yield). Replace with `cap.set(CAP_PROP_POS_FRAMES, target_idx)` seeking, or hardware-accelerated decode via NVDEC. | Decode is paid 2× today (main loop + the unified ROI sweep). Cutting wasted CPU decode is **Low-Med** in isolation — bigger win is moving to NVDEC for both passes. | None (same frames delivered). | None (no detector input changes) | YES (`video_preprocessor.py`) | Med — seek-vs-decode handling is codec-fragile, NVDEC needs cv2 build with CUDA |
| **L9** | **TensorRT / ONNX engine conversion for YOLOv8x-pose** — Ultralytics ships `export(format='engine')`. Real-world YOLO speedups on T4 with TensorRT FP16 are documented at 2-3×. | Stacks on top of L1 (batching benefits multiply with engine optimisations). | **Med-High** — the engine is a different binary; outputs are mathematically equivalent within FP tolerance but Ultralytics has historically had quirks around dynamic input shapes, batch dims, and NMS implementations (engine NMS sometimes differs slightly from torch NMS). | `bench` + reconcile on `9378f2dd` | YES (`Dockerfile` likely needs the TensorRT runtime image; weights file changes; `player_tracker.py` engine path) | High — engine build + verification + fallback path |
| **L10** | **Concurrent stage pipelining** — currently court/ball/MOG2/player run sequentially per frame. Ball + player are GPU-bound and could overlap with CPU MOG2. Or: a producer-decode thread feeds GPU consumer threads. | T4 has one GPU; true concurrent CUDA streams help only if one stage is GPU-light. MOG2 is CPU (potential win = CPU overlap with GPU). | **None on outputs** but high implementation complexity for ~5-10 % win at best given GPU is the bottleneck. | All benches | YES (`pipeline.py` core rewrite) | High; **deprioritise** — pick up only if 1-6 don't get us there |

### Levers explicitly **not** recommended

- **Lowering `FRAME_SAMPLE_FPS` 25 → 15-18** — t5_pipeline_speed.md Lever #6. Bounce x/y is the weakest base field on the north_star scoreboard; lowering temporal resolution attacks the exact field we're trying to fix. **Do not pull.**
- **Retraining a smaller YOLO** — explicitly out of scope per task framing. We use the trained models we have, faster.
- **YOLOv8x → YOLOv8m as a permanent swap** — would help but is the same family of risk as #9 in t5_pipeline_speed.md ("Med-High — `YOLO_IMGSZ=1280` and 0.10 confidence were *deliberately* set to recover the far/near player on GPU"). Don't trade the bronze coverage we already paid for.

---

## 4. Suggested daylight sequence — quick wins first

Each step is one commit, on `main`, bench-green before push, **paired Docker rebuild + dual-region ECR push + new job-def revisions** per CHECKLIST. After each, run **one production-class long-match re-ingest** (e.g. re-run `9378f2dd` or `c645a7ee`) and read `ms_per_frame` + `batch_duration_sec − processing_time_sec` from the DB to confirm the win.

### Step 0 — make the optimisation observable (one half-day, no model change)

- Persist the final `_stage_seconds` + `_sub_seconds` + `_sahi_run_count` / `_sahi_skip_count` tuple onto `ml_analysis.video_analysis_jobs` (add columns or a JSONB). Without this, every subsequent lever is measured by SSH-greping CloudWatch.
- Pull a CloudWatch `player_sub` log line from `9378f2dd` or `c645a7ee` and paste the actual `full_yolo` vs `sahi` vs `choose2` share into this doc. **That number alone may change which of L1/L2/L3 is biggest.**
- BATCH-SIDE CHECKLIST trip: **no** (DB writer change only, not in the perf-critical glob — but `db_writer.py` is in the rule #8 path. So this *is* a checklist trip. Plan for it.)

### Step 1 — L5 (NVENC transcode) — pure ops, isolated blast radius

- Replace `-c:v libx264 -preset ultrafast` with `-c:v h264_nvenc -preset p4 -rc vbr -cq 28` in `_transcode_to_mp4`. Confirm Docker image's bundled ffmpeg has NVENC.
- **Expected: ~5-10 min saved per long match, zero analytic risk.**
- BATCH-SIDE CHECKLIST trip: **yes** (`__main__.py` + possibly `Dockerfile`).

### Step 2 — L1 (player-stage GPU batching)

- Buffer up to N=8 detect-frames in `pipeline._process_frame` and call YOLOv8x-pose in a single `predict([f1...f8])`. Flush at end of loop. SAHI stays per-frame for this step (one variable per commit).
- **Expected: 25-40 % main-loop speedup. ~30-45 min saved per long match.**
- Bench-gate: `bench` must stay 20/24 (or 23/24 on 880dff02); `bench_silver` green; manual reconcile against `9378f2dd` SA pair shows no player-coverage regression.

### Step 3 — L4 (ROI ViTPose + ROI TrackNet batching, FP16)

- Batch the per-crop ViTPose calls in `roi_extractors/pose.py` (16-crop batches with bucketed sizing). Batch the per-window TrackNet calls in `roi_extractors/bounces.py` if not already.
- **Expected: ROI section 3-5× faster. ~30-45 min saved per long match.**
- Bench-gate: `bench_silver` green; far_pose row count on a known job within ±2 %.

### Step 4 — L2(a) (SAHI skip-rule tightening)

- Pull `player_sub` logs from a recent long match. Compute current SAHI hit rate. If <60 %, tighten skip rule A so it fires whenever full-frame YOLOv8x-pose has two pose-carrying candidates split by midline (it already does this but the `has_far_pose` rule additionally requires feet-projects-near-baseline — review whether that's too strict on the long-match regime).
- **Expected: 10-20 % main-loop speedup IF SAHI hit rate is currently low. If the rule is already firing 80 %+, skip this step.**
- Bench-gate: `bench` green; far-player coverage reconcile against `9378f2dd`.

### Step 5 — L3 (YOLO FP16 player + SAHI)

- Flip `predict(half=True)` for both the full-frame YOLOv8x-pose pass and SAHI's YOLOv8m calls.
- **Expected: 15-25 % main-loop speedup on top of L1.**
- Bench-gate: `bench` green; player-coverage reconcile across two long-match SA pairs.

### Stop-check after Step 5

Re-run `9378f2dd`. Read `ms_per_frame`. If ≤ 70 ms and ROI/transcode delta ≤ 25 min, **we are under 1 h on the 44-min match — STOP and ship.** If not, proceed.

### Step 6 (only if stop-check missed) — L7 (G5.xlarge / A10G)

- AWS Batch CE + Job Queue + Job Def updates for G5 on-demand priority queue. No code change. Quota check first.
- **Expected: 1.8-2.2× hardware speedup on top of all software levers.** Almost certainly closes whatever gap remains.

### Held in reserve (don't pull unless needed)

- **L2(b)+(c)** — SAHI tile-size / overlap / batched-tile-fan. Worth ~10-15 % more but carries the highest far-player-coverage risk among the safe levers.
- **L6** (interval 5 → 7) — same.
- **L9** (TensorRT) — biggest unknown blast radius (engine vs torch divergence). Only worth it if all the above stack to ~70 % of target and we still need 10-20 %.
- **L10** (concurrent pipelining) — high implementation cost, low expected gain.
- **L8** (decode-and-discard / NVDEC) — only worth it after Step 3 cuts ROI passes; otherwise we're still paying decode twice on a much smaller fraction of wall time.

---

## 5. Batch-side checklist + bench gates (the constraints)

For every commit in Steps 1-6, in order:

1. **Local bench green** — `python -m ml_pipeline.diag.bench` (serve, 20/24 on a798eff0 baseline, 23/24 on 880dff02). For ball-related commits also `bench_ball`. For silver-shape changes also `bench_silver`.
2. **`git push origin main`** — Render auto-deploys main API + worker (rule #7: push before asking for re-ingest).
3. **Docker build** — `ml_pipeline/Dockerfile` rebuild.
4. **Dual-region ECR push** — eu-north-1 + us-east-1 (rule #8).
5. **New job-definition revisions** — eu-north-1 and us-east-1.
6. **Re-ingest a production long match** (e.g. re-fire `9378f2dd`) — read back `ms_per_frame` + ROI delta from `ml_analysis.video_analysis_jobs`.
7. **Manual reconcile** — if the commit touched player or SAHI, run `python -m ml_pipeline.harness reconcile <SA_task_id> <T5_task_id>` on at least one long-match SA pair and confirm no regression on player coverage / hitter attribution.

CI is **only** the bench fixture (`.github/workflows/bench.yml`) — `bench_ball` and `bench_silver` are local-only. The CI fixture is small (15.3k frames) so it will not catch a long-match-specific regression — that's why Step 7 above is required.

---

## 6. What this plan does NOT cover (out of scope)

- **Retraining any model.** Tomo's framing is explicit — use the trained models we have, faster. No re-training, no dataset expansion, no fine-tuning.
- **Detector accuracy improvements.** Bounce x/y (~4.5 m err), serve precision (receiver-FP), far-stroke classification, identity model. These are training-stage levers tracked in `docs/north_star.md` and `.claude/handover_t5.md`. They become *unblocked faster* by speeding the pipeline up (more dual-submit corpus per day) but the speedup work itself does not touch them.
- **Court calibration.** The court polygon, homography, lens-distortion model. Independent of inference speed.
- **Silver build / Render-side workers.** `build_silver_v2.py`, `build_silver_match_t5.py`, ingest worker, serve_detector. None of these run on the Batch GPU box — the speed bottleneck is upstream of them.
- **Cost optimisation.** On-demand vs Spot pricing is a separate decision (see `feedback_overnight_branch_only.md` + `.claude/playbook_aws_batch_ondemand_fallback.md`). This plan optimises wall time; the dollar number is governed by instance choice and runtime jointly.

---

## 7. Open questions Tomo needs to weigh

1. **What's the actual `player_sub` log breakdown on a recent long-match run?** I could not pull a CloudWatch log line this session. The current per-stage split is inferred from code structure + the empirical fact that ball-batching (5317c50) gave zero speedup. If `full_yolo` is, say, only 30 % of player-stage and SAHI is 60 %, Step 4 (L2 SAHI rationalisation) should move to Step 2. **One CloudWatch grep settles this.**
2. **Is `BALL_BATCH_SIZE=8` actually flipped in the live job-def?** The 27 May commit says "default 1, flip env to activate". If it's not flipped, the 183 ms/frame number is *pre*-batching — and flipping it (zero new code) is the cheapest L0 win. If it *is* flipped and the number is post-batching, this confirms ball was never the bottleneck.
3. **G5.xlarge on-demand quota in eu-north-1?** Step 6 depends on quota — needs an AWS console check before committing to the path.
4. **Are we okay with a NVENC-encoded MP4 in the trimmed bucket?** The visual difference vs libx264 ultrafast at the same bitrate is usually invisible, but the user-facing video in the locker room reads from this file. Tomo's call.
5. **Is the L9 (TensorRT) risk worth taking up front?** It's the biggest single-step potential win on the player stage but also the highest blast-radius. Recommend deferring until Step 5 stop-check; only pull if we're still ~30 % short.

---

## Appendix — the load-bearing rule reminders

- **Rule #8 (BATCH-SIDE CHECKLIST)** — every player/SAHI/ROI/config/Docker change triggers Docker rebuild + dual-region ECR push (eu-north-1 + us-east-1) + new job-def revisions.
- **Rule #9 (bench is sacred)** — a red bench is a real regression. Revert, reproduce locally, ship a fix that turns it green. Never widen the workflow triggers, never lower the baseline.
- **Daylight-only deploys for Batch-side changes** (`feedback_overnight_branch_only.md`).
- **Always commit to `main`** (`feedback_always_main_branch.md`) — no feature branches.
- **One change per commit** — easy bisect when something regresses.
- **Don't touch `ca475740`** — live Batch job at 60 % progress as of this session.
