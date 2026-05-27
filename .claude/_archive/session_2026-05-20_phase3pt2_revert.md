# Session 2026-05-20 — Phase 3 Part 2 attempted + reverted; Phase 5b is next

**Created:** 2026-05-20 PM by Claude session (continuation of the morning session that wrote `session_2026-05-20_review.md`).
**Outcome:** Two pure-SQL implementations of Phase 3 part 2 shipped to main + reverted. Empirical evidence makes the case that Phase 3 part 2 is blocked by Phase 5. `north_star.md` updated. Next session picks up Phase 5b per the existing `phase5_kickoff.md`.

---

## TL;DR

- **Phase 1 detector floor still locked.** Bench green: a798eff0 20/24, 880dff02 23/24, commit `e1ed1ff` → `de06d41` no change. No detector code shipped today.
- **Phase 3 part 2 attempted twice on main, both reverted in commit `de06d41`.** Documented design from `session_2026-05-20_review.md` §"Phase 3 Part 2" was Pattern A. Two SQL variants tried; both flawed.
- **Active T5 silver on `880dff02` is back to 49** (Phase 3 part 1 warm-up filter only). SportAI silver `2c1ad953` is at 85 (unchanged throughout).
- **`docs/north_star.md` Phase 3 entry updated** with empirical findings and "blocked by Phase 5" classification. Don't re-attempt Phase 3 part 2 until ball coverage is materially better.
- **Next direction: Phase 5b (Hough fallback gain-up in `ml_pipeline/ball_tracker.py`).** Already documented in `.claude/phase5_kickoff.md` — that file is the right pickup point for the next session.

---

## What was tried + what failed

### v1 (commit `00b8639`, reverted via `de06d41`)

Pattern A from `session_2026-05-20_review.md`: anchor on every `serve_d=TRUE` row in `with_try_ff`, window = `LEAST(hit+30s, next_serve-2s)`. Wrapped in a `task_apply_between_point` guard CTE so SportAI silver is untouched (checks `model='t5'` in `silver.point_detail` + non-empty serve set).

**Validation on `880dff02` post-deploy:** active T5 row count held at 49 (target ~34). Reconciler unchanged at 0/18 full_match, 20/49 "outside any SA window". **Filter never fired.**

**Root cause:** T5's geometric serve detector emits 107 `serve_d=TRUE` rows on this 18-point match — any overhead-type swing within EPS of a baseline qualifies. 107 dense anchors create overlapping 30s windows that cover the entire match. `NOT EXISTS (point_windows ...)` is FALSE for every row → zero rows excluded.

### v2 (commit `f0b104e`, reverted via `de06d41`)

Fix-forward: tighten anchors to first `serve_d=TRUE` per `point_number` (~18-30 anchors instead of 107) via `DISTINCT ON (task_id, point_number)`. Tighten window cap 30s → 20s.

**Validation on `880dff02` post-deploy:** active T5 row count 49 → **34**. By row count this matched the session review's "~15-row impact" prediction. BUT the reconciler's "T5 strokes outside ANY SA point window" count **held at 20** — every dropped row was INSIDE a real SA window. Per-point evidence:
- pt 5 (SA [178.44–195.96], 3 SA strokes): 8 T5 → 1 (-7 real strokes dropped)
- pt 14 (SA [458.08–468.00], 4 SA strokes): 9 T5 → 1 (-8 real strokes dropped)
- All other points unchanged

**Root cause:** Forward-fill of `point_number` in pass3's `with_point` CTE assigns rows in the gap `[SA_point_start, T5_serve_detection]` to the PREVIOUS `point_number`. Those rows then fall outside that previous point's 20s window cap and get excluded — even though they're real strokes of the CURRENT point. T5 just detected the serve a few seconds late.

### Pattern B analysis (NOT shipped — same structural limit)

`ml_pipeline/point_structure/point_boundaries.py::detect_point_boundaries()` improves the END of windows via bounce-evidence + `idle_gap_s=4.0s` cap (tighter than v2's 20s). BUT `start_frame = serve.frame_idx` is identical to v2 — the start-of-window problem is unchanged. Pattern B would still drop real strokes near point starts whenever T5's serve detection lands late vs SA's true start.

`detect_point_boundaries()` would actually help once Phase 5 produces reliable bounce coverage — the idle_gap rule needs real bounces to discriminate between-point gaps from rally gaps. With Phase 5's current 13% coverage, the idle_gap fires too aggressively (anywhere with no detected bounce → end-of-point at start+4s) and creates short, fragmented windows.

---

## Commits on main this session

```
de06d41 Revert Phase 3 part 2 (v1 + v2 SQL approximations both flawed)
f0b104e build_silver_v2: Phase 3 part 2 v2 — anchor on point_number, not raw serve_d
34b0bdf docs: surface .claude/session_* + phase5_kickoff as live T5 entry points
00b8639 build_silver_v2: between-point filter for T5 silver (Phase 3 part 2)
e1ed1ff docs: 2026-05-20 T5 status review + .claude/ tracking correction  <-- session start
```

Active feature branches on origin (can be deleted):
- `phase-3/between-point-filter` (v1) — superseded by revert
- `phase-3/between-point-filter-v2` (v2) — superseded by revert

`git push origin --delete phase-3/between-point-filter phase-3/between-point-filter-v2` when convenient.

---

## State of main at session end

- `build_silver_v2.py` Phase 3 region matches the pre-session state byte-for-byte (verified via `git diff e1ed1ff -- build_silver_v2.py` returning empty).
- CLAUDE.md cleanup commit (`34b0bdf`) is independent of Phase 3 work and stays — `.claude/session_*` and `phase5_kickoff.md` are now surfaced as live entry points; `ml_pipeline/training/visual_debug/` flagged as untracked debug images.
- North star updated (`docs/north_star.md`) with Phase 3 status flip + per-attempt detail.
- Bench locked at a798eff0 20/24 + 880dff02 23/24 (no change since 2026-05-07).

---

## Next session — read in this order

1. **`docs/north_star.md`** — the macro plan. Phase 3 part 2 is now "blocked by Phase 5" with empirical receipts. Phase 5b is the recommended next phase.
2. **`.claude/phase5_kickoff.md`** — the canonical Phase 5 launching note. Already comprehensive: 5a/5b/5c/5d sub-tasks scoped, reference queries provided, **recommended start: 5b**.
3. **`.claude/handover_t5.md`** — operational doc. The "BATCH-SIDE CHANGE CHECKLIST" section is mandatory for Phase 5b because `ball_tracker.py` is in the Batch container — any edit triggers Docker rebuild + dual-region ECR push + new job-def revisions. The "TEST HARNESS" section gates pushes.
4. **This file** — only for context on why Phase 3 part 2 was parked, in case future sessions wonder.

---

## Phase 5b prep (not run this session — for next session)

Per `phase5_kickoff.md` §"Recommended starting point: 5b":

> Smallest code change, lowest risk, fastest validation. Existing code is the right shape; a parameter tune may yield a measurable coverage delta. Bench protects detector recall as a safety net; ball coverage measurement is direct via `ml_analysis.ball_detections` row count.

Suggested first move (NOT yet executed):
1. **Read `ml_pipeline/ball_tracker.py`** to understand current Hough fallback gates.
2. **Characterise current Hough behaviour** on the 880dff02 fixture before any tuning — explicit caution in the original handover: "Don't blind-tune; characterise the current Hough behaviour on the 880dff02 fixture first."
3. **Identify the gates** that are blocking real ball detections in the six >40s gaps.
4. **Tune in small steps**, with bench + ball-coverage measurement after each.
5. **Batch-side dance**: every push requires Docker rebuild + dual-region ECR push + new job-def revisions. Tomo triggers the user-side Batch rerun for validation.

The ball-coverage query from `phase5_kickoff.md` §"Reference data" is the single-number signal. Today: 13%; target ≥50%; longest gap currently 91.6s, target <5s.

---

## Things to skip / don't redo

- **Don't re-attempt Phase 3 part 2 with any pure-SQL pattern.** Both anchor strategies (every-serve and per-point) have been empirically proven to fail. Pattern B (Python integration) has the same structural limit. The architecture is fundamentally bounce-evidence-limited.
- **Don't extend the warm-up filter to "first real point start" instead of "first detected serve".** This was a side suggestion in `session_2026-05-20_review.md` — same blocker. The warm-up filter as-is catches everything before T5's first serve detection (at 19.68s on 880dff02), which is conservative and safe. The real first SA point starts at 54.48s, so there's a ~35s leak window — but plugging it requires identifying "first REAL serve" which needs bounce evidence.
- **Don't add new ad-hoc diagnostic SQL.** The reconciler (`audit_points_reconcile.py`) and `audit_points.py` are the two tools that catch the "wrong rows dropped" failure mode. Use them.
- **Don't touch `ml_pipeline/training/visual_debug/`** — still Tomo's instruction.
