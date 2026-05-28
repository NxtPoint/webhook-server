# Next-session pickup — 2026-05-28 (close 7 + CALIBRATION session) — Batch perf + ADR-01 v1 + GR candidate gen SHIPPED; COURT CALIBRATION ROOT-CAUSE FIXED & DEPLOYED (rev 56/38)

> ⭐ **CALIBRATION SESSION UPDATE (latest, supersedes the "calibration architectural fix / dedicated research session" items below).**
> The dedicated calibration research session ran AND its fix shipped same-day. Root cause was **NOT wide-angle** (disproven by live reproduction on the real weights) — it was the fixed-camera **"lock in first 300 frames, never re-run CNN"** strategy freezing a degenerate Hough homography when the opening window is unrepresentative (pre-match/setup). The CNN finds 12-13/14 keypoints on rally footage; the calibration just never looked there.
> **Shipped + deployed (all on `main`, bench green 20/24·23/24):** Fix G (lock-only-validated frame-selection + self-heal), Fix B (geometric degeneracy gate — corner-reprojection, NOT H-diag), 45×40 ROI guard, Fix C+ (Render coverage-floor fail-loud), `bench_calib` + 31 fixtures (4/4 pass). Commits `8c720a7`→`7be3cd1`.
> **Batch image rebuilt from latest `main` (bundles calibration fix + L3), pushed dual-region; job-defs registered eu-north-1 rev 56 / us-east-1 rev 38** — amd64 `sha256:f70c5795…`, **all perf env vars preserved** (`PLAYER_BATCH_SIZE=8 + ROI_POSE_FP16=1 + ROI_BATCH_SIZE=16 + YOLO_FP16=1`) + retry 3; **queue untouched (g5 routing survives)**.
> **→ Match 4 (`ca475740`) ready to re-run on rev 56** (g5, ~90 min) — will produce calibration-fixed coords + skip the ~2 h wasted ROI scan + land rich corpus data. **NEXT SESSION: validate that re-run** (queries in the calibration block lower down / impl-kickoff doc).
> **Fix E (lens/camera-agnostic) BUILT + DORMANT** (`bb520e0` `ml_pipeline/lens_distortion.py` + `bench_lens`). Camera-agnostic distortion estimator (division-model→Brown-Conrady + fisheye + auto-select + residual-straightness validation), numerically guarded against the collapse-to-singularity degenerate (caught k1=1e20). **Gated behind `T5_CALIB_LENS_MODE` (default `off`) and NOT wired into the projection hot paths** → zero effect on the running match-4 job (rev 56, no E) or the default path. `bench_lens` confirms it's well-behaved on real wide footage (recovers MATCHi's mild barrel; no divergence). **NOT YET ENABLED** — needs a Class-C/D (phone-ultrawide/GoPro) fixture to validate accuracy + the consistent transform-layer wiring (documented in `lens_distortion.py` docstring §INTEGRATION) + a Batch rebuild. Docs: `court_calibration_silent_degeneracy.md` §Architectural proposal, `court_calibration_camera_taxonomy.md`, `.claude/court_calibration_implementation_kickoff.md`. Memory: [[feedback_calibration_lock_window]].

## ⚡ Executive summary (read first — 30 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (close 7)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. Identity `100%` unchanged. `bench_bounce` v1 GR-retrained @ thr=0.5: **23.9% recall / 9.1% precision** (vs is_bounce-mode 3.0%/3.6% — **6.5× lift**); baseline intentionally NOT locked until multi-match training data is available. `bench_swing_type` STOPGAP (no weights).
**What shipped:** Batch perf (L1+L3+L4+L5+L7), calibration fail-loud (C), ADR-01 v1 training infra + gravity-residual candidate generator with env-rollback, ADR-02 v1 swing-type scaffold. Job-defs eu-north-1 rev 55 / us-east-1 rev 37 with `YOLO_FP16=1 + ROI_POSE_FP16=1 + PLAYER_BATCH_SIZE=8 + ROI_BATCH_SIZE=16`; eu queue re-routed g5→g4dn→Spot.
**What's open:** (1) validate L1+L3+L4+L5+L7 on the next real upload (passive — happens automatically); (2) calibration architectural fix (camera-agnostic court mapping) — **dedicated multi-agent research session** opening prompt at the bottom of this file; (3) tactical calibration (A)+(B) as stopgap; (4) re-ingest Match 4 post-calibration → +273 clean floor labels → retrain bounce v1 → expected 30-40% recall.
**Match 4:** corpus row landed (664/397/114 labels) but bronze-features-corrupt (NULL `court_x`); awaits calibration fix + re-ingest.

If this is enough → go. If you're picking up a blocker or need depth → continue reading.

---

**What shipped this session — four parallel streams:**

### Stream 1: Batch perf L1+L3+L4+L5 + L7 (G5.xlarge) — SHIPPED + DEPLOYED + JOB-DEFS LIVE
- `024bb40` L1 player-stage GPU batching (PLAYER_BATCH_SIZE env, default 1)
- `b25b356` L4 ROI ViTPose batching + FP16 (ROI_BATCH_SIZE / ROI_POSE_FP16 env)
- `c660845` L5 NVENC GPU transcode (Dockerfile installs nvenc-enabled ffmpeg from BtbN; Python auto-falls back to libx264 if nvenc fails; TRANSCODE_CODEC env)
- `2218f09` L3 YOLO FP16 inference (YOLO_FP16 env)
- L7 G5.xlarge (A10G) compute environment — no code, AWS Batch infra-only. New g5 CE created + eu queue re-routed g5→g4dn→Spot. SAHI FP16 (L2) deferred — sahi 0.11.18 has no half flag.
- Docker image rebuilt, pushed to eu-north-1 + us-east-1 ECR.
- **Job-defs registered:** eu-north-1 rev **55**, us-east-1 rev **37** — pin the new digest AND set `YOLO_FP16=1 + ROI_POSE_FP16=1 + PLAYER_BATCH_SIZE=8 + ROI_BATCH_SIZE=16`. FP16 rollback = unset env (no rebuild); queue rollback = update-job-queue g4dn-first.
- Equivalence verified locally: `.claude/tmp/test_player_batch_equiv.py` shows `_run_yolo_batch([f]*N)` per-frame-identical to N separate `_run_yolo(f)` calls (max box diff 0.000000).
- **Nothing validated yet — no upload since close-6;** next real T5 upload runs the full stack on g5/rev55, read `ms_per_frame` (target ≤70 vs 183 baseline).

### Stream 2: Calibration fail-loud safety net (C) — SHIPPED + DEPLOYED
- `eec1dae` Render-side fail-loud in `_do_ingest_t5`: if 0% of bronze rows have court_x populated (≥100 row threshold both sides), mark `ingest_error='calibration_degenerate_no_court_coords'`, set `last_status='failed_calibration'` + `ingest_finished_at=now()`, skip silver/serve/stroke/notify, return False. Also logs WARN when ball-court coverage <20% (currently 5 of 15 recent matches show 25-32%).
- Pure Render — auto-deployed; `/healthz` 200.
- Catches match-4-class catastrophic failures retroactively. Doesn't fix the root cause (degenerate homography locking); that's the Batch-side companion (A)+(B) — STILL TODO.

### Stream 3: ADR-01 v1 bounce_detector training infra + gravity-residual candidate generator (6.5× recall lift)
- `a2bf4b8` `feat(t5): ADR-01 v1 bounce_detector training infra (dataset + trainer + bench --weights-path)` — 4 files, 871 insertions / 3 deletions.
  - `ml_pipeline/bounce_detector/dataset.py` — `build_manifest()` mines positives + 5× negatives. Positives use the audit's strict gate (is_bounce ±5 fr + ≤50 px) where it fires, fall back to SA `bounce_frame_est` otherwise.
  - `ml_pipeline/training/train_bounce_detector.py` — AdamW + cosine warmup + BCELoss + WeightedRandomSampler + per-epoch (loss / acc / prec / rec / F1 / PR-AUC) + early-stop on F1 or PR-AUC. Wrapped `{state_dict, meta}` save format.
  - `ml_pipeline/bounce_detector/cnn.py` — `load_weights()` accepts both bare state_dict AND wrapped trainer output.
  - `ml_pipeline/diag/bench_bounce.py` — `--weights-path` flag.
- v1 weights trained end-to-end on Match 1's 67 floor labels (50 epochs, early-stop epoch 6, val F1=0.40). Match 4's 273 floor labels deliberately excluded — calibration corruption (100% NULL `court_x`) poisons feature channels 0/1/7/8/9/10. **Bench at thresholds 0.3/0.4/0.5/0.55/0.7: recall 3-5% flat; precision climbs 1.2% → 8.3%.**

**The ceiling insight (the load-bearing finding from this session):** the detector's production candidate pool is `ball_detections WHERE is_bounce=TRUE` — TrackNet's velocity-reversal heuristic. Per `.claude/adr01_label_audit_2026-05-28.md`, only **6/67 (9%)** of SA floor labels have a matching is_bounce candidate within ±5 frames + ≤50 px. So even a perfect model on the existing candidate pool maxes out at ~9% recall. **More training data won't fix this** (the corpus could grow to 10,000 labels and recall would still hit the 9% wall). The right lever is **candidate generation**: swap the `is_bounce` filter in `_candidate_frames_from_raw_bounces` (currently `detector.py:177`) for a sliding-window peak detector on the gravity-residual feature (already computed in `feature_extractor.py` channel 6 — that's the most discriminative single feature per ADR-01, fitted against a parabolic prior). Expected after candidate-generation lift: recall ceiling 9% → ~50% (ball-coverage limited). THEN retraining is meaningful. Memory: [project_t5_may28_bounce_v1_candidate_ceiling](memory).

**Baseline NOT locked.** 3-5% recall would be a misleading floor that future work could trivially "beat" without actually improving anything. Lock the baseline after the candidate-generation work lands and we have honest numbers.

**UPDATE later same session — gravity-residual candidate generator SHIPPED (`4a36f34`):**

- `ml_pipeline/bounce_detector/detector.py` — `_candidate_frames_from_gravity_residual(ball_rows, threshold=10px, min_gap=4)` reuses `feature_extractor._gravity_residual`. Image-y based (calibration-independent → still works on Match-4-class failures). `_select_candidates()` dispatches on `BOUNCE_CANDIDATE_MODE` env var (default `is_bounce` — safe; opt-in `gravity_residual`).
- `bounce_detector/dataset.py` + `training/train_bounce_detector.py` — `candidate_mode` parameter so training data can be sampled from the same pool the detector runs at inference (train/inference parity).
- Default threshold 10px tuned on Match 1; full sweep + comparison table is in the detector.py docstring.

**Match 1 bench @ thr=0.5:**

| mode + weights | candidates | recall | precision | mean_err |
|---|---|---|---|---|
| is_bounce + v1 | 55 | 3.0% | 3.6% | 0.55m |
| **gravity_residual + v1 (no retrain)** | 148 | **20.9%** | **9.5%** | **0.30m** |
| **gravity_residual + GR-retrained (val F1 0.52)** | 175 | **23.9%** | **9.1%** | **0.30m** |

**6.5× recall lift from the candidate swap alone**, plus spatial error halves because GR peaks land closer to the actual bounce frame than TrackNet's `is_bounce` flag. GR-retraining adds ~3pp more recall on top. Same weights file gets used either way — no production deploy needed for the lift to materialise once weights are wired (the env var flip is the prod knob).

**Match 4 caveat:** GR loose-match on M4 was only 14% (vs M1's 58%). M4 was SIGKILL'd mid-run + calibration-corrupt; not a fair test. Re-validate on the next clean upload. Until then, default threshold stays at the M1-tuned 10px.

**Next-session priority for bounce work:** Match 4 re-ingest post-calibration-fix → 273 more clean floor labels → retrain GR mode v1 → expected recall climb to 30-40% with multi-match training data.

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

⭐ **PRIORITY 0 — Dedicated calibration research session (Tomo flagged 2026-05-28 close 6).** Match 4's calibration failure was almost certainly a wide-angle camera triggering barrel-distortion-driven homography degeneracy. As more users onboard with diverse phones / GoPros / consumer cameras, this long tail will keep breaking the bottom-most layer of the bronze stack. Move from "fix the bug" to **camera-agnostic court mapping**, multi-agent research-first. **OWN session.** Full scope + multi-agent strategy: `docs/_investigation/court_calibration_silent_degeneracy.md` §"Dedicated research session scope". Opening prompt at the bottom of this pickup file.

| # | Item | Owner | Effort | Notes |
|---|---|---|---|---|
| 1 | **VALIDATE L1+L3+L4+L5+L7 perf** on the next real T5 upload | next session | passive | The next user-uploaded T5 match lands on eu-north-1 rev 55 with `YOLO_FP16=1 + ROI_POSE_FP16=1 + PLAYER_BATCH_SIZE=8 + ROI_BATCH_SIZE=16` active on G5.xlarge. Expected ms/frame ≤70 (vs 183 baseline). Read `ms_per_frame` + `batch_duration_sec − processing_time_sec` from `ml_analysis.video_analysis_jobs`. Target full match ≤90 min vs match 4's 6h (SIGKILL). |
| 2 | ~~L3 YOLO FP16~~ — **SHIPPED `2218f09`.** Active on job-def rev 55/37. | ~~next session~~ | — | — |
| 3 | ~~L7 G5.xlarge (A10G)~~ — **SHIPPED close-7.** g5 CE created, eu queue re-routed g5→g4dn→Spot. | ~~next session~~ | — | — |
| 4 | **Calibration tactical fixes (A) + (B)** — stopgap | next session | ~2-3h with deploy | (A) H_diag sanity gate in `court_detector.py` — reject homographies where `\|H[0,0]\|` or `\|H[1,1]\|` outside `[0.1, 5.0]`. (B) post-lock projection self-test — project 4 court corners, reject if any falls outside frame. Batch-side, trips checklist. Bundle both in same Docker rebuild. **Stopgap until Priority 0 architectural fix lands** — these catch the failure but don't make calibration camera-agnostic. Fix doc: `docs/_investigation/court_calibration_silent_degeneracy.md` §Fix options. |
| 5 | **Calibration architectural fix (E + F + G + H)** | PRIORITY 0 session | research + implementation | Camera-agnostic court mapping. Lens distortion model + correction (E), end-to-end learned calibration (F), multi-frame temporal voting (G), self-supervised player-feet refinement (H). Goes through the multi-agent research session FIRST (this pickup's opening prompt), then a separate implementation session. See doc §"Dedicated research session scope". |
| 6 | ~~ADR-01 v1+ gravity-residual peak detector~~ — **SHIPPED `4a36f34` this session.** Match 1 recall 3.0% → 23.9% (8×). Next bounce work is post-Match-4-re-ingest multi-match retrain. | ~~next session~~ | — | — |
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

---

## ⭐ OPENING PROMPT FOR THE NEXT DEDICATED SESSION: Camera-agnostic court calibration

**Paste this into a fresh chat when starting the calibration research session.**

```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's session: dedicated multi-agent RESEARCH session — make T5 court
calibration camera-agnostic. The goal is 100% robust calibration across
the realistic consumer-camera variation space (wide-angle phones, GoPros,
broadcast-style fixed tripods, handhelds — any field-of-view, any lens
distortion, any vantage angle).

WHY THIS SESSION EXISTS: match 4 (ca475740, 2026-05-28) silently locked a
degenerate homography on a likely-wide-angle camera. CNN returned 0
keypoints; Hough fallback locked H_diag [21.43, 0.05]; every projection
returned None; all 23,796 ball + 52,433 player rows had court_x=NULL;
silver built 0 rows; user got a "your match is ready" email. Match 4 was
1 in 15 catastrophic, but 5 in 15 recent T5 matches show 25-32% ball-court
coverage (well below the canonical 97%). As more users onboard with
diverse cameras, this long tail keeps breaking the bottom-most layer of
the bronze stack — every downstream T5 fact depends on it. Tomo wants the
court mapping moved from ~95% to 100% across cameras, treated as the
priority-zero piece going forward.

Full re-scope + expanded fix set (E/F/G/H) + multi-agent research strategy:
docs/_investigation/court_calibration_silent_degeneracy.md §"Dedicated
research session scope".

THIS IS A RESEARCH SESSION. No production code today. No BATCH-SIDE
CHECKLIST. Output: a concrete architecture proposal + validation plan +
implementation kickoff doc that the NEXT session executes.

EXECUTION — spawn FOUR parallel general-purpose research agents in a
SINGLE message (Agent tool with subagent_type=general-purpose, all in one
turn so they run concurrently). Each owns a focused scope and returns a
written report. Main thread synthesises.

  Agent 1 — Academic + industry literature scan
  Brief: research the state of the art in sports court / pitch
  calibration for video analytics. Target papers + projects: TVCalib
  (Theiner & Ewerth, CVPR 2023), "No Bells, Just Whistles" (broadcast
  sport calibration), TenniSet, CourtSight, KaliCalib, Sport Camera
  Calibration with View-Invariant Keypoints (TPAMI 2024), and any
  related work on amateur/consumer-camera sport calibration (vs
  broadcast). For each promising approach: model architecture, training
  data, what cameras it handles, public code/weights availability, and
  fit for our use case (amateur tennis, side-cam ~3-5m up, variable
  phones / GoPros / tripods). Return a ranked shortlist with pros/cons.
  Use WebFetch + WebSearch liberally.

  Agent 2 — Lens distortion / camera intrinsics estimation
  Brief: research how to estimate and correct lens distortion from a
  single video (NO checkerboard, NO known calibration target). Focus
  on: OpenCV cv2.calibrateCamera + cv2.fisheye.calibrate; Brown-Conrady
  k1/k2/p1/p2 model; vanishing-point-based intrinsic estimation; using
  known straight lines (court lines!) as the calibration target ("plumb
  line" methods). Output: a concrete sub-pipeline that takes a frame
  with visible court lines and outputs (i) lens distortion params,
  (ii) an undistorted frame OR (iii) a distorted canonical court model
  for homography fitting on the original frame. Include code snippets +
  references.

  Agent 3 — Current codebase audit
  Brief: map exactly what ml_pipeline/court_detector.py does today. End
  to end: CNN keypoint head (which model? which weights? what input
  size? what training corpus?); Hough line fallback (algorithm + lock
  criteria + H_diag values seen in prod); to_court_coords semantics
  (±5m sanity bound, strict mode); _last_detection vs _locked_detection
  vs _best_validated_detection vs _best_detection (which does what?);
  every place a bad homography can leak through without warning;
  current camera-intrinsics handling (any? probably none); the radial
  calibration tracked in project_t5_apr15_breakthrough.md. Output: a
  ~2-page architectural map + a list of every assumption the current
  code makes about the camera (likely many implicit pinhole / standard-
  FOV assumptions that wide-angle violates). Use the Explore agent for
  the deep grep work.

  Agent 4 — Production data audit + camera-class taxonomy
  Brief: pull ~20-30 recent T5 videos from S3 (different uploaders,
  dates — find the diversity). For each: extract metadata via ffprobe
  (codec, resolution, FPS, camera signature if present in metadata
  tags), pull 3 sample frames at known timestamps (early / mid / late),
  query DB for current calibration health (ball court% + player court%
  + silver row count + first court_calibration LOCKED log line if
  available via CloudWatch). Build a CSV: task_id, uploader, video
  date, resolution, est_camera_class (wide-angle / standard / unknown
  based on visible distortion in the sample frames), current ball
  court%, silver_rows. Identify which camera classes break and which
  work. Use boto3 (default-chain creds work on this box) for S3, +
  db_init.engine for DB. Sample frames go to .claude/tmp/calib_audit/.

CONSTRAINTS:
- No git commits this session. Output is doc-only.
- No touching production. The L1+L4+L5 perf work + fix (C) shipped in
  close-6; let the next user upload validate that work organically.
- ADR agent is working on detector training (ADR-01 v1+ gravity-residual
  + ADR-02 v1 swing-type). Their files: ml_pipeline/{bounce_detector,
  stroke_classifier,training}/. Avoid touching those entirely.
- All four agents run in PARALLEL (one Agent message with four blocks).
  Don't sequence them — wastes hours.

SYNTHESIS DELIVERABLE (main thread does this AFTER agents complete):
1. docs/_investigation/court_calibration_silent_degeneracy.md — append
   a §"Architectural proposal" section: the recommended approach (likely
   E + G + H, with F as the longer-term target if the literature shows
   it's feasible for amateur cameras). Include diagrams if helpful.
2. docs/_investigation/court_calibration_camera_taxonomy.md (NEW) —
   the camera-class taxonomy from Agent 4 + the breakage matrix +
   a regression-fixture proposal (one canonical video per class).
3. .claude/court_calibration_implementation_kickoff.md (NEW) — what the
   NEXT (implementation) session does. File list. Step ordering. How
   to bench. How to deploy through BATCH-SIDE CHECKLIST. Estimated
   commits + Docker rebuilds.

SUCCESS CRITERIA for this research session:
- Four agent reports landed.
- One synthesis doc with a clear architectural recommendation.
- One taxonomy doc with the camera-class breakage matrix.
- One implementation kickoff doc with a concrete next-session plan.
- Tomo can read the synthesis in ~5 min and have a clear picture of
  what to build and why.

If at any point the multi-agent research surfaces something that
contradicts the wide-angle hypothesis (e.g. match 4's video turns out
NOT to be wide-angle), surface that to Tomo before continuing — the
hypothesis is load-bearing for the architecture choice.
```

---

**END OF PICKUP**
