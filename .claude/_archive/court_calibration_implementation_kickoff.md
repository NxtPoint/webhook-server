# Court calibration — implementation kickoff (for the NEXT session)

**Tier:** handover / kickoff
**Dated:** 2026-05-28 (written by the calibration research session)
**Read first:** `docs/_investigation/court_calibration_silent_degeneracy.md` §"2026-05-28 — ARCHITECTURAL PROPOSAL" (root cause + re-prioritised fix set) and `docs/_investigation/court_calibration_camera_taxonomy.md` (camera classes + fixtures). Raw evidence + repro scripts: `.claude/tmp/calib_audit/`.

This doc is the execution plan. Research is done; this is build + deploy.

## The one-paragraph problem statement

The fixed-camera court calibration **locks within the first 300 frames and never runs the CNN again**. When the opening window is unrepresentative, the keypoint CNN finds 0 keypoints, the Hough fallback fabricates 14 bogus keypoints, and a **degenerate homography locks as `ANY-BEST`** for the whole video — even though the CNN gets 12–13/14 keypoints on rally footage seconds later. Proven on match 4 (`ca475740`) and `f11eed2c`; both are fully calibratable with a representative window. Fix = robust frame selection + temporal voting + never-lock-garbage + a geometric degeneracy gate, plus a co-priority lens-distortion extension for future wide cameras.

## ⚠️ Two hard constraints before you touch code

1. **`court_detector.py` is NOT in the CI bench trigger globs, but it FEEDS the serve detector.** A frame-selection/lock change *will* alter calibration on the `a798eff0`/`880dff02` MATCHi fixtures, which can shift the serve bench. **Run `python -m ml_pipeline.diag.bench` locally after every edit** and keep it `a798eff0=20/24, 880dff02=23/24` (rules #5, #9). CI won't catch a regression here — you must.
2. **All of this except the Render fail-loud is BATCH-SIDE.** `court_detector.py`, `camera_calibration.py`, `roi_extractors/{pose,bounces}.py` run on AWS Batch → Docker rebuild + dual-region ECR push + new job-def revisions in **eu-north-1 + us-east-1** before any re-run (rule #8, full checklist in `.claude/handover_t5.md`). Note: `court_detector.py` + `camera_calibration.py` are pulled in via `pipeline.py` but are **not** named in the rule-#8 trigger glob — treat them as Batch-side anyway and consider adding them to the documented list (as `db_writer.py` was added 2026-05-22).

## File map

| File | Change | Layer | Side |
|---|---|---|---|
| `ml_pipeline/court_detector.py` | Frame-selection + temporal-voting window; never lock `ANY-BEST`-from-Hough when 0 validated; geometric degeneracy gate (corner-reprojection / convex-quad / condition-number); emit a calibration-quality score | G + B | Batch |
| `ml_pipeline/roi_extractors/pose.py` | Min-ROI-area guard in `prepare()` (bail fatal below threshold) | 45×40 guard | Batch |
| `ml_pipeline/roi_extractors/bounces.py` | Same min-ROI-area guard in `prepare()` | 45×40 guard | Batch |
| `upload_app.py::_do_ingest_t5` | Upgrade shipped Fix C (`eec1dae`) from "0 % NULL" to the positive calibration-quality gate (RMS + ≥N obs + corner-reprojection + in-band sample) | C+ | **Render only** |
| `ml_pipeline/camera_calibration.py` | Extend E: line-based division-model distortion front-end; auto fisheye (Kannala-Brandt) escalation by residual straightness; keep `cv2.undistortPoints` transform-layer application | E (co-priority) | Batch |
| `ml_pipeline/diag/bench_calib.py` (NEW) + `ml_pipeline/fixtures_calib/` (NEW) | New calibration regression harness + per-class fixtures incl. the window-trap negative | validation | local |

## Step ordering (one change per commit, bench-green between each)

1. **Geometric degeneracy gate (B)** in `court_detector.py` — corner-reprojection-in-frame + convex-quad + condition-number test. Wire it so the Hough fallback and the lock both pass through it. **Do NOT add an H-diag range gate (Fix A) — it rejects the healthy MATCHi court.** → bench.
2. **Frame selection + temporal voting (G)** — extend the calibration window (≥60 s), skip low-keypoint/occluded frames, require ≥N geometry-validated detections, aggregate (median keypoints + RANSAC), and **never lock `ANY-BEST` when `_best_validated_detection is None` — keep sampling deeper; fail-loud only if the whole video yields nothing.** → bench (this is the change most likely to move the serve bench; verify carefully).
3. **45×40 ROI guard** in `roi_extractors/{pose,bounces}.py::prepare()`. → bench.
4. **`bench_calib` + fixtures** — build the harness and the 3 available fixtures (`fixture_indoor_matchi`, `fixture_outdoor_club`, `fixture_window_trap`); assert VALIDATED locks + projection tolerance + the negative case never locks garbage. Local-only (like `bench_ball`/`bench_silver`).
5. **C+ Render quality gate** in `upload_app.py` — consumes the quality score emitted in step 1/2. Render-only, no Docker rebuild.
6. **E lens extension** in `camera_calibration.py` — division-model front-end + fisheye escalation. Validate on `fixture_indoor_matchi`/`fixture_outdoor_club` (radial path); fisheye path stays unverified until a Class-C/D fixture is acquired (see taxonomy doc). → bench.
7. *(later, separate session)* detector robustness / points+lines model (PnLCalib/TVCalib lineage, train-last on dual-submit corpus) + Fix H (player-feet constraints).

## Deploy (after steps 1–3 + 6, bundle the Batch-side changes into ONE rebuild)

Per `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST":
1. `python -m ml_pipeline.diag.bench` green + `bench_calib` green.
2. Docker build (amd64), push to **eu-north-1 + us-east-1** ECR.
3. Register new job-def revisions in both regions pinning the new amd64 sub-manifest digest; preserve `retryStrategy` and the existing env vars (`PLAYER_BATCH_SIZE=8 / ROI_BATCH_SIZE=16 / YOLO_FP16=1 / ROI_POSE_FP16=1` — confirm current rev: eu `:55`, us `:37` per close-7).
4. Render auto-deploys the `upload_app.py` C+ change on push (no rebuild).
5. `git push origin main` BEFORE asking for any re-run (rule #7).

**Estimated effort:** ~6 commits, **1 Docker rebuild cycle** (bundle B + G + ROI guard + E; C+ is Render-only). bench_calib + fixtures are local. The detector-model upgrade (step 7) is a separate session with its own rebuild.

## The end-to-end proof (Tomo's goal)

After deploy, **re-run match 4 (`ca475740`) as a fresh Batch job** (full re-detection — NOT a re-ingest of the stored bronze, whose court_x is baked NULL). Expect:
- court calibration locks **VALIDATED + radial**, high ball/player court %, silver rows > 0;
- **no ~2 h wasted ROI scan** (the 45×40 guard + correct calibration);
- on the g5/rev-55 perf stack, a **~60–90 min** end-to-end run;
- corpus auto-land emits the rich (non-NULL) data that match 4 should have had.

That single run proves both the calibration fix and the perf stack (L1+L4+L5+L3+L7) together.

## Cross-references
- `docs/_investigation/court_calibration_silent_degeneracy.md` — root cause, receipts, fix set.
- `docs/_investigation/court_calibration_camera_taxonomy.md` — camera classes, breakage matrix, fixtures.
- `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST + how to run the benches.
- `.claude/tmp/calib_audit/` — `audit.csv`, sample frames, `repro.py`/`proof.py`/`preproc.py` (the reproduction harnesses).
