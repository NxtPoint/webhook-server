# ADR-01 label audit + corpus floor-coverage diagnosis — 2026-05-28

**Status:** REFERENCE. Captures the two findings from Streams B + C of the 2026-05-28 evening session.
**Driver:** Before kicking off bounce-detector v1 training, we needed to know (1) is the training data we have actually accurate, and (2) why does corpus task `c645a7ee` have 0 floor labels.
**Next session:** read this doc + ADR-01 §"Training data assessment" before starting training work.

---

## Finding 1 — Floor-label scarcity is an SA-side LOCATION-specific pattern (Stream C)

Across **all 6** SA tasks in `bronze.ball_bounce`:

| SA task | floor | swing | total | who / where / when |
|---|---|---|---|---|
| `0fa94cf6` | **277** | 395 | 672 | Tomo / Rivonia / 2026-05-23 |
| `0d0514df` | **67** | 94 | 161 | Tomo / Rivonia / 2026-05-22 |
| `2c1ad953` | **67** | 93 | 160 | Tomo / Rivonia / 2026-04-27 |
| `2f355924` | 0 | 331 | 331 | Dejan / ccj / 2026-05-27 |
| `ee12d918` | 0 | 327 | 327 | Erin / ccj / 2026-05-24 |
| `0336b82b` | 0 | 515 | 515 | Erin / Bloem / 2026-04-28 |

**Pattern:** 100% of Tomo-at-Rivonia matches have floor labels. 0% of every other-court / other-user match has any. Strongly suggests an **SA-side court-recognition / camera-setup pattern** — Rivonia has setup metadata that enables SA's floor-bounce detection; ccj + Bloem don't.

**Implication for ADR-01 training:**
- Bulk-loading from non-Rivonia courts will **NOT** add floor labels. Wrong assumption from this morning's pickup.
- The real lever is either (a) upload more matches from Rivonia, or (b) understand/fix SA's court-recognition for other courts, or (c) hand-label floor bounces for non-Rivonia matches.

**Hidden upside:** Two unpaired Tomo-Rivonia SA matches (`0fa94cf6` with 277 floor labels, `2c1ad953` with 67) **are not in the corpus yet** because they don't have a T5 partner. If T5-paired, they'd lift the corpus from 67 → **411 floor labels across 3 corpus tasks**. That's a 6× uplift from existing data without any new uploads. Tomo can re-submit those videos to T5 to claim them.

---

## Finding 2 — Label-vs-T5 alignment audit on the 67 Match-1 floor labels (Stream B)

For each SA floor label (`78c32f53` ↔ `0d0514df`), cross-checked against `ml_analysis.ball_detections` for the T5 task. SA `image_x/y` are normalised 0-1; scaled by frame W/H (1920×1080) for pixel comparison.

| Metric | Result | Read |
|---|---|---|
| SA floor labels | 67 | baseline |
| T5 has ball detection at EXACT frame | 46 / 67 (68%) | ball-coverage gap at bounce moments |
| T5 has ball detection within ±5 frames | 64 / 67 (95%) | coverage with timing slack — fine |
| T5 ball position within 50 px in ±5-frame window | 36 / 67 (53%) | "agreement" — usable strong positives |
| T5 `is_bounce=TRUE` within 50 px + ±5 frames | 6 / 67 (9%) | current heuristic recall vs SA |
| Mean pixel error (when both present) | 202 px | pulled by long tail |
| Median pixel error | **80 px** | representative — acceptable for training |
| P90 pixel error | 652 px | long tail — investigate per label |

**Read:**
- **The 67 floor labels are real and reasonably accurate**, but coverage tightens to ~36 strong-positive examples (where T5 ball position agrees with SA label within 50 px in a 10-frame window).
- **Current T5 bounce heuristic recall is 9%** — confirms ADR-01's "55%/27%" measurement was already generous, or the metric uses a wider tolerance. Either way: large headroom for a trained model.
- **31% of SA floor labels (21/67) have no T5 ball detection within ±5 frames** — these are ball-coverage gaps (Phase 5 territory). Bounce detector can't learn from them; either hand-correct or drop.
- **Median 80 px error** is small enough that SA labels are a usable ground-truth signal. P90 of 652 px is long tail — likely tracker swaps / occlusion frames; need spot-checking but not a blocker.

---

## Negative mining recipe (for ADR-01 training session)

Per ADR-01 v1 Build spec: ~5-10× negative windows mined from `ball_detections` excluding ±0.2 s of any positive label.

Concrete plan:
```sql
-- For each (job_id, candidate frame) where is_bounce=TRUE but no SA label nearby,
-- AND for randomly-sampled non-bounce frames within rallies,
-- emit a negative example with the same 14-channel feature window.
WITH positives AS (
    SELECT job_id, frame_nr, ts FROM bronze.ball_bounce
    WHERE type = 'floor' AND task_id::text IN (...3 Rivonia SA tasks...)
),
positive_frames AS (
    SELECT job_id, frame_nr FROM positives
),
candidate_negatives AS (
    SELECT bd.job_id, bd.frame_idx
    FROM ml_analysis.ball_detections bd
    WHERE bd.is_bounce IS TRUE
      AND NOT EXISTS (
          SELECT 1 FROM positive_frames p
          WHERE p.job_id = bd.job_id
            AND ABS(p.frame_nr - bd.frame_idx) <= 6  -- ±0.2 s @ 30 fps
      )
)
SELECT * FROM candidate_negatives
ORDER BY random()
LIMIT 500;
```

Target ratio: ~5× negative per positive → ~335 from 67 positives, ~1,375 from 277 positives (if we land the 0fa94cf6 pair).

**Augmentation idea:** also mine "hard negatives" — frames where T5 currently calls `is_bounce=TRUE` but the trajectory shows the ball is above-net (geometric impossibility). These are exactly the airborne FPs ADR-01 pre-gates should reject; the model can learn them as belt-and-braces.

---

## bounce_type enum extension (ADR-01 deferred work item #3)

Current `bronze.ball_bounce.type` ∈ {`swing`, `floor`}. ADR-01 v1 spec wanted to extend the corpus with `{floor, net_cord, racket_hit}` to enable model-level FP rejection (beyond just pre-gates).

**Recommendation: defer to a future session.** Reasons:
1. We can ship a useful v1 with `floor` only — pre-gates handle the bulk of net-cord/racket-hit FPs.
2. Hand-labelling FP types adds 60-90 min of manual work per match — expensive.
3. The v1 bench-improvement curve will tell us whether pre-gates alone clear the precision target. If yes, the enum extension may never be needed.

If v1 plateaus precision ≤ 60% despite well-tuned pre-gates, *then* add the enum + label ~200 net-cord + ~200 racket-hit examples by hand from the existing corpus videos.

---

## Bottom line for next session

1. **The corpus has 411 floor labels accessible — not 67.** Two unpaired Tomo-Rivonia SA matches need T5 partners (Tomo's manual action to re-submit). Combined with the existing 67, that's a workable training set.
2. **The 67 we have audit at ~53% "strong positive" alignment with T5 ball position**, median 80 px error. Train on them as-is for v1; investigate the 21 missing-T5-detection cases later.
3. **Negative mining ~500 windows** is a half-hour script — recipe above.
4. **Hold off on the `bounce_type` enum extension** until v1 bench tells us pre-gates aren't enough.

Suggested sequence:
- Tomo re-submits the 2 unpaired Rivonia matches to T5 → corpus auto-lands → 411 floor labels in the corpus.
- Next session: negative-mine + train bounce_detector v1 on the full 411-label set. Bench against `bench_bounce.py`.
- If v1 hits ≥ 60% precision: ship. If not: add the bounce_type enum + hand-label a few hundred FPs.
