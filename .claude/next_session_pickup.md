# Next-session pickup — 2026-05-29 — Court calibration FIXED+DEPLOYED & proven in prod; match 4 blocked on SAHI (fix prototyped)

> 🛑 **2026-05-29 11:48 — CORRECTION (new evidence; supersedes "calibration proven in prod" for match 4). Match-4 re-run #3 (`348c2293`, 11h-timeout override) RAN TO COMPLETION (6.5h, no timeout) — and the completed output proves calibration is STILL DEGENERATE on this match.** The earlier "PROVEN in prod" was inferred from the `LOCKED VALIDATED` log line on a run (`6000423a`) that **timed out before it ever projected/exported** — so the coords were never actually checked. The finished run shows:
> - `compute_speeds: 23795/23795 pairs had None court coords` · `roi_pose: far ROI degenerate (48x41)` · `roi_bounces: degenerate (88x81)` · `bronze_export: ball=23796 player=3374` — **100% NULL court coords, 0% coverage** (`court_detected=True` notwithstanding).
> - **Why the "validated" lock is still degenerate:** the locked homography's `H_diag` y-scale is **~8–38×** (healthy ≈ 0.5–2) — the inliers/confidence/corner-reproj gate PASSED it anyway. And `lens calibration locked → mode=piecewise rms=0.0000 px from 8 observations` = an **overfit piecewise lens fit** (8 pts, 0 error). So Fix E (piecewise) WAS active and did not rescue projection.
> - **Net: match 4 still does NOT land usable coords; bounce full-data retrain stays BLOCKED.** I did NOT re-ingest (would load NULL-coord garbage / trip Fix C+). This is a **calibration-accuracy problem on this camera** — needs Tomo + calibration agent, not a brute re-run (same code → same degenerate lock). Leads: (1) make the degeneracy gate reject the ~8–38× y-scale that's passing; (2) the piecewise lens overfits on 8 obs — check it isn't worsening projection.
> - **What worked:** 11h timeout let it finish; ROI guards skipped the wasted scan; pipeline is now robust end-to-end. **SAHI finding stands** (76% of wall) but the skip-relax won't help match 4 (far player full-frame-resolved only ~3% → SAHI genuinely needed; the real lever is L2 tile-fan cost reduction). State: g5 CE idle ($0), nothing running, `ml_analysis` ca475740 clean. Memory: [[project_t5_may28_batch_runtime_plan]].

## ⚡ Executive summary (read first — 30 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. `bench_calib` 4/4. `bench_lens` well-behaved. Identity `100%`. `bench_bounce` v1 GR-mode `23.9%/9.1%` (baseline NOT locked). `bench_swing_type` STOPGAP.
**Last session:** the court-calibration silent-degeneracy fix shipped + deployed (rev 56/38) and is **proven working in prod**. Fix E (camera-agnostic lens distortion) built but dormant. Match-4 re-run then **failed on a NEW bottleneck (SAHI = 76% of wall → 6h cap)** — NOT calibration. SAHI fix prototyped on a branch overnight.
**#1 job next session:** land match 4 — merge `opt/overnight-findings` (SAHI skip relax), bench-green, Batch rebuild from `main` (carries the calibration fix for free), re-run. Then re-ingest → corpus → ADR-01 bounce full-data retrain.
**Don't:** submit direct S3 uploads via the job-def stored command — it's broken on rev 55+ (Lambda double-entrypoint bug, see below). Use args-only `containerOverrides.command`.

If that's enough → go. Depth below.

---

## 🔴 #1 — LAND MATCH 4 (blocked on SAHI; fix prototyped, needs daylight Batch deploy)

Match-4 re-run (`ca475740`, Batch job `6000423a`, rev 56/g5) ran end-to-end with live monitoring and **failed at the 6h cap (frame 65,900/71,915, ~74%), wrote NO bronze**.

- ✅ **Calibration fix PROVEN in prod:** `court_calibration: LOCKED VALIDATED detection after 2220 frames (inliers=11, confidence=0.79). No more CNN runs.` Fix G self-healed on rally footage exactly as designed. Confirmed **NOT wide-angle**.
- ❌ **New bottleneck = SAHI:** `SAHI = 16,101s / 21,119s ≈ 76% of wall`, skip rate ~0% (8/58,880), effective ~320 ms/fr (slower than the 183 baseline). Ironic interaction: fixing calibration gave SAHI a real `court_bbox` to tile every detect-frame, and the skip rule never fired.
- **Why SAHI never skips (root-caused + prototyped overnight):** SAHI skip Rule A needs the far player's feet at `court_y ≤ 5.0m`; a far player mid-court (5–9m) fails it → `has_far_pose` never true → SAHI runs every frame. Prototype on branch **`opt/overnight-findings`**: env-gated **`SAHI_SKIP_A_FAR_YMAX`** (recommend `8.0`) + `MOG2_DOWNSCALE` + the Lambda command fix. Report: `docs/_investigation/batch_optimisation_findings_2026-05-28.md`. **Bench was NOT run in the overnight sandbox — a human MUST run `python -m ml_pipeline.diag.bench` before deploy.**
- **State:** g5 CE idle (`desiredvCpus=0`, $0); queue g5→g4dn→Spot; `ml_analysis` for `ca475740` empty (no bronze). Not resubmitted (would repeat the timeout).

**MORNING PATH (all Batch-side → daylight + bench-green):**
1. Review/merge `opt/overnight-findings` (SAHI skip relax + MOG2 downscale) into `main`. **Run `python -m ml_pipeline.diag.bench` first — must stay 20/24·23/24.**
2. Docker rebuild **from `main`** → dual-region ECR → new job-def rev **cloning 56 (eu) / 38 (us)** (preserve all env) + add `SAHI_SKIP_A_FAR_YMAX=8.0` (+ optionally `MOG2_DOWNSCALE=2`). BATCH-SIDE CHECKLIST. **Building from `main` automatically includes the calibration fix (G/B/ROI/C+) + Fix E (dormant) + L3 — they're all committed; no calibration re-work.**
3. Re-run: `aws batch submit-job ... --container-overrides '{"command":["--job-id","ca475740-9e34-49c3-9b59-0194bfa37013","--s3-key","wix-uploads/1779964630_Tomo_vs_Jimbo.mp4"]}'` (args-only — NOT the job-def stored command). Expect completion well under 6h AND calibration-fixed coords.
4. THEN: re-ingest → corpus → ADR-01 bounce v1 **full-data retrain** (blocked until match 4 lands).

Memory: [[project_t5_may28_batch_runtime_plan]].

### 🐞 Confirmed prod bug (fix in daylight)
Job-def STORED command AND `lambda/ml_trigger.py:85` both repeat the `python -m ml_pipeline` ENTRYPOINT prefix → container dies at argparse (`unrecognized arguments: -m ml_pipeline`). Auto-spawn path (`upload_app.py:923`, args-only) is correct. **Direct S3 uploads BROKEN on rev 55+.** Fix the Lambda (on the branch) + the job-def stored command.

---

## ✅ Court calibration — root cause FIXED + DEPLOYED + PROVEN (2026-05-28 calibration session)

Root cause was **NOT wide-angle** (disproven by live reproduction on the real weights, then re-confirmed by the prod run above). It was the fixed-camera **"lock in first 300 frames, never re-run CNN"** strategy freezing a degenerate Hough homography when the opening window is unrepresentative. The CNN finds 12–13/14 keypoints on rally footage; calibration just never looked there.

**Shipped + deployed (all `main`, bench green; commits `e44f259`→`bb520e0`):**
- **Fix G** — lock ONLY on a geometry-validated, non-degenerate detection after ≥`COURT_MIN_CALIB_OBS`(8) obs; keep sampling past the window (self-heals); never lock ANY-BEST/Hough; fail-loud at the hard cap.
- **Fix B** — `_homography_degenerate()` corner-reprojection / convex-quad / cond-number gate. **NOT an H-diag gate** (healthy MATCHi has H_diag up to −1142 and projects fine via radial — Fix A was dropped as unsafe).
- **45×40 ROI guard** — `roi_extractors/{pose,bounces}.py` bail before the ~2h wasted scan.
- **Fix C+** — Render coverage-floor fail-loud (`T5_CALIB_MIN_COVERAGE`/`T5_CALIB_WEAK_COVERAGE`).
- **`bench_calib`** + 31 fixtures (`ml_pipeline/diag/bench_calib.py`, `fixtures_calib/`) — 4/4 pass.
- **Fix E — BUILT + DORMANT** (`ml_pipeline/lens_distortion.py` + `bench_lens`): camera-agnostic distortion estimator (division-model→Brown-Conrady + fisheye + auto-select + residual-straightness), guarded against the collapse-to-singularity degenerate (caught k1=1e20). Gated behind `T5_CALIB_LENS_MODE` (default `off`), NOT wired into projection hot paths → zero effect on default path. **NOT ENABLED** — needs a Class-C/D (phone-ultrawide/GoPro) fixture to validate + the transform-layer wiring (documented in the module's `§INTEGRATION`) + a rebuild.

Docs: `docs/_investigation/court_calibration_silent_degeneracy.md` (§Architectural proposal), `court_calibration_camera_taxonomy.md`, `.claude/court_calibration_implementation_kickoff.md`. Memory: [[feedback_calibration_lock_window]].

---

## Deploy state (reference)

- **Job-defs ACTIVE:** eu-north-1 **rev 56**, us-east-1 **rev 38** — amd64 `sha256:f70c57954274fadf518acb902e360e4ccd2415a437b46e3e36b01f0b9649e81b` (calibration fix + L3), env `PLAYER_BATCH_SIZE=8 + ROI_POSE_FP16=1 + ROI_BATCH_SIZE=16 + YOLO_FP16=1` + retry 3. (rev 55/37 were the perf-only predecessors.)
- **Compute:** g5.xlarge CE idle ($0); eu queue order g5→g4dn→Spot.
- **Render:** C+ auto-deployed from `main`; `/healthz` 200.
- **Perf stack (L1/L3/L4/L5/L7):** deployed but the ms/frame target (≤70) is **not yet validated** — match-4 run was SAHI-bound + timed out before bronze. Validate on the next clean completion.

## Open items

| # | Item | Notes |
|---|---|---|
| 1 | **Land match 4** | the SAHI morning path above. Unblocks everything downstream. |
| 2 | **Fix Lambda double-entrypoint** | on `opt/overnight-findings`; restores direct S3 uploads. |
| 3 | **Re-ingest match 4 → corpus → ADR-01 bounce full-data retrain** | +273 clean floor labels; GR mode recall expected 24% → 30-40%. Blocked on #1. |
| 4 | **Validate perf stack (ms/frame ≤70, batch ≤90min)** | on the first clean match-4 (or any) completion. Queries below. |
| 5 | **Enable Fix E** | acquire a phone-ultrawide + GoPro tennis clip → add `bench_lens` fixtures → validate/tune → wire transform layer (`lens_distortion.py §INTEGRATION`) → rebuild. Own cycle. |
| 6 | **ADR-02 swing-type weights** | scaffold shipped; needs ~5-10 more matches of swing labels (~1,172 / 2-3k). |

```sql
-- validate a completed match
SELECT task_id, total_frames, ms_per_frame,
       EXTRACT(EPOCH FROM (batch_end_at - batch_start_at))/60 AS batch_min
  FROM ml_analysis.video_analysis_jobs
 WHERE status='complete' AND batch_start_at > NOW() - INTERVAL '24 hours'
 ORDER BY batch_start_at DESC;
-- court coverage (was 0%/0% on the degenerate run): expect HIGH
SELECT (SELECT AVG((court_x IS NOT NULL)::int) FROM ml_analysis.ball_detections   WHERE job_id=<j>) ball_cov,
       (SELECT AVG((court_x IS NOT NULL)::int) FROM ml_analysis.player_detections WHERE job_id=<j>) player_cov;
```

## Coordination with other agents
- **Overnight caretaker** (match-4 re-run): left branch `opt/overnight-findings` + findings doc; CE idled. Their SAHI fix + my calibration fix are both consumed by one rebuild from `main`.
- **ADR agent:** bounce GR candidate generator shipped (`4a36f34`, Match-1 recall 3.0%→23.9%); swing-type scaffold (`8c6a1af`). Files `ml_pipeline/{bounce_detector,stroke_classifier,training}/` — no overlap with calibration. All on `main`.

## Research artefacts (calibration session)
`.claude/tmp/calib_audit/` (gitignored): `audit.csv` (60-job camera/coverage matrix), sample frames, `repro.py`/`proof.py`/`preproc.py`/`verify_fix.py` (reproduction harnesses), `register_jobdef.py` (deploy script). Synthesis docs committed under `docs/_investigation/` + `.claude/court_calibration_implementation_kickoff.md`.

---

**END OF PICKUP**
