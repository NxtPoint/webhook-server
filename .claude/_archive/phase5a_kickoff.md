# Phase 5a Kickoff — Finish ROI Bounce Extractor

**Created:** 2026-05-20 EOD by Claude session, after Phase 5b parking (see `.claude/phase5b_ball_tracker_characterisation.md` Round 0 + commit `d26e8cc`).
**Status:** scoped, not yet started. Next session can pick up cold from this doc.

---

## Why this is now the active 5-track sub-task

Phase 5b (frame-delta Hough fallback gain-up) was empirically parked today. Round 0 baseline diagnostics + a local Tier-4-only sweep on `a798eff0` showed:

- Tier 4 already returns a position on ~99.93% of TrackNet-empty frames — no headroom to "fire more often"
- The staged motion-threshold change (25 → 15) **regresses** post-`_filter_outliers` survival by 11.6% (more candidate motion → Hough picks noisier circles → outlier filter rejects more)
- The dominant filter is `_filter_outliers` (150 px from previous kept), not the Tier-4 threshold
- Source-aware filter surrogate (Option α) didn't show clean leverage either, and CPU-only full BallTracker validation is too slow (~21 hrs, no GPU available locally)

The conclusion is that BallTracker is **saturated** as a coverage source — the obvious upstream tuning levers don't pay off and the downstream filter is hard to tune without real per-frame source labels.

**Phase 5a is additive** rather than competitive: it adds a *second* ball-detection path (TrackNet on tight service-box crops, upsampled) that augments the bronze stream without depending on BallTracker's filter dynamics.

---

## What exists today

### The stub
`ml_pipeline/roi_extractors/bounces.py` is a 48-line placeholder. `extract_far_bounces(video_path, job_id, engine, **kwargs)` returns 0 unconditionally with a log line, and is the only thing exported. The module is **re-exported but never invoked** — there's no caller in `ml_pipeline/__main__.py` or `ml_pipeline/pipeline.py`. The stub docstring proposes three options for a SA-less production version:

  - (a) Scan the entire video with WASB HRNet — expensive (~15000 frames)
  - (b) Use pose-event timestamps from `serve_detector` as anchor points — but `serve_detector` runs POST-Batch on Render, so this would add a 3rd pipeline stage
  - (c) Use bronze `ball_detections` to identify near-service-box candidate bounce moments and scan only those windows

### The working reference (diag tool)
`ml_pipeline/diag/extract_roi_bounces.py` (553 lines) is a CLI-driven diag tool that does the heavy lifting. Key building blocks (all currently behind a `--task <T5_TID> --sportai <SA_TID>` interface that we'd drop in prod):

  - `_calibrate_court(video_path, n_frames=300)` — runs `CourtDetector` on the first 300 frames, returns the detector with a locked detection + calibration
  - `_service_box_pixel_roi(detector, frame_shape, pad_px=40)` — projects the metres-space service-box rectangle through homography to pixels; returns `(x0, y0, x1, y1)`
  - `_run_roi_window(video_path, start_frame, end_frame, roi)` — spawns a fresh `BallTracker`, crops each frame to `roi`, calls `tracker.detect_frame(crop, idx)`, then `interpolate_gaps()` + `detect_bounces()`. Returns `BallDetection` list in crop-pixel coords
  - `_project_to_court(dets, roi, detector)` — maps crop-pixel back to full-frame + court-metres via `detector.to_court_coords`
  - `_in_service_box_zone(cx, cy)` — gate that filters detections to the actual service-box rectangle (after homography)
  - `_init_roi_schema(conn)` — DDL for `ml_analysis.ball_detections_roi` (idempotent)
  - `_insert_rows(conn, task_id, source, rows)` — bulk insert

The output table `ml_analysis.ball_detections_roi` is **already wired into serve_detector** at `ml_pipeline/serve_detector/detector.py:252-297` — it does a guarded existence check and merges ROI rows into `ball_rows` when present. So the *consumer* side is done.

### Architectural precedent
`extract_far_pose` (`ml_pipeline/roi_extractors/pose.py`) is the right shape for Phase 5a. It's called from `ml_pipeline/__main__.py:202` as step "2b" — *after* `pipeline.process(tmp_path)` finishes, with `bounces=result.ball_detections` as one of its anchors. So the precedent for "use bronze pipeline outputs as the anchor, not SA truth" is already established.

---

## Recommended design

**Use option (c) from the stub docstring — bronze ball_detections as anchor source.** This avoids both the cost of (a) and the architectural complexity of (b).

### Anchor selection logic

1. After `pipeline.process(tmp_path)` finishes, the in-memory `result.ball_detections` is a list of `BallDetection` objects (frame_idx, x, y, court_x, court_y) — the prod survivors at full-frame resolution.
2. Filter to detections inside the service-box zone via `_in_service_box_zone(court_x, court_y)`.
3. Cluster the survivors temporally: group consecutive frames into a single "candidate moment" if they fall within ~0.5 s of each other.
4. For each cluster centroid, run a ±2.5 s ROI window (matching the diag tool's `--window-s=2.5`).

Why this works: the bronze detector already produces *some* signal near real serves (just at low spatial resolution because the ball is 1-2 px). Using those low-res anchors to *target* a high-res ROI pass amplifies the signal without scanning the whole video.

### What to port from the diag tool

Lift, in this order:
1. `_init_roi_schema` — DDL (idempotent, runs on every job; cheap)
2. `_calibrate_court` — already runs in the full pipeline; reuse `pipeline.court_detector` instead of recalibrating
3. `_service_box_pixel_roi` — direct port
4. `_run_roi_window` — direct port
5. `_project_to_court` — direct port (needs a `court_detector` reference)
6. `_in_service_box_zone` — direct port
7. `_insert_rows` — direct port, change `source` value to `'roi_far'` (or new `'roi_prod'`)

### What to drop

- SA-truth lookups (`_get_sa_serve_times`, `--sportai` arg)
- S3 video resolution (`_get_video_s3`) — `__main__.py` already downloads `tmp_path`
- The CLI scaffolding (`argparse`, `main()`) — replaced by a Python entry point called from `__main__.py`

### Production wiring (the call site)

Add to `ml_pipeline/__main__.py` after the existing step 2b (pose extraction, around line 211):

```python
if not practice:
    try:
        on_progress("roi_bounces", 80)
        from ml_pipeline.roi_extractors import extract_far_bounces
        n_bounces = extract_far_bounces(
            video_path=tmp_path,
            job_id=job_id,
            engine=engine,
            court_detector=getattr(pipeline, "court_detector", None),
            bounces=getattr(result, "ball_detections", None),
            fps=getattr(result, "video_fps", 25.0) or 25.0,
            window_s=2.5,
        )
        logger.info(f"ROI bounces: wrote {n_bounces} rows")
    except Exception as e:
        logger.warning(f"ROI bounce extraction failed (non-fatal): {e}")
```

Non-fatal failure pattern matches `extract_far_pose`'s — the rest of the job should still complete.

---

## Validation plan

### Stage 1 — local sanity (no Batch)
- Run the new `extract_far_bounces` against `a798eff0` locally using `ml_pipeline/test_videos/a798eff0_sa_video.mp4`.
- Use the bench fixture's `ball_rows` as the anchor source (mocking what the prod pipeline would produce).
- Manually check: does it produce additional bounces inside the service-box zone? Are they concentrated in rally windows (the 24 SA serves on a798eff0)?
- **No Batch round needed** for stage 1.

### Stage 2 — Batch validation on 880dff02
- BATCH-SIDE CHANGE CHECKLIST applies: any edit to `ml_pipeline/roi_extractors/` triggers Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1. See `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST".
- After rerun, query `ml_analysis.ball_detections_roi` for `880dff02`:
  - Total ROI bounce count
  - Distribution across SA point windows — particularly the 0-detection windows from `docs/_investigation/may07_sa_point6_gap.md` (point 6 frames 5599-6003, point N in 91.6 s gap 7539-9829)
- Re-run `reconcile_serves_strict --task 880dff02 --honor-exclude` to confirm bench stays green
- Re-run `audit_points_reconcile --task 880dff02` to measure the per-point match rate (baseline: 0/17 from Phase 4 baseline file)

### Stage 3 — success criteria

Per `docs/north_star.md` Phase 5 done-when:
- T5 ball-detection frame coverage ≥ 50 % (currently 13 % on 880dff02)
- Longest no-ball gap < 5 s (currently 91.6 s)
- SA point 6 has ≥ 3 T5 ball detections in window (currently 0)
- Phase 4 reconciler per-point match rate ≥ 30 % (currently 0/17)

Phase 5a alone is unlikely to hit all four — these are Phase-5-wide targets. The 5a contribution will be evaluated by the **delta** in each metric vs the pre-5a baseline (today's numbers).

---

## Open questions for the next session

1. **CPU vs GPU during ROI pass.** The diag tool runs BallTracker on cropped frames, which on Batch GPU should be ~5x faster than full-frame. But on production-frame budgets, even GPU might be tight — 24 windows × 125 frames × ~10 ms/frame on GPU = 30 s extra. Probably fine. Worth a measurement.

2. **`window_s` size.** Diag tool uses ±2.5 s; should production widen or narrow? Probably leave at 2.5 s pending data.

3. **Anchor clustering threshold.** "Group anchors within 0.5 s" is a guess — could be 1.0 s, could be per-anchor with no grouping. Open for first-pass measurement.

4. **Source tag.** Diag tool writes `source='roi_far'`. Production should pick a distinct tag for traceability — proposal: `source='roi_prod'`. The serve_detector merge logic already does a wildcard "where job_id=:tid" so the source distinction is bookkeeping only.

5. **Practice-mode behaviour.** Phase 5a is scoped for match mode (`not practice`). Practice videos don't have serve concepts; whether to run ROI bounces at all in practice is open.

---

## Things NOT to do

- **Don't re-attempt Phase 5b motion-threshold tuning.** Round 0 receipts are conclusive. Branch `phase-5b/motion-threshold-reduce` retained on origin as falsified-hypothesis record.
- **Don't widen the service-box zone to cover the whole court.** The whole point of ROI is the upsample-into-the-service-box trick; widening defeats the resolution gain.
- **Don't ship a Batch round before the BATCH-SIDE CHANGE CHECKLIST** runs. Edits to `roi_extractors/` are in-container.
- **Don't ship without bench green.** Run `python -m ml_pipeline.diag.bench` before committing any code under `ml_pipeline/`.
- **Don't skip the non-fatal try/except** around the call in `__main__.py`. Phase 5a is additive — if it fails, the rest of the job (silver build, video trim, notify) must still complete.
- **Don't tune `bouncer-validity.py` HALF_Y constants** as part of this work — those are Phase 1 territory and bench is locked against them.

---

## Read in this order for next session

1. `docs/north_star.md` — Phase 5 section (5a marked ACTIVE 2026-05-20; 5b PARKED with receipts)
2. This doc (`.claude/phase5a_kickoff.md`)
3. `.claude/phase5b_ball_tracker_characterisation.md` — receipts for why 5b is parked; the Tuning rounds table records the falsification
4. `ml_pipeline/roi_extractors/bounces.py` — the stub
5. `ml_pipeline/diag/extract_roi_bounces.py` — the working reference
6. `ml_pipeline/__main__.py` lines 180-215 — where step 2b lives, to confirm the wiring shape
7. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST + TEST HARNESS sections
