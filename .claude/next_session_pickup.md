# Next-session pickup — 2026-05-29 — Court degenerate-lock FIXED (projection self-test), validated locally + on `main`; awaits ONE Batch rebuild to land match 4

## ⚡ Executive summary (read first — 30 seconds)

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
