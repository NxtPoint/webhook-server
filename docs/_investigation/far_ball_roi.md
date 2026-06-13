# Far-ball ROI re-detection (option 2, path c) — 2026-06-12

**Status (2026-06-13 PM):** built, validated, **WIRED + merge-resolved on `main`**;
awaiting Batch deploy + reference re-run. `far_ball.py` cherry-picked to main;
`FarBallProcessor` wired as the 3rd consumer in `unified.py`/`__main__.py`
(env `ROI_FAR_BALL_ENABLED`, default on); merge strategy Option A implemented;
export+reingest carry added. Bench green (12/26 + 23/24).

## ★ Bounce-coupling PROVEN (2026-06-13) — far-ROI fixes BOTH halves
`.claude/tmp/far_bounce_coupling.py` on 30 SA far bounces: far-BOUNCE candidate
recall **(A) WASB ball 12/30 (40%) → (B) sharp far-ROI 24/30 (80%)**. B≫A (2×).
The sharp far ball lifts far-bounce candidate recall, so far-ROI is the shared
enabler for BOTH the hit emission (25/25 trackable) AND reliable far-bounce
marking (far hits = non-bounce far events). One deploy, both halves of the far
gate. Decision tree resolved → branch 1 (deploy; do NOT pivot downstream).

## ⚠️ Two carry/order findings from the wire-in (2026-06-13)
1. **Export+reingest carry (DONE).** roi_far_ball writes to ml_analysis.ball_
   detections, which the Render re-ingest blanket-DELETEs + COPYs from the S3
   export → rows wiped unless carried. Added: `bronze_export` carries
   roi_far_ball via `extra_ball_rows` (source field now in the ball dict);
   `bronze_ingest_t5` COPY includes `source`. Probe jobs (no re-ingest) keep the
   rows regardless — fine for the reference far-gate read.
2. **Pipeline ordering (coupling lands next-cycle on Batch).** bounce_detector
   runs BEFORE the ROI sweep in `__main__` (its bounces rally-gate pose), so on a
   single job the bounce stage can't see roi_far_ball. To MEASURE the coupling on
   the reference, re-run the bounce detector OFFLINE on the now-merged ball
   (reads ml_analysis via the deduped `_load_ball_rows`). A production reorder /
   second bounce pass is a later optimisation, not required for the gate read.

## Problem
At full-frame WASB downscale the FAR ball is ~1.6 px (sub-pixel), so its
trajectory is ~300 px-residual noise. The hit model's far candidates were
therefore feature-weak / indistinguishable from bounces (fork probe
2026-06-12: far-hit vs far-bounce identical on angle/speed/density/proximity).
This is the root cause blocking the hit-model **far gate** (6/51 vs heuristic
19/51). Proven NOT fixable by labeling, features, or reweighting — it's an
upstream tracking-precision problem.

## Approach
Hybrid, identical to `roi_extractors/bounces.py` (WASB owns the global frame,
TrackNet re-detects a projected high-res crop). Here the crop is the FAR court
and we anchor on far-court ball PRESENCE (`court_y < HALF_Y`), keeping the whole
far trajectory (not just bounces). New rows carry `source='roi_far_ball'`.

## Validation (local, reference video `a798eff0_sa_video.mp4` / SA `ba4812be`)
Scripts in `.claude/tmp/far_roi_*.py`, `far_ball_smoke.py`.
1. **A/B, same TrackNet, full-frame vs far-crop:** trajectory residual
   **298 px → 45 px (6.7× sharper)**, identical detection count. The far crop
   (640×360 → TrackNet 640×360, ~1:1) gives the ball ~3× more pixels.
2. **Real `candidates.py` on the re-detected far trajectory:** far HITS and far
   BOUNCES now **25/25 matched** with clean ~169° discontinuities (baseline: far
   candidates were feature-weak noise). **Trackability is FIXED** — the far ball
   is now a real, trackable arc.
3. **Honest nuance:** angle (hit 169° vs bounce 168°) and speed (37.7 vs 56.9,
   overlapping) do NOT cleanly separate hit from bounce — *expected*, since both
   a racquet hit and a ground bounce are sharp velocity reversals. Separation
   must come from **proximity-to-player + court-height/temporal** features, which
   need the CALIBRATED pipeline (court_y + far-player positions) — not provable
   locally without reconstructing calibration.

## Proximity de-risk (the key follow-up, 2026-06-13)
Tested whether the hit model's actual discriminator — ball→far-player gap
(image-space, `far_player_gap_px`) — separates hit from bounce once the ball is
sharp. **It does NOT:** far-HIT gap med 445px vs far-BOUNCE 391px (both ~400px,
hit even farther). Confirmed (not a frame-alignment artifact): the fork probe on
`a35b37f6`'s internally-consistent ball+player data found the same ~500px for
both. Far-player coverage is fine (7203 dets). So a far hit and a far bounce sit
~equally far from the far player — proximity can't tell them apart.

## Verdict (REVISED — necessary, not sufficient)
- **Far-ball trackability is FIXED** by the ROI sharpening — a real bronze
  improvement, valid on its own, and it feeds BOTH the hit candidates and the
  bounce detector. Ship it.
- **But the sharp ball ALONE does NOT fix the far hit GATE.** Three discriminators
  the hit model relies on — angle (169° both), speed (overlapping), proximity
  (~400px both) — ALL fail to separate a far hit from a far bounce even on a clean
  trajectory. At distance they genuinely look alike.
- **What the far gate actually needs (beyond the sharp ball):**
  1. **Reliable far-bounce marking** — if the bounce model (ADR-01, currently 38%
     recall) reliably tags far bounces, then far HITS = the far discontinuities
     that AREN'T bounces. The sharp far ball ALSO improves far-bounce candidates,
     so far-ROI is the shared enabler. **This couples strokes-far with bounce #4.**
  2. and/or **temporal/sequence features** — a hit is preceded by the ball arriving
     at a player and reverses up→down; a bounce is the ball descending to ground
     and reverses down→up. The current hit model is PER-CANDIDATE (no sequence).
     The rally's hit→bounce→hit alternation is unused signal.
- **So:** far-ROI is necessary infrastructure (build + ship it), but don't expect
  the far gate to jump on the sharp ball alone — plan the bounce-coupling
  (and/or a sequence head) as the second half of the far-gate fix.

## ⚠️ KEY OPEN DESIGN DECISION — merge strategy (resolve before wiring)
`far_ball.py` inserts `roi_far_ball` rows that OVERLAP WASB (`main`/NULL) far
rows → 2 rows per far frame → `candidates.py`/silver read a corrupted
trajectory. Options:
- **A (recommended): read-time source preference.** Consumers reading
  `ball_detections` for a trajectory prefer `roi_far_ball > roi_prod > main` per
  `frame_idx`. Non-destructive, reversible, single-table-compliant. Centralize in
  the ball-load (hit_model `dataset.py::load_task_arrays`, silver Pass-1 ball
  load, serve_detector ball load — audit ALL readers).
- B: extractor DELETEs WASB far rows in its windows before insert. Destructive;
  re-created on Render re-ingest (export+reingest-carry trap) — avoid.
- C: separate table + merge at read — violates single-canonical-bronze (rule).

## Daylight execution checklist
1. **Resolve merge (A):** implement per-frame `source` preference at every
   `ball_detections` trajectory reader; verify none is missed.
2. **Wire** `FarBallProcessor` as a 3rd consumer in
   `roi_extractors/unified.py` (mirror `RoiBounceProcessor`:
   build→prepare→feed→finalize, return `n_far_ball`); update the
   `run_unified_roi(...)` call + return unpack in `__main__.py` (~line 411).
3. **Batch deploy (rule #8 — daylight):** `roi_extractors/` is wholesale-`COPY`d
   so `far_ball.py` is auto-included (NO Dockerfile edit) — but still requires
   Docker rebuild + dual-region ECR push (eu-north-1 + us-east-1) + new job-def
   revisions. Confirm cross-region digest equality (handover step 3).
4. **Re-run the reference** (a35b37f6 lineage) on Batch → far ball re-detected
   WITH calibration (court_y + proximity now accurate).
5. **Rebuild hit dataset → retrain → read far gate.** EMISSION should rise (the
   model can now fire on real far candidates, 25/25 trackable). But per the
   proximity de-risk, DISCRIMINATION (hit-vs-bounce) likely WON'T fully resolve on
   the sharp ball alone — temper the far-gate expectation. The definitive proof is
   here, but a partial far-gate gain is the realistic first outcome.
6. **Bounce #4 is now part of the far-gate fix, not just a free beneficiary.**
   Re-measure bounce recall on the sharp far ball (should improve), and treat
   reliable far-bounce marking as the SECOND half of the far hit gate (far hits =
   non-bounce far discontinuities). Strokes-far and bounce #4 are coupled through
   the sharp ball. Consider also a sequence/temporal head on the hit model
   (hit→bounce→hit alternation) as an alternative discriminator.
7. **Bench:** `far_ball` doesn't touch serve — confirm serve `bench` stays green
   (12/26 + 23/24) after the Batch-side change anyway.

## Runtime note (your <2h/45-min budget)
The ball stage is NOT on the critical path (north_star close-3: WASB ball-
batching delivered ~0 ms/frame change). The far-ROI eager pass runs only on
far-court windows = a small fraction of frames. Profile `ms_per_frame` on the
first Batch run; port the `bounces.py` batched-forward path only if it actually
shows up in the budget.
