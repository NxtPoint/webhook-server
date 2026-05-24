# Next-session pickup — 2026-05-24 night (huge strategic session)

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-24 (long session: morning cron wiring + match upload + afternoon Phase 6/7 probes)
**Phase active:** Phase 6 unblocked (pose-only stroke detection viable, no training needed). Phase 7 measured but failing target — known calibration issue.
**Bench:** Serve `a798eff0=20/24, 880dff02=23/24` — green on main.
**Today's three strategic findings (the big news):**
1. **Path 0 (no training) viable for Phase 6** — pose-only wrist-velocity peak detector hits 63-67% recall baseline at ±6 tolerance on the `78c32f53` fixture. Realistic refinement target 75-80%. Training stays as insurance/runway to 90%+, not load-bearing.
2. **Ball coverage adequate** — 52% on match 1 (`78c32f53`), 23,791 ball detections + 823 valid bounces on match 2 (`54710da5`). Phase 5 done-when materially met.
3. **Phase 7 (bounce x,y accuracy) MEASURED — INSUFFICIENT.** Median Euclidean error 3.2-4.05m vs <2m target. x-axis (~width) is fine (<2m); y-axis (court length) has 3-6m systematic offset on normal bounces and 10-17m on some far-baseline bounces. Direction is consistent: T5 reports far-side bounces too close to the net. **This is the documented "2.4-7m y-axis offset" backlog item, now quantified.**

**Next session's primary job (Tomo decision 2026-05-24 night):** Close out **Phase 3 part 2** (between-point filter, Render-side) THEN **Phase 6** (production stroke detector at `ml_pipeline/stroke_detector/`, Render-side). Both are product-facing wins that make silver data + stroke detection materially better, neither needs Batch redeploys. **Phase 7 (y-axis calibration) is deferred to a later session** — it's a bigger structural fix needing Docker rebuild + dual-region ECR push, and the immediate product gains from 3b + 6 are larger.

**Match 2 RESOLVED 17:04 UTC — FAILED at 6h timeout. No data persisted.** Three diagnostic findings came out of the failure (ROI misalignment, roi_bounces slowdown, plus the existing y-axis offset). See "Match 2 diagnostic findings" section below — three concrete bugs to investigate alongside Phase 3b + 6.

If the above is enough, stop reading. The rest is depth + verification commands.

---

## What today's session actually shipped

11 commits on `origin/main` since the session-close commit. Headline trajectory:

```
b097974 docs: north_star -- Phase 3 part 2 + Phase 8 unblocked  ← latest
991abc7 diag: bounce_xy_accuracy.py -- Phase 7 measurement (meters of error vs SA truth)
b1b7932 strategy: Phase 6 unblocked (pose-only), Phase 7 is next critical measurement
74e863e diag: ball_hit_pose -- add --in-rally-only + --use-truth-window flags
e833f91 diag: ball_hit_pose.py -- pose-only stroke detector (no ball signal)
8a05570 diag: ball_hit_fusion.py -- pose+ball heuristic, follow-up to baseline  [DELETED in b1b7932]
60d7301 diag: ball_hit_baseline.py -- 20-line heuristic ported from ameynarwadkar repo  [DELETED in b1b7932]
50201fc phase 5c.4 groundwork: bench_finetuned + --weights-path threading
cd903fd docs: morning pickup — cron script ready, Phase 5c.4 branch ready
ec357f7 cron: add cron_sweep_t5_orphans.py — Phase 5c.3 closure
```

Three probes were written today; only `ball_hit_pose.py` survives. Its findings + the comparison are captured in `docs/north_star.md` strategy update.

## The three probes (one-line summary each)

| Probe | Approach | Result | Status |
|---|---|---|---|
| `ball_hit_baseline.py` | y-reversal heuristic (ameynarwadkar port) | 0% recall | DELETED — broadcast-camera assumption |
| `ball_hit_fusion.py` | ball position vs wrist distance | 15% recall | DELETED — ball occluded at contact |
| `ml_pipeline/diag/ball_hit_pose.py` | wrist velocity peaks, no ball | **63-67% recall** | KEPT — Phase 6 path |
| `ml_pipeline/diag/bounce_xy_accuracy.py` | matched-bounce Euclidean error | **median 3.2m, target <2m** | KEPT — Phase 7 measurement |

## State at session end (2026-05-24 late evening)

**`origin/main` at `b097974`.**

**Match 1 (`78c32f53`, completed yesterday):**
- 161 SA bounces, 139 T5 bounces, 45 time-matched within ±0.5s
- Phase 7 median error 4.05m (loose tolerance), 3.16-3.38m (tight tolerance)
- Phase 6 pose-only detector: 63.2% recall ±6 / 37.7% recall ±3 frames
- 1 corpus row in `ml_analysis.training_corpus` (`78c32f53`, 161 labels)

**Match 2 (`54710da5`, retry2 = batch job `ac8e28b3-a1ac-4004-b112-dd16078e56a3`):**
- STATUS AT SESSION CLOSE: **RUNNING** in ROI pose stage, `progress_pct=78`, no log output for 1h+
- Started 2026-05-24 10:56:36 UTC, 6h timeout hits at 16:56 UTC
- Main pipeline completed at 14:57:52 UTC (4h 1m): 23,791 ball detections + 823 valid bounces + 0 phantom bounces filtered
- Data NOT YET persisted to ml_analysis.* (writes happen at end of ROI pose stage)
- Will likely either complete naturally (data lands, corpus row #2 auto-creates) OR timeout (need restart)

**Render cron status:**
- `/ops/sweep-t5-orphans` cron is wired and running (wired this morning 2026-05-24); confirmed firing.

**Batch state:**
- eu-north-1 `:49` (timeout 21600s = 6h), us-east-1 `:31` (same). Older revs `:48` / `:30` retained.
- Image still `sha256:bc8f7d72…` (chain-rejection fix + WASB + source='main').

---

## Strategic scorecard for the project

```
DONE          Phase 1 (serve detection)        — 20/24 + 23/24 bench-locked
DONE          Phase 4 (point reconciler tool)
DONE          Phase 5a (ROI bounce extractor)
DONE          Phase 5e (WASB integration)
DONE          Phase 5e follow-ups (chain-rejection + source='main')
MOSTLY DONE   Phase 5 (ball coverage)          — 52% on match 1, 23k on match 2; done-when met
DONE          Phase 5c.0-5c.3 (corpus pipeline, with cron wiring)
PARTIAL       Phase 2 (point boundaries)       — function exists, needs re-measurement
PARTIAL       Phase 3 part 1 (warmup filter)
UNBLOCKED     Phase 3 part 2 (between-point filter)  — reconcile exposes the symptom
UNBLOCKED     Phase 6 (stroke detection)       — pose-only path validated, production module needed
MEASURED      Phase 7 (bounce x,y accuracy)    — INSUFFICIENT (median 3.2m vs <2m target)
UNBLOCKED     Phase 8 (final serve cleanup)    — but lower priority
INSURANCE     Phase 5c.4 (bench-gate-before-promotion) + Phase 5d (training)
```

## The Phase 7 finding in detail

This is THE most important practical finding of today. Worth understanding before doing the fix.

**Test data:** Match 1 (`0d0514df ↔ 78c32f53` dual-submit pair). 161 SA bounces, 139 T5 bounces, court is 10.97m wide × 23.77m long.

**Results at three tolerance levels:**

| Tolerance | Matches | Median err | Within 1m | Within 2m | Within 3m |
|---|---|---|---|---|---|
| ±0.5s | 45 | 4.05m | 11% | 22% | 42% |
| ±0.15s | 27 | 3.16m | 7% | 11% | 44% |
| ±0.08s | 24 | 3.38m | 4% | 4% | 38% |

Tightening tolerance barely helped. So errors are real, not matching artefacts.

**Direction of errors (from same-time `dt=0.00` pairs):**

```
sa=(3.98, 10.30)  t5=(3.82,  4.16)  err=6.14m   <-- y too low by 6.14m
sa=(7.64, 18.85)  t5=(9.57, 13.22)  err=5.96m   <-- y too low by 5.63m
sa=(2.62, 24.79)  t5=(3.15, 20.08)  err=4.75m   <-- y too low by 4.71m
sa=(9.59, 25.27)  t5=(11.92, 21.96) err=4.05m   <-- y too low by 3.31m
```

- **x errors ≈ 0.2-2.3m** (acceptable, fixable later)
- **y errors ≈ 3-6m on normal bounces, 10-17m on far-baseline bounces**
- **Always in the direction "T5 reports the bounce closer to the net than SA does"**

**Best pairs (proves T5 CAN report accurate coords):**

```
sa=(3.96, 10.52)  t5=(3.97, 10.23)  err=0.29m   <-- ~perfect
sa=(0.85, 26.24)  t5=(1.07, 25.71)  err=0.58m
sa=(4.52, 19.35)  t5=(4.14, 19.13)  err=0.44m
```

When the projection works, errors are <1m. So geometry isn't broken in general — only the far-baseline extrapolation is broken.

**Diagnosis:** This is the documented "2.4-7m y-axis offset" backlog item, but quantified more precisely:
- 3-6m systematic offset on regular far-side bounces
- 10-17m on some far-baseline bounces (catastrophic projection failure)
- x-axis is fine
- Fix direction (per backlog): "pixel-y-based far-baseline check (replacing `_baseline_zone(court_y)`) — touches multiple call sites"

---

## Next session's job (in order — Tomo-prioritised 2026-05-24 night)

**Strategic note:** the original ordering had Phase 7 calibration as the primary task. Tomo's revised priority on session close is **Phase 3 part 2 + Phase 6 production module first** (both Render-side, product-facing). Phase 7 (Batch-side, calibration) is deferred. The work below reflects the revised order.

### 1. Match 2 status — FAILED, captured below (no action needed)

Match 2 (`ac8e28b3-a1ac-4004-b112-dd16078e56a3`) timed out at 16:57 UTC after 6h. Exit code 137 (SIGKILL by Batch). Last log was `roi_bounces: [129/194]` — got 66% through the ROI bounce extraction stage. **No data persisted to ml_analysis.** Three diagnostic findings came out of the run; see "Match 2 diagnostic findings" section at the bottom of this file before starting Phase 3b/6 work — these are real bugs that may affect what you build.

### 2. Phase 3 part 2 — between-point filter (Render-side, product-facing)

Now unblocked (Phase 5 coverage prerequisite met). Today's reconcile showed the exact symptom this filter fixes:
- T5 has 139 silver rows vs SA's 94 — ~45 extra rows
- `shot_ix_in_point` populated on 38% of T5 rows vs 81% of SA — ~86 T5 rows are warmup/between-point activity
- Knock-on effects: backhand 2.2× over-detection, volley 3.3× over-detection, bimodal ball_speed (low warmup + jitter spikes)

**Required reading before touching:** `docs/north_star.md` §"Phase 3" — the v1/v2 reverted-attempt history is preserved. Both v1 (107 dense serve anchors collapsed windows) and v2 (forward-fill of point_number put rows in the wrong point's window) failed in specific documented ways. **Don't repeat them.**

**Approach (the actual fix this time):**
- Now that ball coverage is 50%+, we have bounce evidence between rallies.
- The signal: between points, the ball is either (a) static / picked up by player or (b) bouncing on the ground unprompted. Both differ from rally play.
- Use `ml_analysis.ball_detections` density windows + bounce clusters to identify between-point gaps.
- Anchor on (first_serve_per_point, last_bounce_in_rally) windows rather than relying on serve detection alone.

**Where to land it:** `build_silver_v2.py` pass 3 (after the warmup filter, before zone classification). Sets `exclude_d=TRUE` on between-point rows.

**How to verify:**
- Active T5 silver row count within ±5% of SA's (target ~94 ± 5 vs current 139)
- `audit_points_reconcile.py` "T5 strokes outside ANY SA point window" count drops
- Stroke distribution closes the gap: Backhand 38→17ish, Volley 20→6ish
- Bench stays green (20/24 + 23/24)

**Risk:** Render-side only, no Batch redeploy. CI bench runs on every PR. Safe to iterate.

**Estimate:** 1-2 days focused.

### 3. Phase 6 — production stroke detector (Render-side, product-facing)

Today's `ball_hit_pose.py` probe scored 63-67% recall on match 1. Production version lives at `ml_pipeline/stroke_detector/` (sibling to `serve_detector/`), pattern-matches the serve detector's pose-first architecture. Emits stroke events the silver builder consumes.

**Key heuristic refinements to apply from today's probe findings:**

1. **Peak+offset correction.** Velocity peak fires 4-6 frames BEFORE SA's contact frame (backswing). Report `predicted_hit = velocity_peak_frame + 4` to align with SA's timing. This alone recovered 25pp recall when we widened tolerance from ±3 to ±6 — applying the offset means we can match at tight tolerance.

2. **Tighten `min_gap_frames` 15 → 25.** Today's probe over-fired on the same swing (backswing + forward swing + follow-through all generated peaks within 15 frames). Wider gap suppresses these duplicates without losing real hits (typical between-shot time is >1s = 25+ frames).

3. **Add a deceleration check.** A genuine swing has the velocity profile: rising → peak → falling. Pure rises (player picking up ball) shouldn't fire. Filter peaks where `v[i+3] > v[i] * 0.5` (still rising).

4. **Better FAR pose extraction.** Today's probe found NEAR pose at 10,115 entries vs FAR at 6,130 — 40% less coverage. Investigate ROI pose extractor (`ml_pipeline/roi_extractors/pose.py`) — already runs per Phase 1.

**Target metrics:** 75% recall at ±3, 50%+ precision. No training needed for this ceiling.

**Where to land:**
- `ml_pipeline/stroke_detector/` — new module, mirrors `serve_detector/` structure
- Integrate into `build_silver_match_t5.py` so stroke events become silver rows
- Silver consumer of stroke events similar to how `serve_events` is consumed

**Risk:** Render-side, no Batch redeploy. Same iteration loop as Phase 3 part 2.

**Estimate:** 2-3 days.

### 4. DEFERRED — Phase 7 y-axis calibration

The dominant blocker for shipping bounce-dependent features (heatmaps). Per today's measurement, median Euclidean error is 3.2m vs <2m target. Direction: T5 reports far-side bounces too close to net. Fix direction documented in north_star backlog: "pixel-y-based far-baseline check (replacing `_baseline_zone(court_y)`)."

**Why deferred (Tomo decision 2026-05-24 night):**
- Trips BATCH-SIDE CHANGE CHECKLIST (Docker rebuild + dual-region ECR push)
- Bigger structural change than 3b + 6
- 3b + 6 ship product-facing wins immediately; 7 doesn't show up until next data run anyway
- Doing 7 last means we can validate the fix against a Phase-6-stable silver

**Don't start Phase 7 until 3b + 6 are landed and bench is still green.**

---

## Read in this order before doing anything else

1. **This file** — you're here.
2. `docs/north_star.md` §"Strategy update 2026-05-24" — full three-probe story + phase ladder updates.
3. `docs/north_star.md` §"Phase 7" — the specific calibration issue + fix direction.
4. `ml_pipeline/diag/bounce_xy_accuracy.py` — read the docstring to understand the measurement methodology.
5. `ml_pipeline/diag/ball_hit_pose.py` — read the docstring; this is the model for the production stroke detector.

Then run the locked bench to confirm nothing regressed:

```bash
.venv/Scripts/python -m ml_pipeline.diag.bench
# Expect: a798eff0=20/24, 880dff02=23/24
```

---

## Open admin items

- **Match 2 may be hung** — Batch may have timed out at 16:56 UTC tonight. Check first thing.
- **Render Postgres still open to `0.0.0.0/0`** — re-lock to `105.214.8.31/32` or build NAT Gateway + EIP. Outstanding for 4+ days.
- **Silver-bench has only 1 fixture** — adding `880dff02` would give a denser regression target.
- **EU job-def `:49` has a broken default command** (latent — production submissions use containerOverrides which bypasses). Cleanup: register `:50` with short command matching US `:31`, deregister `:49`. Low urgency.
- **Stale GPU box `i-0fb3983fa555c16e3`** (eu-north-1a) parked stopped, ~$3.70/mo EBS.

---

## Things NOT to do (load-bearing)

- **Don't auto-spawn a task without a paired server-side trigger** (CLAUDE.md "Things not to do" #10).
- **Don't merge ball_tracker.py, wasb_*, pipeline.py, config.py, db_writer.py, Dockerfile changes without BATCH-SIDE CHANGE CHECKLIST.**
- **Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD`.**
- **Don't rollback WASB without bench against TrackNetV2 first.**
- **Don't change `AUTO_DUAL_SUBMIT_T5` / `AUTO_LABEL_DUAL_SUBMIT_PAIRS` env-flag defaults to ON in code** (production override only).
- **Don't ask Tomo to do Docker work.**
- **Don't create parallel bronze tables.**
- **Don't skip the bench CI check to land a PR.**

---

## Verification commands (paste-ready)

```bash
# 1. Locked serve bench
.venv/Scripts/python -m ml_pipeline.diag.bench

# 2. Phase 7 bounce accuracy on match 1 (yesterday's known result)
.venv/bin/python -m ml_pipeline.diag.bounce_xy_accuracy \
    --sa-task 0d0514df-68aa-4346-9e2d-64413429e47f \
    --t5-task 78c32f53-5580-4a88-a4e7-7506e59b2b52 --verbose

# 3. Phase 6 pose-only stroke detector on match 1
.venv/bin/python -m ml_pipeline.diag.ball_hit_pose \
    --sa-task 0d0514df-68aa-4346-9e2d-64413429e47f \
    --t5-task 78c32f53-5580-4a88-a4e7-7506e59b2b52 \
    --tolerance-frames 6 --use-truth-window --in-rally-only --verbose

# 4. Match 2 Batch status
aws batch describe-jobs --region eu-north-1 --jobs ac8e28b3-a1ac-4004-b112-dd16078e56a3 \
    --query 'jobs[0].[status,statusReason,stoppedAt]' --output json

# 5. Match 2 data persistence check (on Render shell)
psql "$DATABASE_URL" -c "SELECT status, current_stage, progress_pct, (SELECT COUNT(*) FROM ml_analysis.ball_detections WHERE job_id::text = '54710da5-7bcd-4f81-b2ea-82929b02d6ec') AS balls FROM ml_analysis.video_analysis_jobs WHERE job_id = '54710da5-7bcd-4f81-b2ea-82929b02d6ec';"

# 6. Corpus row count
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM ml_analysis.training_corpus;"
```

---

## Match 2 diagnostic findings (added 17:00 UTC after timeout)

Three concrete bugs surfaced from the failed match 2 run. None blocks Phase 3b or Phase 6 work directly, but all three are next-session-relevant.

### Bug 1 — Far-ROI region misalignment (HIGH IMPACT)

Match 2's ROI pose stage scanned 65,248 frames and produced **0 detections**:

```
roi_pose: far ROI pixel (461,695)-(506,735) size=45x40
...
roi_pose: scanned 65248 sampled frames (every 2), skipped 21050 IN_RALLY frames, 0 detections, 0 usable poses in 5270.7s
```

A 45×40 pixel ROI is correct size for a far-baseline player on this camera setup, but it was pointed at the wrong patch of the frame — no person ever appeared there. For comparison, the prior Tomo-vs-Jimbo-Ma `1d6feb3a` run found 7,244 detections from 7,650 frames at 94.7% hit rate.

**Why this matters:** the ROI is computed from court keypoint regression. Court detection on match 2 must have placed the far baseline differently than `1d6feb3a` (same opponent, different match) — different enough that the downstream ROI computation produced a useless region.

**Fix direction (next session):**
- `ml_pipeline/roi_extractors/pose.py` and `ml_pipeline/roi_extractors/bounces.py` both compute ROI from the same source — find the function
- Add tolerance: expand the ROI by N pixels in each direction so small court-detection drift doesn't kill the entire ROI stage
- OR: validate the ROI by checking that at least one frame in a sample has a high-confidence person bbox inside it before committing to it
- OR: redo court detection per-N-frames instead of once, and re-derive ROI

**Impact on stroke/bounce accuracy:** ROI pose enhances FAR-player pose data when it works. With 0 detections, the FAR player gets only the SAHI-tiled YOLO pose (which IS still working in the main pipeline — 6,130 FAR pose entries for match 2 in earlier diag).

### Bug 2 — roi_bounces per-window slowdown (CAUSED THE TIMEOUT)

The Phase 5a ROI bounce extractor (`ml_pipeline/roi_extractors/bounces.py`) iterates "windows" around clustered bounces. On match 2 it had 194 windows. Per-window timing degraded badly:

```
[7/194]    ... 0 dets, 0 bounces (7.1s)   <-- early
...
[129/194]  ... 0 dets, 0 bounces (50.8s)  <-- late, KILLED here
```

**7s → 50s per window = ~7× slowdown.** Wallclock budget for the remaining 65 windows would have been ~54 min — well beyond the 6h timeout. THIS is why the job died.

**Why this matters:** even with bug 1 fixed (ROI aligned), bug 2 means roi_bounces can't reliably complete on long matches. Could be memory growth, GPU state accumulation, model-reload-per-window overhead (each window logs "BallTracker: loaded" from scratch — suspicious), or something else.

**Fix direction:**
- Load TrackNet V2 model ONCE outside the window loop, not per-window
- Profile memory growth between iterations
- Check for accumulating tensors that aren't released

The "BallTracker: loaded" log appearing on every single window strongly suggests per-window model loading. That's almost certainly the slowdown source — model load time is fixed cost, but cuDNN warmup and CUDA memory fragmentation might be cumulative.

### Bug 3 — Y-axis bounce calibration offset (CONFIRMED on match 1)

This is the Phase 7 finding from earlier: median Euclidean error 3.2m, direction "T5 reports far-side bounces too close to net by 3-6m." Documented in north_star `Phase 7` section. Match 2 couldn't validate but the direction-consistent finding on match 1 is strong enough to act on.

### Suggested ordering for next session (revised)

If you tackle Phase 3b + 6 first (as Tomo's plan stands), keep these three bugs in mind:
- **Bug 1 (ROI misalignment) doesn't block 3b or 6** but explains why some matches produce sparse FAR-player data
- **Bug 2 (roi_bounces slowdown) doesn't block 3b or 6** either but is the reason match 2 didn't land tonight
- **Bug 3 (y-axis offset) is Phase 7, explicitly deferred**

A possible insertion point: after Phase 3b lands (which doesn't touch the Batch container), Bug 2 is a worthwhile detour — it's a small, contained Batch-side fix (move model loading outside the window loop in `roi_extractors/bounces.py`), trips guardrail #8 minimally (one file change), and unlocks long-match processing for future corpus rows.

---

## Final framing

You went from "are we training when we shouldn't be?" (this morning) to **three definitive strategic answers in one session**:
1. Pose-only Phase 6 is viable — no training needed for stroke detection
2. Coverage is met — Phase 5 done-when materially achieved
3. Phase 7 is a specific, scoped calibration problem with a known fix — not a fundamental redesign

The bounce-x,y accuracy gap (3-6m y-axis offset) is real and is the dominant blocker for shipping product features that depend on bounce locations. But it's a **fixable calibration issue, not an architecture failure.**

The day's work shipped 10+ commits, three diag probes, a major doc update, code cleanup, and answered three blocking questions. Big day.
