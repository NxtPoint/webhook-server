# T5 ML Pipeline — North Star

**Last updated:** 2026-05-22 morning — **Phase 5c.2 (corpus pipeline foundation) SHIPPED** (`d7718e0`) + **silver-builder bench fully implemented** (`83e1ab7`, snapshot + orchestrator). Phase 5e (WASB integration) shipped 2026-05-21 deep eve; production verification still pending parallel-agent thread. Both ECRs hold the new image (`sha256:8fe82a3…`), eu-north-1 rev 47 + us-east-1 rev 29 both active with `BALL_TRACKER=wasb` env var.
**Last verified:** 2026-05-22 — serve bench green on both fixtures (a798eff0 20/24, 880dff02 23/24); ball-tracker bench v2 baseline locked at `7100792`; silver-bench schema init verified locally (creates 24 expected tables on fresh Docker Postgres, including the new `ml_analysis.training_corpus`). Production WASB verification on task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` still pending. 5c.2 hook gated behind `AUTO_LABEL_DUAL_SUBMIT_PAIRS=0` until Tomo flips it.
**Previous version archived:** `docs/_archive/north_star_2026-05-07_phantom-bounce-era.md`
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

## Status snapshot — 2026-05-07 EOD

**What shipped today (validated end-to-end on `880dff02-58bd-412c-9a29-5c5151004447` vs SA `2c1ad953-...`):**

- **Phase 1 — bounce-validity rule** → DONE. Strict reconcile **23/24 (10/10 FAR)**, all three target FAR misses recovered. Bench locked at 880dff02 23/24 + a798eff0 20/24.
- **Phase 3 part 1 — warm-up filter** → 35-row noise reduction; **Backhand crushed 62→10**; T5 active silver 49 vs SA 85.
- **Phase 4 — reconciler tool** → shipped (`audit_points_reconcile.py` + baseline + `--honor-exclude` flag).
- Tier 2 SQL endpoint + Tier 3 bench CI live. Batch deploy protocol documented.

**What today's investigation revealed:**

- The per-point reconciler floor of **0/17** isn't a noise problem and isn't a Phase 3 problem. Root cause classified in `docs/_investigation/may07_sa_point6_gap.md`: T5's bronze ball detection sits at **~13% frame coverage** match-wide, with **six >40-second gaps**. SA point 6 (9 strokes, ~16s rally) falls inside a 61.8-second ball-detection blackout. Player tracking is fine — it's purely the ball.
- This blocks Phase 6 (stroke classification), Phase 7 (coordinate reconciliation), and Phase 8 (final serve cleanup). All three depend on T5 strokes existing at the right times, which depends on ball detection coverage.

**Renumbered ladder:** what was old "Phase 5/6/7" is now "Phase 6/7/8". A new Phase 5 — **Ball detection coverage** — is inserted as the top bottleneck.

---

## Current bottleneck

**Ball detection coverage.** TrackNetV2 returns ball coordinates for ~13% of frames on the validation match. Six >40s gaps include the 61.8s window that contains SA point 6. Without ball data, T5 cannot emit silver rows at the right timestamps for real rally play, so per-point reconciliation cannot rise above its current floor. This is multi-week investment (TrackNetV3 retrain, ROI bounce extractor finish, Hough fallback gain-up, dual-submit training data) — not a session.

Phase 1 is closed; the phantom-bounce era described in the archived north_star is over.

---

## Phase ladder

| # | Phase | Done-when | Owner / Status |
|---|---|---|---|
| 0 | Doc cleanup + this file | handover ≤700 lines, ≤5 active T5 memory files, this file exists with phase ladder | DONE 2026-05-07 |
| 1 | Bounce-validity rule | net-crossing filter applied; bench 20/24 floor; new fixture confirms 458/463/584 movement | DONE 2026-05-07 — 880dff02 fixture **23/24 (10/10 FAR)** |
| 2 | Point boundary detection | `detect_point_boundaries()` function exists; per-point match ≥80% on `a798eff0` | PARTIAL — function landed (POINT 2026-05-07); IOU 17.6% pre-Phase-3, **pending re-measurement on post-Phase-3 active silver** |
| 3 | Pre-/between-point filter | Active T5 silver ±5% of SA event count; stroke distribution within ±10% per class | PARTIAL — warm-up half shipped; **between-point empirically blocked by Phase 5 (2026-05-20)** |
| 4 | Point-completeness reconciler | Diag tool shipped with baseline alongside `bench_baseline.json` | DONE 2026-05-07 — tool live, baseline 0/17 (root cause classified as Phase 5 territory) |
| **5** | **Ball detection coverage (NEW)** | **T5 ball-detection frame coverage ≥50%; longest gap <5s; SA point 6 has T5 ball detections** | **UNCLAIMED — top bottleneck, multi-week** |
| 6 | Stroke classification reconciliation (was 5) | T5 vs SA stroke distribution within ±10% per class on validated points | BLOCKED by 5 |
| 7 | Coordinate reconciliation (was 6) | Per-event `bounce_court_x/y` populated; geometric error vs SA <2m | BLOCKED by 5 |
| 8 | Final serve-detection cleanup (was 7) | Revisit 4 a798eff0 misses with all upstream fixes in place | BLOCKED by 5 |

---

## Per-phase detail

### Phase 1 — Bounce-validity rule — DONE 2026-05-07
**What landed:** `ml_pipeline/serve_detector/bounce_validity.py` exposing `validate_bounces()` (HALF_Y=11.885), wired into `RallyStateMachine.build_from_db`, `extract_far_pose`'s in-memory rally-gate block, and `detect_serves_offline` so bench mirrors prod. Image rebuilt + pushed to both ECRs (eu-north-1 rev 44, us-east-1 rev 26, amd64 sub-manifest digest `sha256:3f2a3fa1...c6b8`).
**Validation:** Fixture `880dff02` ran end-to-end on the new image: bench reports **23/24 (13/14 NEAR, 10/10 FAR)** vs the locked a798eff0 baseline of 20/24. All three target FAR misses (458.08, 463.52, 584.92) flipped to MATCH on the strict reconciler. New baseline locked in `ml_pipeline/diag/bench_baseline.json`.
**Residual:** 1/24 still missing — 148.52 NEAR. Bucket C class (bronze pose-amplitude gap, `arm_ext` distribution caps at 0.1px), independent of phantom-bounce class. Backlog. Not worth chasing without a pose model swap.
**Key learning:** `extract_far_pose` lives in the Batch container. The first push of Phase 1 was Render-only — Batch jobs ran the OLD image silently. Pre-merge checklist + on-demand-priority queue swap added to `handover_t5.md` + CLAUDE.md as a result.

### Phase 2 — Point boundary detection — PARTIAL 2026-05-07
**What:** Function `detect_point_boundaries(serves, ball_events, fps) -> [(point_start_frame, point_end_frame)]`.
**Where:** `ml_pipeline/point_structure/point_boundaries.py` (function), `ml_pipeline/diag/audit_points.py` (audit tool).
**Status:** Function landed. Audit reported 17.6% IOU≥0.5 / 64.7% IOU≥0.3 on the noisy pre-Phase-3 silver. **Re-measurement on post-Phase-3 active silver is the next step** — should rise materially with 35 noise rows removed and active T5 only 49 rows.
**Done-when:** Per-point match rate ≥80% IOU≥0.5 on `880dff02` post-Phase-3.
**Blocker:** None for re-measurement. Integration into silver is Phase 3 part 2.

### Phase 3 — Pre-/between-point filter — PARTIAL 2026-05-07
**What:** Filter pass in `build_silver_v2.py` that drops T5 silver rows outside detected point boundaries via `exclude_d=TRUE`.
**Where:** `build_silver_v2.py` pass 3 + (eventually) consumes Phase 2's `detect_point_boundaries()`.

**Part 1 — warm-up filter — DONE 2026-05-07.** New `first_serve_task` CTE + OR clause in the `final` CTE flips `exclude_d=TRUE` on rows where `ball_hit_s < per-task MIN(ball_hit_s) FILTER (serve_d)`. Predicted 35-row impact on `880dff02` confirmed via direct query (76 pre-existing exclusions + 35 new = 111 TRUE). Backhand count on active silver dropped from 62 → 10 (now slightly *under* SA's 15). Bench unchanged.

**Part 2 — between-point filter — BLOCKED by Phase 5 (2026-05-20).** Two pure-SQL attempts shipped + reverted; both flawed for the same upstream reason.

  - **v1 (commit 00b8639, reverted)** — Pattern A from session_2026-05-20_review.md: anchor on every `serve_d=TRUE` row in `with_try_ff`, window = `LEAST(hit+30s, next_serve-2s)`. Result on 880dff02: **no-op**. T5's geometric serve detector emits 107 detections on an 18-point match (any overhead-type swing within EPS of a baseline qualifies). 107 dense anchors create windows that cover the entire match → nothing falls outside any window → 0 rows excluded. Active T5 rows held at 49.
  - **v2 (commit f0b104e, reverted)** — anchor on first `serve_d=TRUE` per `point_number` (~18-30 anchors), 20s cap. Result on 880dff02: **wrong rows dropped**. Active T5 49 → 34 (-15 by count) but the reconciler's "T5 strokes outside ANY SA point window" held at 20 — all 15 dropped rows were INSIDE real SA windows. Per-point: pt 5 (SA [178.44–195.96]) 8 T5 → 1; pt 14 (SA [458.08–468.00]) 9 T5 → 1. Forward-fill of `point_number` assigns rows in the [SA_point_start, T5_serve_detection] gap to the PREVIOUS point_number; those rows then fall outside that previous point's 20s window and get excluded — even though they're real strokes of the current point.
  - **Pattern B (Python `detect_point_boundaries()` integration) — inherits the same start-of-window limit.** `detect_point_boundaries()` improves the END of windows via `idle_gap_s=4.0s` (bounce-driven, tighter than v2's 20s cap), but `start_frame = serve.frame_idx` is identical to v2. The structural problem is "T5's serve detection lands later than SA's true point start" — Pattern B doesn't address this.
  - **Root cause confirmed empirically: this work requires reliable bounce evidence to distinguish 'real stroke before serve detection' from 'between-point noise'. That's Phase 5.** Don't re-attempt Phase 3 part 2 until Phase 5 ball-detection coverage is materially better.

  Revert lives at `de06d41` on main. Phase 3 part 1 (warm-up filter at line 713-718 of `build_silver_v2.py`) is unaffected and still shipping. Restart the design when Phase 5 has produced ≥30% ball-coverage on 880dff02.

**How to verify (when re-attempted):** Active T5 silver row count within ±5% of SA's. **AND** the reconciler's "T5 strokes outside ANY SA point window" count drops. Don't trust row-count alone — v2 hit the row-count target but dropped real strokes; the reconciler's window-overlap metric is the load-bearing signal.

### Phase 4 — Point-completeness reconciler tool — DONE 2026-05-07
**What landed:** `ml_pipeline/diag/audit_points_reconcile.py` + `ml_pipeline/diag/points_reconcile_baseline.json`. CLI: `python -m ml_pipeline.diag.audit_points_reconcile --task <T5_TID> [--honor-exclude]`. Reports per-SA-point match/partial/missing per stroke; produces a single number "X/Y points fully reconcile."
**Baseline:** **0/17 points fully reconcile** on `880dff02`. Today's investigation classified this as ball-coverage-limited (Phase 5 territory), not a tool problem.
**Future use:** Re-run after each Phase 5 milestone to track how per-point reconciliation moves. Re-run after Phase 3 part 2 lands to measure noise→accuracy tradeoff.
**Done-when:** Tool committed (✓), baseline file committed (✓), `--honor-exclude` flag for active-view (✓).

### Phase 5 — Ball detection coverage — TOP BOTTLENECK
**What:** Get T5's bronze `ml_analysis.ball_detections` to ≥50% frame coverage, with longest gap <5s on the validation match. Currently ~13% coverage with six >40s gaps.

**Why this is the bottleneck (evidence from `docs/_investigation/may07_sa_point6_gap.md`):**
- SA point 6 (9 strokes, ~16s rally, frames 5599-6003) has **0 T5 ball detections** in window
- Match-wide T5 has 1,983 ball detections across 15,300 frames = 13% coverage
- Six gaps >40s; top three are 91.6s, 73.2s, 61.8s
- Player tracking is fine through these windows (490/400 court-coord rows in SA point 6)
- 10 of 17 SA points have zero T5 strokes in their windows because of this — Phase 6 + 7 cannot proceed

**Sub-tasks (parallelizable, all in `ml_pipeline/`):**

- **5a — Finish ROI bounce extractor — DONE 2026-05-21.** `ml_pipeline/roi_extractors/bounces.py` rewritten from stub to production extractor (~320 lines). Anchor strategy: bounce-only no-zone-filter (chosen after fixture diagnostic showed the kickoff doc's default would cover only 1/24 SA serves vs 6/24 for bounce-only). Anchor windows are ±2.5s around clustered bronze bounces, TrackNet rerun on tight service-box crop, results merged INTO canonical `ml_analysis.ball_detections` (NOT a parallel `_roi` table — architectural pivot to Option A on 2026-05-21 PM). Validated on task `763c9ee9`: 459 ROI rows / 23 bounces added; silver row count 160 → 183 (+23); first NEAR T5 serve in silver ever (id=92, ts=178.76s, hit_y=24.05). Bench unchanged at 23/24 + 20/24. Production image: eu-north-1 job-def rev 46, us-east-1 rev 28, both `sha256:87435dbfd…`. Phase 5 done-when targets only PARTIALLY met (frame coverage gain is modest — bigger gains need WASB integration / Phase 5d).
- **5b — Frame-delta Hough fallback gain-up. PARKED 2026-05-20 with empirical receipts.** Round 0 baseline diagnostics (CloudWatch on 880dff02 + local Tier-4 sweep on a798eff0) showed: (i) Tier 4 already returns a position on ~99.93% of TrackNet-empty frames — there's no headroom to "fire more often"; (ii) the staged motion-threshold change 25→15 regresses post-`_filter_outliers` survival by 11.6% (local exp on a798eff0), because lowering the gate makes Hough's strongest-circle pick noisier rather than catching more real balls; (iii) `tier2_cc_rejected = 0` on 880dff02 — the Tier 2 area-gate change is a no-op too; (iv) the dominant filter is `_filter_outliers` (150px from previous-kept) which eats ~79% of Tier-4 returns. Source-aware filter surrogate (Option α) showed -3.0pp rally-precision and the deeper finding that `ball_rows` aren't strongly concentrated in rally windows even pre-filter (Tier-1 fires across the whole match, not just in rallies) — so "gate Tier-4 by recent Tier-1 anchor" doesn't get the concentration boost the design assumed. Full BallTracker local validation aborted (40-min estimate was off by ~30×; actual ~21 hrs on CPU without GPU). Receipts: `.claude/phase5b_ball_tracker_characterisation.md` (Tuning rounds + reprioritised candidates) + commit `d26e8cc`. Branch `phase-5b/motion-threshold-reduce` retained on origin as a falsified-hypothesis record; do not merge.
- **5c — Dual-submit training data pipeline. Phase 5c.0 + 5c.1 READY.** Auto dual-submit flag (`AUTO_DUAL_SUBMIT_T5`) safety-reviewed and ready to flip on Render. Retro backfill endpoint shipped at `/ops/dual-submit-t5-backfill` (commit `98d20bf`). Phase 5c.2 (pair-completion hook + corpus index) is the next big build. Full breakdown: `.claude/strategy/dual_submit_status_2026-05-20.md`.
- **5d — TrackNetV3 retrain.** Architecture ported (`ml_pipeline/tracknet_v3.py`); weights not trained. Blocks on 5c. Once weights exist, swap them in via the existing config path — no architectural changes needed. **Lower urgency post-5e:** if WASB delivers production-equivalent F1 gains, the 5d retrain story collapses to "WASB beats us out the gate; finetune V3 only if WASB plateaus."
- **5e — WASB-SBDT integration. SHIPPED 2026-05-21.** WASB (HRNet backbone, BMVC 2023) wired into `ml_pipeline/pipeline.py` as a drop-in alternative to `BallTracker`, env-gated via `BALL_TRACKER` (default `tracknet_v2`; both prod job-defs set to `wasb`). Validated by ball-tracker bench (`ml_pipeline/diag/bench_ball_baseline.json`, commit `7100792`): WASB recovers 2/9 vs TrackNetV2's 0/9 SA point 6 strokes on the canonical bronze-coverage-gap regime. Lower raw detection rate (11.76% vs 99.73% on 880dff02) because TrackNetV2's 4-tier output is 58-67% motion-fallback noise that gets stripped by `_filter_outliers`; WASB's pre-filter output is honest. End-to-end verified on Tesla T4: downstream methods (interpolate_gaps, detect_bounces, compute_speeds, assign_peak_flight_speeds) work on WASB's sparser detections. Image `sha256:8fe82a3…`, eu-north-1 job-def rev 47, us-east-1 rev 29. Production verification on a fresh upload pending (task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` in flight at session end). Rollback path: unset `BALL_TRACKER` env on the job-def, no code change needed.

**How to verify:**
- Match-level: ball-detection frame coverage ≥50% (up from 13%)
- Worst-gap: longest contiguous no-ball frames <5s (down from 91.6s)
- SA point 6 specifically: ≥3 T5 ball detections in window
- Phase 4 reconciler: per-point match rate ≥30% (up from 0%)

**Blocker:** 5a DONE 2026-05-21. 5b parked (2026-05-20). 5e SHIPPED 2026-05-21 pending production verification. 5d blocks on 5c. Next moves: (a) close out 5e production verification on `1d6feb3a`, (b) 5c.0+5c.1 flip — turn `AUTO_DUAL_SUBMIT_T5=1` + run backfill, (c) 5c.2 pair-completion hook + corpus index.

### Phase 6 — Stroke classification reconciliation (was 5) — BLOCKED by 5
**What:** Validate T5's FH/BH/V/OH classifications match SA on validated points.
**Status:** Phase 1 + Phase 3 part 1 already crushed the dominant racquet-bounce-as-Backhand misclassification (62→10). Remaining gap is real classification logic on actual strokes.
**Where:** `build_silver_v2.py` stroke derivation logic.
**How to verify:** Phase 4 reconciler reports per-class accuracy ≥90% on validated points.
**Blocker:** Phase 5. Until ball detection covers real-point windows, there are too few strokes to measure classification accuracy on.

### Phase 7 — Coordinate reconciliation (was 6) — BLOCKED by 5
**What:** Per-event `bounce_court_x/y` populated; geometric error vs SA <2m on validated points.
**Where:** `silver.point_detail` columns, `roi_extractors/bounces.py` (overlaps with Phase 5a), `build_silver_v2.py`.
**How to verify:** Geometric error <2m vs SA on validated points. Phase 4 reconciler reports per-event coordinate error.
**Blocker:** Phase 5. Need bounce coverage before reconciling coordinates.

### Phase 8 — Final serve-detection cleanup (was 7) — BLOCKED by 5
**What:** With ball coverage, point boundaries, and clean silver in place, revisit the 4 a798eff0 misses + 1 880dff02 miss (148.52 NEAR). Whichever still don't recover gets a one-line memo in the Backlog + parked.
**Blocker:** Phases 5–7.

---

## Backlog (issues we know about but aren't in the phase ladder)

- **2.4-7m y-axis offset.** Calibration extrapolation behind the far baseline produces court_y -3 to -7m for players who are visually at the baseline. Apr 29 verified naive widening (-3.5→-5.0) loses 2 PASS. Likely needs a pixel-y-based far-baseline check (replacing `_baseline_zone(court_y)`) — touches multiple call sites; deferred.
- **148.52 NEAR pose-amplitude gap.** Real serve, real keypoints (0.95 conf), but dominant wrist physically never clears avg shoulder line by more than 0.1px. Needs pose-model swap or training data — deferred to Phase 8.
- **Stroke classifier (optical flow CNN) training.** `ml_pipeline/stroke_classifier/` exists with model + flow extractor, but no trained weights. Unblocks once Phase 5c (dual-submit training data) lands.
- **Custom T5 skill** (`.claude/skills/t5/`). Marginally helpful for new sessions; ~1 hour of work; not blocking. Add when the project enters a calmer phase.
- **Silver should consume `ml_analysis.serve_events`** (branch `silver/connect-serve-events` / 2026-05-07, **NOT shipped** — backlog entry only). Naive OR overshoots the impact band because `serve_events` holds all 107 detector candidates, not just the 23 reconciler-validated ones. Two viable paths: (a) persist strict-reconciler MATCH verdict to a column on `serve_events`; (b) gate EXISTS on `rally_state` ∈ ('pre_point','in_rally') AND `confidence ≥ 0.7`. Belongs in Phase 6 (with Phase 5 bench harness as the safety net).
- **TrackNetV3 retraining moved to Phase 5d** (was here).
- **`extract_roi_bounces.py` integration moved to Phase 5a** (was here).

---

## Progress measurement

These are the metrics this file is tracking:

| Metric | Phase | Today's value | Target |
|---|---|---|---|
| Bench MATCH (strict reconcile) on `880dff02` | 1 | **23/24** | 24/24 (Phase 8) |
| Bench MATCH on `a798eff0` | 1 | 20/24 | unchanged baseline |
| Active T5 silver row count vs SA on `880dff02` | 3 | 49 vs 85 | within ±5% (≈ 81-89) |
| T5 active stroke distribution: Backhand | 3 | **10 vs SA 15** | within ±10% (13-16) |
| T5 active stroke distribution: Forehand | 3 | 21 vs SA 40 | within ±10% (36-44) |
| T5 ball-detection frame coverage | 5 | **13%** | ≥50% |
| Longest no-ball gap | 5 | **91.6s** | <5s |
| Per-point reconciler full_match | 4 → 5/6 | **0/17** | ≥8/17 after Phase 5; ≥14/17 after Phase 6 |
| Coordinate error vs SA | 7 | unmeasured | <2m |

The single-number metrics that matter most for "is the dashboard trustworthy" are bottom three. All blocked by Phase 5.

---

## Autonomy infrastructure (separate track)

| Tier | What | Status |
|---|---|---|
| 1 | Local diag where possible; user only intervenes on Batch reruns | Already there |
| 2 | Read-only `/api/diag/sql` Flask endpoint | **DONE 2026-05-07** (`infra/tier-2-sql-endpoint`) |
| 3 | GitHub Actions runs `bench` on push + PR | **DONE 2026-05-07** (`infra/tier-3-bench-ci`) |
| 4 | All diag tools DB-aware via the SQL endpoint | Ongoing — comes naturally as Phase 5/6/7 tools land |
| 5 | Render→Batch automation: trigger reruns from agent context, watch CloudWatch | Deferred — schedule during a Phase 5 lull, scope tighter than original brief (just SubmitJob + DescribeJobs, no streaming) |

---

## Operating rules

1. **No detector edit without `bench` green first.** Hard rule from CLAUDE.md.
2. **No T5 detector branch merges without the Batch-side change check.** `git diff --stat` against `ml_pipeline/roi_extractors/`, `__main__.py`, `pipeline.py`, `Dockerfile`, `requirements.txt`, `serve_detector/`. Non-empty diff → Docker rebuild + dual-region ECR push + new job-def revisions before user reruns. See `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST".
3. **Phase work updates this file.** Anyone closing a phase: bump status, write a 3-line "what changed" entry under the phase. Anyone starting: claim it (write your name + date in Status column).
4. **New ideas → Backlog, not into phases.** New directions get triaged by Tomo before they become phases. Keeps scope contained.
5. **One agent per phase, isolated worktrees.** No file conflicts.
6. **Validation that requires Batch reruns is a Tomo-trigger step.** Agent commits + pushes; Tomo reruns Batch when convenient. Not real-time.
7. **Don't ship code that depends on SA truth at runtime.** The strict reconciler is a diag tool; production has no SA counterpart. Filters and rules need to work without it.

---

## How to update this file

- **Closing a phase:** flip Status to DONE with date; write 3 lines under the phase explaining what shipped + key learnings. Update Progress measurement metrics table.
- **Starting a phase:** flip Status from UNCLAIMED to `<your session ID> <YYYY-MM-DD>`; commit before work starts.
- **Major restructuring (new bottleneck, new phases):** copy current file to `docs/_archive/north_star_YYYY-MM-DD_<context>.md` first, then rewrite. Don't lose history.
- **Bench baseline shifts:** mention here (not just in `bench_baseline.json` commit message). The single-number metric for the dashboard's data quality is what this file is tracking, not just the detector's.
