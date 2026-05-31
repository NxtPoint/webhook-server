# T5 Batch runtime — master optimization backlog (living list)

**Purpose:** the single running list of EVERY known runtime lever for the T5 Batch pipeline,
with cost / expected impact / risk / validation-needed / status. Update this as levers land or new
ones are found. Supersedes the scattered opportunity lists in `batch_optimisation_plan.md` +
`match4_opt_run_2026-05-30.md` (kept for their run detail).

**Baseline (rev 59, 47-min / 60-fps match, MEASURED):** main loop 57.5 min @ 48.0 ms/frame;
ROI sweep **52 min** (was 91 pre-fix); total **118 min** (was 157 pre-ROI-fix), $0.31. far-pose +
bounce now 25-fps-aligned with bronze.
Stage profile (main loop): **player 43% (17.6 ms/fr) · ball 32% (13.2) · MOG2 23% (9.3) · court 2% · ~16% unaccounted**.

Legend — Risk: 🟢 safe/env-only · 🟡 needs validation · 🔴 accuracy/correctness risk.
Status: ✅ shipped · 🔬 needs measurement · 📋 todo.

---

## A. DONE (shipped)
| # | Lever | Effect | Status |
|---|---|---|---|
| A1 | YOLO-pose FP16 (`YOLO_FP16`) | player stage faster | ✅ rev 55+ |
| A2 | ROI ViTPose batching + FP16 (`ROI_BATCH_SIZE=16`, `ROI_POSE_FP16`) | pose pass | ✅ (fp16 crash fixed rev 59) |
| A3 | Player-detect batching (`PLAYER_BATCH_SIZE=8`) | player | ✅ |
| A4 | Ball batching (`BALL_BATCH_SIZE=8`, WASB) | ball | ✅ |
| A5 | SAHI batched tile-fan (`SAHI_BATCHED=1`) | player/SAHI (was 76%→43%) | ✅ rev 58 |
| A6 | MOG2 downscale=2 (`MOG2_DOWNSCALE`) | motion_mask | ✅ rev 58 |
| A7 | CPU/GPU stage overlap (`PIPELINE_STAGE_OVERLAP=1`) | hides ~38% of MOG2 (8.5 ms/fr) | ✅ rev 58 |
| A8 | NVENC encode (L5) | trim | ✅ |
| A9 | g5/A10G hardware (L7) | GPU-bound stages 1.5-2× | ✅ |
| A10 | **ROI sweep 25-fps alignment + decode-skip** | far-pose/bounce ALIGNED w/ bronze (correctness) + ~2.4× fewer decodes + correct bounce window widths | ✅ rev 59 (measuring) |

## B. NEXT — main loop (the ~58 min floor for sub-1h)
| # | Lever | Est. impact | Risk | Validation | Status |
|---|---|---|---|---|---|
| **B1** | **✅ CONFIRMED: main `VideoPreprocessor.frames()` over-decodes.** `video_preprocessor.py:84` does `cap.read()` on EVERY source frame and yields only the sampled ~41% (60→25fps) — it fully decodes ~100k frames it discards. Fix = same `grab()`/`retrieve()` decode-skip as the ROI sweep (grab the skipped frames, retrieve only sampled). **TOP main-loop lever, SAFE (output-identical).** Likely a big chunk of the ~16% (575 s) unaccounted overhead. | large (only on >25fps sources) | 🟢 output-identical | confirm frame count + a bench-style spot check | 📋 **DO FIRST next session** |
| B2 | `MOG2_DOWNSCALE=4` (env, no rebuild) | halve MOG2 compute again (~part of 9.3 ms/fr) | 🟡 motion-mask feeds far-player scoring (bonus, not gate) | player-coverage reconcile vs SA | 📋 fold into next run |
| B3 | `SAHI_SKIP_A_FAR_YMAX=8.0` (env) | skips SAHI when full-frame already resolved far player — helps CLOSER cameras, ~0 for match-4-far | 🟡 far-player coverage | coverage reconcile | 📋 |
| B4 | `BALL_BATCH_SIZE=16` (env) | ball stage | 🟡 GPU OOM (memory theme — see D1) | watch OOM + bench_ball | 📋 |
| B5 | YOLO-pose `imgsz` 1280→960 (env/code) | player stage (dominant) | 🔴 small far-player detection | far-player coverage reconcile | 📋 |
| B6 | Ball TrackNet FP16 (code) | ball stage | 🟡 fp-noise on heatmap | bench_ball | 📋 |

## C. NEXT — ROI sweep (post-alignment)
| # | Lever | Est. impact | Risk | Notes | Status |
|---|---|---|---|---|---|
| C1 | Bounce pass is **CPU/postprocess-bound** (~0.13 s/frame): per-window `BallTracker()` construction (194×), frame-delta Hough fallback on no-ball frames, resize. | large if addressed | 🟡 | reuse tracker / batch CPU work / profile the Hough fallback frequency | 📋 |
| C2 | Far-pose `sample_every` (12.5 fps effective) — raise stride to sample fewer far-pose frames | pose pass | 🔴 far-player coverage density | reconcile | 📋 |
| C4 | **Far-pose density DROPPED 47,974→14,153** after the 25fps fix (now `every-2` of 25fps = 12.5fps; was `every-2` of 60fps = 30fps). Now aligned + fp16-cheap → consider `pose_sample_every=1` (25fps, ~matches bronze player density). | restores far-pose coverage | 🟡 | far-player coverage reconcile vs SA | 📋 |
| C3 | Fold ROI passes into the main decode (single decode total) — currently 2 decodes/job | decode | 🔴 ROI needs final bounce list (ordering) | architectural | 📋 |

## D. Cross-cutting
| # | Lever | Notes | Status |
|---|---|---|---|
| D1 | **GPU memory audit** — all models resident at once caps batching (caused the rev-58 ROI OOM). Unload/free models between phases (`empty_cache`), or share. UNLOCKS B4, ROI batching. | high leverage — gates several batching levers | 📋 |
| D2 | Trim/NVENC ~7 min runs SERIALLY after detection — overlap with bronze-export, or skip during validation (it's the review video, not analysis) | 🟢 | 📋 |
| D3 | Overnight g5 capacity scarcity (clean re-run waited 8.5 h) — g4dn/Spot fallback didn't pick up. Check CE min-vCPU / capacity strategy. | infra | 📋 |

## Honest sub-1h math
Main loop ~58 min is the floor. Even with a fast ROI sweep (~35 min) + trim (7), total ~100 min.
**True sub-1h requires cutting the MAIN loop** — B1 (decode-skip, if applicable) is the most promising
safe lever; B2/B3/B5 trade some accuracy; D1 unlocks the batching levers. Sequence: B1 → measure →
B2+D1 → reconcile → B3/B5 with coverage validation.
