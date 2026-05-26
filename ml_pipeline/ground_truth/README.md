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

## Caveat on a798eff0
Its local `ml_analysis` ball run is **stale** (13 % coverage, pre-WASB). To
score hand-truth against the current pipeline, re-run a798eff0 through the
WASB pipeline first, or label a match that already has a current run.
