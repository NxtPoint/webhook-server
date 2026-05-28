# ADR-01: Bounce model architecture

**Status:** APPROVED 2026-05-28 (architectural + research-grounded spec). Build can start once an agent claims this ADR via the pickup file.
**Owner:** Tomo decides; any agent can implement post-approval.
**Sequence:** see [ADR-05](./adr_05_detector_build_sequencing.md). Independent of swing-type (ADR-02) and identity (ADR-03).
**Last updated:** 2026-05-28.

## Context

Per [bronze_silver_18_audit.md §"Build backlog reframed"](./bronze_silver_18_audit.md), bounce detection is the **worst-performing of the 18 fields**: recall 55%, precision 27%, 4.57 m error on Match 1 (`78c32f53`). T5 emits 303 bounce events vs SportAI's 161 — roughly half are airborne false-positives, not ground bounces.

The audit calls today's implementation a violation of north_star rule #2 ("one model per fact"):

> "ball bounce | velocity-reversal rule *inside* `ball_tracker` | promote to a real bounce model in the MODEL layer; move the silver proximity-filter into it" — `bronze_silver_18_audit.md:88`

[north_star.md §"Current bottleneck"](../north_star.md) reframes the problem from "calibration" (it isn't — court calibration is faithful to 0.11 m) to **detection precision + ~0.5 s timing jitter**. The far baseline is resolution-limited (~1 px ≈ metres) — a physical cap a bounce model can't lift on its own.

## Sub-questions

1. **Where does the model live** — Batch-side (alongside `ball_tracker`) or Render-side (standalone, like `serve_detector`)?
2. **What inputs does it consume** — ball trajectory only, or +court geometry, +player position, +rally state?
3. **Point-context** — is "in-point" an output of the model, or a downstream filter?
4. **Schema** — keep the `is_bounce` flag on `ml_analysis.ball_detections` or add a new curated `ml_analysis.ball_bounces` table?

## Options

### Q1 — placement
| Option | Pros | Cons |
|---|---|---|
| **A. Batch-side** (extend `ball_tracker.py` to emit a curated bounce stream) | Tightest integration with ball detection; same forward pass; no extra Render CPU | Trips the BATCH-SIDE CHANGE CHECKLIST every iteration; Docker rebuild + dual-region ECR push to ship; rule-#8 friction |
| **B. Render-side standalone** (`ml_pipeline/bounce_detector/`, mirrors `serve_detector`) | No Batch deploy on every iteration; CPU-only inference is feasible (sequence model on a few thousand frames); identical shape to the proven `serve_detector` deploy story | Adds a second consumer of `ball_detections`; slight memory cost on Render (same memory-fix pattern the parallel agent is shipping for serve/stroke will cover it) |

### Q2 — inputs
| Option | Pros | Cons |
|---|---|---|
| **A. Trajectory only** (ball x/y across a window) | Simplest; least dependencies | Can't separate net-clip from floor bounce; misses court-geometry context that rejects above-net trajectories |
| **B. Trajectory + court geometry** | Rejects above-net velocity reversals; uses the existing calibration | Doesn't reject racquet-hits / net-cord bounces — both have geometry-valid signatures |
| **C. Trajectory + court + player position + rally state** | Full context; rejects racquet-hits (ball near player wrist) and out-of-rally noise; aligns with Tomo's intuition that bounces must be "in the context of a point" | Largest input surface; need rally state from `serve_events` (already exists) |

### Q3 — point-context
| Option | Pros | Cons |
|---|---|---|
| **A. Downstream filter** (model emits all bounces; rally-state filter clips to in-point) | Simple; clean separation of perception vs business logic | The model wastes capacity learning out-of-rally noise we'll throw away anyway |
| **B. Model output** (`in_point` flag is part of each emitted event) | Tomo's stated preference; matches the way `serve_events` carries `rally_state`; downstream consumers don't have to recompute it | Couples model to rally-state; need to be careful when rally state is uncertain (between points) |

### Q4 — schema
| Option | Pros | Cons |
|---|---|---|
| **A. Keep `is_bounce` flag on `ball_detections`** | Single source; existing readers continue working | Mixes raw signal with curated output; we already have ~177 nulled `is_bounce` flags — adds confusion |
| **B. New `ml_analysis.ball_bounces` table** | Clean separation: `ball_detections.is_bounce` = raw signal (TrackNet/WASB output); `ball_bounces` = curated, confidence-scored events. Same pattern as `serve_events` (curated) vs `player_detections` (raw) | One more table; silver needs to be updated to read from it (parallel agent's territory — coordination required) |

## Recommendation

**Q1: B — Render-side standalone module `ml_pipeline/bounce_detector/`.**
Mirrors the proven `serve_detector` shape. Keeps `ball_tracker.py` single-purpose. No Batch deploy needed per iteration — the bench discipline applies cleanly (build a `bench_bounce` harness once a corpus fixture exists). Memory-pattern alignment with the parallel agent's streaming-keypoints work.

**Q2: C — Trajectory + court geometry + player position + rally state.**
Inputs come from existing bronze tables (`ball_detections`, `court_keypoints` via `video_analysis_jobs`, `player_detections`, `serve_events`). No new infrastructure. Tomo's "must be in point context" requirement is met by feeding rally state in as input, not by gating after the fact.

**Q3: B — `in_point` is a model output**, not a downstream filter. Same shape as `serve_events.rally_state`. Carries through cleanly; downstream silver / volley analytic (ADR-04) consumes it without recomputing.

**Q4: B — New `ml_analysis.ball_bounces` table.** Curated, confidence-scored, one row per real ground bounce. Columns at least: `id, job_id, ts, frame_idx, player_id (which side bounced), court_x, court_y, confidence, in_point, source (model_v1)`. `ball_detections.is_bounce` stays as the raw signal that feeds the model (and gets deprecated from silver reads once the bounce model is live — silver work owned by the parallel agent).

## Open follow-ups (decide at build time)

1. **Sequence-model architecture** — temporal CNN on ball trajectory window (e.g. ±20 frames), or transformer? Recommend a small temporal CNN matching the `stroke_detector` mental model.
2. **Confidence threshold** — start at 0.5, calibrate via bench against SA `bronze.ball_bounce` (488 labels in corpus today).
3. **Latency budget** — single-match inference should be sub-30s on Render to fit in the ingest flow; same envelope as `serve_detector`.
4. **Net-cord handling** — should the model emit net-cord events as a separate class, or just suppress them? Decide after measuring how often they appear in the corpus.

---

## Build spec v1 (research-grounded, 2026-05-28)

**Algorithm.** **1D temporal CNN over a ±20-frame trajectory window**, output per-frame `P(bounce)`, post-process with NMS + 0.15 s minimum-gap. Architecture: ~3 conv blocks, kernel 5, channels 32→64→64, dropout 0.3, sigmoid head. CPU-runnable in the Render ingest flow. Blueprint: TTNet (CVPR-W 2020, 97% spotting accuracy on table-tennis bounces, <6 ms inference). Precise Event Spotting literature (ECCV 2022 + 2025 survey) confirms lightweight temporal CNN + light shift modules beat heavier transformers when data budget < 1000 labels and CPU latency matters.

**Pre-gates (run BEFORE scoring; cheap & decisive).** These two FP classes dominate today's 84% FP rate; gate them out before the model ever sees them — converts the problem from "find rare events in noise" to "score candidates that survived the gate" (same shape as `serve_detector`).

| Gate | Threshold | Source |
|---|---|---|
| Wrist proximity | min distance to any wrist `< 0.6 m` in court coords → reject (racket-hit) | `player_detections_roi` keypoints |
| Net-line proximity + above-net | `< 1.0 m` from net + above-net z trajectory → reject (net-cord / above-net velocity reversal) | `court_keypoints` |
| Rally state | not `in_rally` AND not `serve_in_flight` → reject (warmup / between-point) | `serve_events.rally_state` |

**Feature list (14 channels × 41-frame window = 574-dim model input).**

| Feature | Source | Rationale |
|---|---|---|
| `court_x, court_y` (normalised) | `ball_detections × court_keypoints` | Position in canonical frame |
| `dx_court, dy_court` (1st diff) | derived | Velocity signal |
| `ddx_court, ddy_court` (2nd diff) | derived | Acceleration — bounces are 2nd-order discontinuities |
| **`gravity_residual` = `y_t − parabolic_fit(y_{t±N, t excluded})`** | derived (fit excludes candidate frame) | **Most discriminative single feature** — ballistic-trajectory model |
| `dist_to_baseline, dist_to_sideline, dist_to_service_line` (signed) | `court_keypoints` | Zone-specific bounce probability; rejects above-net trajectory dips |
| `dist_to_net_line + above_net_flag` | `court_keypoints` | Net-cord rejection |
| `min_dist_to_any_wrist` (court coords) | `player_detections_roi` | Racket-hit rejection (model-level, in addition to pre-gate) |
| `rally_state` (one-hot: pre-serve / in-rally / post-point) | `serve_events` | Suppresses out-of-rally noise as model INPUT (Tomo's in-point requirement) |
| `frames_since_last_serve, frames_since_last_bounce_candidate` | derived | Temporal context |
| `ball_detection_confidence` | `ball_detections` | Down-weight uncertain candidates |

**Threshold defaults (starting values for `bench_bounce`).**

| Knob | Starting value | Rationale |
|---|---|---|
| Window size | ±20 frames (~0.67 s @ 30 fps) | Visible bounce motion ~0.3 s; ±20 covers in/out trajectory |
| Confidence threshold | **0.55** | Above the open-source `yastrebksv/TennisProject` 0.45 floor; targets ≥75% precision |
| Min-gap NMS | 0.15 s (~4 frames) | Tennis bounces never legitimately repeat <0.15 s |
| Spatial TP tolerance | 1.0 m in-bounds / 2.0 m OOB, ±0.2 s | Matches ADR target; PES tolerance norms |

**Training data assessment.** The 488 `ball_position` corpus labels (with `bounce_frame_est + pixel_x/y + court_x/y`) are the right shape — match what TTNet/PES models train on. Three gaps to close before training v1:

1. **Negative mining** — need ~5-10× negative windows (frames near but not at bounces, including racket-hit + net-cord frames). Mine automatically from `ball_detections` excluding ±0.2 s of any label.
2. **`bounce_type` enum** — to learn net-cord/racket-hit rejection at the model level (not just pre-gates), extend the schema with `bounce_type ∈ {floor, net_cord, racket_hit}` and hand-label a few hundred FPs from current high-confidence T5 bounces in implausible positions.
3. **Label-accuracy audit** — verify the 488 labels are themselves < 1 m accurate before training against them, else we train against noise.

**Top 3 references.**
1. Voeikov et al., *TTNet: Real-time temporal and spatial video analysis of table tennis*, CVPRW 2020 — the architectural blueprint (per-frame event spotting on ball trajectory).
2. Hong et al., *Spotting Temporally Precise, Fine-Grained Events in Video*, ECCV 2022 + Action Spotting survey arXiv 2025 — PES task formulation + tolerance norms.
3. `yastrebksv/TennisProject` (`bounce_detector.py`) — open-source CatBoost-on-9-features baseline (threshold 0.45); the floor `bench_bounce` v1 must beat.

---

## Cross-references

- [bronze_silver_18_audit.md](./bronze_silver_18_audit.md) — the "model gap" framing this ADR closes.
- [bounce_accuracy.md](./bounce_accuracy.md) — the measurement that established the 55%/27%/4.57m baseline.
- [north_star.md §"Current bottleneck"](../north_star.md) — why bounce is the top remaining build-phase field.
- [ADR-04](./adr_04_volley_model_or_analytic.md) — depends on this ADR's output table.
- [ADR-05](./adr_05_detector_build_sequencing.md) — places this build in the queue.
