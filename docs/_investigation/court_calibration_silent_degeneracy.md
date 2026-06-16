# Court calibration silent degeneracy — investigation kickoff

> **RESOLVED 2026-05-29 (rev 57) — fixes shipped + proven in prod ("court calibration FIXED & PROVEN IN PROD", north_star). This doc is frozen historical; retained for the failure-mode taxonomy.**

**Tier:** REFERENCE / investigation
**Dated:** 2026-05-28
**Status:** KICKOFF — concrete diagnosis from match 4; fixes A/B/C ready to be sequenced; (D) needs research
**Triggered by:** match 4 (`ca475740-9e34-49c3-9b59-0194bfa37013`) producing 0 silver rows on 23,796 ball + 52,433 player detections, all with `court_x=NULL`

---

## The failure

Match 4 ran the full Batch pipeline (4h 58m of compute before transcode timeout) and exported bronze.json.gz successfully. The Render ingest read it cleanly. But every single bronze row had `court_x=NULL` / `court_y=NULL`, so silver Pass 1 — bounce-driven and dependent on court coords — produced **0 rows**. SES email was sent. No error surfaced anywhere.

SportAI processed the same video into 391 silver rows. **The video is fine — our calibration broke silently on it.**

## How rare is "0% court"?

Scanned the last 60 days of T5 matches:

| Match | Frames | Ball court% | Player court% | Verdict |
|---|---|---|---|---|
| **ca475740 (match 4)** | 71915 | **0.0%** | **0.0%** | **CATASTROPHIC** |
| 9378f2dd, c645a7ee | 66937 | 25.7% | 92.5% | weak ball, OK player |
| 78c32f53, 1d6feb3a | 15300 | 28-32% | 76-79% | weak ball |
| (8 prior canonical Tomo/Jimbo matches) | 15300 | ~97% | ~77% | healthy |

So:
- 0% catastrophic is rare (1 in 15)
- Sub-50% partial degradation is becoming common on the longer / newer videos (~5 in 15)

Trend: as new videos arrive with slightly different camera framing, calibration health is drifting down. Worth fixing before another silver-0 happens silently.

## Root cause — three layered failures

### (1) CNN returned 0 keypoints across the entire calibration window

The court detector tries the ResNet50 keypoint CNN first, then falls back to a Hough-line geometric fit. On match 4, the CNN returned 0 keypoints for **every** calibration frame (300-frame search window). Every detection went through the Hough fallback.

Why? Unknown — likely a camera-angle / lighting combination the CNN wasn't trained on. The canonical training corpus may not cover this perspective. **This is the (D) research item below.**

CloudWatch log evidence:
```
12:08:19  ml_pipeline.court_detector: _detect_hough: found 14/14 keypoints from 16 h_clusters × 9 v_clusters
12:08:19  ml_pipeline.court_detector: court_detect: using hough fallback (valid=14) because CNN returned 0 keypoints
12:08:19  ml_pipeline.court_detector: court_calibration: new best-ANY at frame=0 inliers=14 confidence=1.00 geometry=FAIL
```
("geometry=FAIL" = the perspective sanity check separately rejected this homography but it became the best-ANY anyway.)

### (2) Hough fallback validation passed a degenerate homography

The lock criterion checks `inliers >= threshold` and a computed `confidence`. Match 4's locked detection had:
- inliers = 11
- confidence = 0.79

Both pass. **But the homography H_diag values were wildly out of physical range:**

```
12:09:18  H_diag=[-0.67, -5.75]   (frame 200)
12:09:19  H_diag=[1.48, 3.60]
12:09:24  H_diag=[2.58, 8.20]      ← bad
12:09:29  H_diag=[-0.54, -2.09]
12:09:35  H_diag=[-0.59, -6.57]
12:09:36  H_diag=[21.43, 0.05]     ← catastrophic, became the LOCKED detection
12:09:36  court_calibration: LOCKED VALIDATED detection after 300 frames (inliers=11, confidence=0.79). No more CNN runs.
```

For a stable court projection at typical broadcast/handheld camera distance, `|H[0,0]|` and `|H[1,1]|` should sit roughly in `[0.5, 2.0]`. Values like `21.43` or `-5.75` mean the homography scales one axis by 20× or flips the other — a clear sign the 4-point projective fit is degenerate (collinear points, near-singular system, etc.).

**The validator never inspects H_diag.** Inliers + confidence pass; lock fires.

### (3) No post-lock projection sanity test; no Render-side fail-loud

After lock, `to_court_coords()` is called for every ball detection (in `BallTracker.detect_bounces`) and every player detection (in `PlayerTracker.map_to_court`). With a degenerate homography, every projection lands outside the ±5m sanity band and returns `None`. Silently. The bronze rows just get NULL court coords.

The ROI extractor logged the symptom: `roi_pose: scanned 65243 sampled frames, 0 detections, 0 usable poses in 7736.8s` — 2 hours of GPU time finding nothing because the ROI rectangle was projected to a 45×40 pixel box. But this is a warning, not a fatal.

Render-side `_do_ingest_t5` then runs silver build which produces 0 rows. The `silver_built = True` flag still flips because the SQL didn't ERROR — it just returned 0. SES email fires. Job marked complete.

**Three layers, all silent. Zero alerts at any point.**

## Fix options

| # | Fix | Where | Bench risk | BATCH-SIDE? | Estimated effort | Status |
|---|---|---|---|---|---|---|
| **A** | **H_diag sanity gate** before locking. Reject homographies where `|H[0,0]|` or `|H[1,1]|` is outside `[0.1, 5.0]` regardless of inliers/confidence. Falls back to next candidate, or stays unlocked (job continues with no court coords but at least bronze stays NULL deterministically). | `ml_pipeline/court_detector.py` validator | Low — strictly more rejective | YES | 1h | TODO |
| **B** | **Projection sanity self-test** after lock. Project the 4 doubles-court corners back to pixels via the locked homography. If any falls outside the frame bounds (with some margin), the homography is degenerate. Don't lock; keep searching. | `ml_pipeline/court_detector.py` lock path | Low | YES | 1h | TODO |
| **C** | **Render-side fail-loud check** in `_do_ingest_t5`. After bronze ingest, before silver build: if 0% of `ball_detections` for the task have `court_x` populated, set `ingest_error='calibration_degenerate_no_court_coords'`, skip silver build entirely, surface the error in the SES email. Idempotent — re-firing the ingest won't help (deterministic bronze), so this is a terminal state. | `upload_app.py::_do_ingest_t5` | None | NO (Render only) | 30 min | **SHIPPED** (`eec1dae`) |
| **D** | **Investigate why CNN returned 0 keypoints** on this video. Is it camera angle? Court color/contrast? Lighting? Cross-check against the canonical Tomo/Jimbo videos (which work fine) and the newer Dejan videos (which work partially). If a fixable pattern emerges, augment training data or add input-conditioned preprocessing. | research + maybe `court_detector.py` model layer | Medium — model change | YES (if model changes) | 1-3 days research | TODO (folds into the dedicated research session below) |

---

## 2026-05-28 (close 6) — RE-SCOPE: from "fix the bug" to "make calibration camera-agnostic"

**Tomo's read:** match 4 was likely recorded with a **wide-angle camera**. Standard pinhole-camera homography assumes straight lines remain straight under projection — wide-angle / fisheye lenses introduce **barrel distortion** that breaks this assumption. The Hough-fallback fits 4 court corners to a planar homography, but with barrel-distorted court lines, no 4-point fit can be both geometrically valid AND consistent with the curved image — the optimiser settles on a degenerate solution that passes inlier counts but is mathematically broken (H_diag `[21.43, 0.05]` is the signature).

This is **systemic, not a one-off bug**. As Tomo onboards more users with their own phones / GoPros / consumer cameras, the variability in:
- **Lens type** — wide-angle vs standard vs zoom
- **Field of view** — 60° vs 90° vs 120°+
- **Camera height** — tripod (~1.5m) vs handheld vs ceiling-mounted (~5m)
- **Court visibility** — full court vs partial (far baseline cropped)
- **Lighting** — daylight outdoor vs indoor floodlight vs night-mode

…will keep producing degenerate calibration in long-tail cases. We need a **camera-agnostic court mapping system** that is robust across the realistic variation space, not patches that only catch the specific failure mode we just saw.

**This warrants its own dedicated session, multi-agent research-first.** See `docs/_investigation/court_calibration_silent_degeneracy.md` §"Dedicated research session scope" below.

### Expanded fix set (E / F / G / H added)

| # | Fix | Scope | When |
|---|---|---|---|
| **E** | **Lens distortion model + correction.** Estimate barrel/fisheye distortion parameters (Brown-Conrady k1, k2, p1, p2 — `cv2.calibrateCamera`-style) up-front by fitting court lines as straight under undistortion. Apply undistortion to frames OR distort the canonical court model. Then homography becomes well-conditioned. Reference: OpenCV `cv2.undistort` + `cv2.fisheye.calibrate`. | Bronze-side, court_detector.py | Dedicated session |
| **F** | **End-to-end learned calibration.** A network that takes a frame and outputs the full 4×4 projection matrix (or 14 court keypoints) jointly. Trained on a diverse multi-camera corpus. Reference: TVCalib (CVPR 2023), Sport Camera Calibration with View-Invariant Keypoints (TPAMI 2024), No Bells Just Whistles (broadcast sport calibration). Replaces or augments the current 2-stage CNN-keypoint + homography-fit approach. | Bronze-side, new model layer | Dedicated session |
| **G** | **Multi-frame temporal consistency.** Stop trying to lock from a single frame. Aggregate keypoint detections across the first ~10-30 seconds, RANSAC the consensus, use motion to disambiguate near-duplicate solutions. Even a weak per-frame detector becomes strong via temporal voting. | Bronze-side, court_detector.py | Dedicated session |
| **H** | **Self-supervised calibration via player feet.** Player feet on the baseline / service line provide a free calibration signal (we know they're standing on a known line). YOLOv8x-pose already gives us ankle keypoints. Use feet-line correspondences as additional homography constraints. Robust to lens distortion if combined with E. | Bronze-side, court_detector.py + player_tracker.py | Dedicated session |

---

## Dedicated research session scope — "Court mapping 100% across cameras"

**Goal:** Move from the current ~95% (which silently drops to 0% on wide-angle outliers) to a calibration system that gracefully handles ANY consumer camera the product will encounter — wide-angle phones, GoPros, broadcast feeds, fixed tripods, handhelds.

**Why this is critical:** every downstream T5 fact (bounce x/y, serve detection, stroke classification, identity, far-player pose) is conditioned on a correct court projection. Bad calibration = bad bronze = bad silver = bad analytics. The user-facing dashboard is built on top of this layer. As the product onboards customers with diverse camera setups, the **bottom-most layer of the bronze stack** has to be the most robust.

**Session output deliverables:**
1. **Camera diversity audit** — collect 10-20 sample frames per camera class (wide-angle, standard, broadcast, low-angle, high-angle). Measure current calibration health on each. Identify which classes are broken and how.
2. **State-of-the-art landscape** — what does the academic + industry literature offer? TVCalib, CourtSight, broadcast sport calibration papers, OpenCV intrinsic estimation, fisheye unwrap, vanishing-point methods. What's a fit for amateur consumer cameras (vs broadcast)?
3. **Proposed architecture** — concrete recommendation for a calibration system that handles the realistic camera variation space. Likely: lens distortion estimation (E) + multi-frame temporal voting (G) + self-supervised player-feet refinement (H), gated by a robust sanity test that catches degeneracy before lock.
4. **Validation plan** — how to test we've actually hit 100% across the diversity audit. Includes a regression bench fixture that covers every camera class.

**Multi-agent strategy (PARALLEL, per the session prompt below):**
- **Agent 1 — academic literature scan.** State-of-the-art in sports/court calibration: TVCalib, no-bells-just-whistles, sport camera intrinsic estimation, learned vs geometric.
- **Agent 2 — lens distortion / camera intrinsics.** OpenCV calibrateCamera, fisheye module, Brown-Conrady model, distortion estimation from court lines / vanishing points.
- **Agent 3 — current codebase audit.** Map out exactly what `court_detector.py` does today (CNN keypoint head, Hough fallback, lock logic, projection sanity, to_court_coords semantics). Identify every place a bad homography can leak through.
- **Agent 4 — production data audit.** Pull ~20 recent T5 videos from S3 with diverse provenance (different uploaders, dates, camera signatures via ffprobe). Score each on current calibration health. Build the camera-class taxonomy from real data.

Each agent produces a focused report. Main thread synthesises into the proposed architecture + validation plan + a kickoff doc for the actual implementation session (which then sits separately from this research session).

**Out of scope for this research session:**
- Writing any production code
- Touching the deployed pipeline
- BATCH-SIDE CHECKLIST work
- The Render-side fail-loud already shipped as Fix (C) — orthogonal safety net

**In scope:**
- Reading code (Explore agent)
- Reading academic papers + project repos (WebFetch + WebSearch)
- Analysing production data (DB queries, S3 sampling)
- Producing a concrete architecture proposal + implementation plan

## Recommended sequencing

1. **(C) first** — pure Render, no Batch redeploy, 30 min. Catches future cases retroactively (won't silver-build a degenerate run). Cheap insurance.
2. **(A) + (B) together** — same Batch-side change cycle, bench has to be green, both attack the source of the silence. 2-3h end-to-end including Docker rebuild + dual-region ECR push + job-def revisions.
3. **(D) is its own thread** — schedule when the team has bandwidth for ML investigation. Lower urgency once (A)+(B)+(C) are in place because the silent-failure mode is gone.

## Why this matters beyond match 4

The trend in the table above is real: longer / newer videos are showing weaker court coverage (25-30% on ball is well below the canonical 97%). Even partial degradation matters because:
- silver Pass 1 row count drops proportionally to bounce-with-court count
- serve detector needs court-projected pose to distinguish baseline serves from mid-court trophy poses
- bounce_d zone classification is impossible without court coords
- the corpus auto-land hook depends on bronze quality — partial calibration may emit lower-quality labels

The catastrophic mode is rare today. But the failure is silent, and the partial-degradation trend suggests we'll hit another 0% case sooner rather than later if we don't add the gates.

## Cross-references

- `feedback_t5_architecture_rules.md` — bronze = single source of truth; degenerate bronze cascades to silver=0
- `_archive/north_star_2026-05-07_phantom-bounce-era.md` — historical court-homography bug; ongoing concern
- `bronze_silver_18_audit.md` — court mapping is field #2 of the 18, currently rated "faithful homography ~90%" — the degenerate-lock failure mode is what flips this from 90% to 0%

---

# 2026-05-28 — ARCHITECTURAL PROPOSAL (multi-agent research session + live reproduction)

**Status:** Research complete. This section SUPERSEDES the "wide-angle barrel distortion" root-cause hypothesis above with reproduced, evidence-backed findings. The fix set A–H is re-prioritised accordingly. Sibling docs: `docs/_investigation/court_calibration_camera_taxonomy.md` (camera classes + breakage matrix + fixtures) and `.claude/court_calibration_implementation_kickoff.md` (next-session execution plan). Raw audit data: `.claude/tmp/calib_audit/` (`audit.csv` + sample frames + repro scripts).

## ⚡ Executive summary (read this first — 90 seconds)

The wide-angle hypothesis was **not** the cause. We reproduced both catastrophic cases locally against the real detector and weights.

**Root cause (proven):** the fixed-camera calibration strategy **locks within the first 300 frames and then never runs the CNN again** ("No more CNN runs"). When that opening window is unrepresentative (pre-match / setup / panning / occlusion), the keypoint CNN finds 0 keypoints → the **Hough fallback fabricates 14 bogus keypoints** → a **degenerate homography locks as `ANY-BEST`** and is frozen for the entire video — even though the CNN nails 12–13/14 keypoints seconds later on real rally footage.

**Receipts (local reproduction on the real `court_keypoints.pth`):**

| Test | Result |
|---|---|
| Match 4, frames 0–330 (the actual lock window), CNN only | **0–3 / 14** keypoints → no geometry-valid frame → `calibration=None`, `n_obs=0` → locks Hough garbage `ANY-BEST` |
| Match 4, frames at 5/30/50/70 % (rally footage), CNN only, **no preprocessing** | **12–13 / 14** keypoints |
| Match 4, fed a representative window | locks **VALIDATED + radial** (13/14, conf 0.93, 11 obs); projects mid `x=5.57` (≈centre line), near `y=24.4`, far `y=15.0` — all physically correct ✅ |
| `f11eed2c` (the *second* 0 % case — the MATCHi bench-fixture court), run on its clean trim | locks **VALIDATED + radial**, projection identical to the healthy `880dff02` ✅ |

Match 4 was **always fully calibratable**; the lens and court never defeated the system. `f11eed2c` is the *same camera/court* as the healthy 97 % `880dff02` bench fixture — a fixed lens cannot be the variable. Both failures are **calibration-window / frame-selection failures**, and the partial-coverage tail (25–32 %, see table in §"How rare") is the same mechanism locking a *mediocre* window.

**Two corrections to the original fix set:**
- **🚩 Fix A (H-diag range gate) is actively DANGEROUS — DROP IT.** The *healthy* MATCHi lock carries `H_diag` up to `(-109, -1142)` and projects perfectly (projection runs through the radial calibration model, not the raw homography). The `MAX_SCALE=20` gate was correctly removed; re-adding any H-diagonal bound would reject the bench-fixture court.
- **Fix C (0 %-NULL fail-loud, already shipped `eec1dae`) is necessary but INSUFFICIENT.** A degenerate homography can project to *plausible-but-wrong* non-NULL coordinates (our local match-4 repro did exactly this), which slips past a NULL-count check. We need a **positive calibration-quality gate**, not just "are the coords NULL".

**Tomo's camera note stands and is consistent with the evidence:** both courts *are* wide lenses — the radial Brown-Conrady calibration is actively chosen and doing real work (`mode=radial`, `n_obs=11`) on both MATCHi and match 4 whenever it gets good keypoints. Wide-angle is real; it is just **not** the failure trigger here. Lens-distortion robustness (Fix E) therefore stays a **co-priority** (Tomo, 2026-05-28) — built now for the genuinely-wide phones/GoPros coming as users onboard — but it is decoupled from the priority-zero frame-selection fix.

## Re-prioritised fix set

Layered, defence-in-depth. Priority-zero closes the proven failure; co-priority future-proofs for diverse cameras.

| Layer | Fix | What it does | Priority | Side | Trips BATCH-SIDE? |
|---|---|---|---|---|---|
| **0** | **G — robust frame selection + temporal voting** | Stop locking blindly at frame 300. Sample the CNN across a longer/smarter window (≥ first 60 s, skip low-keypoint/occluded frames), accumulate ≥N **geometry-validated** detections, aggregate via median-keypoint + RANSAC consensus, then lock. **Never lock an `ANY-BEST`/Hough detection when zero validated detections exist — keep sampling deeper into the video instead.** Fail-loud only if the *entire* video yields no validated calibration. | **P0** | court_detector.py | YES |
| **0** | **B — geometric degeneracy gate** | Before accepting/locking any homography: reproject the 4 doubles corners (+ baselines) to pixels; reject if any corner falls outside frame (+margin), if the quad is non-convex, or if the homography condition number is pathological. Gates the Hough fallback's fabricated keypoints. **Use this, NOT H-diag (Fix A).** | **P0** | court_detector.py | YES |
| **0** | **C+ — positive calibration-quality gate** | Upgrade the shipped Render fail-loud from "0 % NULL" to a quality score: radial-calibration RMS ≤ threshold, ≥N validated observations, corner-reprojection pass, AND a sample of real detections projecting in-band. Below quality → `calibration_degenerate`, skip silver, surface in SES. Catches plausible-but-wrong degenerates. | **P0** | upload_app.py (Render) + court_detector.py emits the score | NO (Render) |
| **0** | **45×40 ROI guard** | In `roi_extractors/{pose,bounces}.py::prepare()`, bail (fatal) if the projected ROI area is below a min fraction of frame — prevents the ~2 h wasted GPU scan when calibration is degenerate. | **P0** | roi_extractors/ | YES |
| **co** | **E — lens / camera-agnostic distortion** | Extend the existing radial Brown-Conrady (k1,k2) model per Agent 2: (a) line-based **division-model** distortion estimation as a front-end so distortion is recoverable even with sparse keypoints; (b) auto **fisheye (Kannala-Brandt)** escalation chosen by residual line-straightness for GoPro-class lenses; (c) apply correction at the **coordinate-transform layer via `cv2.undistortPoints`** (court keypoints for the fit, individual detections downstream) — **never full-frame remap**; (d) validate via residual line straightness. Future-proofs for diverse consumer cameras. | **co-P0** (Tomo) | camera_calibration.py | YES |
| **later** | **Detector robustness (build-first / train-last)** | Short term: strengthen the line/Hough fallback so it only proposes through the Layer-B gate. Medium term: add a points+lines geometric calibrator (PnLCalib / TVCalib lineage) to vote with the CNN; train-last, fine-tune keypoints on the free dual-submit corpus across courts/lighting/FOV. | later | court_detector.py / new model | YES if model |
| **later** | **H — self-supervised player-feet** | Use YOLOv8-pose ankle keypoints on known lines (baseline/service) as extra homography constraints when court keypoints are sparse. | later | court_detector.py + player_tracker.py | YES |

## Why this ordering

Layer-0 (G + B + C+ + ROI guard) is **cheap, Render/Batch-side `court_detector.py` logic, requires no model retrain**, and provably fixes match 4, f11eed2c, and the partial tail in one cycle. It is the highest leverage per line changed. E is built in parallel as the camera-agnostic insurance but is decoupled — it would not have fixed these matches. Detector-robustness/training and H follow the north-star "build-first, train-last" rule and ride the free dual-submit corpus.

## What the literature contributes (Agents 1 + 2)

- The current detector is a **TrackNet-style keypoint CNN** (yastrebksv/TennisCourtDetector lineage, 640×360 in, 15 heatmaps), **broadcast-trained** — a narrow-FOV inductive bias. An off-the-shelf end-to-end *learned* calibrator is **not** a drop-in: every sports calibrator with public weights (TVCalib, No-Bells, PnLCalib, KaliCalib) is trained on broadcast soccer/basketball; no public amateur-tennis calibrator with weights exists. The durable detector upgrade is a **points+lines + physically-constrained camera-model fit** (PnLCalib / TVCalib design) **fine-tuned on our own corpus** — which is the train-last lane, not today.
- **PnLCalib** (HRNet points+lines → DLT + non-linear refinement, **native lens-distortion optimisation as of Mar 2026**, GPL-2.0) and **TVCalib** (differentiable segment-reprojection optimiser recovering pose+focal+**distortion**, immune by construction to a degenerate flat homography) are the two reference architectures for the later detector upgrade.
- For Fix E specifically: the court lines **are** the calibration target — **plumb-line / one-parameter division-model** straightness fitting (Fitzgibbon; IPOL 2014/106 + 2016/130) estimates distortion with no checkerboard, converts to OpenCV `(k1,k2)`, escalates to fisheye when residual straightness demands. Apply via `cv2.undistortPoints` at the transform layer (microseconds/detection, no detector retraining, no full-frame remap).

## Validation plan ("100 % across cameras")

1. **New `bench_calib` harness** (local; mirrors `bench`/`bench_ball` discipline): replay the *opening calibration window* of one fixture per camera class and assert — locks **VALIDATED** (never `ANY-BEST`-from-Hough), correct calibration `mode`, ≥N observations, known reference points project within tolerance, and synthetic court-coverage ≥ threshold. Add the **window-trap negative fixture** (match 4's first 300 frames) and assert it does **NOT** lock garbage (must keep searching / fail-loud, not freeze a degenerate H).
2. **Serve bench stays green** — the `a798eff0`/`880dff02` fixtures ARE the MATCHi court; any `court_detector.py` change can shift calibration on them, so `python -m ml_pipeline.diag.bench` must stay `20/24` & `23/24` after every edit (rules #5, #9).
3. **Re-run match 4** (`ca475740`) on the fixed Batch image → expect VALIDATED + radial + high coverage + **no 2 h wasted ROI scan** → lands the rich corpus data. This is the end-to-end proof Tomo wants, and on the new g5/rev-55 perf stack it should be the ~60–90 min run.
4. **Caveat (honest):** production has **zero real consumer-camera diversity** today (all uploads 1080p h264, single uploader). True camera-agnostic validation needs borrowed/synthetic GoPro-fisheye + phone-wide fixtures — proposed in the taxonomy doc. Until those exist we cannot *prove* E end-to-end, only the radial path on MATCHi/outdoor-club.
