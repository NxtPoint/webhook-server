# Far-ball ROI re-detection (option 2, path c) — 2026-06-12

**Status:** built + locally validated (trackability FIXED); NOT wired/deployed.
Code on branch `far-ball-roi` (`ml_pipeline/roi_extractors/far_ball.py`).
Far-gate proof is daylight Batch work (see checklist).

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

## Verdict
Far-ball **trackability is fixed** = a real bronze-accuracy improvement, valid
on its own ("build bronze to the ceiling"). The **far-gate** fix is plausible
(proximity becomes reliable once the ball position is accurate) but UNPROVEN —
it needs the Batch run (calibrated court_y/proximity) + a hit-model retrain.

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
5. **Rebuild hit dataset → retrain → read far gate** (target: >6/51, toward the
   19/51 heuristic). This is the definitive proof.
6. If far gate improves: far-strokes unblocked; also **re-measure bounce recall**
   (sharper far ball → better far-bounce candidates — bounce #4 likely benefits
   for free).
7. **Bench:** `far_ball` doesn't touch serve — confirm serve `bench` stays green
   (12/26 + 23/24) after the Batch-side change anyway.

## Runtime note (your <2h/45-min budget)
The ball stage is NOT on the critical path (north_star close-3: WASB ball-
batching delivered ~0 ms/frame change). The far-ROI eager pass runs only on
far-court windows = a small fraction of frames. Profile `ms_per_frame` on the
first Batch run; port the `bounces.py` batched-forward path only if it actually
shows up in the budget.
