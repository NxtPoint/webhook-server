# Phase 5b — Ball Tracker Characterisation

**Created:** 2026-05-20 PM by Claude session (post-Phase-3-pt2-revert), before any tuning.
**Purpose:** Document the four-tier ball-detection pipeline + every Hough/threshold parameter that gates coverage, so next session can run a measurement-first tuning loop instead of blind-tuning.
**Status of code:** unchanged at session end. A single small change (motion threshold 25 → 15) is staged on branch `phase-5b/motion-threshold-reduce` (NOT merged to main, NOT in Batch image — see "What's already on a branch" below).

---

## TL;DR for next session

1. The original handover said "don't blind-tune; characterise first." This doc IS the characterisation.
2. **Coverage diagnostics are already enabled in prod** — every Batch job runs `BallTracker.log_diagnostics()` via `pipeline.py:292`. Fetch the latest 880dff02 Batch CloudWatch log to read the actual tier breakdown.
3. **The "Hough fallback" in the original handover refers to `_detect_ball_frame_delta` (Tier 4 below), not the Tier 1 Hough on TrackNet's heatmap.** Tier 1 Hough's parameters are already extremely permissive (`param2=2`, `min/max radius 1-10`) — almost no headroom left there.
4. **The five biggest-leverage tuning candidates are listed at the bottom** with predicted impact + measurement plan. Ship one at a time. Bench is downstream of `ball_tracker.py` so it stays green by design — the real validation is a Batch rerun + comparing `ml_analysis.ball_detections` row count.
5. A safe single-parameter change is already staged on the branch above. If you want to ship something fast, that's the lowest-risk start.

---

## The four detector tiers (in order, per frame)

Every frame in a Batch job runs through up to four detectors. The first one that returns a position wins.

```
Frame N (BGR)
   │
   ▼
┌──────────────────────────────────────────────────────────────────┐
│ TrackNet (V2 3-frame OR V3 8-frame+background) → heatmap         │
└──────────────────────────────────────────────────────────────────┘
   │ heatmap → _postprocess_heatmap(feature_map)
   ▼
┌──────────────────────────────────────────────────────────────────┐
│ Tier 1: cv2.HoughCircles on binary mask of heatmap               │
│         params: TRACKNET_HOUGH_* (config.py)                     │
│         counter: _diag["tier1_hough"]                            │
└──────────────────────────────────────────────────────────────────┘
   │ none found
   ▼
┌──────────────────────────────────────────────────────────────────┐
│ Tier 2: cv2.connectedComponentsWithStats — largest blob          │
│         area gate: 2 <= area <= 200                              │
│         counters: _diag["tier2_cc"], _diag["tier2_cc_rejected_size"]│
└──────────────────────────────────────────────────────────────────┘
   │ no blob fits
   ▼
┌──────────────────────────────────────────────────────────────────┐
│ Tier 3: heatmap argmax (any signal above THRESHOLD=127)          │
│         counter: _diag["tier3_argmax"]                           │
└──────────────────────────────────────────────────────────────────┘
   │ heatmap empty (max < 127) — TrackNet produced nothing
   ▼  (returns None from _postprocess_heatmap → detect_frame falls through)
┌──────────────────────────────────────────────────────────────────┐
│ Tier 4: _detect_ball_frame_delta (THIS IS "THE HOUGH FALLBACK")  │
│         absdiff(curr_gray, prev_gray) → threshold(25)            │
│           → GaussianBlur(5,5) → threshold(15)                    │
│           → HoughCircles(dp=1, minDist=30, param1=50, param2=5,  │
│                          minRadius=2, maxRadius=15)              │
│         counter: _diag["delta_fallback_hits"]                    │
└──────────────────────────────────────────────────────────────────┘
   │ no circle found
   ▼
   None → frame has no ball detection
```

**Match-level coverage target:** ≥50% (currently 13% per `docs/_investigation/may07_sa_point6_gap.md`). **Worst-gap target:** <5s (currently 91.6s).

---

## Tier 1 Hough — TrackNet heatmap (config-driven)

Defined in `ml_pipeline/config.py`, used at `ball_tracker.py:382`.

| Param | Current | What it gates | Headroom |
|---|---|---|---|
| `TRACKNET_HOUGH_DP` | 1 | Inverse ratio of accumulator resolution. dp=1 = same as image. | None — dp=2 reduces precision more than it gains candidates. |
| `TRACKNET_HOUGH_MIN_DIST` | 1 | Min pixel distance between detected circle centres. | None — already 1, can't go lower. |
| `TRACKNET_HOUGH_PARAM1` | 50 | Canny edge upper threshold (internal). | Minor — could try 30. |
| `TRACKNET_HOUGH_PARAM2` | 2 | **Accumulator vote threshold for circle centres.** Lower = more circles. | **NONE — already extremely loose.** Lower than 2 is undefined behaviour. |
| `TRACKNET_HOUGH_MIN_RADIUS` | 1 | Smallest accepted radius. | None — already 1. |
| `TRACKNET_HOUGH_MAX_RADIUS` | 10 | Largest accepted radius. | Could raise to 15 to catch motion-blurred / closer-to-camera balls. |

**Net assessment of Tier 1:** maxed out on permissiveness. If Tier 1 is failing, the heatmap is empty — the model isn't producing signal at all (Tier 1 needs `heatmap.max() ≥ TRACKNET_HEATMAP_THRESHOLD=127`). **Don't tune Tier 1 first.**

---

## Tier 2 Connected Component (hardcoded)

`ball_tracker.py:400-411`.

| Setting | Current | Role | Tuning notes |
|---|---|---|---|
| `connectivity` | 8 | 8-direction neighbour scan. | Standard, leave. |
| Area gate | `2 <= area <= 200` | Ball blob size in 640×360 input. | Upper bound 200 may reject motion-blurred balls (smears can be 200-300 px). Try **upper = 300**. |

`_diag["tier2_cc_rejected_size"]` shows how often a blob fired but was rejected by size. If that number is significant on 880dff02, widen the upper bound.

---

## Tier 3 argmax (hardcoded)

`ball_tracker.py:419-423`. Only runs if `fm.max() > TRACKNET_HEATMAP_THRESHOLD`. No parameters to tune; it's the "give us any peak in the heatmap" fallback.

---

## Tier 4 frame-delta Hough — THE FALLBACK (hardcoded, biggest target)

`ball_tracker.py:473-517` (`_detect_ball_frame_delta`). This is what the original handover meant by "Hough fallback gain-up." Activates ONLY when all three TrackNet tiers return None.

Pre-processing pipeline:
```python
delta = cv2.absdiff(curr_gray, prev_gray)               # frame difference
_, motion_mask = cv2.threshold(delta, 25, 255, BINARY)  # gate 1: motion intensity
motion_mask = cv2.GaussianBlur(motion_mask, (5,5), 0)   # smear nearby motion
_, motion_mask = cv2.threshold(motion_mask, 15, 255, BINARY)  # gate 2: post-blur cleanup
circles = cv2.HoughCircles(motion_mask, HOUGH_GRADIENT,
    dp=1, minDist=30, param1=50, param2=5,
    minRadius=2, maxRadius=15)
```

| Setting | Current | Role | Gain candidate |
|---|---|---|---|
| **Motion threshold (gate 1)** | **25** | Reject pixels where between-frame intensity diff is small. Ball-against-bright-court can have local diff of 10-25. | **YES — try 15.** Highest-leverage single change. |
| Post-blur threshold (gate 2) | 15 | Keep only well-merged motion regions after blur. | Try 8-10. Lower preserves smaller blobs. Couple to motion threshold. |
| GaussianBlur kernel | (5,5) | Smear motion blobs to make them more circular for Hough. | Try (7,7). Larger kernel = bigger circular blobs = easier Hough match. |
| Hough `param2` | 5 | Vote threshold. | Try 3 (already aggressive, room to go more). |
| Hough `minRadius` | 2 | Smallest ball. | 1 (catches further/smaller-appearing balls). |
| Hough `maxRadius` | 15 | Largest ball. | 20 (motion-blurred balls smear bigger). |
| `prev_gray` source | N-1 frame only | Single-step temporal diff. | A 2-frame or 3-frame max-diff would catch faster balls that don't show enough N→N-1 motion. Bigger refactor, defer. |

**Net assessment of Tier 4:** the motion-threshold gate (25) is probably the single biggest leverage point in the entire pipeline. Tennis balls on a bright hard court can move with local pixel diff of 15-25, especially when the ball is brightly lit and the background is also bright. Lowering to 15 lets in this class of motion at the cost of admitting some lighting flicker — but Hough's shape filter (radius 2-15, param2=5) discriminates flicker from ball-shaped motion.

---

## Diagnostics — what to fetch before tuning

`BallTracker.log_diagnostics()` runs automatically (`pipeline.py:292`) at the end of every Batch job. The output lives in CloudWatch logs for the job. Pull the 880dff02 logs to see:

```
=== BallTracker diagnostics ===
frames_inferred: N
heatmap_empty (fm_max < threshold): X (Y.Y%)       ← Tier 1-3 input quality
avg mask nonzero pixels per frame: F.F
tier1_hough:         A (B.B%)                       ← Tier 1 hit rate
tier2_cc:            C (D.D%)                       ← Tier 2 hit rate
tier2_cc_rejected:   E (F.F%)                       ← Tier 2 size-gate misses (TUNE TARGET)
tier3_argmax:        G (H.H%)                       ← Tier 3 hit rate
none_returned:       I (J.J%)                       ← All TrackNet tiers failed
delta_fallback_hits: K (L.L%)                       ← Tier 4 saved them (TUNE TARGET)
fm_raw_max histogram (argmax class index, PRE *255):
  [  0- 31]: ...  ← bucket counts
  ...
fm_max histogram (uint8 value, POST *255):
  [  0- 31]: ...
  ...
```

### How to read it

- **If `heatmap_empty` is high (>60%):** TrackNet itself is failing on most frames. The 87% missing-coverage is dominated by TrackNet output, not by Tier 1-3 thresholds. **Tier 4 is the main lever.**
- **If `none_returned` is high AND `delta_fallback_hits` is low:** Tier 4 isn't catching the misses. **Motion threshold + Hough gates in Tier 4 need loosening.** Start with motion threshold.
- **If `tier2_cc_rejected_size` is high (10%+):** size gate is rejecting real-ball blobs. **Widen upper bound from 200 → 300.**
- **If `fm_raw_max_hist` shows most frames in bucket 0-31:** model is producing weak signal, regardless of postprocess. Suggests an input-pipeline issue (BGR/RGB, resolution, frame rate) — bigger investigation, not a single tune.

---

## Tuning workflow — measurement-first

Strict iteration protocol. Each change is a separate branch + Batch run.

1. **Pull baseline diagnostics** from latest 880dff02 Batch log. Record per-tier % in this doc.
2. **Read CLAUDE.md "Batch-side change checklist"** before any code change. Any edit to `ball_tracker.py` triggers Docker rebuild + dual-region ECR push + new job-def revisions.
3. **Pick ONE change from the candidate list below.** Lowest-risk first.
4. **Implement on branch** `phase-5b/<change-name>`. Bench check locally (must stay green).
5. **Docker rebuild + dual-region ECR push + new job-def revisions** per `.claude/handover_t5.md` "BATCH-SIDE CHANGE CHECKLIST".
6. **Tomo triggers Batch rerun** on 880dff02. ~30-60 min on Spot.
7. **Pull new diagnostics + ball_detections row count.** Compare against baseline.
8. **Record in this doc** under "Tuning rounds" below.
9. **If improved AND no regression in downstream silver/bench:** merge to main, lock as new baseline. If regression: revert, try next candidate.

### Candidate changes — **REPRIORITISED 2026-05-20** after Round 0 findings

The original eight candidates all targeted Tier 4 upstream of `_filter_outliers`.
Round 0 local diag (below) showed Tier 4 already fires on ~100% of frames at
the current threshold — so all Tier-4-upstream candidates have near-zero
leverage on the persisted-row count. The real bottleneck is `_filter_outliers`
eating 79% of Tier-4 returns.

**New priority list:**

| # | Change | File:Line | Predicted impact | Risk |
|---|---|---|---|---|
| **α** | **Source-aware `_filter_outliers`** (new top candidate) | `ball_tracker.py:551` + new `source` field on `BallDetection` | Big. Tier-4 detections gated against projected trajectory from recent Tier-1 anchors instead of previous-detection-of-any-kind. Specifically rescues Tier-4 contributions inside rally windows where Tier 1 is firing periodically. | Medium. Adds a "trust horizon" for Tier-1 anchor staleness. Locally characterisable before any Batch push. |
| β | Track-confirmation requirement | `detect_frame` + ring buffer | Require N consecutive nearby detections before any acceptance. Filter noise at source. | Higher. Loses leading 1-2 frames of each rally; bigger refactor. |
| — | ~~Motion threshold 25 → 15~~ | `ball_tracker.py:498` | **CROSSED OFF — Round 0 local experiment showed −11.6% post-filter survival. Hypothesis falsified.** | — |
| — | ~~Tier 2 CC upper bound 200 → 300~~ | `ball_tracker.py:408` | **CROSSED OFF — `tier2_cc_rejected_size = 0` on 880dff02 baseline. Gate never rejects on this video.** | — |
| (parked) | Post-blur threshold 15 → 8, Hough min/max radius tweaks, GaussianBlur kernel, param2 = 3, 2-frame max-diff | various lines in `_detect_ball_frame_delta` | All target Tier 4 upstream of the outlier filter. Round 0 evidence is the outlier filter is the dominant gate; tuning upstream of it has near-zero leverage on persisted-row count. **Reconsider only after α or β shifts the bottleneck.** | — |

---

## Status of `phase-5b/motion-threshold-reduce` — **SUPERSEDED 2026-05-20, do not merge**

The branch contains the threshold change documented above. Round 0 local
experiment ran the *exact* `_detect_ball_frame_delta` logic on the local
`a798eff0_sa_video.mp4` at both threshold=25 and threshold=15, with the
verbatim `_filter_outliers` applied. Result:

- Tier 4 per-frame returns: 15293 → 15299 (+6, ~0%)
- After `_filter_outliers`: 3205 → **2833 (−11.6%)**
- Surviving clusters: 27 → 41 (more, but shorter)
- Mean cluster length: 118.7 → 69.1 frames

The lever does NOT increase per-frame Tier 4 yield (saturated already);
it changes which circle Hough picks per frame, and with more competing
motion blobs the strongest pick is noisier — `_filter_outliers` rejects
more of them. Branch should be closed without merging.

The branch is retained on origin as a record of a tested-and-falsified
hypothesis. Don't reuse the name.

---

## Tuning rounds

| Round | Date | Change | Baseline coverage | New coverage | Verdict |
|---|---|---|---|---|---|
| 0 | 2026-05-20 | baseline from CloudWatch (`880dff02`, eu-north-1 stream `1f5ce...`, 2026-05-07 11:39 UTC) + local Tier-4 experiment on `a798eff0` video | 13% persisted on `880dff02`; tier-4 fires on 63.5% of `880dff02` frames, 100% on `a798eff0` | — | **Baseline established.** Per-frame detector returns ~100% of frames (5521 tier-1 + 63 tier-2 + 9707 tier-4 on `880dff02`); 7.7× collapse between detector returns and DB rows = `_filter_outliers` (150px gate) is the dominant filter, not the Tier-4 threshold. `tier2_cc_rejected = 0`, `tier3_argmax = 0` → CC upper-bound + argmax candidates inert on this video. Full receipts: `.claude/tmp/phase5b_round0_baseline.md` + `.claude/tmp/phase5b_round0_findings.md`. |
| 1 (cancelled) | 2026-05-20 | Motion threshold 25 → 15 (branch `phase-5b/motion-threshold-reduce`) | 13% | local diag: −11.6% post-filter survival | **CANCELLED, branch SUPERSEDED.** Local Tier-4-only experiment falsified the hypothesis before any Batch push. See "Status of `phase-5b/motion-threshold-reduce`" above. Next round 1 will target `_filter_outliers` instead (Option α from the candidate list). |

---

## Things NOT to do

- **Don't tune Tier 1 Hough** (`TRACKNET_HOUGH_*` in config.py). `param2=2` and radius 1-10 are already maximally permissive. If Tier 1 is failing it's because the heatmap is empty, not because Hough is rejecting good circles.
- **Don't lower `TRACKNET_HEATMAP_THRESHOLD=127`.** Comment in config.py explicitly notes "lowering to 100 broke ball detection" — prior session tried.
- **Don't ship multi-parameter changes.** Each candidate is one variable. Pairing #1 + #3 (motion threshold + post-blur) is tempting but breaks isolation of cause and effect.
- **Don't tune without diagnostics.** "It looks better visually" is not signal. Use `ml_analysis.ball_detections` row count + diag output.
- **Don't forget the Batch-side dance.** Edit in `ball_tracker.py` ≠ in Batch. Docker rebuild + ECR push + job-def revision is mandatory before each rerun.
- **Don't touch `ml_pipeline/training/visual_debug/`** — leftover debug images, Tomo's instruction.
