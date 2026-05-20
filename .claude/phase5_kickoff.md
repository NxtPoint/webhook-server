# Phase 5 Kickoff — Ball Detection Coverage

**Created:** 2026-05-07 EOD by previous session (Phase 1 + Phase 3 part 1 closure)
**Purpose:** Hand off to a fresh Claude session that will run T5 Phase 5.

---

## Why this exists

Today's session closed Phase 1 (bounce-validity rule, validated 23/24 strict reconcile on `880dff02`) and Phase 3 part 1 (warm-up filter, BH 62→10 on active silver). It also discovered the next bottleneck: **T5's bronze ball detection sits at ~13% frame coverage with six >40s gaps**, blocking Phase 6/7/8 downstream. North Star was rewritten with this finding (commit `e6ea506`); previous version archived at `docs/_archive/north_star_2026-05-07_phantom-bounce-era.md`.

This file isn't the plan — `docs/north_star.md` is. This is just a launching note so the next session walks in oriented.

---

## Read in this order

1. `docs/north_star.md` — full plan. Phase 5 is the new entry; sub-tasks 5a/5b/5c/5d are scoped.
2. `docs/_investigation/may07_sa_point6_gap.md` — the receipts. SA point 6 (~16s rally) sits inside a 61.8s ball-detection blackout. ~13% match-wide coverage. **Don't redo this diagnosis.**
3. **`.claude/phase5b_ball_tracker_characterisation.md` — required reading for Phase 5b specifically.** Four-tier detector map, every Hough/threshold parameter with current value + tuning headroom, prioritised candidate-change list with predicted impact, measurement-first workflow. A staged single-parameter change (motion threshold 25 → 15) lives on branch `phase-5b/motion-threshold-reduce` ready for the Batch dance.
4. `.claude/handover_t5.md` — operational doc. Specifically the "BATCH-SIDE CHANGE CHECKLIST" (any roi_extractors / ball_tracker / pipeline.py / Dockerfile edit needs Docker rebuild + dual-region ECR push + new job-def revisions) and the "TEST HARNESS" section (bench is the regression gate — currently locked at a798eff0 20/24 + 880dff02 23/24).

---

## Phase 5 sub-tasks (parallelizable)

| # | Task | Where | Blocks | Status |
|---|---|---|---|---|
| 5a | Finish ROI bounce extractor STUB | `ml_pipeline/roi_extractors/bounces.py` | — | UNCLAIMED |
| 5b | Frame-delta Hough fallback gain-up | `ml_pipeline/ball_tracker.py` | — | UNCLAIMED |
| 5c | Dual-submit training data pipeline | TBD | 5d | UNCLAIMED |
| 5d | TrackNetV3 retrain weights | `ml_pipeline/tracknet_v3.py` (architecture exists) | — | UNCLAIMED, blocks on 5c |

5a and 5b are independent and can run in parallel. 5c is foundation for 5d.

**Recommended starting point: 5b (Hough fallback gain-up).** Smallest code change, lowest risk, fastest validation. Existing code is the right shape; a parameter tune may yield a measurable coverage delta. Bench protects detector recall as a safety net; ball coverage measurement is direct via `ml_analysis.ball_detections` row count.

**Phase 5b characterisation pass already done (2026-05-20).** See `.claude/phase5b_ball_tracker_characterisation.md` for the four-tier detector map, eight candidate tuning changes with predicted impact + risk, and the measurement-first iteration protocol. A staged round-1 change (motion threshold 25 → 15 in `_detect_ball_frame_delta`) is on branch `phase-5b/motion-threshold-reduce`, NOT yet pushed to Batch — the next session runs the Batch dance and measures the delta.

---

## Reference data

- **T5 fixture:** `880dff02-58bd-412c-9a29-5c5151004447` — locked at bench 23/24, validated post-Phase-1
- **SA truth:** `2c1ad953-b65b-41b4-9999-975964ff92e1` — 85 silver rows, 17 points, canonical
- **Per-task ball coverage signal:**
  ```sql
  SELECT count(DISTINCT frame_idx) AS detected_frames,
         (SELECT total_frames FROM ml_analysis.video_analysis_jobs WHERE job_id = '880dff02-58bd-412c-9a29-5c5151004447') AS total_frames
  FROM ml_analysis.ball_detections
  WHERE job_id = '880dff02-58bd-412c-9a29-5c5151004447';
  ```
  Today: 1983 / 15300 ≈ 13%
- **Worst-gap signal** (frame ranges with no ball detection >5s):
  ```sql
  WITH frames AS (
    SELECT frame_idx, lead(frame_idx) OVER (ORDER BY frame_idx) AS next_frame
    FROM ml_analysis.ball_detections
    WHERE job_id = '880dff02-58bd-412c-9a29-5c5151004447'
  )
  SELECT frame_idx, next_frame, (next_frame - frame_idx) / 25.0 AS gap_s
  FROM frames WHERE next_frame - frame_idx > 125 ORDER BY gap_s DESC LIMIT 10;
  ```
  Today: top three are 91.6s, 73.2s, 61.8s

---

## Operating rules (carry forward from CLAUDE.md + north_star.md)

1. **No detector edit without bench green first.** `python -m ml_pipeline.diag.bench` must report unchanged numbers (a798eff0 20/24, 880dff02 23/24) before commit.
2. **BATCH-SIDE CHANGE CHECKLIST.** Any edit to `ml_pipeline/roi_extractors/`, `__main__.py`, `pipeline.py`, `Dockerfile`, `requirements.txt`, `ball_tracker.py`, `serve_detector/` requires Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1. The full sequence ran successfully today (commits `bc07293` for the code, see handover_t5.md for the script). On-demand CE is queue order 1, Spot is order 2 — accepted ~$0.40/job premium for testing reliability.
3. **One sub-task per agent per worktree.** Don't bundle 5a + 5b in one agent — independent edit surfaces.
4. **Update `docs/north_star.md` Status column when claiming.** Phase 5 row, sub-task table.
5. **Validation that requires Batch reruns is a Tomo-trigger step.** Agent commits + pushes + does the Docker dance; Tomo re-uploads via Media Room when convenient. Not real-time.
6. **Phase 5 metric targets:** match-level coverage ≥50%, longest gap <5s, SA point 6 has ≥3 T5 ball detections. These are the load-bearing numbers — track them in commit messages and in north_star.md Progress measurement table.

---

## What today's session left in flight

- 6 phase/infra branches merged to main during session
- Phase 1 DONE, Phase 3 part 1 DONE (warm-up filter), Phases 2 + 4 are tools-landed
- Tier 2 (SQL endpoint) + Tier 3 (bench CI) live in production
- Batch deploy protocol documented in handover_t5.md and CLAUDE.md
- All investigation memos in `docs/_investigation/`

Nothing is half-shipped. Phase 5 starts cleanly.

---

## When Phase 5 closes

Update `docs/north_star.md` Phase 5 row + Progress measurement table. Then Phase 6 (stroke classification reconciliation) becomes unblocked — and Phase 4 reconciler should jump from 0/17 toward 8-14/17 as a natural side effect of better ball coverage. That's the load-bearing dashboard quality metric.
