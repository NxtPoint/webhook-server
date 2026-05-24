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
