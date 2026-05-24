# T5 bronze accuracy investigation — far-player detection + A/B identity

**Status:** REFERENCE / investigation. Read-only diagnosis, 2026-05-24. Feeds a strategic
decision on what to fix before the stroke-driven silver pivot (Option B in
`next_session_pickup.md`) can land.

> **UPDATE 2026-05-25 — Q1-A DONE (commit `ead857a`).** `player_detections_roi` is now merged
> into both the silver `_build_player_buckets` and the stroke detector `_load_pose_rows`
> (ROI wins wholesale for pid=1, same as serve_detector). Verified on Match 1: the ROI table
> holds **958 far ViTPose poses** (all keypoint-carrying); merging lifts far groundstroke
> classification (live bounce-driven row count unchanged at 139, active 60→66, far Backhand
> 14→19) and stroke-detector far attribution (63→85 of 256). R1 and the R2 far-coverage
> numbers are now CONFIRMED (not `[VERIFY]`). **Two follow-on gaps the wiring exposed:**
> far fh/bh discrimination over-calls backhands (mirrored far-player geometry in
> `_infer_swing_type_from_keypoints`), and far stroke velocity is sub-threshold (needs
> size-normalisation in `velocity_signal.py`). These now precede Q2-B in the sequence.

> **UPDATE 2026-05-25 — far fh/bh mirror FIXED (commit `a8479a8`).** `_infer_swing_type_from_keypoints`
> now mirrors the far half (dominant hand on image-right iff right-handed XOR far), since the far
> player faces the camera. M1 far fh 9→11, bh 13→11 (toward SA 18/6); near byte-identical. Validated
> by aggregate stats: across all 958 far ROI poses the corrected logic reads ~73% forehand, matching
> SA's ~75%. **But per-hit far fh/bh stays pose-NOISE limited** — ViTPose left/right flickers on the
> ~32px far body; a windowed majority vote over-corrects to ~all-forehand (the rare real backhands
> never form a per-hit majority, so it would zero SA's 6 far backhands). Conclusion: precise per-hit
> far fh/bh is a **trained-stroke-classifier (Q1-D)** job, not in-silver heuristic tuning (a vote
> threshold would overfit one match). Next bronze gap before the gate: **far stroke velocity
> size-normalisation** (`velocity_signal.py`).

> **UPDATE 2026-05-25 — far velocity size-normalisation DONE (commit `956b65a`).** `velocity_signal.py`
> now scales each player's wrist velocity by (reference_body / player_body), reference = the largest
> player's median torso, so the near player is unchanged (factor 1, 30px threshold valid) and only the
> far player scales up (factor 3.03 on M1). Stroke attribution 208/34 → 165/106; gated stroke-driven
> **far active 27→43, matching SA's 41**. **Far is now fully unblocked across all 3 stages** (ROI
> pose `ead857a`, fh/bh mirror `a8479a8`, velocity norm `956b65a`). The blocker flipped sides: gated
> stroke-driven **near active 108 vs SA 43** — near-player false-positive peaks (detector PRECISION),
> which is the Q1-D / trained-classifier territory (or a precision gate), not far starvation.

**Evidence provenance note.** This session could not run live DB queries (the Bash and
PowerShell tools were denied in the sandbox, and WebFetch cannot attach the `X-Ops-Key`
header that `/ops/diag/sql` requires). Every number below is therefore sourced from
**code reading** + the **verified figures already captured** in `.claude/next_session_pickup.md`
and `.claude/handover_t5.md`. Numbers that still need a live query to confirm are tagged
**[VERIFY]** with the exact SQL to run. The code-path findings (which table feeds which
consumer) are first-hand from the files and are not [VERIFY].

---

## 1. Executive summary — the binding constraints

1. **The one healthy source of far-player pose is wired to only 1 of its 3 consumers.**
   `extract_far_pose` (ViTPose-Base ROI scan) writes high-quality far keypoints to
   `ml_analysis.player_detections_roi` (`source='far_vitpose'`) — ~3,725 usable rows on a
   validated full run. But **only the serve_detector reads that table.** The silver builder
   (`_build_player_buckets`) and the stroke_detector (`_load_pose_rows`) both read **only
   `ml_analysis.player_detections`**, so they never see the ROI pose. This single wiring gap
   is the largest, cheapest lever on both Q1 and Q2 and is Render-side only.

2. **Far-player attribution in `stroke_events` is structurally near-biased and cannot be
   trusted as hitter identity.** `compute_global_max_velocity` picks the player whose wrist
   has the highest **pixel** velocity. The near player is ~400 px tall and the far player
   ~30-40 px, so near-wrist pixel velocity wins almost every frame regardless of who hit.
   This is why the detector resolves 208 near / 34 far (vs SportAI's ~50/50). It is a
   perspective artifact, not a tracking bug — no threshold tweak fixes it.

3. **Player A/B identity is assigned by court SIDE, per-frame, with no track-level identity
   at all.** `_assign_ids` is deliberately stateless: `pid 0 = near half, pid 1 = far half`
   by bbox-center pixel-y vs the frame midline. The silver builder re-applies the same
   side rule. This is robust (cannot swap mid-match) but it is **position labelling, not
   identity** — if the two players change ends (every odd game) the *physical person*
   behind pid=0 flips, and nothing in the pipeline records that.

4. **Far far-pose coverage in the main table is ~10x worse than near** (near ≈ 11,755 vs far
   ≈ 1,105 keypoint-carrying rows on Match 1 per the brief), because full-frame YOLOv8x-pose
   has a ~60-80 px keypoint floor and the far player sits below it. SAHI/detection-only
   passes recover the *bbox* but carry no keypoints. So even with perfect side assignment,
   far strokes/serves are pose-starved in the table that silver+stroke actually read.

5. **The far y-axis calibration offset (Phase 7) is real and separate** — far-side bounce
   court_y reads 2.4-7m too close to the net (median 3.2m error, far-baseline 10-17m). It
   degrades placement/heatmaps and the serve-geometry gate, but it is **not** the cause of
   the count problems in 1-4 above. Fix order matters: clean the row set first, then
   re-measure Phase 7 against cleaner silver.

---

## 2. Q1 — Far-player detection accuracy

### 2.1 Current pipeline map (code-verified)

**Batch GPU (`ml_pipeline/__main__.py::_run_batch`), per frame:**

```
frame ──▶ CourtDetector.detect (radial lens calib, locked after 300 frames)
      ──▶ ball_tracker.detect_frame (TrackNetV2 default; WASB via BALL_TRACKER env)
      ──▶ MOG2 motion mask
      ──▶ PlayerTracker.detect_frame  (every PLAYER_DETECTION_INTERVAL=5 frames)
```

`PlayerTracker.detect_frame` (`player_tracker.py`):
- **Full-frame YOLOv8x-pose** (`_run_yolo`, imgsz=1280, conf=0.10) — the **only** path that
  produces keypoints. Resolves the near player well; resolves the far player only when it is
  large enough (≈60-80 px), which on MATCHI wide-angle it usually is not.
- **SAHI tiled inference** (`_run_sahi`, 640px tiles on the court ROI) — recovers small far
  bodies as **bboxes with `kps=None`**. Skipped per-frame when rule A (pose in both halves)
  or rule B (a candidate projects near the far baseline) fires.
- (legacy 3-pass `_run_yolo_court_crop` + `_run_yolo_far_baseline` only when `SAHI_ENABLED=False`).
- `_choose_two_players` scores candidates by court-metre tier (in-court 3000 / behind-baseline
  2000 / wide-alley 1000 / net-zone-with-pose 500 / off-court 0) + motion + baseline-closeness
  + bbox-area + **pose +300**. Picks the best of each half; requires a 0.35·frame_h y-span.
- `_assign_ids` → `pid 0 = near, pid 1 = far` by midline. **Stateless.**
- `map_to_court` projects **feet** (cx, y2) to court metres via the locked calibration.

Writes → `db_writer.save_player_detections` → `ml_analysis.player_detections`
(`detection_source` ∈ `yolo_pose | yolo_det | sahi`; keypoints only on `yolo_pose`).

**Far-pose supplement — the key path (`ml_pipeline/roi_extractors/pose.py::extract_far_pose`):**
- Computes a far-baseline ROI in court metres `[-1.5 .. W+1.5] x [-8 .. +5]`, projects the 4
  corners to pixels, scans every 2nd frame, runs YOLOv8m-det → ViTPose-Base on the expanded
  crop, keeps rows with usable wrist+shoulder confidence.
- Writes to **`ml_analysis.player_detections_roi`** with `player_id=1`, `source='far_vitpose'`.
- A validated full run (handover, task `4a591553`) produced **7,650 sampled → 7,244 YOLO dets
  → 3,725 usable ViTPose rows** in 500s. This is the *good* far-pose data.

**Far-stroke classifier (`pipeline.py::_classify_far_player_strokes` + `stroke_classifier/`):**
- Optical-flow CNN intended to classify far strokes (fh/bh/serve/volley) where pose is too
  small. **Weights `models/stroke_classifier.pt` do NOT exist** (confirmed — models dir holds
  tracknet/yolo/wasb only). So `classifier.available` is False and this path is **dormant**.
  The silver cascade's `stroke_class` tier therefore never fires → far strokes fall through to
  the position-based fh/bh fallback.

### 2.2 Root causes (with evidence)

| # | Root cause | Evidence | Where |
|---|---|---|---|
| R1 | **Far ViTPose pose never reaches silver or stroke detection.** Both `_build_player_buckets` and stroke_detector `_load_pose_rows` SELECT only `ml_analysis.player_detections`; the 3,725 `far_vitpose` rows live in `player_detections_roi` and are read **only** by `serve_detector._load_pose_rows`. | Code: `build_silver_match_t5.py:439`, `stroke_detector/detector.py:168-171`, `serve_detector/detector.py:81-127` | Render |
| R2 | **Main-table far keypoint coverage is ~10x below near.** near≈11,755 vs far≈1,105 kp-carrying rows (Match 1). Full-frame YOLOv8x-pose has a ~60-80 px keypoint floor; far player is 30-40 px. SAHI returns far bboxes with `kps=None`. | Brief (today's session); code `_run_sahi` sets `kps.append(None)`; `_run_yolo_far_baseline` `crop_kps.append(None)` | Batch |
| R3 | **Far forehands under-detected** (T5 far_fh=6 vs SA far_fh=18 on Match 1) — a direct consequence of R1+R2: no far pose → wrist-vs-shoulder fh/bh inference can't run → falls to position fallback / "other". | Brief | — |
| R4 | **Match 2 far-ROI misalignment (Bug 1):** `extract_far_pose` scanned 65,248 frames and produced **0 detections** (ROI pixel (461,695)-(506,735), 45x40). The ROI was the right *size* but pointed at the wrong patch — calibration on that video placed the projected far-baseline corners off-target. A healthy prior run (`1d6feb3a`) hit 94.7% (7,244/7,650). | `next_session_pickup.md` Match 2 Bug 1 | Batch |
| R5 | **`roi_bounces` per-window slowdown (Bug 2)** killed Match 2 at the 6h timeout (7s→50s/window, ~7x). The "BallTracker: loaded" log every window points at per-window model load inside `_run_roi_window` (a fresh `BallTracker()` is constructed each call at `bounces.py:239`). Long matches never finish → no far data at all. | `next_session_pickup.md` Bug 2; code `bounces.py:239` | Batch |
| R6 | **Far y-axis calibration offset (Phase 7 / Bug 3):** far court_y reads 2.4-7m too near the net (median Euclidean 3.2m; far-baseline 10-17m). Corrupts placement features AND nudges the serve-geometry gate (which already carries a +6m far tolerance and a 1.5m baseline eps to compensate). | `next_session_pickup.md` Bug 3; `apr15_breakthrough` memory; `build_silver_match_t5.py:124-128` | Batch |
| R7 | **Far-stroke classifier weights never trained** → optical-flow stroke tier dormant; far stroke type relies entirely on (sparse) pose or position fallback. | Models dir; `project_far_player_stroke_research` memory | — |

**[VERIFY] queries** (run via Render shell / `/ops/diag/sql` when a session has access):
```sql
-- R2: keypoint coverage by side, main table (Match 1)
SELECT (court_y > 11.885) AS near_side,
       COUNT(*) AS rows,
       COUNT(*) FILTER (WHERE keypoints IS NOT NULL) AS with_kp
FROM ml_analysis.player_detections
WHERE job_id = '78c32f53-5580-4a88-a4e7-7506e59b2b52'
GROUP BY 1;

-- R1: does the ROI far-pose table actually have rows for Match 1, and who reads it?
SELECT source, COUNT(*), MIN(frame_idx), MAX(frame_idx)
FROM ml_analysis.player_detections_roi
WHERE job_id = '78c32f53-5580-4a88-a4e7-7506e59b2b52'
GROUP BY source;

-- detection_source split (how much far coverage is bbox-only / pose-less)
SELECT detection_source, (court_y > 11.885) AS near_side, COUNT(*)
FROM ml_analysis.player_detections
WHERE job_id = '78c32f53-5580-4a88-a4e7-7506e59b2b52'
GROUP BY 1,2 ORDER BY 1,2;
```

### 2.3 Options

| Opt | Fix | Effort | Risk | Batch/Render | Phase 7 dep |
|-----|-----|--------|------|--------------|-------------|
| **Q1-A** | **Merge `player_detections_roi` (far_vitpose) into the silver + stroke pose buckets.** Make `_build_player_buckets` and stroke_detector `_load_pose_rows` UNION the ROI table the same way serve_detector already does (ROI wins wholesale for pid=1). Directly lifts far keypoint coverage from ~1,105 to ~1,105+3,725 and is the prerequisite for far fh/bh inference. | **~1 day** | **Low-med** — changes the row set silver sees; needs a Match 1 before/after on far_fh and a SportAI-side sanity (SportAI tasks have no ROI table rows, so no-op there). | **Render** | None (do first) |
| **Q1-B** | **Fix `roi_bounces` per-window model reload (Bug 2/R5):** construct `BallTracker()` once outside the window loop in `bounces.py`. Unblocks matches >~45 min so they produce any far data at all. | **~half day** | Low — contained, well-diagnosed. | **Batch** (trips BATCH-SIDE CHECKLIST) | None |
| **Q1-C** | **Harden `extract_far_pose` ROI (Bug 1/R4):** validate the projected ROI by sampling N frames for a high-conf person bbox before committing; widen tolerance / fall back to a fixed far-baseline pixel band when projection looks off. Prevents the silent 0-detection Match 2 failure. | **~1 day** | Med — touches calibration-dependent projection; needs a multi-video check. | **Batch** | Partial — same projection that Phase 7 fixes |
| **Q1-D** | **Train the far-stroke optical-flow classifier (R7).** Export dual-submit pairs (SportAI as teacher), train `stroke_classifier.pt`, drop into models/. Gives a far fh/bh signal independent of pose size. | **2-4 days** + data | Med — needs 200+ labelled pairs; inference path already wired. | Batch (weights) + Render | None |
| **Q1-E** | **Phase 7 far y-axis calibration (R6).** Biggest placement win; daylight-only Batch change. | **2-3 days** | High blast radius (Batch, dual-region). | **Batch** | — |

---

## 3. Q2 — Player A/B identity persistence

### 3.1 How identity works today (code-verified)

There is **no track-level identity**. Two independent, both **side-based**, both **stateless**
mechanisms:

1. **Bronze (`player_tracker._assign_ids`, lines 1323-1391):** for each frame's chosen
   candidates, `pid 0` = the candidate whose bbox-center pixel-y is **below** the frame
   midline (near half), `pid 1` = **above** the midline (far half). Biggest bbox wins if two
   land in the same half. The docstring is explicit: this replaced an IoU tracker that had a
   "swap-lock bug" (2026-04-18) — when both players were lost for
   `PLAYER_TRACK_TIMEOUT_FRAMES` and the far player re-appeared alone it got locked into
   pid=0. The fix was to drop tracking entirely and make pid a **pure function of current
   pixel position** — "no state, no possibility of swap." `_prev_players`, IoU, and the
   timeout/drift constants still exist but are no longer consulted for ID.

2. **Silver (`build_silver_match_t5._build_player_buckets` + Pass 1):** groups
   `player_detections` by `player_id`, takes the **top-2 by detection count** (`top_pids`),
   and maps every other ghost id onto those two. But the silver row's `player_id` is then
   re-derived **purely from the hitter's court SIDE** at the moment of the shot:
   `hitter_pid = top_pids[0] if hitter_side_near else top_pids[1]` (bounce-driven line 924;
   stroke-driven line 1189). The comment is explicit: *"Assign player_id based on court side
   — guarantees 2 distinct players even when ML tracker only detected 1 player_id."* And in
   the stroke-driven path: *"The stroke detector's player_id attribution is perspective-biased
   toward the near player ... so it is NOT used to assign the silver player_id — side is taken
   from court position."*

So both the detector's `player_id` (208/34 near-biased) **and** the velocity attribution are
deliberately discarded; silver identity = court side.

### 3.2 Why it is unreliable for "the same person all match"

| # | Problem | Evidence |
|---|---|---|
| I1 | **Side ≠ person.** Players swap ends every odd game. "pid 0 = near" means the *physical person* behind pid 0 flips at each changeover. So "Player A's stats" silently merges Person-X's near games with Person-Y's near games. For a per-player match report this is wrong by construction. | `_assign_ids` docstring; Pass 1 side assignment |
| I2 | **Detector `player_id` is near-biased and not used.** `stroke_events.player_id` resolves 208 near / 34 far (brief) because `compute_global_max_velocity` picks max **pixel** velocity. Even if we wanted track identity from it, it's unusable. | `velocity_signal.py:127-144`; brief |
| I3 | **No appearance/anchor signal exists.** Nothing records shirt colour, body embedding, serve order, or "who started which end," so there is no way to re-stitch identity across a changeover even in principle today. | absence in `player_tracker.py`, `db_writer.py` |
| I4 | **Far track is fragmentary** (R2: ~1,105 far kp rows, sparse coverage, frequent `kept_1_span_fail`/`kept_0` frames per the `_diag` counters), so any future real tracker would face long far-side gaps to bridge. | `_choose_two_players` diag; brief coverage gap |

Note: side-based ID is genuinely *better* for the current silver math than a swap-prone
tracker — Pass 3 serve/point numbering relies on the two silver players mapping to the two
court ends. So this is the right call **for analytics that are inherently per-end** (serve
side, placement). It is the wrong call **only** for "this is the same human all match."

**[VERIFY] queries:**
```sql
-- I1/I2: does a given detector player_id stay on one court side, or swap with games?
SELECT player_id,
       COUNT(*) AS n,
       AVG(court_y) AS mean_y,
       MIN(court_y) AS min_y, MAX(court_y) AS max_y,
       COUNT(*) FILTER (WHERE court_y > 11.885) AS near_rows,
       COUNT(*) FILTER (WHERE court_y <= 11.885) AS far_rows
FROM ml_analysis.player_detections
WHERE job_id = '78c32f53-5580-4a88-a4e7-7506e59b2b52'
  AND court_y IS NOT NULL
GROUP BY player_id ORDER BY n DESC;

-- distinct player_ids over the match + how fragmented
SELECT COUNT(DISTINCT player_id) AS distinct_pids FROM ml_analysis.player_detections
WHERE job_id = '78c32f53-5580-4a88-a4e7-7506e59b2b52';

-- stroke_events attribution split (confirms the 208/34 near bias)
SELECT player_id, COUNT(*) FROM ml_analysis.stroke_events
WHERE task_id::text = '78c32f53-5580-4a88-a4e7-7506e59b2b52' GROUP BY player_id;
```

### 3.3 Options

| Opt | Approach | Effort | Risk | Notes |
|-----|----------|--------|------|-------|
| **Q2-A (status quo)** | Keep side-based ID; **redefine the product contract** as "Near player / Far player" (or "Player at end A / end B"), not "Person 1 / Person 2." | ~0 (doc/UI wording) | Low | Honest about what the data is. Correct for serve-side and placement. Wrong only if the report claims a stable human. |
| **Q2-B** | **End-anchoring + changeover detection.** Detect game changeovers (Pass 3 already numbers games) and **flip the side→person map** every odd game so "Player A" = the person who started at the near end. Pure SQL/Python on existing silver. | **~1-2 days** | Med — depends on game numbering being right; a mis-counted changeover swaps a whole game's stats. | No new ML. Biggest identity win per unit effort. Validate against a known scoreline. |
| **Q2-C** | **Appearance embedding (shirt-colour histogram or lightweight re-ID) per track**, persisted on `player_detections`, used to stitch identity across the changeover and across far-side gaps. | **3-5 days** | Med-high — new model/feature in Batch; far player too small for strong re-ID, so colour histogram is the realistic version. | Robust but heavier; only worth it if B's changeover heuristic proves unreliable. |
| **Q2-D** | **Serve-order anchoring.** Use detected serve events (who serves game 1) + alternating-serve rule to label identity, cross-checked with end-anchoring (B). | **~1 day on top of B** | Med — depends on serve detection being right early in the match. | Cheap consistency check that strengthens B. |

---

## 4. Recommended sequence

The stroke-driven silver pivot (Option B in `next_session_pickup.md`) is **gated on far-player
pose reaching the tables silver actually reads.** Without that, pivoting to stroke-driven row
generation just re-exposes the same far under-detection (far_fh=6). So:

1. **Q1-A first — merge `player_detections_roi` into silver + stroke pose buckets** (Render,
   ~1 day, low risk). This is the prerequisite for the stroke-driven pivot and the single
   highest-leverage, lowest-blast-radius change. Validate with the R1/R2 [VERIFY] queries +
   a Match 1 far_fh before/after.
2. **Then the stroke-driven silver pivot** (Option B) — now fed by far pose, it can actually
   recover far forehands.
3. **Q2-B — end-anchored A/B identity** (Render, ~1-2 days) — turns "near/far" into "Player A/B"
   for the report layer, independent of the ML. Pair with Q2-D serve-order as a check.
4. **Q1-B — `roi_bounces` model-reload fix** (Batch, half day) — unblocks long matches so the
   above pipeline can run on real organic uploads, not just the two reference matches.
5. **Q1-C — ROI-misalignment hardening** (Batch) alongside or just after B (same projection code).
6. **Q1-E — Phase 7 far y-axis calibration** (Batch, daylight, 2-3 days) — biggest placement
   win, done **last** so it measures against the cleanest possible silver.
7. **Q1-D / Q2-C** (train far-stroke classifier / appearance re-ID) — only if 1-6 leave a
   material far fh/bh or identity gap.

Prerequisite map: **Q1-A → stroke-driven pivot**; **Q1-B → reliable far data on long matches**;
**Q2-B independent** (can run anytime); **Phase 7 after silver is clean**.

---

## 5. Open questions for Tomo

1. **Product contract for identity:** is "Near player / Far player (this game)" acceptable for
   the match report, or must it be a stable "Player 1 / Player 2" across the whole match? This
   decides whether Q2-A (free) suffices or Q2-B/C is required.
2. **Single-camera-end assumption:** is MATCHI footage always one fixed camera behind one
   baseline (so near/far is stable in pixels even as players swap ends)? End-anchoring (Q2-B)
   depends on this.
3. **Is the far ViTPose ROI table being populated on recent matches?** The R1 fix assumes
   `player_detections_roi` actually has `far_vitpose` rows for Match 1. If recent ingests
   skipped it (or Bug 1 zeroed it), Q1-A needs Q1-C first. (Run the R1 [VERIFY] query.)
4. **Acceptable risk for the silver row-set change (Q1-A):** there is no silver bench fixture
   for matches yet (`bench_silver` is shipped but empty). Do we want to build that fixture
   before merging ROI pose into silver, or validate manually on Match 1?
5. **Phase 7 ordering:** confirm Phase 7 stays last. The serve-geometry gate currently carries
   compensating tolerances (+6m far hitter band, 1.5m baseline eps) tuned to the *uncalibrated*
   far offset — those will need re-tightening once Phase 7 lands, which is easier on clean silver.
