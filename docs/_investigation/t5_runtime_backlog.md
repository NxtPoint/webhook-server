# T5 Batch runtime — master optimization backlog (living list)

**Purpose:** the single running list of EVERY known runtime lever for the T5 Batch pipeline,
with cost / expected impact / risk / validation-needed / status. Update this as levers land or new
ones are found. Supersedes the scattered opportunity lists in `batch_optimisation_plan.md` +
`match4_opt_run_2026-05-30.md` (kept for their run detail).

**Baseline (rev 60, 47-min / 60-fps match `b2f16f55`, MEASURED 2026-06-03):** main loop **49.9 min @ 41.6 ms/frame**
(was 57.5 / 48.0 at rev 59 — B1 decode-skip); ROI sweep **51.4 min** (pose pass alone 39.4 min = THE rock);
trim/export ~8 min; total **109 min** (was 118), court_conf 0.93 locked clean.
Stage profile (main loop): **player ~46% · ball ~16% · court 2% (post-lock) · MOG2 ~6% (overlap-hidden)**.
**Two rocks for sub-1h:** main loop ~50 min + ROI sweep ~51 min (sequential). D1 confirmed 380MB/24GB GPU use →
batching headroom is wide open (run 2 testing ROI_BATCH=32 / PLAYER_BATCH=16 / BALL_BATCH=16, accuracy-neutral).

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
| A11 | **B1: main `VideoPreprocessor` decode-skip** (`grab()`/`retrieve()`) | main loop 57.5→**49.9 min**, ms/fr 48→**41.6** | ✅ rev 60, VALIDATED 2026-06-03 (run `b2f16f55`: `decoded 71915 of 172596`, output byte-identical) |
| A12 | **D1: free main-loop GPU cache at main→ROI boundary** | only **380MB reserved before ROI on 24GB A10G** → huge batching headroom confirmed; no OOM | ✅ rev 60, VALIDATED |
| A13 | **B2: `MOG2_DOWNSCALE=4`** | MOG2 compute halved again | ✅ rev 60, VALIDATED accuracy-NEUTRAL (far-pose 14153=14153, bounces 952=952 vs baseline — identical) |

## B. NEXT — main loop (the ~58 min floor for sub-1h)
| # | Lever | Est. impact | Risk | Validation | Status |
|---|---|---|---|---|---|
| **B1** | ✅ **SHIPPED rev 60** — main `VideoPreprocessor` decode-skip. main loop 57.5→49.9 min. | large | 🟢 output-identical | ✅ byte-identical (synthetic 60fps + prod row counts) | ✅ DONE (A11) |
| B2 | ✅ **SHIPPED rev 60** — `MOG2_DOWNSCALE=4`. | part of MOG2 | 🟡→🟢 | ✅ accuracy-NEUTRAL on `b2f16f55` (identical coverage) | ✅ DONE (A13) |
| B3 | `SAHI_SKIP_A_FAR_YMAX=8.0` (env) | skips SAHI when full-frame already resolved far player — helps CLOSER cameras, ~0 for match-4-far | 🟡 far-player coverage | coverage reconcile | 📋 |
| B4 | batching: PLAYER=16 / ROI=32 / BALL=16 | **❌ NO-OP** — ms/fr 41.6→41.9, total unchanged. **GPU is COMPUTE-bound at batch 8** (D1 proved 380MB/24GB free, so headroom was never the limit). | n/a | ❌ run 2 (`ce048588`) — REJECTED, reverted to defaults |
| B5 | YOLO `imgsz` 1280→960 (env-gated `c852352`) | **❌ REJECTED** — only **−3.6 min** for **−18.5% FAR-player dets** (pid1 23643→19264). Bad trade vs far-court priority. | 🔴 | ❌ run 3 (`d39a6f07`) — rolled back to 1280 (rev 62/43) |
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

## Honest sub-1h math — UPDATED 2026-06-03 (after 3 g5 runs)
**Banked, accuracy-clean: 118 → ~109 min** (B1 decode-skip + D1 + B2). **That is the safe floor.**
- Batching (B4): NO-OP — GPU compute-bound, not headroom-bound. Off the table.
- imgsz960 (B5): −18.5% far player for −3.6 min. REJECTED.

**Sub-1h is now gated behind ACCURACY-SENSITIVE levers only** (both touch the far-court signal — the
project's #1 weakness per rule #11; imgsz960 already proved far-court is fragile to compute cuts):
- **C1** (ROI bounce CPU, ~30 min): model already shared; cost = `detect_frame` postprocess + Hough
  fallback. Risk = bounce coords. Validate bounce count(952)+coords vs baseline. **Daylight + sign-off.**
- **Pose stride** every-2→every-3 (~13 min): risk = far-pose density (14153→~9400). **Daylight + sign-off.**

**Decision required (Tomo):** is sub-1h worth ~15-20% far-court signal loss? If not, bank 109 (clean).
Sequence if pursuing: C1 → far-court reconcile → pose-stride → far-court reconcile, all with eyes on coverage.
