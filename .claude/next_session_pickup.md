# Next-session pickup — 2026-05-24 night (huge strategic session)

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-24 (long session: morning cron wiring + match upload + afternoon Phase 6/7 probes)
**Phase active:** Phase 6 unblocked (pose-only stroke detection viable, no training needed). Phase 7 measured but failing target — known calibration issue.
**Bench:** Serve `a798eff0=20/24, 880dff02=23/24` — green on main.
**Today's three strategic findings (the big news):**
1. **Path 0 (no training) viable for Phase 6** — pose-only wrist-velocity peak detector hits 63-67% recall baseline at ±6 tolerance on the `78c32f53` fixture. Realistic refinement target 75-80%. Training stays as insurance/runway to 90%+, not load-bearing.
2. **Ball coverage adequate** — 52% on match 1 (`78c32f53`), 23,791 ball detections + 823 valid bounces on match 2 (`54710da5`). Phase 5 done-when materially met.
3. **Phase 7 (bounce x,y accuracy) MEASURED — INSUFFICIENT.** Median Euclidean error 3.2-4.05m vs <2m target. x-axis (~width) is fine (<2m); y-axis (court length) has 3-6m systematic offset on normal bounces and 10-17m on some far-baseline bounces. Direction is consistent: T5 reports far-side bounces too close to the net. **This is the documented "2.4-7m y-axis offset" backlog item, now quantified.**

**Next session's primary job:** Fix the y-axis far-baseline projection (court calibration). See backlog item in `docs/north_star.md`: "Likely needs a pixel-y-based far-baseline check (replacing `_baseline_zone(court_y)`)." Estimate: 2-3 days of focused calibration work.

**Open at session close:** Match 2 (`ac8e28b3-a1ac-4004-b112-dd16078e56a3`) STILL RUNNING on Batch in ROI pose stage with no log output for 1h+; will hit 6h timeout at ~16:56 UTC if it doesn't finish. **Check first thing — may have completed naturally OR timed out.**

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

## Next session's job (in order)

### 1. First thing: check match 2 status

```bash
aws batch describe-jobs --region eu-north-1 --jobs ac8e28b3-a1ac-4004-b112-dd16078e56a3 \
    --query 'jobs[0].[status,statusReason,stoppedAt,container.exitCode]' --output json
```

Three possible outcomes:

- **SUCCEEDED:** Data is in ml_analysis. The orphan-sweep cron will fire ingest within 5 min if not already. Re-run all three probes against the `0fa94cf6 ↔ 54710da5` pair to validate findings on a second match.
- **FAILED (timeout):** 5h of work lost. Either resubmit with longer timeout (job-def `:49` is already at 6h — would need `:50` with 8-10h) OR investigate why ROI pose hung for so long.
- **STILL RUNNING:** Wait it out; check again later.

```bash
# If SUCCEEDED, re-bench on match 2:
.venv/bin/python -m ml_pipeline.diag.bounce_xy_accuracy \
    --sa-task 0fa94cf6-7cdd-4a8f-9bf9-c603ce31e872 \
    --t5-task 54710da5-7bcd-4f81-b2ea-82929b02d6ec --verbose

.venv/bin/python -m ml_pipeline.diag.ball_hit_pose \
    --sa-task 0fa94cf6-7cdd-4a8f-9bf9-c603ce31e872 \
    --t5-task 54710da5-7bcd-4f81-b2ea-82929b02d6ec \
    --tolerance-frames 6 --use-truth-window --in-rally-only --verbose
```

If match 2 also shows ~3m median y-error, the calibration hypothesis is confirmed across matches and the fix work is justified.

### 2. THE big work: fix the y-axis far-baseline projection

This is the dominant blocker for shipping the product (heatmaps need <2m bounce accuracy). Per `docs/north_star.md` backlog:

> *"Calibration extrapolation behind the far baseline produces court_y -3 to -7m for players who are visually at the baseline. Apr 29 verified naive widening (-3.5→-5.0) loses 2 PASS. Likely needs a pixel-y-based far-baseline check (replacing `_baseline_zone(court_y)`) — touches multiple call sites; deferred."*

**Concrete starting points:**
- `_baseline_zone(court_y)` function (grep the codebase — appears in multiple call sites per the backlog note)
- `ml_pipeline/court_detector.py` and homography code
- `ml_pipeline/camera_calibration.py` if it exists
- Check `ml_pipeline/serve_detector/` for any y-axis correction logic that might already exist for serves

**Approach (from the backlog note):**
1. Diagnose: identify the pixel-y → court-y projection that goes wrong for far-baseline points
2. Fix: replace the projection function with a pixel-y-based far-baseline check
3. Validate: re-run `bounce_xy_accuracy.py` — median should drop from 3.2m to <2m
4. Bench: ensure serve detection bench stays at 20/24 + 23/24

Trips BATCH-SIDE CHANGE CHECKLIST: court detection / homography code is in the Batch container. Docker rebuild + dual-region ECR push required after the fix.

### 3. After Phase 7 fix: production Phase 6 stroke detector

When Phase 7 is sorted, the next concrete work is `ml_pipeline/stroke_detector/` — production version of the `ball_hit_pose.py` probe. Pattern matches `ml_pipeline/serve_detector/`. Emits stroke events the silver builder consumes.

Heuristic refinements to apply (from today's probe findings):
- Peak+offset correction (report `velocity_peak_frame + 4` instead of `velocity_peak_frame`) — recovers 25% recall at ±3 tolerance
- `--min-gap-frames` 15→25 to suppress multi-peak detection on same swing
- Better FAR pose extraction
- Optional: swing-template matching (acceleration profile)

Target: 75% recall at ±3, 50%+ precision. No training.

### 4. Phase 3 part 2 (between-point filter)

Now unblocked. The reconcile showed it would clean up the over-detection problem (T5 139 silver rows vs SA 94 — ~86 rows are warmup/between-point). Lower priority than Phase 7 + Phase 6 production. Could be a 1-2 day effort once Phase 7 + 6 ship.

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

## Final framing

You went from "are we training when we shouldn't be?" (this morning) to **three definitive strategic answers in one session**:
1. Pose-only Phase 6 is viable — no training needed for stroke detection
2. Coverage is met — Phase 5 done-when materially achieved
3. Phase 7 is a specific, scoped calibration problem with a known fix — not a fundamental redesign

The bounce-x,y accuracy gap (3-6m y-axis offset) is real and is the dominant blocker for shipping product features that depend on bounce locations. But it's a **fixable calibration issue, not an architecture failure.**

The day's work shipped 10+ commits, three diag probes, a major doc update, code cleanup, and answered three blocking questions. Big day.
