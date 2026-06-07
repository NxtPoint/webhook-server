"""Hit model v1 — trained ball-hit (stroke event) detector.

Stroke-arc step B2 (the serve_model recipe, replayed). The heuristic
stroke_detector is at its ceiling: event-level alignment vs SA ball_hit_s
is near 13/51, far 19/51 @1.0s with 3-4x over-emission, stable across
rev 67/68/72 — and B1 proved it contributes ZERO candidate anchors beyond
ball-trajectory discontinuities (strictly dominated).

B1 findings that fix the design (.claude/tmp/stroke_b1_p{1..4}.py, 2026-06-07):
  - Ball velocity-vector discontinuity (angle > ~45deg) recalls 94-96% of
    SA swings standalone, BALANCED near/far (47/47 on the reference video).
  - Bounce-discontinuity and hit-discontinuity are 0.3-0.7s NEIGHBOURS:
    cluster-merging conflates them (gap=0.3s collapsed recall to 73/102).
    Therefore: PER-CANDIDATE classification (hit / not-hit), dedup only
    ~0.1s, exactly the bounce-CNN candidate->scorer pattern.
  - ~900-1500 candidates per match vs ~100 true hits (~10:1).

Recipe:
  1. candidates.py — per-detection trajectory discontinuities (no clustering)
  2. features.py  — discontinuity geometry + ball image trajectory + CNN-
     bounce proximity (a candidate NEAR a CNN bounce is the bounce, not the
     hit) + player proximity + rally context. Image-space first (warp-
     resistant: train tasks are warp-era, eval is clean).
  3. dataset.py   — labels = SA player_swing.ball_hit_s via
     training_corpus.sa_task_id (2,592 labels / 8 pairs, straight from
     bronze — no S3 label JSONs). Split by VIDEO; reference video held out
     (a35b37f6 + 17e2da3a warp-era + the CLEAN rev-77 rerun 86ade942).
  4. model.py     — small torch MLP (CPU-trainable).
  5. train.py     — python -m ml_pipeline.hit_model.train

WHO (player attribution) is a deterministic RULE, not a model output: the
incoming ball direction at the discontinuity names the hitter (ball moving
down-image toward the near player -> the near player hit it back). Verified
against SA pid labels in dataset.py (trust-the-rule pattern).

WHAT (fh/bh swing type) stays the swing classifier's fact — out of scope.

Gate before wiring (B3): event-level pid-strict alignment on the held-out
CLEAN video >= heuristic (near 13/51, far 19/51 @1.0s) at >= precision,
AND the serve bench stays green. Wire-in design: Batch stage writing the
model-layer fact (the ball_bounces / serve_candidates pattern), silver
flips to hit-driven verbatim rows (feedback_silver_must_be_hit_driven).
"""
