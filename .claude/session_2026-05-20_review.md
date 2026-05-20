# Session 2026-05-20 — T5 Status Review & Phase 3 Part 2 Integration Design

**Created:** 2026-05-20 by autonomous Claude session (Tomo's terminal closed mid-review on prior T5 session; re-running due diligence and laying runway for next session).
**Purpose:** Pick up cold, capture what's still verified, document the Phase 3 part 2 integration design so it can ship in <1 hr next session.

---

## TL;DR

- May 7 detector / serve-detection work is still locked. **Bench green** on both fixtures (a798eff0 20/24, 880dff02 23/24) — verified locally on commit `6cd8156` 2026-05-20.
- **Two non-detector commits landed late on 2026-05-20** (concurrent session): `6d82ac6` build_silver_v2 tiebreak coalescing (silver-build CTE, affects both SportAI + T5 silver) and `aa6c9ff` dashboards polish pass. Bench unaffected (silver/frontend only).
- **Phase 5 (ball detection coverage) is still the bottleneck.** Multi-week investment. UNCLAIMED.
- **Phase 3 part 2 (between-point filter) integration design is below** — small impact (~15 rows) but the function exists, no tests, integration site is well-defined. Cheap to ship next session.
- **`.claude/phase5_kickoff.md` is still accurate** — the four sub-tasks (5a/5b/5c/5d) and the recommended starting point (5b: Hough fallback gain-up) haven't changed.

---

## Read in this order

1. **`docs/north_star.md`** — the macro plan. Phase ladder + done-when criteria + Progress measurement table. This is still the truth.
2. **`.claude/handover_t5.md`** — operational detail. Batch deploy protocol + test harness rules.
3. **`.claude/phase5_kickoff.md`** — Phase 5 sub-tasks (5a/5b/5c/5d). Still the right next-direction.
4. **This file** — only for the Phase 3 part 2 integration design + the validation commands listed at the bottom.

---

## What was re-verified today (2026-05-20)

### Bench (2 fixtures, sub-second)

```
fixture         near     far   total   delta
880dff02      13/14   10/10   23/24   (no change)
a798eff0      13/14    7/10   20/24   (no change)
```

Both fixtures present in `ml_pipeline/fixtures/` locally (gitignored). CI fixture also present at `ml_pipeline/fixtures_ci/a798eff0.pkl.gz`. Baseline in `ml_pipeline/diag/bench_baseline.json` unchanged since May 7.

### File state

- `ml_pipeline/point_structure/point_boundaries.py` — function `detect_point_boundaries()` clean, well-documented, has an explicit `# TODO (Phase 3 integration)` comment pointing at the integration site.
- `ml_pipeline/point_structure/__init__.py` — empty package marker (exports may need adding when Phase 3 part 2 ships).
- No tests under `ml_pipeline/point_structure/tests/` — directory doesn't exist.
- `build_silver_v2.py` line 704-718: Phase 3 part 1 (warm-up filter) precedent intact.

### Doc state

- `docs/north_star.md` last updated 2026-05-07 EOD. Status table still correct.
- `.claude/phase5_kickoff.md` written 2026-05-07 EOD. Still accurate as a Phase 5 launching note.
- `ml_pipeline/training/visual_debug/` — 9 leftover debug images from the May 7 session. **Skip these** — Tomo's instruction.

---

## Phase 3 Part 2 — Integration Design

This is the cheapest remaining cleanup before Phase 5 dominates the bottleneck. Expected impact: ~15 rows on `880dff02` (the survivors of Phase 3 part 1's warm-up filter that fall in between-point gaps). Not a Phase-5-level metric move; just hygiene.

### Where to integrate

**Site:** `build_silver_v2.py` `pass3_point_context()`, parallel to the existing `first_serve_task` CTE (lines 704-718). The warm-up filter pattern is the precedent — extend `exclude_d` via OR in the final CTE, never via subtraction.

**Why NOT in `excl_chain`:** `excl_chain` only operates on rows where `point_number > 0` (line 682). Between-point rows have `point_number IS NULL` and are unreachable from inside `excl_chain` (same constraint that motivated the warm-up filter to live outside it).

### Two viable implementation patterns

**Pattern A — Pure SQL approximation** (lower risk, doesn't import the Python function):

Add a CTE that defines per-task point windows from the serve list:

```sql
point_windows AS (
  SELECT task_id,
    ball_hit_s AS point_start_s,
    LEAD(ball_hit_s) OVER (PARTITION BY task_id ORDER BY ball_hit_s) AS next_serve_s,
    LEAST(
      ball_hit_s + 30.0,  -- hard cap: real points rarely exceed 30s
      COALESCE(LEAD(ball_hit_s) OVER (PARTITION BY task_id ORDER BY ball_hit_s), ball_hit_s + 30.0) - 2.0
        -- buffer: 2s before next serve = walk-to-serve-position
    ) AS point_end_s
  FROM with_try_ff
  WHERE serve_d IS TRUE AND ball_hit_s IS NOT NULL
)
```

Then in the `final` CTE, OR-extend `exclude_d`:

```sql
OR NOT EXISTS (
  SELECT 1 FROM point_windows pw
  WHERE pw.task_id = so.task_id
    AND so.ball_hit_s >= pw.point_start_s
    AND so.ball_hit_s <= pw.point_end_s
)
```

**Pros:** Pure SQL, no Python state, easy review. **Cons:** Approximation of the function; doesn't track idle-bounce gaps mid-rally (might mis-filter long rallies near the 30s cap).

**Pattern B — Python pre-compute + temp table** (matches `detect_point_boundaries()` exactly):

Inside `pass3_point_context(conn, task_id, cfg)`:

1. Query `silver.point_detail` for `(frame_idx, ball_hit_s)` where `serve_d=TRUE` and `task_id=:tid`.
2. Query `ml_analysis.ball_detections` for `(frame_idx)` where `is_bounce=TRUE` and `job_id=:tid` and `model='t5'`.
3. Query `ml_analysis.video_analysis_jobs` for `fps` of this task.
4. Call `detect_point_boundaries(serves, ball_events, fps)` → list of `(start_frame, end_frame)` tuples.
5. INSERT into a session-scoped temp table `_point_windows(task_id uuid, point_start_s real, point_end_s real)`.
6. Final CTE in pass 3 SQL joins `_point_windows` and OR-extends `exclude_d` the same way.

**Pros:** Reuses the tested-elsewhere function; preserves idle-bounce-gap nuance. **Cons:** Introduces Python in pass 3 (warm-up filter is pure SQL), bigger blast radius, needs careful task-scoping to not affect SportAI silver.

### Recommendation

**Pattern A first** — pure SQL, conservative, easy revert. Ship on a feature branch `phase-3/between-point-filter`, push to remote (NOT main), let CI bench prove serve detector is unaffected. Then Tomo runs the validation commands (next section) on a Render shell with DB access to confirm the row-count delta is sensible. Only merge after that.

If Pattern A's approximation undercounts (some real long rallies get filtered), upgrade to Pattern B as a follow-up.

### Task scoping (don't break SportAI silver)

The shared `build_silver_v2.py` pass 3 runs for both SportAI and T5 tasks. Warm-up filter (May 7) was implicitly safe because SportAI's first row aligns with first serve. Between-point filter is **NOT implicitly safe** — SportAI tasks might have legitimate rows that the SQL-approximation flags as "outside any point window".

Either:
- Gate the new CTE on a `task_model_t5` predicate (lookup against `bronze.submission_context.sport_type` or check the silver rows' `model` column), OR
- Verify empirically on SportAI task `2c1ad953` that the new CTE is a no-op before considering merge.

Verification SQL:
```sql
WITH pw AS (
  -- the new point_windows CTE here, with WHERE task_id = '2c1ad953-...'
)
SELECT count(*) AS would_exclude
FROM silver.point_detail
WHERE task_id = '2c1ad953-b65b-41b4-9999-975964ff92e1'
  AND NOT EXISTS (
    SELECT 1 FROM pw WHERE ...
  );
-- Expect: 0 (SportAI events already inside point windows)
```

---

## Validation commands (run when back at Render shell / DB)

These are the queries the next session needs to run to verify Phase 3 part 2 lands cleanly. Pre-computed for copy-paste.

### Re-measure Phase 2 IOU on post-Phase-3 silver

```bash
python -m ml_pipeline.diag.audit_points \
  --task 880dff02-58bd-412c-9a29-5c5151004447 \
  --sportai 2c1ad953-b65b-41b4-9999-975964ff92e1
```

Expected: IOU≥0.5 rises above 17.6% (the pre-Phase-3 number). Phase 2 closes if it lands ≥80%.

### Re-measure Phase 4 reconciler with --honor-exclude

```bash
python -m ml_pipeline.diag.audit_points_reconcile \
  --task 880dff02-58bd-412c-9a29-5c5151004447 \
  --sa   2c1ad953-b65b-41b4-9999-975964ff92e1 \
  --honor-exclude
```

Expected today: 0/17 (per `points_reconcile_baseline.json`). After Phase 3 part 2: marginal change — between-point filter doesn't recover missing strokes (that's Phase 5). The number to watch is whether `--honor-exclude` count *holds steady* or *improves* — never regresses.

### Row count delta on 880dff02 (after Phase 3 part 2 ships + silver re-built)

```sql
-- Before reprocess: 49 active rows (per north_star table)
SELECT count(*) FROM silver.point_detail
WHERE task_id = '880dff02-58bd-412c-9a29-5c5151004447'
  AND NOT exclude_d;
-- Expected after Phase 3 part 2: ~34 (drops 15 between-point survivors)
```

### Confirm SportAI silver is unaffected

```sql
SELECT count(*) FROM silver.point_detail
WHERE task_id = '2c1ad953-b65b-41b4-9999-975964ff92e1'
  AND NOT exclude_d;
-- Expected: 85 (unchanged — SportAI events were already all inside point windows)
```

If SportAI count drops, Pattern A's task-scoping guard is missing.

### Silver rebuild

After merging, rebuild silver on the validation tasks via the existing endpoint:

```bash
curl -sS -X POST https://api.nextpointtennis.com/api/client/matches/880dff02-58bd-412c-9a29-5c5151004447/reprocess \
  -H "X-Client-Key: $CLIENT_API_KEY"
```

Or via Render shell:
```bash
python -m ml_pipeline.harness rerun-silver 880dff02-58bd-412c-9a29-5c5151004447
```

---

## Recommended next-session sequence

1. **Bench locally** — confirm 20/24 + 23/24 floor still locked.
2. **Pick Phase 3 part 2 OR Phase 5b** — neither is critical-path; both are reasonable next steps.
   - **Phase 3 part 2** (~1 hr): implement Pattern A above on `phase-3/between-point-filter`. Push to remote. Run validation commands above. Merge if green.
   - **Phase 5b** (~2-4 hr including Docker rebuild + Tomo upload): Hough fallback gain-up in `ml_pipeline/ball_tracker.py`. Bigger payoff (Phase 5 is the bottleneck) but requires the Batch dance from `handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST".
3. **After either lands** — update `docs/north_star.md` Status column + Progress measurement table. Bump status to DONE / PARTIAL / etc. with date.

---

## Things to skip / don't redo

- **Don't re-diagnose Phase 5.** `docs/_investigation/may07_sa_point6_gap.md` has the full evidence chain. Ball coverage is 13%, longest gap is 91.6s. Don't waste a session re-measuring.
- **Don't re-tune `_baseline_zone` slack.** Apr 29 verified -3.5→-5.0 lost 2 PASS.
- **Don't try to relax `idle_threshold_s`.** Phantom bounces are <1s apart; threshold tuning can't fix it. The bounce-validity rule already shipped (Phase 1).
- **Don't widen exclude_d's reach into SportAI silver.** SportAI events are already clean; any new filter must be T5-only.
- **Don't touch `ml_pipeline/training/visual_debug/`.** Tomo's instruction.

---

## Open file state at session end

- Working tree clean on `main` after this session's commits.
- Untracked: `.claude/tmp/` (local scratch), `ml_pipeline/training/visual_debug/` (Tomo asked to skip).
- Three docs committed today by this session: CLAUDE.md `.claude/`-tracking correction, this handover, north_star.md verification snapshot.
- Two unrelated commits also landed on main today from a concurrent session (`6d82ac6` tiebreak coalescing, `aa6c9ff` dashboards polish). Neither touches serve_detector; bench unaffected.
- Phase 1 detector floor (a798eff0 20/24 + 880dff02 23/24) is the active baseline.
