# Next-session pickup — 2026-05-25 (Bronze-first pivot; stroke-driven silver gated OFF)

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-25
**Phase active:** Phase 6 step 2 attempted → **reframed to BRONZE-FIRST.** Silver row-generation is frozen until the 18 base fields reconcile to SportAI in `ml_analysis.*`.
**Bench:** `a798eff0=20/24, 880dff02=23/24` — green on main.
**What shipped:** Stroke-driven T5 silver Pass 1 (`build_silver_match_t5.py`), **committed but env-gated OFF** behind `T5_STROKE_DRIVEN_SILVER` (commit `f09d5df`). Bounce-driven stays the live path (139/60 on Match 1, unchanged).
**What's blocked:** The stroke-driven pivot overshoots (Match 1: 141 vs SA's 84 active; near 114 / far 27 vs SA's 43/41) because T5 **bronze** is inaccurate — near-biased hitter attribution + sparse far pose. Reconciliation is a bronze problem, not a silver one.
**Next session's job:** **Q1-A — wire `ml_analysis.player_detections_roi` into the silver + stroke pose buckets** (Render-side, ~1 day, low risk). Verified today: that table holds 958 far poses (all with keypoints) on Match 1 that the silver builder and stroke detector never read. It nearly doubles far coverage and is the prerequisite to ever flipping the gate on.

If the above is enough, stop and go. Read on for the why and the full option set.

---

## The decision that reframes everything (Tomo, 2026-05-25)

**T5 reconciliation to SportAI is a BRONZE (`ml_analysis.*`) accuracy problem. Silver is NOT to be touched until the 18 base fields align with SportAI in the bronze layer.** Now load-bearing in:
- `CLAUDE.md` "Things not to do" **#11**
- `docs/north_star.md` §"★ BRONZE-FIRST PRINCIPLE" (supersedes the old B→C→A order)
- memory `feedback_bronze_first_t5_reconciliation.md`

Layering reminder: the T5 "bronze" is `ml_analysis.*`. `build_silver_match_t5.py` **Pass 1 is the bronze→base-fact projection** (the 18 columns that must match SportAI). Passes 3-5 are the silver analytics (serve location 1-8, zones, aggression) — garbage-in/garbage-out on top of Pass 1.

## What was built today (and why it's gated OFF)

`build_silver_match_t5.py` was refactored + extended:
- Extracted `_build_player_buckets`, `_lookup_dominant_hand`, `_insert_pass1_rows` shared helpers.
- `_t5_pass1_load` → `_t5_pass1_load_bounce_driven` (behaviour-preserving; verified 139/60).
- New `_t5_pass1_load_stroke_driven`: one `ml_analysis.stroke_events` row → one silver row, bounce coords joined within ~1s after `predicted_hit_frame`. Hitter side from bounce-opposite-side (reliable) with attributed-pid fallback.
- Dispatcher `_t5_pass1_load` picks stroke-driven **only when `T5_STROKE_DRIVEN_SILVER` is set** AND stroke events exist; else bounce-driven (the live default).

**Measured on Match 1 (T5 `78c32f53-5580-4a88-a4e7-7506e59b2b52` ↔ SA `0d0514df-68aa-4346-9e2d-64413429e47f`):**

| | t5 stroke-driven | SA truth |
|---|---|---|
| active total | **141** | 84 |
| near / far | **114 / 27** | 43 / 41 |
| near fh / far fh | **38 / 6** | 20 / 18 |

The Forehand "recovery" (17→44 total) is **near-side false positives**, not real far recovery — far fh got *worse* (6 vs 18). Don't be fooled by the headline fh count.

## Root cause (why it's bronze, with evidence)

1. **Hitter attribution is near-biased.** `stroke_events.player_id` = whichever wrist has the global-max *pixel* velocity (`compute_global_max_velocity`); the near player is ~10× larger in frame, so it resolves 208 near / 34 far vs the true ~50/50. Unusable as hitter identity.
2. **Far pose is sparse in the table silver reads.** `player_detections` far-with-keypoints = 1,105 vs near 16,245 on M1. **But** — verified today — `player_detections_roi` has **958 far poses, all with keypoints**, that silver + stroke detector never read (only the serve_detector reads the ROI table). ← cheapest lever.
3. **No player A/B identity.** `_assign_ids` is stateless: pid 0 = near half, pid 1 = far half by pixel midline. Silver re-derives player_id by court SIDE. That's *side labelling, not identity* — when players change ends every odd game, the physical person behind pid=0 flips and nothing records it. Tomo's instinct ("not convinced we can tag A/B and hold it across a match") is correct.
4. **Bounce x/y inaccurate** (Phase 7): median 3.2m, far-baseline 10-17m off.

## Far-player research — read before acting

A read-only agent investigated far-player accuracy + A/B identity → **`docs/_investigation/far_player_accuracy.md`**. Headline finding (code-verified, DB-confirmed by this session): the good far pose from `extract_far_pose` (ViTPose-Base) lands in `ml_analysis.player_detections_roi` but the silver builder (`_build_player_buckets`) and stroke detector (`_load_pose_rows`) only SELECT `ml_analysis.player_detections`. Some numbers in that doc are tagged `[VERIFY]` (the agent couldn't run DB queries) — the M1 ROI count (958 far-kp) and the stranded-table claim are now CONFIRMED.

## Priority order (reframed — fix bronze first)

1. **Q1-A — merge `player_detections_roi` into the silver + stroke pose buckets** (Render-side, ~1 day, low risk). The cheapest, lowest-blast-radius bronze fix; gating prerequisite for the stroke-driven pivot. Start in `build_silver_match_t5.py::_build_player_buckets` and `ml_pipeline/stroke_detector/detector.py::_load_pose_rows`.
2. **Q2-B — end-anchored player A/B identity** (Render-side). Anchor identity to court end + serve order so A/B persists across end-changes.
3. **Bug 2 — `roi_bounces` per-window slowdown** (Batch-side, contained; load `BallTracker` once outside the window loop). Unblocks long matches. Trips BATCH-SIDE CHANGE CHECKLIST.
4. **Phase 7 — y-axis bounce calibration** (Batch-side, daylight only). Do LAST — measures against clean silver.

**Only flip `T5_STROKE_DRIVEN_SILVER` on after #1-#2 land and far/near reconciles to ~50/50.**

## Verification commands (paste-ready)

```bash
# Serve bench (mandatory before any detector edit)
.venv/Scripts/python -m ml_pipeline.diag.bench

# Confirm the stranded far pose (the next move's premise)
.venv/Scripts/python -c "from db_init import engine; from sqlalchemy import text; c=engine.connect(); \
print([dict(r._mapping) for r in c.execute(text(\"SELECT 'main' t, COUNT(*) FILTER (WHERE keypoints IS NOT NULL AND court_y<11.885) far_kp FROM ml_analysis.player_detections WHERE job_id::text='78c32f53-5580-4a88-a4e7-7506e59b2b52' UNION ALL SELECT 'roi', COUNT(*) FILTER (WHERE keypoints IS NOT NULL AND court_y<11.885) FROM ml_analysis.player_detections_roi WHERE job_id::text='78c32f53-5580-4a88-a4e7-7506e59b2b52'\"))])"

# Try the gated stroke-driven path locally (env ON) — for experimentation only
T5_STROKE_DRIVEN_SILVER=1 .venv/Scripts/python -c "from ml_pipeline.build_silver_match_t5 import build_silver_match_t5; print(build_silver_match_t5('78c32f53-5580-4a88-a4e7-7506e59b2b52', replace=True))"
# Restore live state (env OFF / default rebuilds bounce-driven 139/60)
.venv/Scripts/python -c "from ml_pipeline.build_silver_match_t5 import build_silver_match_t5; print(build_silver_match_t5('78c32f53-5580-4a88-a4e7-7506e59b2b52', replace=True))"
```

## Read in this order before doing anything else

1. This file.
2. `docs/_investigation/far_player_accuracy.md` (the far-player + A/B identity diagnosis).
3. `docs/north_star.md` §"★ BRONZE-FIRST PRINCIPLE".
4. `CLAUDE.md` "Things not to do" #11.
5. `.claude/handover_t5.md` §"Stroke detection" + §"BATCH-SIDE CHANGE CHECKLIST" if touching detector code.

Then run the bench to confirm the floor before touching code.

## Things NOT to do (load-bearing)

- **Don't flip `T5_STROKE_DRIVEN_SILVER` on** until far pose is wired in (#1) and near/far reconciles. It currently inflates the near side ~2.6×.
- **Don't chase SportAI reconciliation by reorganising silver.** It's a bronze problem (CLAUDE.md #11).
- **Don't touch Phase 7 / Bug 2 overnight** — Batch-side, big blast radius. Daylight only.
- **Don't push T5 detector changes without `bench` green** (CLAUDE.md #5).
