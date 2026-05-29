# Next-session pickup — 2026-05-29 (PM) — MATCH 4 LANDED ✅ calibration fix PROVEN in prod; runtime optimization roadmap to sub-1h is the next cycle

> ✅ **2026-05-29 PM — THE BIG ONE: match 4 ran clean, landed, and the calibration fix is PROVEN IN PROD. The "awaits ONE Batch rebuild" framing below is DONE.** Sequence executed this session:
> 1. **Deployed** calibration fix + L2c (batched SAHI) in one image (`2bd946a2`) → **job-defs eu rev 57 / us rev 39** (`SAHI_BATCHED` OFF on job-def, set per-job). Source re-uploaded (`wix-uploads/1779964630_Tomo_vs_Jimbo.mp4`, byte-verified).
> 2. **Match 4 ran on g5 (job `51e0ffee`):** calibration locked a **radial fit, 84% projection coverage, conf 0.93** (vs the broken run's 0%) → **ms_per_frame 70.4** (broken 321 / baseline 183), ~2h12, **504 valid bounces**, player 69,424. Re-ingested → **silver 334 rows, ALL with court_x** (SA = 391, ~85%). Fix C+ did NOT fail-loud (43% ball coverage cleared the floor).
> 3. **Calibration PROVEN in prod** — the decisive prod test passed (real coords, not the LOCKED-line false positive from last time).
> 4. **Bounce v1 full-data retrain ran** (M1 78c32f53 + M4 ca475740, `gravity_residual`): **val F1 0.40 → 0.677.** Weights `ml_pipeline/models/bounce_detector_v1_m1m4.pt` (NOT baseline-locked, **DO NOT SHIP**). **Bench result (GR-mode): the retrain REGRESSED — Match 1 dropped ~24% → 10.5% recall @0.5, Match 4 = 0%.** Root cause = the `fps=60` mismatch (below): Match 4's labels are 92% anchor-fallback noise → training on M1+noisy-M4 pulled the model off M1's optimum, and the fps mismatch breaks M4's bench timestamp matching. **Keep the M1-only v1 (~24%) live. The fps/frame-alignment fix is a PREREQUISITE — not an enhancement — before Match 4 (or any 60fps match) is usable bounce-training data.** Next bounce step: fix fps handling → rebuild corpus → retrain.
>
> **⚠️ Two honest caveats on match 4's quality:**
> - **Coverage moderate (43% ball vs healthy ~97%)** — prod locked `rms=13.3px` vs the dev-box repro's `2.84px`. Same video/fix, looser prod lock (radial/32-obs). **Calibration agent is investigating WHY** (closing it lifts M4 coverage materially). Non-blocking; M4 is usable.
> - **🐞 NEW corpus finding (mine to chase): Match 4 is tagged `fps=60` vs M1's `fps=25`** → its bounce-label strict-match to gravity-residual candidates **cratered to 8% (23/273)** vs M1's 58% → most of M4's 273 floor labels came in as **weak anchor-fallback positives**. A frame-rate/indexing mismatch between the SA label file and the T5 bronze. **Likely the bigger lever for M4's training value than the 43% coverage.** Investigate the fps source + SA-label↔T5-bronze frame alignment.
>
> ## 🚀 NEXT CYCLE (fresh session) — RUNTIME OPTIMIZATION to sub-1h
> The live profile (clean match-4 run) gives a ranked, evidence-based roadmap. Target: **~2h → sub-1h** on a 45-min match.
> | # | Lever | Status | Effect |
> |---|---|---|---|
> | 1 | **MOG2 downscale** (`MOG2_DOWNSCALE`) | **CODED** on `opt/overnight-findings` | motion_mask is **58% of wall** (38ms/fr, CPU) → ~halve it |
> | 2 | **CPU/GPU stage overlap** (`PIPELINE_STAGE_OVERLAP`) | **CODED** on `opt/runtime-overlap-roi` (`d2eff02`) | MOG2(CPU) runs on a worker thread (cv2 releases GIL) concurrent with court+ball GPU, joined before player. ~15–20% main-loop cut. |
> | 3 | **ROI bounce-window batching** (`ROI_BOUNCE_BATCH`) | **CODED** on `opt/runtime-overlap-roi` (`d2eff02`) | 194 sequential TrackNet windows → batched. ~25min → ~6–10min. V2-only (V3 falls back). |
> | 4 | **SAHI_BATCHED=1** | **CODED + DEPLOYED** (rev 57, off on job-def) | flip the env to activate |
> | 5 | **L7 g5/A10G** | **ACTIVE** (queue order 1) | ~1.5–2× on GPU-bound stages |
> - **Honest stack math (per the prototype report):** MOG2 + overlap + ROI-batch land **~2h → ~1h30m**; **sub-1h additionally needs flipping `SAHI_BATCHED=1` + the g5 hardware lever** (both already in place). All software levers are now CODED/banked — the next cycle is **deploy + validate**, not build.
> - **g5 EARNS its keep post-optimization**: pipeline goes GPU-bound (A10G ≈ 1.5–2× T4); g4dn would be ~1.5–2× slower (~over 1h) at ~flat cost-per-job. Optimize → stay on g5.
> - **Deploy as ONE daylight cycle** (Batch rebuild from `main` + merged opt branches → bench-green → dual-region ECR → job-defs → validation run). **3 human-verify gates from the prototype report:** (1) `frame_errors==0` on the overlap run; (2) `roi_prod` bounce-row count matches eager within fp-noise; (3) the `overlapped_hidden` log line is non-zero (confirms cv2 releases GIL on the base image). Needs a video source — **match 4's source was deleted again on this successful run** (pipeline deletes source on success → one-shot; consider disabling source-delete during the optimization phase).
> - **Banked branches to merge for the cycle:** `opt/sahi-batched-tilefan` (L2c — already merged), `opt/overnight-findings` (MOG2 + SAHI-skip + Lambda fix), `opt/runtime-overlap-roi` (overlap + ROI-batch). Report: `docs/_investigation/runtime_overlap_roi_2026-05-29.md`.
>
> ## Banked branches + open items
> - `opt/sahi-batched-tilefan` (L2c) — **MERGED to main + deployed** (rev 57).
> - `opt/overnight-findings` — MOG2 downscale + SAHI skip-relax (`SAHI_SKIP_A_FAR_YMAX`, won't help match-4-class but helps closer cameras) + **the Lambda command-fix**. NOT merged.
> - **🐞 Confirmed prod bug:** job-def STORED command + `lambda/ml_trigger.py:85` both double-invoke the `python -m ml_pipeline` ENTRYPOINT → **direct S3 uploads broken on rev 55+**. Use args-only `containerOverrides.command`. Fix = deploy the Lambda branch + a job-def stored-command fix.
> - **Calibration follow-up — FIXED on `main` (`0ceec5b`), validated locally, NOT yet deployed.** The rev-57 prod-vs-local gap (43% vs 86%) was the lock committing on the FIRST fit above the 35% floor. Now it **locks the BEST fit across attempts** (early-exit at `COURT_GREAT_COVERAGE=0.70`; keep searching past mediocre fits; best-so-far at the cap; monotonically safe, best ≥ first). Verified: bench_calib 4/4, match-4 self-heal 86% (sound geometry), serve bench 20/24·23/24. **Rides the next (runtime) Batch rebuild — no separate deploy.** Optional further polish (NOT done): make `fit_calibration` select candidates by projection quality not pixel-rms (would lift 86%→~92%, preferring radial over the overfit piecewise). Bigger M4-training lever is still the **fps=60 label-alignment bug**.
> - **Deploy state:** image `2bd946a2`, job-defs eu rev 57 / us rev 39, g5 queue (g5→g4dn→Spot), g5 CE idle ($0). Serve bench GREEN (20/24, 23/24).

## ⚡ Executive summary (read first — 30 seconds) — ⚠️ SUPERSEDED by the block above (the rebuild it "awaits" is DONE; match 4 landed)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. `bench_calib` 4/4. `bench_lens` well-behaved. Identity `100%`. `bench_bounce` v1 GR-mode `23.9%/9.1%` (baseline NOT locked). `bench_swing_type` STOPGAP.
**Court calibration:** the silent-degeneracy + the follow-on degenerate-lock are BOTH fixed on `main` and **validated locally** (match 4 self-heals to sound coords). **NOT yet rebuilt/redeployed — the live image (rev 56/38) still runs the OLD calibration.** No "proven in prod" claim until a real Batch run projects coords.
**#1 job:** ONE Batch rebuild from `main` (carries the calibration fix + Fix E) merged with `opt/overnight-findings` (SAHI relax) → re-run match 4 → lands *correct AND faster*. Then re-ingest → corpus → ADR-01 bounce full-data retrain.
**Don't:** submit direct S3 uploads via the job-def stored command — broken on rev 55+ (Lambda double-entrypoint bug). Use args-only `containerOverrides.command`.

If that's enough → go. Depth below.

---

## ✅ Court calibration — FIXED on `main`, validated LOCALLY (2026-05-28/29 calibration sessions)

Root cause was **NOT wide-angle** (disproven by live reproduction + a completed prod run). Two stacked failure modes, both fixed:

1. **Silent degeneracy / frame-selection** — the fixed-camera "lock in first 300 frames, never re-run CNN" froze a degenerate Hough homography when the opening window was unrepresentative. The CNN finds 12-13/14 keypoints on rally footage. **Fix G** (lock only on geometry-validated, non-degenerate detections; keep sampling past the window; self-heal; never lock ANY-BEST/Hough) + **Fix B** (`_homography_degenerate` corner-reprojection/convex/cond gate — NOT an H-diag gate; healthy MATCHi H_diag hits −1142 and projects fine via radial).
2. **Degenerate/overfit LOCK** (caught by the completed match-4 run #3 `348c2293`) — Fix G locked a geometry-"VALIDATED" detection on **8 clustered observations** → piecewise lens fit **overfit (rms=0.0)** → 0/23,795 court coords. Every gate passed; the OUTPUT was never checked. **Fix (projection self-test, `5dc5e97`):** `_projection_quality()` projects a court-region pixel grid through the candidate calibration; the lock now requires `coverage ≥35% AND y_span ≥6m AND x_span ≥3m` (robust p90−p10) — rejects the 0%-coverage overfit AND the "80% coverage but y-collapsed-to-23.77" degenerate. On failure: keep sampling (re-fit every +4 obs); fail-loud at the hard cap. `COURT_MIN_CALIB_OBS` 8→12, hard cap 18k→36k, fit wrapped try/except (clustered obs can raise singular-matrix).

**Local receipts (real weights + match-4 original):** self-heals at frame 5550 / 24 obs → real fit (`rms=2.84px`, 86% coverage, monotonic geometry far `y=15.7` → near `y=24.4`, x≈5.1 centre) — matches the hand-fed `proof.py`. `bench_calib` 4/4, serve bench green. **Prod confirmation pending the next Batch rebuild + re-run.**

Also shipped: **45×40 ROI guard** (`roi_extractors/{pose,bounces}.py`, fires correctly — caught match-4's degenerate ROI), **Fix C+** Render coverage-floor fail-loud (`upload_app.py`), **`bench_calib`/`bench_lens`** harnesses + fixtures.

**Fix E (camera-agnostic lens distortion) — BUILT + DORMANT on `main`** (`lens_distortion.py`, `T5_CALIB_LENS_MODE=off`): division-model→Brown-Conrady + fisheye + auto-select + residual-straightness, guarded against the collapse-to-singularity degenerate. NOT wired into hot paths → zero effect on default. **NOT ENABLED** — needs a Class-C/D (phone-ultrawide/GoPro) fixture to validate + transform-layer wiring (`§INTEGRATION` in the module) + a rebuild. Included in any `main` rebuild — can't be left behind.

Docs: `docs/_investigation/court_calibration_silent_degeneracy.md` (§Architectural proposal), `court_calibration_camera_taxonomy.md`, `.claude/court_calibration_implementation_kickoff.md`. Memory: [[feedback_calibration_lock_window]].

---

## 🔴 #1 — LAND MATCH 4 (one Batch rebuild from `main` + SAHI relax)

Match-4 re-run #3 (`348c2293`, 11h-timeout override) **ran to completion (6.5h)** on rev 56/g5 — proving the pipeline is now robust end-to-end (11h timeout + ROI guards = no silent 2h scans / 6h guillotine) — but produced **0% court coords** because rev 56's image predates the projection-self-test fix. The fix is on `main`; it just needs to ship.

**PATH (Batch-side → bench-green required):**
1. Merge `opt/overnight-findings` (SAHI skip relax + MOG2 downscale + Lambda fix) into `main`. **Run `python -m ml_pipeline.diag.bench` first — must stay 20/24·23/24.** (`main` already has the calibration fix + Fix E.)
2. Docker rebuild **from `main`** → dual-region ECR → new job-def rev cloning **56 (eu) / 38 (us)** (preserve all env) + add `SAHI_SKIP_A_FAR_YMAX=8.0` (+ optionally `MOG2_DOWNSCALE=2`). BATCH-SIDE CHECKLIST. (Use `.claude/tmp/calib_audit/register_jobdef.py <amd64-digest> <region>`.)
3. Re-run: `aws batch submit-job ... --container-overrides '{"command":["--job-id","ca475740-9e34-49c3-9b59-0194bfa37013","--s3-key","wix-uploads/1779964630_Tomo_vs_Jimbo.mp4"]}'` (args-only — NOT the job-def stored command).
4. **Validate before celebrating** (queries below): court coverage HIGH (was 0%), silver rows >0, `court_calibration: LOCKED ... cov=..% y_span=..m` in logs. THEN re-ingest → corpus → ADR-01 bounce full-data retrain.

**SAHI reality (other agent):** SAHI = 76% of wall; the skip-relax alone WON'T fully fix match 4 (far player full-frame-resolves only ~3% → SAHI genuinely needed). The real cycle-time lever is **L2 tile-fan cost reduction**, not skip. So match 4 may still be slow-ish until L2; the calibration fix makes it *correct* regardless. Cycle-time is the other agent's lane.

### 🐞 Confirmed prod bug (fix in daylight)
Job-def STORED command AND `lambda/ml_trigger.py:85` both repeat the `python -m ml_pipeline` ENTRYPOINT → container dies at argparse. Auto-spawn (`upload_app.py:923`, args-only) is correct. **Direct S3 uploads BROKEN on rev 55+.** Fix on `opt/overnight-findings`.

## Deploy state (reference)
- **Live job-defs:** eu rev **56** / us rev **38** — amd64 `sha256:f70c5795…` = calibration fix **without** the projection self-test (pre-`5dc5e97`) + L3. **Stale vs `main`.** Next rebuild supersedes.
- `main` HEAD has: Fix G/B/ROI/C+ **+ projection self-test** + Fix E (dormant) + L3. Render C+ auto-deployed.
- g5.xlarge CE idle ($0); eu queue g5→g4dn→Spot (untouched).

## Open items
| # | Item | Notes |
|---|---|---|
| 1 | **Rebuild from `main` (+SAHI merge) → land match 4** | the path above. Deploys the projection-self-test fix. |
| 2 | **Validate match 4 in prod** | coverage HIGH + silver >0 + sound geometry. Only THEN is calibration "proven in prod". |
| 3 | **Re-ingest match 4 → corpus → ADR-01 bounce full-data retrain** | blocked on #1/#2. |
| 4 | **SAHI L2 tile-fan cost reduction** | other agent; the real cycle-time lever (skip-relax insufficient). |
| 5 | **Fix Lambda double-entrypoint** | on `opt/overnight-findings`; restores direct S3 uploads. |
| 6 | **Enable Fix E** | needs a phone-ultrawide/GoPro fixture → validate/tune → wire transform layer → rebuild. Own cycle. |

```sql
-- validate match 4 after the rebuild + re-run
SELECT (SELECT AVG((court_x IS NOT NULL)::int) FROM ml_analysis.ball_detections   WHERE job_id=<j>) ball_cov,
       (SELECT AVG((court_x IS NOT NULL)::int) FROM ml_analysis.player_detections WHERE job_id=<j>) player_cov;
SELECT COUNT(*) FROM silver.point_detail WHERE task_id='ca475740-9e34-49c3-9b59-0194bfa37013' AND model='t5';
```

## Coordination
- **Other agent:** SAHI findings + Lambda fix on `opt/overnight-findings`; owns cycle-time (6.5h→target, L2). One rebuild from `main`+their branch lands both fixes.
- **ADR agent:** bounce GR candidate gen (`4a36f34`), swing-type scaffold (`8c6a1af`). No overlap.

## Research artefacts
`.claude/tmp/calib_audit/` (gitignored): `audit.csv`, sample frames, `repro_prod.py`/`spread_test.py`/`proof.py`/`verify_fix.py` (reproduction harnesses), `register_jobdef.py` (deploy script). Synthesis docs committed under `docs/_investigation/` + `.claude/court_calibration_implementation_kickoff.md`.

---

**END OF PICKUP**
