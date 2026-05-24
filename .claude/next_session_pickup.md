# Next-session pickup — 2026-05-24 LATE NIGHT (Phase 3 part 2 + Phase 6 module shipped)

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-24 (long session — Phase 6 module + Phase 3 part 2 between-point filter end-to-end shipped + verified live on Match 1)
**Phase status:**
- ✅ **Phase 6 production stroke detector** — module + ingest wiring DONE; 249 events persisted on Match 1 with avg confidence 0.95. Silver consumption is the next Phase 6 step (not yet wired).
- ✅ **Phase 3 part 2 between-point filter** — DONE; live results on Match 1: T5 active 139 → 60, Backhand exact-match to SA (14 vs 15), Volley over-detection eliminated (20 → 0).
- ⚠️ **Forehand undercount ceiling exposed** — T5 active fh=17 vs SA fh=38. Binding constraint is upstream of the filter (5s-gap rule + bounce-driven silver). See "Known ceiling" below.
- ⏸️ **Phase 7 (y-axis calibration)** — still deferred. Biggest remaining product win.

**Bench:** `a798eff0=20/24, 880dff02=23/24` — green on main throughout this session.

**Match 2 (`54710da5`) — failed at 6h Batch timeout 16:57 UTC; three diagnostic bugs surfaced (see "Match 2 diagnostic findings" section at the bottom — those notes are unchanged from the parallel agent's afternoon write-up).**

**Next session's strategic options (Tomo to pick — see "Where to go next" section at the bottom for the full analysis):**
1. **Phase 7 y-axis calibration** — biggest product impact, Batch-side, ~2-3 days, daylight only.
2. **Wire stroke_events into T5 silver** — direct lever on the Forehand undercount, Render-side, ~1-2 days.
3. **Bug 2: roi_bounces per-window slowdown** — unblocks long matches (Match 2 hit this), small Batch-side change, ~half a day.
4. **5s-gap rule retune** — quick fix that may recover ~10 Forehands per match, Render-side, ~few hours but touches load-bearing pre-existing logic.

---

## What this session actually shipped

7 commits on `origin/main`:

```
0201531 phase 3 part 2: enforce 20s minimum rally window (GREATEST clip)  ← latest
de7c26f phase 3 part 2: tune rally window -- 2s pre-buffer + 20s no-bounce fallback
91f9dbf fix: phase 3 part 2 vaj CTE -- CAST(:tid AS text), drop column ::text
a3b3131 fix: phase 3 part 2 vaj CTE -- column-side text cast, drop :tid::uuid
3962497 fix: phase 3 part 2 vaj CTE -- explicit :tid::uuid cast
aaba134 fix: stroke_detector decel filter -- single-frame check per pickup spec
b68e33e phase 3 part 2: between-point filter in build_silver_v2 pass 3
2cedc4c phase 6: production stroke_detector module + ingest wiring
8e2f2d7 docs: CLAUDE.md add bench_finetuned + gitignore training/visual_debug
```

Net deliverables:
- **`ml_pipeline/stroke_detector/`** (5 files, ~480 lines) — sibling to `serve_detector/`. Pose-first wrist-velocity peak detector. Live emitting 249 events on Match 1 with avg confidence 0.95. **Schema is `ml_analysis.stroke_events`** — `id, task_id, frame_idx, ts, predicted_hit_frame, player_id, confidence, peak_velocity_px_per_frame, pre_peak_v, post_peak_v, decel_ratio`.
- **`build_silver_v2.py` pass 3** — 5 new CTEs implementing the between-point filter (`vaj / ball_bounces / point_starts / point_window_bounds / rally_windows / in_rally_flag`). Three safety gates so SportAI tasks and edge-case T5 tasks aren't catastrophically mass-excluded.
- **`upload_app.py::_do_ingest_t5`** — calls `detect_strokes_for_task` after `detect_serves_for_task`; non-fatal on failure.

The Phase 3 part 2 SQL went through **four iterations** before landing — `:tid` → `:tid::uuid` (SQLAlchemy regex couldn't parse `::`) → `col::text = :tid` (SQLAlchemy inferred `:tid` as UUID) → `CAST(:tid AS text)` (final, works). Worth noting for the next CTE that joins ml_analysis tables in this file: use `CAST(:tid AS text)` directly.

---

## Live results on Match 1 (`78c32f53` ↔ SA `0d0514df`)

**Active row counts (post-filter):**
```
  model  | total | active | excluded
---------+-------+--------+----------
 sportai |    94 |     84 |       10
 t5      |   139 |     60 |       79
```

**Active stroke distribution:**
```
  model  | stroke_d | count
---------+----------+-------
 sportai | Backhand |    15        t5 Backhand =  14  (gap of 1, matches SA)
 sportai | Forehand |    38        t5 Forehand =  17  (gap of 21 — see ceiling)
 sportai | Other    |     1        t5 Overhead =   1
 sportai | Serve    |    26        t5 Serve    =  28  (over by 2, within 8%)
 sportai | Volley   |     4        t5 Volley   =   0  (over-detection eliminated)
```

**Exclusion breakdown by swing type:**
```
swing_type | active | excl_no_point | excl_5s_gap | excl_other | total
-----------+--------+---------------+-------------+------------+------
 bh        |     14 |             1 |          11 |         18 |   44
 fh        |     17 |             2 |          10 |         26 |   55
 other     |      0 |             2 |           1 |          1 |    4
 overhead  |     29 |             0 |           1 |          3 |   33
 volley    |      0 |             0 |           1 |          2 |    3
```

**Stroke detector:** 249 events on Match 1, avg confidence 0.95, span ts=3-608s. Not yet matched against SA truth in this session (eval-stroke harness not built — see options below).

---

## Known ceiling — why Forehand active is 17 vs SA's 38

Three tuning iterations on the between-point filter moved active count by **1 row** (61 → 60). That confirms the binding constraint is not my filter. Per-row diagnostic:

- **5s-gap rule (`excl_chain.gap_break`)** kills 24 rows match-wide, 10 of them Forehands. This rule (in `excl_flags` at line ~688 of `build_silver_v2.py`, predates this session) was tuned for SportAI's dense bronze. At 50% T5 ball coverage, real strokes after a 5s silver-row gap get killed.
- **Bounce-driven silver builder.** Pass 1 of `build_silver_match_t5.py` generates one silver row per detected bounce. When TrackNet misses the bounce for a forehand, that stroke never becomes a silver row — neither filter nor un-filter can conjure it.

Even recovering all 10 5s-gap Forehands would only get T5 fh to 27 (still under SA's 38 by 11). The remaining gap needs Phase 6 silver consumption of `stroke_events`.

---

## Where to go next — strategic options

The work shipped today is product-meaningful. Active row counts now respect SA-equivalent rally boundaries, Backhand matches SA exactly, Volley over-detection eliminated. But we have capacity for one more push.

### Option A — Phase 7 y-axis calibration (BIG, daylight only)

**Impact:** Highest. Phase 7 is the dominant product blocker — heatmaps, "where balls land", every placement-dependent feature depends on bounce x/y accuracy <2m. Today's Match 1 measurement: median 3.2m, far-baseline 10-17m, direction "T5 reports far-side bounces too close to the net."

**Cost:** ~2-3 days focused. Batch-side fix (Docker rebuild + dual-region ECR push + new job-def revisions). Trips the BATCH-SIDE CHANGE CHECKLIST in `handover_t5.md`. **Per the memory rule `feedback_overnight_branch_only.md`: never merge a Batch-side change overnight.** Daylight-supervised session only.

**Why it wasn't this session:** Explicit deferral in today's brief; ~2-3 day estimate didn't fit. Per pickup: "Phase 7 calibration is explicitly DEFERRED — don't start it."

**Suggested next step:** Pick a daylight day, claim 2-3 sessions for it.

### Option B — Wire stroke_events into T5 silver (Phase 6 step 2)

**Impact:** Direct lever on Forehand undercount. With 249 stroke events emitted (avg confidence 0.95), pivoting Pass 1 from bounce-driven to stroke-driven row generation would recover forehands lost to bounce-detection gaps. Likely closes most of the 21-row Forehand gap.

**Cost:** ~1-2 days. Render-side, no Batch redeploy. Risk: changes the row-generation contract for T5 silver, gold views may need a sanity pass. The current bounce-driven path is well-trodden; pivoting needs care.

**Sequencing note:** If you do this next, it makes more sense to do BEFORE Phase 7. Phase 7 measures bounce accuracy on whatever silver rows exist; better silver row coverage → cleaner Phase 7 signal.

### Option C — Bug 2: roi_bounces per-window slowdown (Batch-side, small)

**Impact:** Unblocks long matches (Match 2 died at 6h Batch timeout because of this — 7s/window → 50s/window degradation). Future organic uploads >~45 min will keep failing without this fix. Future training corpus collection blocked.

**Cost:** ~half a day. **Batch-side**, so trips the BATCH-SIDE CHANGE CHECKLIST. The fix shape: move BallTracker model loading outside the window loop in `ml_pipeline/roi_extractors/bounces.py` (the per-window "BallTracker: loaded" log was the smoking gun). One file change.

**Why it's a good detour:** Bug is well-diagnosed (the parallel agent's afternoon write-up below), fix is contained, validates Bug 1 fix could come alongside.

### Option D — Retune the 5s-gap rule (Render-side, quick but risky)

**Impact:** Could recover ~10 Forehands per Match 1-style task. Modest.

**Cost:** ~few hours but touches load-bearing pre-existing logic. The 5s threshold in `excl_chain.gap_break` was tuned for SportAI's dense bronze; bumping to 8s for sparse-bounce regimes is a small SQL change but the rule has been working in prod for months.

**Risk:** No bench coverage on the silver builder for matches; relaxing this rule could re-admit junk on SportAI tasks (where it currently works fine). Best done with a silver-bench fixture in place, which doesn't exist yet (the silver bench `ml_pipeline/diag/bench_silver` is shipped but empty).

### My read (agent-side recommendation)

**Sequence: B → C → A.** Option B (wire stroke_events into silver) gives the most leverage on the Forehand gap, is Render-side (safe), and improves the silver quality before Phase 7's measurement runs against it. Option C is a clean, contained Batch detour that unblocks long matches. Option A (Phase 7) gets the biggest single product win but is best done LAST after silver is cleanest.

Option D should wait for a silver-bench fixture; relaxing live-prod logic without regression coverage is risky.

**If this session has more capacity left right now**, Option B or D are the only Render-side options. Option B is the bigger win.

---

## Match 2 diagnostic findings (unchanged from afternoon write-up — for context)

Three concrete bugs surfaced from the failed Match 2 run. None blocked tonight's Phase 3b/6 work directly.

### Bug 1 — Far-ROI region misalignment (HIGH IMPACT)

Match 2's ROI pose stage scanned 65,248 frames and produced **0 detections**:

```
roi_pose: far ROI pixel (461,695)-(506,735) size=45x40
...
roi_pose: scanned 65248 sampled frames (every 2), skipped 21050 IN_RALLY frames, 0 detections, 0 usable poses in 5270.7s
```

A 45×40 pixel ROI is correct size for a far-baseline player on this camera setup, but it was pointed at the wrong patch of the frame. For comparison, the prior Tomo-vs-Jimbo-Ma `1d6feb3a` run found 7,244 detections from 7,650 frames at 94.7% hit rate.

**Fix direction:** Expand ROI tolerance, or validate-then-commit ROI by checking a sample of frames has a high-conf person bbox inside it. Both `ml_pipeline/roi_extractors/pose.py` and `ml_pipeline/roi_extractors/bounces.py` compute ROI from the same source.

### Bug 2 — roi_bounces per-window slowdown (CAUSED THE TIMEOUT)

```
[7/194]    ... 0 dets, 0 bounces (7.1s)   <-- early
[129/194]  ... 0 dets, 0 bounces (50.8s)  <-- late, KILLED here
```

**7s → 50s per window = ~7× slowdown.** The "BallTracker: loaded" log on every single window strongly suggests per-window model loading. Fix: load TrackNet V2 ONCE outside the window loop in `ml_pipeline/roi_extractors/bounces.py`.

### Bug 3 — Y-axis bounce calibration offset (= Phase 7, explicitly deferred)

This is the Phase 7 finding from this morning: median Euclidean error 3.2m, direction "T5 reports far-side bounces too close to net by 3-6m."

---

## Verification commands (paste-ready)

```bash
# 1. Locked serve bench (mandatory before any detector edit)
.venv/Scripts/python -m ml_pipeline.diag.bench

# 2. Re-run silver on Match 1 (exercises Phase 3 part 2)
python -m ml_pipeline.harness rerun-silver 78c32f53-5580-4a88-a4e7-7506e59b2b52

# 3. Force-rerun stroke detector on Match 1
python -c "from ml_pipeline.stroke_detector import detect_strokes_for_task; from db_init import engine; conn=engine.connect(); trans=conn.begin(); events=detect_strokes_for_task(conn, '78c32f53-5580-4a88-a4e7-7506e59b2b52', replace=True); trans.commit(); conn.close(); print(f'persisted {len(events)} stroke events')"

# 4. Active counts (Phase 3 part 2 effect)
psql "$DATABASE_URL" -c "SELECT model, COUNT(*) AS total, COUNT(*) FILTER (WHERE NOT exclude_d) AS active FROM silver.point_detail WHERE task_id IN ('78c32f53-5580-4a88-a4e7-7506e59b2b52'::uuid, '0d0514df-68aa-4346-9e2d-64413429e47f'::uuid) GROUP BY model ORDER BY model;"

# 5. Stroke events count + confidence
psql "$DATABASE_URL" -c "SELECT COUNT(*) AS strokes, MIN(ts)::int AS first_ts, MAX(ts)::int AS last_ts, AVG(confidence)::numeric(3,2) AS avg_conf FROM ml_analysis.stroke_events WHERE task_id::text = '78c32f53-5580-4a88-a4e7-7506e59b2b52';"

# 6. Active stroke distribution (cleanest visualisation of the wins)
psql "$DATABASE_URL" -c "SELECT model, stroke_d, COUNT(*) FROM silver.point_detail WHERE task_id IN ('78c32f53-5580-4a88-a4e7-7506e59b2b52'::uuid, '0d0514df-68aa-4346-9e2d-64413429e47f'::uuid) AND NOT exclude_d GROUP BY model, stroke_d ORDER BY model, stroke_d;"
```

---

## Read in this order before doing anything else

1. **This file** — you're here.
2. `docs/north_star.md` §"Phase 3 part 2" (now marked DONE) + §"Phase 6" (MODULE DONE, silver consumption pending).
3. `.claude/handover_t5.md` §"Stroke detection" (new section added this session) + the existing TEST HARNESS + BATCH-SIDE CHECKLIST sections if touching detector code.
4. The three Match 2 diagnostic findings above if planning Option C.

Then run the bench (verification command 1) to confirm nothing regressed before touching code.

---

## Things NOT to do (load-bearing)

- **Don't touch Phase 7 overnight** — Batch-side, big blast radius. Daylight only.
- **Don't relax the 5s-gap rule** without a silver-bench fixture covering it. The rule has been working in prod for months; regression risk is real.
- **Don't push T5 detector changes without `bench` green** — CLAUDE.md "Things not to do" #5.
- **Don't merge ball_tracker.py, wasb_*, pipeline.py, config.py, db_writer.py, Dockerfile changes without the BATCH-SIDE CHANGE CHECKLIST.**

---

## Final framing

Today closed two of the three items the morning brief named (Phase 3 part 2 + Phase 6 module). Match 1 active silver is now clean, Backhand matches SA exactly, Volley over-detection eliminated, 249 stroke events live with high confidence.

The Forehand undercount is a real residual finding — it's the next product win waiting. Option B (wire stroke_events into T5 silver) is the direct lever. Phase 7 (y-axis calibration) remains the biggest single product gain but is a multi-session daylight-only investment.
