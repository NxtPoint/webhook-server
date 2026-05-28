# Court calibration silent degeneracy — investigation kickoff

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

| # | Fix | Where | Bench risk | BATCH-SIDE? | Estimated effort |
|---|---|---|---|---|---|
| **A** | **H_diag sanity gate** before locking. Reject homographies where `|H[0,0]|` or `|H[1,1]|` is outside `[0.1, 5.0]` regardless of inliers/confidence. Falls back to next candidate, or stays unlocked (job continues with no court coords but at least bronze stays NULL deterministically). | `ml_pipeline/court_detector.py` validator | Low — strictly more rejective | YES | 1h |
| **B** | **Projection sanity self-test** after lock. Project the 4 doubles-court corners back to pixels via the locked homography. If any falls outside the frame bounds (with some margin), the homography is degenerate. Don't lock; keep searching. | `ml_pipeline/court_detector.py` lock path | Low | YES | 1h |
| **C** | **Render-side fail-loud check** in `_do_ingest_t5`. After bronze ingest, before silver build: if 0% of `ball_detections` for the task have `court_x` populated, set `ingest_error='calibration_degenerate_no_court_coords'`, skip silver build entirely, surface the error in the SES email. Idempotent — re-firing the ingest won't help (deterministic bronze), so this is a terminal state. | `upload_app.py::_do_ingest_t5` | None | NO (Render only) | 30 min |
| **D** | **Investigate why CNN returned 0 keypoints** on this video. Is it camera angle? Court color/contrast? Lighting? Cross-check against the canonical Tomo/Jimbo videos (which work fine) and the newer Dejan videos (which work partially). If a fixable pattern emerges, augment training data or add input-conditioned preprocessing. | research + maybe `court_detector.py` model layer | Medium — model change | YES (if model changes) | 1-3 days research |

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
