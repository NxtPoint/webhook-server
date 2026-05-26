# ml_pipeline/ground_truth/

SA-**independent** hand-labelled ground truth for measuring T5 accuracy where
SportAI can't be trusted as the yardstick.

## Why
SportAI is accurate on most signals but **weak on ball bounce** (Tomo). So
bounce recall / precision / xy-error cannot be measured against SA. This dir
holds human-labelled truth instead. See `docs/_investigation/bounce_accuracy.md`
§7–§8.

## What's here
- `<video-stem>_bounces.json` — hand-labelled floor/swing bounces produced by
  `ml_pipeline/training/label_bounces_manual.py`. Schema: per label
  `{frame_idx, pixel_x, pixel_y, ts, type, confidence}`, pixels in ORIGINAL
  video space. Court (x,y) is projected at scoring time (faithful player-feet
  homography; see `diag/bounce_xy_accuracy.py`), not stored here.

## Produce a label set
```bash
# headless plumbing check (no GUI)
python -m ml_pipeline.training.label_bounces_manual --selfcheck
# label the a798eff0 bench-reference match (video is local)
python -m ml_pipeline.training.label_bounces_manual \
    --video ml_pipeline/test_videos/a798eff0_sa_video.mp4
```
Controls are documented in the tool's module docstring. Needs a GUI-capable
OpenCV (`opencv-python`, not `-headless`). Far-court / top-of-frame bounces are
resolution-limited (~1 px ≈ metres) — mark them `confidence=low` (key `l`).

## Video availability (important)
T5 **originals are deleted from S3 after trim** — `bronze.submission_context.s3_key`
goes stale (404). What survives per task under `trimmed/<task_id>/`:
- `review.mp4` — heavily cut (rally segments only); frame base does NOT match
  `ml_analysis`. Not usable for labelling against bronze.
- `practice.mp4` — **full-length render, frame-aligned to `ml_analysis`**
  (same frame count @ 25 fps), but **downscaled to 1280×720** while the pipeline
  processed at 1920×1080. So labels clicked on it are in 720 space and need a
  **×1.5 scale** into `ml_analysis` pixel space before homography projection.
  The labeller records its `frame_width/height` (1280×720) so scoring can derive
  the factor from the job's `video_width` (1920) automatically.

**Recommended target: Match 1 `78c32f53`** — it already has a current WASB run
(52 % coverage) AND its `practice.mp4` is frame-aligned. Retrieved to
`ml_pipeline/test_videos/78c32f53_practice.mp4` (gitignored). Label with:
```bash
python -m ml_pipeline.training.label_bounces_manual \
    --video ml_pipeline/test_videos/78c32f53_practice.mp4
```
(a798eff0's local video is intact but its `ml_analysis` run is stale — 13 %
pre-WASB — so it would need a re-run before scoring.)
