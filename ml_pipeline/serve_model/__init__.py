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

Gate before wiring into the serve detector: per-serve far recall/precision
on the held-out video ≥ the heuristic baseline (far 4/12), AND the serve
bench stays green (the model only ADDS far candidates; near pose path is
untouched). Wire-in is env-gated SERVE_MODEL_ENABLED (default 0).
"""
