"""Serve model v1 — trained far-serve candidate scorer.

Roadmap step 2 (serve) training phase. The heuristic serve detector is at
its dev ceiling: near 13/14 (pose-first), far 4/12 — far faults that land
outside the service box and receiver-FP cases are structurally invisible
to the bounce-first far path (bench-proven twice; see
`.claude/serve_model_v1_kickoff.md` and memory
`project_t5_may27_serve_dev_ceiling`).

Recipe (port of the bounce_detector ADR-01 pattern):
  1. candidates.py — HIGH-RECALL anchors (98.5% of 200 corpus far labels):
     near-half bounces (no service-box gate) ∪ far-pose cluster peaks.
  2. features.py  — per-anchor window features from ml_analysis arrays
     (pure functions, no DB).
  3. dataset.py   — corpus serve labels (S3 JSON) + DB arrays → (X, y).
     Split by VIDEO, not task (the reference video has two corpus tasks).
  4. model.py     — small torch MLP scorer (CPU-trainable).
  5. train.py     — python -m ml_pipeline.serve_model.train

WIRED IN AND LIVE (default-on since 2026-06-06): the Batch `serve_candidates`
stage scores far-serve anchors (SERVE_MODEL_STAGE=1) and the serve detector
merges `model_far` additively (SERVE_MODEL_ENABLED default 1). It only ADDS
far candidates; the near pose path is untouched and the serve bench stays
green. Gate that was met before enabling: per-serve far recall/precision on
the held-out video ≥ the heuristic baseline — validated far 3/12→7/12 (rev 73).
Rollback: either env=0 (no rebuild).
"""
