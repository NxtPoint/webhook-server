# T5 ML Pipeline — North Star

**Last updated:** 2026-05-07 by Tomo + Claude (cleanup session)
**This is the single place where the T5 macro plan lives.** Phase work happens against this ladder. Don't invent new directions — pick a phase, claim it, deliver, update.

---

## Product goal

A tennis match analytics dashboard where:

1. Every event shown corresponds to a real point-stroke (no pre-serve racquet-bouncing leaking in, no between-points walking).
2. Every coordinate is geometrically accurate within the dashboard's tolerance: hitter location, bounce location, ball trajectory.
3. Per-stroke claims (forehand winner, backhand depth, attack/defence, serve-side) are correct on validated points.
4. The user can trust per-rally and per-set aggregates because the underlying events are clean.

We are NOT trying to hit 100% serve detection. We're trying to get the dashboard data trustworthy enough that a coach using it would draw the same conclusions as if they'd watched the match.

---

## Current bottleneck

Bronze TrackNet emits dense clusters of phantom "bounces" on the near baseline (cy 21-26, gaps 0.3-1.0s, never crossing the net) — these are players bouncing the ball on their racquet pre-serve. They pollute `RallyStateMachine` and cascade two ways:

- Upstream (`extract_far_pose`): rally state stays IN_RALLY for 16-second blocks, blocking ROI pose extraction during real serves (3 of 4 remaining a798eff0 misses).
- Downstream (silver): non-point activity gets ingested as if it were rally play. T5 has 160 events on a798eff0 vs SportAI's 85 — almost 2× the events, most of those extras being pre-/between-point noise.

**Fix direction (Tomo's bounce-validity rule, May 7):** A bounce is valid rally evidence only if it crosses the net (`b1.cy - HALF_Y` and `b2.cy - HALF_Y` have opposite signs) or terminates on a net hit. Same-side multi-bounce sequences are racquet-bouncing or detector noise.

---

## Phase ladder

| # | Phase | Done-when | Owner / Status |
|---|---|---|---|
| 0 | Doc cleanup + this file | handover ≤700 lines, ≤5 active T5 memory files, this file exists with phase ladder | DONE 2026-05-07 |
| 1 | Bounce-validity rule | net-crossing filter applied at every `RallyStateMachine` consumer; bench remains 20/24; new fixture (post-Batch-rerun) confirms 458/463/584 movement | BOUNCE 2026-05-07 (filter shipped; bench 20/24; awaiting Batch rerun + re-snapshot to confirm 458/463/584) |
| 2 | Point boundary detection | Single function: given silver events, identify point start/end. Validated against SA `point_number` boundaries on a798eff0 — ≥80% point-boundary match. | POINT 2026-05-07 (function landed; 17.6% IOU≥0.5 / 64.7% IOU≥0.3 — gated by Phase 1 noise) |
| 3 | Pre-/between-point filter | `silver.point_detail` no longer contains events outside point boundaries. T5 event count for a798eff0 within ±5% of SA event count. | PREP agent-ac1bff976f520a088 2026-05-07 — see `docs/_investigation/may07_t5_event_noise.md` |
| 4 | Point-completeness reconciler | New diag tool: for each SA point, report match/partial/missing per stroke. Single-number metric committed alongside `bench_baseline.json`. | RECONCILER 2026-05-07 (baseline 0/17) |
| 5 | Stroke classification reconciliation | T5 vs SA stroke distribution within ±10% per class on validated points. Stop calling racquet-bounces "backhands". | UNCLAIMED (depends on 3) |
| 6 | Bounce + ball-hit coordinate reconciliation | Per-event `bounce_court_x/y` populated; geometric error vs SA <2m on validated points. | UNCLAIMED (depends on 1) |
| 7 | Final serve-detection cleanup | Revisit 4 a798eff0 misses with the upstream fixes in place. Whatever doesn't recover is genuinely upstream and gets parked. | UNCLAIMED |

---

## Per-phase detail

### Phase 1 — Bounce-validity rule
**What:** New module `ml_pipeline/serve_detector/bounce_validity.py` containing a pure function `validate_bounces(bounce_seq) -> filtered_seq` that keeps bounces whose ball trajectory crosses the net (or terminates in a net-hit). Wire into `RallyStateMachine.__init__` consumers (both `build_from_db` and the extract_far_pose call site).
**Where:** `ml_pipeline/serve_detector/`. `extract_far_pose` in `ml_pipeline/roi_extractors/pose.py`.
**How to verify:** Local bench stays 20/24 (no PASS regression). After production Batch rerun on a798eff0 + re-snapshot, expect at least 1 of 458/463/584 to flip to PASS. If not, the bounce signal alone isn't enough — add the cluster_size relaxation in 7.
**Blocker:** None.

### Phase 2 — Point boundary detection
**What:** Function `detect_point_boundaries(serves, ball_events, fps) -> [(point_start_frame, point_end_frame)]`. Point starts at accepted serve, ends at the next accepted serve OR the next idle gap >N seconds in valid (net-crossing) bounce activity.
**Where:** `ml_pipeline/serve_detector/` or new `point_structure/` module.
**How to verify:** Reconciler diag tool that compares T5 boundaries to SA `point_number` ground truth on a798eff0. Per-point match rate ≥80%.
**Blocker:** Phase 1 should land first because point-end detection depends on knowing real bounces from racquet-bounces.

### Phase 3 — Pre-/between-point filter
**What:** In `build_silver_match_t5.py`, add a filter pass that drops any `player_swing` row whose timestamp falls outside any detected point boundary.
**Where:** `build_silver_match_t5.py` and possibly the shared `build_silver_v2.py` passes 3-5.
**How to verify:** T5 event count for a798eff0 within ±5% of SA event count (currently 160 vs 85, target ≤89). T5 stroke distribution per class within ±10% of SA.
**Blocker:** Phases 1+2.

### Phase 4 — Point-completeness reconciler
**What:** New `ml_pipeline/diag/audit_points.py` (parallel to `audit_all_serves.py`). For each SA point, find matching T5 point, report match/partial/missing per stroke. Output: single number ("X/Y points fully reconcile") + per-point breakdown.
**Where:** `ml_pipeline/diag/`.
**How to verify:** Tool runs on the a798eff0 fixture, reports a number, commits the baseline alongside `bench_baseline.json`.
**Blocker:** None — can be built in parallel with Phase 1, but useful for measuring Phases 2/3.

### Phase 5 — Stroke classification reconciliation
**What:** Validate that T5's FH/BH/V/OH classifications match SA on validated points. Currently T5 reports BH=62 vs SA's 15 — most of the gap is misclassified racquet-bouncing (Phase 3 should fix). The remainder is classification logic.
**Where:** `build_silver_v2.py` stroke derivation logic.
**How to verify:** Phase 4 reconciler reports per-class accuracy ≥90% on validated points.
**Blocker:** Phase 3.

### Phase 6 — Bounce + ball-hit coordinate reconciliation
**What:** Phase 1 lands `validate_bounces()` which already filters phantom bounces. Phase 6 ensures the *valid* bounces are populated into silver as `bounce_court_x/y` columns and reconciled vs SA truth. May also need bounce extraction improvements for low-confidence far-half bounces (the Apr 22 ROI bounce extractor was a STUB; revisit `roi_extractors/bounces.py`).
**Where:** `silver.point_detail` columns, `roi_extractors/bounces.py`, `build_silver_v2.py`.
**How to verify:** Geometric error <2m vs SA on validated points. Reconciler tool from Phase 4 reports per-event coordinate error.
**Blocker:** Phase 1.

### Phase 7 — Final serve-detection cleanup
**What:** With bounce validation, point boundaries, and clean silver in place, revisit the 4 a798eff0 misses. Whichever still don't recover gets a one-line memo in the Backlog + parked.
**Blocker:** Phases 1-6.

---

## Backlog (issues we know about but aren't in the phase ladder)

- **2.4-7m y-axis offset.** Calibration extrapolation behind the far baseline produces court_y -3 to -7m for players who are visually at the baseline. PASS gate `[-3.5, 4.5]` admits them with 0.1m margin. Apr 29 verified naive widening (-3.5→-5.0) loses 2 PASS. Likely needs to be addressed via a pixel-y-based far-baseline check (replacing `_baseline_zone(court_y)`) — touches multiple call sites; deferred.
- **148.52 NEAR pose-amplitude gap.** Real serve, real keypoints (0.95 conf), but dominant wrist physically never clears avg shoulder line by more than 0.1px. Needs pose-model swap or training data — deferred.
- **`extract_roi_bounces.py` integration.** WASB bounce extractor (`roi_extractors/bounces.py`) is currently a STUB. Phase 6 revisits.
- **Stroke classifier (optical flow CNN) training.** `ml_pipeline/stroke_classifier/` exists with model + flow extractor, but no trained weights. Awaiting clean dual-submit data — Phase 3+ might unblock.
- **TrackNetV3 retraining.** Architecture ported (`ml_pipeline/tracknet_v3.py`); weights not trained — blocked on clean far-half bounce labels.
- **Custom T5 skill** (`.claude/skills/t5/`). Marginally helpful for new sessions; ~1 hour of work; not blocking. Add when the project enters a calmer phase.

---

## Autonomy infrastructure (separate track)

Not blocking phases 1-7. Captured here so it doesn't get lost.

| Tier | What | Effort | Gain |
|---|---|---|---|
| 1 | Local diag where possible; user only intervenes on Batch reruns | Already there | ~30% |
| 2 | Read-only `/api/diag/sql` Flask endpoint with `OPS_KEY` auth + SELECT-only enforcement; agents query via WebFetch | ½ day | ~50% |
| 3 | GitHub Actions workflow runs `bench` on every push | Few hours | Catches detector regressions before review |
| 4 | All diag tools DB-aware via the SQL endpoint (point reconciler, bounce extractor, etc.) | Ongoing | ~70% |
| 5 | Render→Batch automation: trigger reruns from agent context, watch CloudWatch via API | Weekend project | ~90%; only worthwhile if 2+ months of T5 work remain |

Tier 2 is highest immediate leverage. Schedule for after Phase 1 lands.

---

## Operating rules

1. **No detector edit without `bench` green first.** Hard rule from CLAUDE.md.
2. **Phase work updates this file.** Anyone closing a phase: bump status, write a 3-line "what changed" entry under the phase. Anyone starting: claim it (write your name + date in Status column).
3. **New ideas → Backlog, not into phases.** New directions get triaged by Tomo before they become phases. Keeps scope contained.
4. **One agent per phase, isolated worktrees.** No file conflicts.
5. **Validation that requires Batch reruns is a Tomo-trigger step.** Agent commits + pushes; Tomo reruns Batch when convenient. Not real-time.

---

## How to update this file

- **Closing a phase:** flip Status to DONE with date; write 3 lines under the phase explaining what shipped + key learnings.
- **Starting a phase:** flip Status from UNCLAIMED to `<your session ID> <YYYY-MM-DD>`; commit before work starts.
- **Major restructuring (new bottleneck, new phases):** copy current file to `docs/_archive/north_star_YYYY-MM-DD.md` first, then rewrite. Don't lose history.
- **Bench baseline shifts:** mention here (not just in `bench_baseline.json` commit message). The single-number metric for the dashboard's data quality is what this file is tracking, not just the detector's.
