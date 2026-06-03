# Next-session pickup — 2026-06-03 — overnight runtime cycle: 118→109 min ACCURACY-CLEAN; sub-1h needs accuracy trades

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (unchanged all night).
**Canonical Batch config:** **job-def eu rev 62 / us rev 43** (image `b5gate` = `sha256:84c1c4a2`). Validated-good:
B1 decode-skip + D1 GPU-cache-free + B2 `MOG2_DOWNSCALE=4` + `YOLO_IMGSZ=1280` + standard batch (8/16/8). Both regions synced.
**What shipped (overnight, 3 g5 runs on the 60fps Jimbo match `1779519790`):**
- ✅ **B1 (decode-skip)** — `VideoPreprocessor` grab()/retrieve(): main loop **57.5→~50 min**, ms/fr 48→41.6. Byte-identical (proven synthetic + prod). `c5109ff`
- ✅ **D1 (free GPU cache at main→ROI boundary)** — no OOM; **only 380MB/24GB reserved → proved the GPU is COMPUTE-bound, not memory/launch-bound.** `9e0e980`
- ✅ **B2 (`MOG2_DOWNSCALE=4`)** — accuracy-NEUTRAL (coverage identical to baseline). Live in rev 62.
- ❌ **Batching REJECTED (run 2)** — PLAYER_BATCH=16/ROI_BATCH=32/BALL_BATCH=16 = **no-op** (ms/fr 41.6→41.9, total unchanged). GPU compute-bound at batch 8. Reverted to defaults.
- ❌ **imgsz=960 REJECTED (run 3)** — env-gated `c852352`, **but cost −18.5% FAR-player dets (pid1 23643→19264) for only −3.6 min.** Bad trade vs far-court priority (rule #11). Rolled back to 1280 (rev 62/43).
**Net: 118 → ~109 min, ZERO accuracy loss.** B1 is the accuracy-safe runtime win; B2 neutral; D1 enabling.
**NOT sub-1h.** The two rocks (main loop ~50 + ROI sweep ~51-60, SEQUENTIAL) only yield to **accuracy-sensitive** levers — see below.
**Lambda:** still IAM-blocked (`nextpoint-uploader` denied `lambda:*`). DEFERRED per Tomo. Code fix committed (`lambda/ml_trigger.py`).

If that's enough, go. Depth below.

---

## 🎯 The honest sub-1h verdict (the decision Tomo needs to make)

Tonight banked every **accuracy-safe** runtime lever (118→109). The remaining gap to sub-1h is locked behind the **far-court signal**, which is the project's stated #1 weakness/priority (rule #11). Proven empirically tonight: **imgsz960 cut far-player dets 18.5%.** The far court is fragile to every compute cut.

**The two rocks, and why safe levers don't crack them:**
| Phase | Time | Safe lever? |
|---|---|---|
| Main loop | ~50 min | player stage dominant → only imgsz cuts it → **−18.5% far player (rejected).** Batching = no-op (GPU-bound). |
| ROI sweep | ~51-60 min | pose pass (ViTPose ~39 min, GPU-bound, batching no help) + bounce finalize (CPU-bound ~30 min). |

**The ONLY remaining levers (ALL accuracy-sensitive → need Tomo sign-off, do in daylight):**
- **C1 — ROI bounce-pass CPU rework.** The model is ALREADY shared (`_shared_model`); the ~30 min is genuine `detect_frame` postprocess + per-frame Hough fallback on no-ball frames (`bounces.py`). Lever = skip/cheapen the Hough fallback or reduce no-ball-frame work. **Risk: bounce coords** (a silver accuracy field). Validate bounce count(=952)+coords vs baseline.
- **Pose stride** every-2→every-3: cuts ViTPose ~33% (~13 min) but drops far-pose density 14153→~9400. **Risk: far-pose coverage** (the far-court ceiling — same signal imgsz960 hurt).
- D2 (trim/export overlap, ~min), C3 (single-decode fold, architectural/risky).

**Recommendation:** the ~109 min is accuracy-clean and shipped. Sub-1h requires deliberately trading far-court accuracy — which contradicts bronze-first (rule #11). **Decide with eyes open:** is <1h worth −15-20% far-court signal? If runtime isn't a hard blocker, bank 109 and move on. If sub-1h is required, the path is C1 + pose-stride, each gated on a far-court-coverage reconcile, **with you watching.**

---

## How to reproduce / validate (mechanics that worked)
- **Submit a run:** `.claude/tmp/submit_run3.py`-style (fresh job_id, `aws s3 cp ... --copy-props none` to a throwaway `b1val_<id>.mp4` key — uploader IAM lacks GetObjectTagging; submit on eu rev 62, g5 queue `ten-fifty5-ml-queue`). Source = `wix-uploads/1779519790_Tomo_vs_Jimbo.mp4` (60fps, 172596 frames, = the rev-59/baseline Jimbo match; PRESERVED, copy-on-submit). Also local: `ml_pipeline/test_videos/1779519790_Tomo_vs_Jimbo.mp4`.
- **Validate speed + far-player accuracy:** `.claude/tmp/bronze_compare.py <job_id> <baseline_job_id>` — pulls bronze.json.gz from S3, reports ms/frame, main-loop proc_sec, and **per-player_id det counts** (pid0=near ~38660, pid1=FAR ~23643). >10% pid1 drop = material regression. **1280 baseline = `b2f16f55`.**
- **Validate logs:** `.claude/tmp/validate_v60.py <batch_job> <t5_job>` — decode-skip line, court lock, D1, ROI sweep, coverage.
- **Deploy:** `docker build -f ml_pipeline/Dockerfile ...` → push both ECR → `.claude/tmp/calib_audit/register_jobdef_opt.py <digest> <region>` (edit OPT_ENV for flags; clones latest active). g5 overnight had ~2h capacity wait (D3) — consider g4dn-first for unattended runs.

## Open items
| # | Item | Notes |
|---|---|---|
| 1 | sub-1h decision (above) | C1 + pose-stride, accuracy-gated, daylight + Tomo sign-off |
| 2 | Lambda function deploy | BLOCKED — needs Lambda-capable cred (Tomo). Code committed. |
| 3 | 3 manual runs NOT ingested to silver | jobs `b2f16f55`(1280 base), `ce048588`(batching), `d39a6f07`(imgsz960). bronze in S3; not in corpus. Don't double-count. |

## Jobs (manual submits — NOT auto-ingested)
- Run 1 (B1+D1+B2, 1280 baseline): `b2f16f55` — 109min, ms/fr 41.6, far pid1=23643. THE accuracy baseline.
- Run 2 (batching): `ce048588` — 109min, no-op.
- Run 3 (imgsz960): `d39a6f07` — far pid1=19264 (−18.5%), rolled back.

---
**END OF PICKUP**
