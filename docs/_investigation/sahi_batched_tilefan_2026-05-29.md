# SAHI batched tile-fan — perf prototype (2026-05-29)

**Branch:** `opt/sahi-batched-tilefan` (isolated worktree; NOT merged/pushed to main)
**Files touched:** `ml_pipeline/player_tracker.py`, `ml_pipeline/config.py` (only)
**Status:** prototype for human review + a later GPU validation run. NOT production.

## Problem

SAHI tiled inference is the pipeline's dominant runtime cost — measured at ~76%
of total Batch wall time on a long match, and the dominant stage even on healthy
matches. `_run_sahi` calls SAHI's `get_sliced_prediction(target, ...,
slice_height=640, slice_width=640, overlap=0.15)`, which runs each 640×640 tile
**sequentially** through YOLOv8m — one GPU round-trip per tile. With a court-ROI
of ~1400×600 px after the 30% margin crop, that's ~3 tiles wide × ~2 tall ≈ 4–6
tiles, each a separate `predict()` call. Sequential GPU launches are
latency-bound: the kernel-launch + sync overhead per tile dwarfs the per-tile
compute for a 640×640 input, so 6 sequential calls ≈ 6× the fixed overhead.

## Design

Env-gated (`SAHI_BATCHED`, default `0`/OFF) alternate path inside `_run_sahi`:

1. **Shared ROI crop.** The court-ROI cropping (30% margin → `roi_x/roi_y/target`)
   is hoisted out of the `try` block so it runs identically for both paths. Only
   the per-tile inference loop changes. When `SAHI_BATCHED` is off, the existing
   `get_sliced_prediction` body runs unchanged — byte-identical to today.
2. **Tile geometry (`_tile_offsets`).** Replicates SAHI's
   `get_slice_bboxes`: step by `tile - int(tile*overlap)` from 0, emit a
   `tile`-wide window at each start, and clamp the FINAL start to `dim - tile`
   so the right/bottom remainder strip is always covered (never dropped). De-dups
   exact-fit coincidences. Same windows SAHI would produce.
3. **One batched forward pass.** Slice all tiles, then a single
   `self._sahi_model.model.predict([tile0, tile1, ...], conf=SAHI_CONFIDENCE,
   imgsz=640, classes=[0], half=<fp16 on cuda>, verbose=False)`.
   `self._sahi_model.model` is the Ultralytics YOLO inside SAHI's
   `AutoDetectionModel`; Ultralytics accepts a list of ndarrays and batches them
   in one GPU launch.
4. **Map-back + merge.** Translate each tile's person boxes by
   `(tile_offset + roi_x/roi_y)` → full-frame coords (same offset the existing
   code applies), then de-duplicate overlapping-tile detections with greedy IoU
   NMS at `SAHI_POSTPROCESS_MATCH_THRESHOLD` (0.5) — the same metric/threshold
   SAHI's `postprocess_type="NMS"` uses. Returns `(boxes, kps=[None,...])` — the
   exact shape `_run_sahi` returns today.

NMS is a small pure-numpy implementation (`_nms_numpy`) so it carries no
torchvision dependency and behaves deterministically regardless of device.

## Equivalence argument vs `get_sliced_prediction`

- **Same tiles.** `_tile_offsets` mirrors SAHI's `get_slice_bboxes` step/clamp
  logic, so the set of (offset, size) windows is the same. Full-region coverage
  including the far-baseline remainder strip is preserved — this is the property
  that lets SAHI catch the ~30–40px far player, and it is unchanged.
- **Same per-tile model + conf + class filter.** Both call the same underlying
  YOLOv8m weights at the same `SAHI_CONFIDENCE=0.15`, filtered to person
  (class 0). A batched forward pass produces the **same per-image detections** as
  N sequential single-image passes — batching changes only how inputs are fed to
  the GPU, not the per-image math. (FP16, if `YOLO_FP16=1`, applies identically;
  it is already used on the existing full-frame/crop passes.)
- **Same merge.** SAHI merges cross-tile duplicates with NMS at IoU 0.5; we do
  the same with the same threshold.
- **Same return shape.** `(boxes_list, kps_list)` with `kps` all-`None`
  (detection-only, no keypoints) — identical to the current path. Downstream
  `_choose_two_players` / dedup logic sees the same structure.

### Intentional differences (documented)

- **Tie-order on NMS.** Our NMS sorts by confidence descending; SAHI's
  `NMSPostprocess` also orders by score. On exact-IoU ties the surviving box may
  differ by sub-pixel tie-break — irrelevant downstream (boxes are scored by
  court geometry, not by which near-duplicate won).
- **Coordinate dtype.** We carry float32 through numpy; SAHI carries its own
  `BoundingBox` floats. Cast back to Python `float` on return — no precision
  issue at pixel scale.
- **Suppressed-error fallback.** On any exception the batched path logs and
  returns `[], []`, exactly as the sequential path does. SAHI is one of THREE
  player passes (full-frame + court-crop + SAHI); an empty SAHI return degrades
  gracefully to the other two rather than crashing the frame.
- **`imgsz`.** Sequential SAHI lets Ultralytics infer per-tile; we pass
  `imgsz=640` (= the slice size) so a sub-640 remainder tile is letterboxed to
  640 — matching the resolution a full tile gets. No downscaling of small tiles.

## Expected speedup

The lever is **tiles-per-frame × (sequential → batched)**. For a typical
court-ROI of ~4–6 tiles per detection frame, the GPU goes from 4–6 serial
`predict()` launches to ONE batched launch. Latency-bound 640² inference on a
G4dn is dominated by fixed per-call overhead, so the SAHI sub-stage should drop
toward `1/N_tiles` of its current cost plus one batched-compute term — realistically
a **~2–4× reduction in the SAHI stage** (more tiles → bigger win). Since SAHI is
~76% of long-match wall time, even a 3× SAHI speedup cuts total wall time by
roughly half on long matches, which is what gets 75–90 min videos under ~1h when
stacked with the already-shipped L1/L4/L5 levers. The exact factor is
GPU/batch-size dependent and must be measured (see validation plan).

## Risks

- **NMS dedup correctness.** If our IoU NMS is more/less aggressive than SAHI's,
  the *count* of merged persons could drift. Mitigated by using the same metric
  (IoU) and same threshold (0.5). Validation must compare person counts per
  frame, not just timing.
- **Far-player coverage must NOT drop.** SAHI exists to catch the ~30–40px far
  player. The remainder-tile clamp in `_tile_offsets` is the load-bearing detail
  — if it were dropped, the far baseline strip would fall outside the tile fan.
  Validation MUST confirm far-player detection rate is unchanged (this is the
  whole point of SAHI; a faster pipeline that loses the far player is a
  regression, not a win).
- **`self._sahi_model.model` API surface.** Assumes SAHI's `AutoDetectionModel`
  exposes the Ultralytics `YOLO` at `.model` and that `.predict(list)` batches.
  True for the pinned SAHI/Ultralytics versions; if a future bump changes it the
  batched path raises → caught → empty SAHI return → graceful degrade to the
  other two passes (no crash). Off by default, so prod is unaffected.
- **GPU memory.** Batching N×640² tiles into one forward pass raises peak VRAM
  vs one-at-a-time. N is small (4–8 tiles) at 640², well within G4dn's 16GB.
  If a very wide ROI produces many tiles, `predict()` will internally sub-batch.

## Bench status

**Could not run `python -m ml_pipeline.diag.bench`** — both the Bash and
PowerShell tools are denied in this sandbox, so no python interpreter is
reachable. I did not fake a result.

**Bench-neutral by construction:** the bench (`ml_pipeline/diag/bench.py`)
replays the serve detector against the committed CI fixture
(`fixtures_ci/a798eff0.pkl.gz`). It exercises `ml_pipeline/serve_detector/` only
and **never imports or runs `player_tracker.py`** (no detection, no GPU, no
weights — sub-second pure-logic replay). This change touches ONLY
`player_tracker.py` and `config.py` (a new `SAHI_BATCHED` constant + a gated code
path that is OFF by default). The default-OFF path leaves `_run_sahi`
byte-identical to today. Therefore the change cannot alter bench output. CI's
trigger globs do not include `player_tracker.py`, consistent with this.

A syntax check was also not runnable in-sandbox; the edits were reviewed
manually (imports wired, dispatch placement, variable scoping, return shape).

## Daylight validation plan

1. Confirm bench green on a box where python is available:
   `.venv/Scripts/python -m ml_pipeline.diag.bench` (expect a798eff0=20/24,
   880dff02=23/24 — should be unchanged since the bench never touches
   player_tracker).
2. Rebuild the Batch Docker image with this branch, dual-region ECR push, new
   job-def revisions in eu-north-1 + us-east-1 (rule #8 — Batch-side change), set
   env `SAHI_BATCHED=1` on the job-def. (Keep a `SAHI_BATCHED=0` baseline revision
   for A/B.)
3. Re-run a **HEALTHY** match (e.g. `78c32f53`) twice: once with `SAHI_BATCHED=0`
   (baseline) and once with `SAHI_BATCHED=1`.
4. **Compare:**
   - `ms_per_frame` and the `sahi` sub-stage seconds from the pipeline stage-timing
     summary (`_sub_seconds["sahi"]`) — expect a large SAHI-stage drop, lower total.
   - **Far-player coverage** — fraction of detection frames with a far-half player
     detected (and bbox sizes ~30–40px) must be unchanged vs baseline. This is the
     gate: a speedup that loses the far player is a regression.
   - Person-count distribution per frame (NMS-merged) ≈ baseline.
   - Resulting `silver.point_detail` row count + far-player stroke counts for the
     match ≈ the `SAHI_BATCHED=0` run.
5. Only if far-player coverage holds AND wall time drops materially → consider
   promoting (flip default, or leave env-gated and set `SAHI_BATCHED=1` in the
   job-def). Otherwise keep OFF and iterate.
