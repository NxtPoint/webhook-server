# T5 bronze accuracy investigation — ball-bounce reconciliation

**Status:** REFERENCE / investigation. 2026-05-25. Decision-grade — feeds the Phase 7 reframe.
**Provenance:** every number below is from **live DB queries** (this dev box reaches the Render
prod Postgres via `db_init.engine`; see memory `reference_local_db_access.md`). Scripts archived
under `.claude/tmp/bounce_*.py`. Measured on **Match 1** (T5 job `78c32f53-5580-4a88-a4e7-7506e59b2b52`,
SA task `0d0514df-68aa-4346-9e2d-64413429e47f`), both 25 fps, same source video (time bases align,
offset ≈ 0).

> **One-line conclusion:** the bounce problem is **NOT court-calibration** (Phase 7 as scoped).
> The calibration is a faithful planar homography. The real levers are **bounce-detection
> precision** (T5 over-detects ~2×, mostly airborne false-positives) and **~0.5 s timing jitter**
> (downstream of ball coverage). The far baseline is **resolution-limited** (~1 px ≈ metres) — a
> physical limit recalibration can't remove.

---

## 1. Executive summary — the binding findings

1. **SA's 161 "bounces" are two kinds:** `floor` = 67 (ground bounces, the placement target) +
   `swing` = 94 (racquet contacts). Only the 67 floor bounces are the geometric target. (SA's own
   bounce data is **not gospel** — Tomo: SA is accurate on most signals but weak on ball bounce —
   so SA-relative coordinate error is a soft reference, not ground truth.)

2. **Event detection is fine; localization and precision are not.** T5 detects **85 %** of SA floor
   bounces within ±0.8 s (94 % within ±1.2 s) — we are not blind to bounce events. But of T5's
   **303** `is_bounce` flags in the match window, only **126 carry court coordinates**; **177 are
   nulled at projection.**

3. **The 177 nulled bounces are ~84 % airborne false-positives, not lost ground bounces.**
   Re-projecting their image pixels through a faithfully-reconstructed homography sends them
   off-court (median `court_y` −11 m); **149/177 (84 %) are detected *above* the far-baseline image
   row** (physically above/beyond the court plane); they show the ground-bounce trajectory
   signature 3× less often than real bounces (15 % vs 43 %). The strict ±5 m clamp in
   `to_court_coords` is correctly filtering them. At most ~28 (16 %) could be real far-baseline
   bounces — and those sit in the resolution-limited zone.

4. **The calibration is a faithful planar homography, reconstructable Render-side with no video.**
   A homography fit on the 14,198 player-feet correspondences in `player_detections`
   (`center_x, bbox_y2` → `court_x, court_y`) reproduces the 126 good bounces' stored court coords
   to **0.11 m median** (p90 0.21 m). So the per-job projection can be rebuilt locally — but it is
   **not persisted** today (the job row stores only `court_detected/confidence/fallback` booleans).

5. **Over-detection on the kept set too.** Even the 126 on-court bounces are a mix: only 43 % show
   a clean ground-bounce trajectory signature; the rest are racquet contacts (matching SA `swing`)
   or on-court-projecting noise. T5 fires 303 events vs SA's 161 → ~1.9× over-detection.

6. **Timing jitter ≈ 0.5 s** (nearest-neighbour median, SA floor → nearest T5 `is_bounce`).
   Symptom of 52 % ball-frame coverage — the bounce frame is loosely pinned.

7. **Far court is physically ill-conditioned.** Far baseline at image row ~243, near baseline at
   ~815; near the far baseline ~1 px ≈ several metres. This caps far-half bounce *accuracy*
   regardless of calibration (same root reason the far *player* is only ~30 px — see
   `far_player_accuracy.md`). Near-half placement is well-conditioned and likely already good.

---

## 2. Measurement detail

### 2.1 Counts

| Source | Events | Breakdown |
|---|---|---|
| SA `bronze.ball_bounce` | 161 | 67 floor + 94 swing; ts 54.5–603.7 s |
| T5 `ml_analysis.ball_detections is_bounce`, match window (≥54 s) | 303 | 126 with `court_x/y`, **177 image-only (court NULL)** |
| T5 warm-up (<54 s, excluded) | 13 with-xy | racquet-bouncing pre-first-serve, all near baseline |

T5 bounces are flagged on `ml_analysis.ball_detections` rows (`is_bounce=TRUE`, `source` NULL/`main`),
keyed by `job_id` = task_id; `frame_idx/25` → seconds. SA in `bronze.ball_bounce` (`timestamp`,
`court_x/y`, `type`). T5 writes nothing to `bronze.ball_bounce` (correct — single canonical bronze).

### 2.2 Recall / timing (time-matched, ±window)

| Window | SA floor ↔ any T5 is_bounce (event recall) | SA floor ↔ T5 with-coords (localization) | all-SA ↔ T5-all |
|---|---|---|---|
| ±0.4 s | 39 % | 21 % | 53 % |
| ±0.8 s | **85 %** | 61 % | 83 % |
| ±1.2 s | 94 % | 76 % | 94 % |

Nearest-neighbour time gap (SA floor → nearest T5 is_bounce): **median 0.48 s.** Event detection is
good; *timing* is loose (sparse ball coverage) and *localization* lags event detection (the gap is
the nulled 177).

### 2.3 Coordinate error (SA-relative, soft reference)

On the 13 floor bounces matched in BOTH time (±0.4 s) AND coords: euclid median **3.31 m**, |dy|
median 3.09 m, signed dy median −0.40 m; depth-dependent (far-half ≈ −4.5 m, near-half ≈ 0).
**Down-weight:** n = 13, and SA bounce coords are themselves unreliable.

### 2.4 Re-projection validation (`.claude/tmp/reproject_test.py`, `compose_177.py`)

- Homography (RANSAC) on 14,198 player feet → reproduces 126 good bounces to **0.11 m** median.
- Applied to the 177 nulled bounces → median `court_y` −11.3 m, **0/177 land on court** (radial-model
  caveat: a plain homography diverges from the real Brown-Conrady radial calibration exactly in the
  far court, so the precise far reproj is unreliable — but the next test is model-independent).
- **Model-independent:** far-baseline image row ≈ 243 px; **149/177 (84 %) detected above it** = above
  the court plane = airborne.
- Ground-bounce trajectory signature (ball at local-max image_y = lowest screen point): **15 %** of
  the 177 vs **43 %** of the 126 on-court control.

---

## 3. The NULL mechanism (code-verified)

`court_detector.py:870 to_court_coords(..., strict=True)`:

```python
if strict and not (-5.0 <= mx <= COURT_WIDTH_DOUBLES_M + 5.0 and
                   -5.0 <= my <= COURT_LENGTH_M + 5.0):
    return None
```

A detected ball projects to court metres; if `court_y` falls outside [−5, ~28.8] it is nulled. The
177 are airborne points near the vanishing line whose court-plane projection lands far behind the
far baseline → nulled. **The clamp is correct.** Relaxing it would admit garbage, not recover real
bounces.

---

## 4. What this means for Phase 7

| Earlier framing (north_star pre-2026-05-25) | Corrected by this investigation |
|---|---|
| y-calibration 3–7 m off → recalibrate (Batch, 2–3 days) | Calibration is a **faithful homography** (0.11 m self-consistency). **Recalibration is not the lever.** |
| 58 % of real bounces dropped by bad projection | The 177 are **~84 % airborne false-positives**, correctly clamped |
| Recall is the bottleneck | Event recall is fine (85 % @0.8 s). Bottlenecks are **over-detection precision** + **timing jitter** |

**Recommended levers, in order:**

1. **Bounce-detection precision (highest value, detector-side, Render-adjacent).** Reject airborne
   `is_bounce` flags — require the contact to be near the court plane (a projected `court_y` inside
   bounds, ball at a descending→ascending image_y inflection, ball–floor proximity). Cuts the ~177
   airborne FPs and tightens the kept 126. **Not a calibration change.**
2. **Timing / ball coverage.** The 0.5 s jitter is downstream of 52 % ball-frame coverage; better
   coverage pins bounce frames and improves the matchable set. (Overlaps Phase 5 / WASB work.)
3. **Persist the per-job homography** (cheap Batch-side add to the job row / a calib table) so
   bounces can be re-projected and audited without reconstructing or re-running.
4. **Accept the far-baseline resolution limit.** Near-half placement is well-conditioned; far-half
   accuracy is capped by ~1 px ≈ metres. No calibration removes this — only higher far-ball
   detection precision (or resolution) would. Manage expectations on far-half heatmaps.

**What NOT to do:** trigger a Batch court-recalibration cycle to chase the 3 m number — the
calibration is already faithful, and the dominant error is bounce *precision*, not projection.

---

## 5. Open questions for Tomo

1. **Confirm the bounce-precision direction** (filter airborne `is_bounce`) over recalibration.
2. **Where should the precision filter live** — in the Batch bounce detector (`roi_extractors/bounces.py`,
   trips the BATCH-SIDE CHECKLIST) or as a silver-side guard on `is_bounce` rows (Render, faster to
   validate, but bronze-first #11 says fix bronze)? A Render-side *measurement* of the filter on
   Match 1 first, then port to Batch, mirrors the de-risking that produced this doc.
3. **Is the original Match 1 video still in S3?** Not needed for this diagnosis (done from DB), but
   required if we want to re-run the Batch bounce detector to validate a precision change end-to-end.

---

## 6. Bounce-precision filter — scope (2026-05-25)

**Problem (precise).** `ball_tracker.detect_bounces` (ball_tracker.py:583) flags **any** image-y
velocity sign-flip as `is_bounce`. A floor bounce is specifically `vel +→−` (ball at local *max*
image-y = lowest screen point). The current code also flags `vel −→+` (**ball-arc apexes**, local
min) and racquet contacts (SA `swing`), and raw image-y is confounded by perspective foreshortening
(image-y drifts as the ball moves near↔far regardless of height). Net: 303 events vs SA's 161; only
~43 % of the coord-bearing 126 are ground-bounce-shaped. The 177 airborne FPs are already clamped
out (NULL coords, excluded by the Pass-1 query `build_silver_match_t5.py:608`), so the live damage is
the **impure 126 coord-bearing bounces** feeding bounce-driven silver rows + placement.

**Goal / done-when.** On Match 1, the T5 floor set approaches SA's **67 floor** bounces — precision
up, recall held (≈85 % event recall), count 126 → ~70–85.

**Signals (by robustness under sparse ball data).**
1. **Sign-order gate** — keep only `vel +→−` (local-max image-y). Kills apex FPs. ~1-line change.
2. **Player-proximity gate** — reject a "bounce" co-located with a player's hitting zone (racquet
   contact, not a floor landing). Uses `player_detections`. Attacks the `swing` contamination.
3. **Net-crossing validity** — reuse `serve_detector/bounce_validity.validate_bounces` (Tomo's
   May-7 rule) on the *placement* set too (today it only gates serve detection + ROI pose).
4. **Perspective-aware detection** (court-y / height proxy instead of raw image-y) — **deferred**,
   bigger change.

**Where it lives — two stages (de-risk pattern).**
- **Stage 1 (Render, ~1 day):** prototype signals 1–3 as a filter over `ml_analysis.ball_detections`,
  measure precision/recall/count vs SA floor on M1, tune thresholds. No Batch, no schema change.
- **Stage 2 (Batch, ~1–2 days, daylight):** port the validated rule into `ball_tracker.detect_bounces`
  (+ `roi_extractors/bounces.py`) — the durable bronze fix (aligns with #11). Trips the BATCH-SIDE
  CHECKLIST (Docker rebuild + dual-region ECR + job-def revisions).

**Validation.** M1 reconciliation vs SA floor (precision↑, recall held, count→~SA). **Serve bench
MUST stay green** — bounces gate serve detection (`rally_state.build_from_db` reads `is_bounce`; the
geometric serve check uses `bounce_court_y`). Re-run bounce-driven silver on M1.

**Risks.** (1) Serve-detection regression (highest — bench-gate everything; the net-crossing filter
is already bench-safe at 20/24). (2) Over-filtering real bounces — sparse coverage makes the
trajectory signal weak (43 % local-max on real bounces), so prefer proximity + net-crossing over
pure trajectory; tune to hold recall. (3) Single-match calibration — validate on Match 2 when
unblocked (Bug 2). (4) Perspective confound remains under 1–3 (full fix = signal 4, deferred).

---

## 7. Stage 1 measurement — RESULT 2026-05-25 (the cheap filter underdelivers)

Ran the harness (`.claude/tmp/stage1_filter.py`) on Match 1's 126 coord-bearing bounces, labelling
each by nearest SA event (±0.8 s) and testing each signal + combos vs SA `floor` (67).

**Composition of the 126:** floor 41 / swing 14 / **none 71** (56 % match no SA event at all).

**Signal separation — nothing separates cleanly:**

| signal | floor | swing | none |
|---|---|---|---|
| local-max image-y rate | 51 % | 43 % | 39 % |
| player-dist median (m) | 4.6 | 4.7 | 3.6 |

**Filter combos (vs SA floor, ±0.8 s):**

| filter | kept | floor-recall | precision(floor) |
|---|---|---|---|
| baseline | 126 | 61 % | 33 % |
| sign-order (local-max) | 55 | 36 % | 38 % |
| net-crossing | 74 | 37 % | 32 % |
| **player-proximity ≥1.5 m** | **93** | **57 %** | **40 %** |
| sign-order + net + proximity | 25 | 16 % | 40 % |

**Conclusion.** The trajectory (local-max) and net-crossing signals **crater recall** (61 %→36 %/37 %)
for ~no precision gain — sparse ball coverage destroys the trajectory signal (51 % local-max on real
floor bounces is barely above the 39 % FP rate). The best signal, **player-proximity ≥1.5 m**, only
nudges precision 33 %→40 % at held recall (drops 25 of the 71 FPs, 4 of the 41 floor). **A modest,
safe Render-side guard — not a home run, and not worth a Batch cycle for +7 pts.**

**Two deeper findings that re-orient the work:**
1. **SA-relative precision is unreliable here.** Tomo: SA is weak on ball bounce. So the "71 none"
   almost certainly includes **real bounces SA missed**, meaning true T5 precision is *higher* than
   33 % and we **cannot trust SA floor as the precision denominator.** Bounce precision is not
   reliably measurable against SA — we need **hand-labelled bounce ground truth** for ≥1 match
   (cf. `.claude/serve_ground_truth/`) before this is a measurable target.
2. **The real bottleneck is upstream:** 52 % ball coverage both starves the trajectory signal and
   loosens timing. Higher coverage (Phase 5 / better far-ball detection) would do more for bounce
   precision than any post-hoc filter, and would make the perspective-aware detector (signal 4)
   viable.

**Recommendation:** do **not** port a Stage-2 Batch change yet. Either (a) ship `proximity ≥1.5 m` as
a small Render-side guard if a quick safe win is wanted, or (b) invest in hand-labelled bounce truth
+ coverage first, since the filter ceiling is capped by both sparse coverage and SA's unreliability
as a yardstick.

> **UPDATE 2026-05-25 — (a) SHIPPED** (`aa6c522`): proximity ≥1.5 m guard live in the bounce-driven
> path (`build_silver_match_t5.py`). M1: rows 139→97, serve precision vs SA ~45→67 %, bench green.

---

## 8. Scope — hand-labelled bounce truth + ball coverage (2026-05-26)

The two real levers behind §7. **A (truth)** is the measurement substrate; **B (coverage)** is the
upstream fix. B's *bounce-quality* impact is only verifiable through A.

### Existing infra to leverage (do NOT rebuild)
- `diag/bounce_xy_accuracy.py` — SA-relative bounce accuracy probe (time-match + Euclidean error).
  This is the §2-§7 reconciliation, already a tool. **SA-relative only** — extend for hand-truth.
- `training/label_ball_positions.py` (~160 SA events/match) + `label_serve_bounces.py` — **SA-as-teacher**
  TrackNet labels. Good for *ball-position* training (coverage), **not** bounce-event truth.
- `training/{build_serve_bounce_dataset,extract_frames,tracknet_dataset,train_tracknet}.py` — full
  fine-tune pipeline. `diag/{bench_ball,bench_finetuned}.py` + `bench_ball_baseline.json` — regression.
- Phase 5c corpus accumulation (auto-label dual-submit) — multi-match training runway.
- **No interactive human labeller exists** — Workstream A's tool is genuinely new.

### Workstream A — hand-labelled bounce ground truth (NEW, Render/local)
- **Goal:** SA-independent floor-bounce reference for ≥1 match — `frame_idx` + true court (x,y) +
  in/out + confidence, so recall / precision / xy-error are measurable without SA.
- **Build:** an OpenCV scrub-and-click labeller (this box has cv2): step the video, mark each
  floor-bounce frame, click the bounce pixel; project pixel→court via the **faithful homography**
  (§4, 0.11 m). Reuse `extract_frames.py`. Store CSV/JSON in a new `ground_truth/` dir (mirror
  `.claude/serve_ground_truth/`). Then extend `bounce_xy_accuracy.py` to score vs hand-truth.
- **Which match:** **`a798eff0`** — its video is already local (`ml_pipeline/test_videos/a798eff0_sa_video.mp4`)
  and it's the bench reference with SA data. (Match 1 `78c32f53`'s video is **not** local — would need S3.)
- **Caveat:** far-court hand-truth is still resolution-limited (~1 px ≈ m); near-half is the
  trustworthy core. Flag per-bounce confidence.
- **Effort:** tool ~1 day; labelling 1 match ~2-3 h human.

### Workstream B — ball coverage (upstream lever)
Match 1 gap analysis (`.claude/tmp/coverage_gaps.py`): 52 % coverage; short gaps (2-4 f) are
interpolatable → **+7 % (59 %)**; but **29 % of the video is lost to *sustained* gaps (>8 f, up to
4.6 s)** — a detector problem, not interpolation.
- **B1 (cheap, Render or Batch):** short-gap (2-4 f) trajectory interpolation. +7 % coverage;
  tightens bounce *timing* near detected segments. Does not recover sustained gaps.
- **B2 (the real lever, Batch):** cut sustained gaps via detector fine-tune (TrackNet/WASB on
  multi-match SA-teacher *position* labels + Phase 5c corpus). Measure with `bench_ball`/`bench_finetuned`.
  Batch rebuild + dual-region, daylight, bench-gated. **SA-teacher labels are fine here** — SA ball
  *position* is accurate when SA detects; only SA bounce-*event* truth is untrustworthy (that's A).
- Coverage is measurable now (no truth needed); bounce-recall impact needs A.

### Sequencing
1. **A** — build the labeller, label `a798eff0` (~1-1.5 days). Unblocks trustworthy measurement.
2. Re-score the shipped proximity guard + current bounce set **vs hand-truth** (is precision really
   ~67 %, or higher once SA's undercount is removed from the denominator?).
3. **B1** short-gap interpolation (cheap coverage + timing win), measured vs A.
4. **B2** detector fine-tune for sustained gaps (the big lever), measured vs A + `bench_ball`.

### Open questions
1. Label `a798eff0` (video local) or retrieve Match 1's video from S3 to keep the M1 thread? **Lean: a798eff0.**
2. Near-half first (reliable) with far-half flagged low-confidence — acceptable?
3. Label floor bounces only, or also tag in/out + service-box for serve-placement truth?
