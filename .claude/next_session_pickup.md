# Next-session pickup — 2026-05-29 (PM) — MATCH 4 LANDED ✅ calibration proven in prod + fully polished on `main`; next cycle = runtime optimization deploy (sub-1h)

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. `bench_calib` 4/4. `bench_lens` well-behaved. Identity `100%`. `bench_bounce` v1 (M1-only) GR-mode ~24% (live; the M1+M4 retrain REGRESSED — see Bounce). `bench_swing_type` STOPGAP.
**Court calibration:** DONE — proven in prod (match 4 landed) and fully polished on `main`. The remaining polish (lock-best + projection-quality select) is committed but not yet redeployed; it rides the next runtime rebuild and lifts match 4 from 43%→~94% coverage.
**#1 next cycle:** the **runtime-optimization deploy** to sub-1h — all software levers are CODED/banked on branches; the cycle is *deploy + validate*, not build. One daylight Batch rebuild from `main` (+ merge the opt branches) carries the calibration polish too.
**Don't:** submit direct S3 uploads via the job-def stored command — broken on rev 55+ (Lambda double-entrypoint bug). Use args-only `containerOverrides.command`.

---

## ✅ Court calibration — PROVEN IN PROD + fully fixed (calibration agent, 2026-05-28/29)
Root cause was **NOT wide-angle** (disproven by live reproduction + the completed prod run). Three stacked failure modes, all fixed:
1. **Silent degeneracy / frame-selection** — "lock in first 300 frames, never re-run CNN" froze a degenerate Hough homography on an unrepresentative opening window. **Fix G** (lock only on geometry-validated non-degenerate detections; keep sampling past the window; self-heal; never lock ANY-BEST/Hough) + **Fix B** (`_homography_degenerate` corner-reprojection/convex/cond gate — NOT an H-diag gate; healthy MATCHi H_diag hits −1142 and projects fine via radial).
2. **Degenerate/overfit LOCK** — locked a "VALIDATED" piecewise fit on 8 clustered obs (rms=0.0 overfit) → 0% coords. **Fix (projection self-test, `5dc5e97`):** `_projection_quality()` requires the calibration to actually project a court grid (`coverage ≥35% AND y_span ≥6m AND x_span ≥3m`); keep sampling on failure, fail-loud at cap. **DEPLOYED (rev 57).**
3. **First-fit vs best-fit + rms-vs-projection selection** (the prod 43%-vs-local-86% gap). **`0ceec5b` lock the BEST fit** (early-exit at `COURT_GREAT_COVERAGE=0.70`, best-so-far at cap) + **`8356237` `fit_calibration` selects by projection quality, not pixel-rms** (stops the overfit piecewise being chosen over the radial). **ON `main`, NOT yet deployed** → match-4 self-heal now locks **radial 94%** (local). Rides the next rebuild.

**Prod receipts (rev 57, job `51e0ffee`):** match 4 locked radial / 84% projection coverage / conf 0.93 → **ms_per_frame 70.4**, ~2h12, 504 bounces, **silver 334 rows ALL with court_x** (SA 391, ~85%). 43% ball-court coverage (the polish above lifts this on the next rebuild). **Calibration is proven in prod.**

Also shipped: **45×40 ROI guard** (`roi_extractors/{pose,bounces}.py`), **Fix C+** Render coverage-floor fail-loud (`upload_app.py`), **`bench_calib`/`bench_lens`** harnesses + fixtures.
**Fix E (camera-agnostic lens distortion) — BUILT + DORMANT on `main`** (`lens_distortion.py`, `T5_CALIB_LENS_MODE=off`): division-model→Brown-Conrady + fisheye + auto-select, guarded against collapse-to-singularity. **NOT ENABLED** — needs a Class-C/D (phone-ultrawide/GoPro) fixture + transform-layer wiring (`§INTEGRATION`) + rebuild. Can't be left behind (on `main`).
Docs: `court_calibration_silent_degeneracy.md`, `court_calibration_camera_taxonomy.md`, `court_calibration_implementation_kickoff.md`. Memory: [[feedback_calibration_lock_window]].

---

## 🚀 #1 NEXT CYCLE — RUNTIME OPTIMIZATION to sub-1h (runtime agent's lane)
Live profile from the clean match-4 run gives a ranked, evidence-based roadmap. Target **~2h → sub-1h** on a 45-min match. **All software levers are CODED/banked — the cycle is deploy + validate, not build.**

| # | Lever | Status | Effect |
|---|---|---|---|
| 1 | **MOG2 downscale** (`MOG2_DOWNSCALE`) | CODED on `opt/overnight-findings` | motion_mask = 58% of wall (38ms/fr CPU) → ~halve |
| 2 | **CPU/GPU stage overlap** (`PIPELINE_STAGE_OVERLAP`) | CODED on `opt/runtime-overlap-roi` (`d2eff02`) | MOG2 worker-thread concurrent w/ GPU → ~15–20% main-loop |
| 3 | **ROI bounce-window batching** (`ROI_BOUNCE_BATCH`) | CODED on `opt/runtime-overlap-roi` (`d2eff02`) | 194 sequential TrackNet windows → batched, ~25→~6–10min (V2-only) |
| 4 | **SAHI_BATCHED=1** | CODED + DEPLOYED (rev 57, off on job-def) | flip env to activate |
| 5 | **L7 g5/A10G** | ACTIVE (queue order 1) | ~1.5–2× on GPU-bound stages |

- **Stack math:** MOG2 + overlap + ROI-batch ≈ ~2h → ~1h30m; sub-1h additionally needs `SAHI_BATCHED=1` + g5 (both in place).
- **Deploy as ONE daylight cycle:** rebuild from `main` (merge `opt/overnight-findings` + `opt/runtime-overlap-roi`; `opt/sahi-batched-tilefan` already merged) → **bench-green** → dual-region ECR → job-defs (clone rev 57/39, preserve env) → validation run. **This rebuild ALSO carries the calibration polish (`0ceec5b`+`8356237`) automatically.**
- **3 human-verify gates** (from the overlap prototype report): (1) `frame_errors==0`; (2) `roi_prod` bounce-row count matches eager within fp-noise; (3) `overlapped_hidden` log line non-zero (confirms cv2 releases GIL on the base image).
- **⚠️ Need a video source** — pipeline deletes the source on success (every run is one-shot); consider disabling source-delete during the optimization phase. Match 4's source was deleted on its successful run.
- Report: `docs/_investigation/runtime_overlap_roi_2026-05-29.md`.

## Bounce (ADR-01) — fps fix is a PREREQUISITE
- `bench_bounce` v1 **M1-only** GR-mode ~24% recall — **keep this live.**
- The **M1+M4 full-data retrain REGRESSED** (val F1 0.677 but bench: M1 ~24%→10.5%, M4 0%). Weights `models/bounce_detector_v1_m1m4.pt` — **DO NOT SHIP.**
- **🐞 Root cause = fps mismatch:** Match 4 is tagged `fps=60` vs M1 `fps=25` → its bounce-label↔gravity-residual strict-match cratered to 8% (23/273) → 92% of M4's floor labels are weak anchor-fallback noise, and the fps mismatch breaks M4's bench timestamp matching. **Fix fps/frame-alignment FIRST (runtime agent backlog) → rebuild corpus → retrain.** Until then, 60fps matches are not usable bounce-training data.

## 🐞 Confirmed prod bug
Job-def STORED command + `lambda/ml_trigger.py:85` both double-invoke the `python -m ml_pipeline` ENTRYPOINT → container dies at argparse → **direct S3 uploads broken on rev 55+.** Auto-spawn path (`upload_app.py:923`, args-only) is correct. Fix on `opt/overnight-findings` (Lambda) + a job-def stored-command fix.

## Deploy state
- Image `2bd946a2`, **job-defs eu rev 57 / us rev 39** (calibration through projection-self-test + L2c SAHI). g5 queue (g5→g4dn→Spot), g5 CE idle ($0). Serve bench GREEN.
- `main` is AHEAD of rev 57 by the calibration polish (`0ceec5b` lock-best + `8356237` projection-select) — undeployed; rides the next rebuild.

## Open items
| # | Item | Owner |
|---|---|---|
| 1 | Runtime optimization deploy cycle (sub-1h) — merge opt branches, rebuild from `main`, validate (3 gates) | runtime |
| 2 | fps=60 frame-alignment fix → rebuild bounce corpus → retrain | runtime/ADR |
| 3 | Lambda double-entrypoint fix (restores direct S3 uploads) | runtime |
| 4 | Enable Fix E (needs phone-ultrawide/GoPro fixture → validate → wire → rebuild) | calibration |
| 5 | Re-run match 4 on the polished image to confirm 43%→~94% coverage in prod | either (rides the runtime rebuild) |

## Coordination
- **Calibration agent:** nothing outstanding — all calibration fixes on `main`, proven in prod (rev 57) + polished (undeployed, rides next rebuild). Files: `court_detector.py`, `camera_calibration.py`, `roi_extractors/` (ROI guard), `lens_distortion.py` (dormant).
- **Runtime agent:** owns the optimization deploy cycle + fps fix + Lambda fix; branches `opt/overnight-findings`, `opt/runtime-overlap-roi`. Their next rebuild from `main` carries the calibration polish for free.

## Research artefacts
`.claude/tmp/calib_audit/` (gitignored): `audit.csv`, frames, `repro_prod.py`/`spread_test.py`/`cov_trajectory.py`/`proof.py`/`verify_fix.py`, `register_jobdef.py`. Synthesis docs under `docs/_investigation/` + `.claude/court_calibration_implementation_kickoff.md`.

---

**END OF PICKUP**
