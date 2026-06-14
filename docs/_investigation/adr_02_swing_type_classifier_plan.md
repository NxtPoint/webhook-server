# ADR-02: Swing-type classifier training plan

**Status:** APPROVED 2026-05-28. **REVISED 2026-06-14 (Tomo) — class set finalised `{fh, bh, overhead, other}` + volley split out as its own fact. See the REVISION block immediately below; the original 3-class plan + options analysis is preserved underneath as history.**
**Owner:** Tomo decides; any agent can implement post-approval.
**Sequence:** see [ADR-05](./adr_05_detector_build_sequencing.md). Independent of bounce (ADR-01) and identity (ADR-03).
**Last updated:** 2026-06-14.

---

## ★ REVISION 2026-06-14 — class set finalised + volley split (supersedes Q3)

This is the decision of record. The original Q3 ("3 classes; volley derived") and Recommendation below are kept as history, but the class set and volley handling are now **revised on live-data evidence**.

### What changed and why

The original ADR picked **3 classes `{forehand, backhand, overhead}`** and deferred volley to a derivation (ADR-04). Two things forced a revision, both measured on the 8 corpus SA tasks (`bronze.player_swing`, 2,592 labelled swings):

1. **The classifier needs a 4th `other` class — and SportAI already provides it.** The 3-class model is forced to assign fh/bh/overhead to *every* hitter-candidate, including the hit detector's false positives (it over-emits ~3.2× on the far side). With no "not a groundstroke" option, every junk detection gets a confident wrong label → distribution pollution. This is why the trained v2 model **lost the per-hit gate to the silver heuristic (32% vs 38%, 2026-06-05)**. SA's raw `swing_type` vocabulary **already contains `other` (291 of 2,592 labels)** — the extractor was *discarding* it. So `other` is a real, teacher-labelled class, not mined negatives.

   | SA raw `swing_type` | count | → canonical |
   |---|---|---|
   | `fh` | 864 | forehand |
   | `fh_overhead` | 819 | overhead |
   | `1h_bh` + `2h_bh` | 618 | backhand |
   | **`other`** | **291** | **other** ← was dropped; now the 4th class |

2. **Volley is NOT a swing type — it is a separate boolean fact, and SA labels it as one.** `bronze.player_swing` has a dedicated `volley BOOLEAN` column (+ `confidence_volley`), exactly like `serve`. Live counts: **96 volley=TRUE / 2,496 FALSE**, and volley **cross-cuts** swing_type (fh 30, fh_overhead 34, 1h_bh 19, other 13, 2h_bh 0 are volleys). A volley is mechanically a forehand/backhand/overhead *struck near the net before the bounce* — never its own swing shape. There are **zero `volley` values in the `swing_type` column**, so it cannot be a teacher-trained swing class regardless. This reconciles the original ADR-02 (volley isn't a swing type ✅) with Tomo's correction (volley is a real SA fact, not a heuristic ✅).

### Revised decisions

- **Q3 → `{forehand, backhand, overhead, other}` (4 classes).** `other` = SA `other` (non-groundstroke / junk-hit). This lets the model reject false-positive hits instead of mislabelling them.
- **Volley = a separate fact (call it ADR-02b / folds into ADR-04).** Its ground truth is `bronze.player_swing.volley` (96 positives). For T5 it is **derived** (fh/bh/overhead struck before the ball's bounce crosses the net) and **validated against SA's 96 labels** with its own bench — replacing today's net-distance-only heuristic (`VOLLEY_NET_DISTANCE_M`). Built as a **separate follow-up**, not inside the swing_type classifier.
- **Silver consumes the classifier verbatim.** Delete the silver swing_type heuristics (`_infer_swing_type_from_keypoints` / `_infer_swing_type_from_position`) — the classifier owns `{fh, bh, overhead, other}` to ceiling. Safe to delete now because T5 silver is **not prod-consumed** (same basis as the 2026-06-14 hit-driven flip); accuracy fills in at train-last.
- **Build-validation train runs now (Tomo, 2026-06-14)** to produce valid 4-class weights + lock the swing bench. The train-to-ceiling (GPU, full epochs, sharp-far corpus) happens later in the batched train-all-5 push. Build-first / train-last.

### Build checklist (this session)

1. `label_swing_types.py` — add `other`→`other` to `SA_TO_CANONICAL` + `DEFAULT_INCLUDE_TYPES` + validation set; stop discarding it.
2. `model_v2.py` + `inference_v2.py` — `CLASSES`/`_VOCAB_MAP` gain `other`; `NUM_CLASSES` auto-derives to 4.
3. `build_swing_type_dataset.py` — `other` in the class tallies; regenerate labels from bronze (the S3 label JSONs are stale 3-class).
4. Rebuild the 4-class dataset → build-validation train → lock `bench_baseline_swing_type.json`.
5. `build_silver_match_t5.py` — delete the two swing heuristics; project `stroke_class` verbatim (incl. `other`). Leave the volley flag as the stopgap until the volley fact (point above) lands.

**Note on serves:** `fh_overhead` includes serve motions (a serve is mechanically a smash). Serves remain owned by `serve_events`; the classifier's `overhead` covers mid-court smashes. Unchanged from the original plan.

---

## Context

Per [bronze_silver_18_audit.md §"Build backlog reframed"](./bronze_silver_18_audit.md):

> "swing type | none (classifier untrained) | train stroke_classifier → emits fh/bh to bronze → silver inherits"

Today the scaffold exists at `ml_pipeline/stroke_classifier/` (optical-flow CNN architecture) but no trained weights (`models/stroke_classifier.pt` absent). `ml_analysis.stroke_events` carries timing + confidence but **no swing type** — fh/bh/overhead is re-derived in silver via a pose-keypoint heuristic. The current pose-inference STOPGAP is the reason backhand over-counts (T5 28 vs SA 18 on Match 1).

The dual-submit corpus today only carries `label_kind='ball_position'` (488 labels). To train this classifier the corpus extractor needs to start emitting a new `label_kind` — see [Agent 2 audit findings](../../#corpus-audit) for the extension recipe.

## Sub-questions

1. **Features** — optical-flow CNN only (the scaffolded architecture), pose-feature MLP, or fusion of both?
2. **Labels** — SportAI-only via dual-submit (teacher), or accept manual labels too?
3. **Classes** — {forehand, backhand, overhead} only, or include volley as a fourth class?
4. **Where the model lives at inference time** — Render-side (in the ingest flow, like serve_detector) or Batch-side (during detection)?
5. **Where the model trains** — local GPU dev box, AWS Batch GPU one-off job, or Render (CPU — won't work for a real CNN)?
6. **Output shape** — hard label only, or label + per-class confidences?

## Options

### Q1 — features
| Option | Pros | Cons |
|---|---|---|
| **A. Optical-flow CNN** (as scaffolded) | Handles far-player where pose is sparse (~1,105 keypoint rows vs 11,755 near-player on M1); works on small ROI crops | Heavier inference; needs the player ROI extracted around hit event |
| **B. Pose-feature MLP** (wrist/elbow trajectory ±N frames around hit event) | Tiny; CPU-runnable; trains fast | Useless on far player (where pose is sparse) — leaves the far-court ceiling intact |
| **C. Fusion** (optical flow + pose) | Best of both — pose for near, optical flow as fallback | Largest training surface; more places for the model to overfit |

### Q2 — labels
| Option | Pros | Cons |
|---|---|---|
| **A. SportAI dual-submit only** | Free, auto-accumulating, scales as more matches land | SA is "generally good but not perfect" — caps us at SA's accuracy |
| **B. SA + selective manual override** | Use SA as the base, hand-correct the misses we measure | Manual labelling cost; coordination overhead |

### Q3 — classes
| Option | Pros | Cons |
|---|---|---|
| **A. {forehand, backhand, overhead}** | Volley falls out as a derivation (see [ADR-04](./adr_04_volley_model_or_analytic.md)) — clean separation | None — this is the right shape if ADR-04 picks "analytic" |
| **B. {forehand, backhand, overhead, volley}** | Single model for all stroke types | Volley is mechanically a *forehand/backhand happening before the bounce* — folding it into perception conflates two concerns |

### Q4 — inference placement
| Option | Pros | Cons |
|---|---|---|
| **A. Render-side, in ingest flow** (after stroke_detector, before silver build) | Mirrors serve_detector shape; no Batch deploy per iteration; rule-#8 friction-free | Per-hit-event inference on Render CPU could be slow if model is large; need to size accordingly |
| **B. Batch-side, during detection pass** | Cheaper if it shares features with the YOLO/ViTPose pose pass | Trips BATCH-SIDE CHANGE CHECKLIST on every model iteration; slower deploy story; harder to A/B |

### Q5 — training placement
| Option | Pros | Cons |
|---|---|---|
| **A. Local GPU dev box** (Tomo's box per `.claude/infrastructure/gpu_dev_box_runbook.md`) | No AWS GPU cost; full control; iterates in minutes | Trainer needs to commit weights file to S3 + sync to `ml_pipeline/models/` (git-ignored) |
| **B. AWS Batch GPU one-off training job** | Reuses existing compute env (`ten-fifty5-ml-ce-eu-ondemand`); reproducible; weights to S3 directly | Costs $; queue waits; less interactive |

### Q6 — output shape
- **Hard label only** vs **label + per-class confidences**.
- Confidences enable downstream filtering (drop low-confidence calls; downstream consumers like volley analytic can weight by confidence). Trivial cost. No reason to skip.

## Recommendation

**Q1: A — Optical-flow CNN** (the scaffolded architecture). Reasoning: pose-only fails on far player (sparse pose), which is exactly half the dashboard. Fusion (C) is appealing but adds training complexity for marginal gain — start with optical-flow only, add pose features later if measurement shows near-player accuracy lags.

**Q2: A — SportAI dual-submit only**, with a measurement gate. SA's serve+stroke mapping is "generally good" (per the 2026-05-27 correction). If measurement on Match 1 + future matches shows a systematic SA error class, escalate to (B). The training caution in [north_star.md §"OVERARCHING GOAL"](../north_star.md) applies — verify, don't blindly fit.

**Q3: A — {forehand, backhand, overhead}** only. Volley is genuinely derivative ([ADR-04](./adr_04_volley_model_or_analytic.md)) — folding it in conflates perception with event-order logic.

**Q4: A — Render-side inference**, in the ingest flow after stroke_detector. Mirrors serve_detector. Avoids the BATCH-SIDE CHANGE CHECKLIST iteration cost. Performance budget: ~10-30 s for a full match (~200 hit events × ~50 ms per inference on CPU is feasible for an optical-flow CNN on ROI crops).

**Q5: A — Local GPU dev box** for v1; AWS Batch GPU job for re-training once the corpus is large (~20+ matches). Manual `aws s3 cp` of the weights file to S3 + a `ml_pipeline/models/` sync step in the deploy. Document this in `.claude/handover_t5.md`.

**Q6: Label + per-class confidences.** Output one row per hit-event in `ml_analysis.stroke_events` with new columns `swing_type` + `swing_type_confidence`, OR as a separate `ml_analysis.swing_type_events` table joined on `stroke_event_id`. Prefer **columns on `stroke_events`** — cleaner; same shape as `serve_events` carries `confidence`. Coordinate with parallel agent (they own `stroke_detector/`).

## Open follow-ups (decide at build time)

1. **Corpus extractor for swing-type labels** — write `ml_pipeline/training/label_swing_types.py` paralleling `label_ball_positions.py`. Source: `bronze.player_swing.swing_type`. Wire into `upload_app._label_pair_now()`. ~150 LOC + a few lines in the hook.
2. **Label-kind value** — schema (`ml_pipeline/db_schema.py:265-284`) already lists `'stroke_classifier'` as a planned `label_kind`. Use that.
3. **ROI window** — ±0.5 s around `stroke_events.predicted_hit_frame` is the starting guess. Tune via bench.
4. **Bench fixture** — first one captured from Match 1 + Match 2 corpus pairs; locked baseline like `bench_baseline.json`.

---

## Build spec v1 (research-grounded, 2026-05-28)

**Architecture.** **R(2+1)D-18** backbone on the optical-flow stream — 16-frame clip at 112×112 ROI. (2+1)D factorisation (2D spatial then 1D temporal) keeps CPU inference cheap. **MoViNet-A0-Stream** is the fallback if R(2+1)D-18 is too heavy on Render CPU (~4 ms/frame on mobile CPU per the MoViNet paper). Benchmark to beat: **Hovad et al. 2024 — 74% generalisation accuracy on THETIS 12-class** using SlowFast variants; our 3-class problem is easier. Two-stream RGB + optical-flow (Martin et al. 2022) is the v2 upgrade path; start with optical-flow only to halve training-data hunger.

**Input spec.**

| Parameter | Value | Rationale |
|---|---|---|
| Window | 16 frames @ 30 fps (~0.53 s), centred **10 frames before → 6 frames after** `predicted_hit_frame` | Asymmetric pre-hit bias matches perceptual classification studies (humans use 0.8 s before to 0.2 s after contact) |
| ROI | player bbox × 1.5, square-padded, resized to **112×112** | R(2+1)D / SlowFast standard; far-player upsamples from ~30 px native — accept the resolution loss, motion matters more than sharpness |
| Modality | dense optical flow (TV-L1 or RAFT-tiny), 2-channel (dx, dy), **16 × 2 = 32-channel input** | Optical-flow-only handles far-player where pose is sparse |
| Frame rate | 30 fps native, no interpolation | — |

**Training recipe.**

| Element | Choice | Reference |
|---|---|---|
| Loss | Cross-entropy with **label smoothing ε=0.1** | Müller et al. 2019 — improves confidence calibration |
| Optimizer | AdamW, lr 1e-4, cosine decay, weight decay 1e-4, 5-epoch warmup | Standard |
| Augmentation | Horizontal flip 50% **with handedness-bit toggled**, colour jitter, random temporal crop ±2 frames, mixup α=0.2 | Hong et al. ICCV 2021 (handedness flip-bit) |
| Imbalance handling | Focal loss γ=2 if overhead recall < 70%; WeightedRandomSampler oversampling OH 3-4× per epoch | Sports-video standard for rare classes (OH ~3-5% of strokes) |
| Confidence calibration | Label smoothing in training + post-hoc temperature scaling on held-out val | Standard deployment trick |
| Volume target | **2,000-3,000 labelled hit-events** (~10-15 dual-submit matches), ~50 epochs, early-stop on val macro-F1 | Scales from Hovad 2024 (8,374 clips for harder 12-class) |

**Handedness (per ADR-02 decision, 2026-05-28): auto-infer**, not a form field. Determine from the first ~10 hits — forehand-side preference reveals the dominant hand. Concat as a 1-bit input feature to the penultimate FC layer. Flip-augmentation must toggle this bit. Failure mode (ambidextrous player, very small sample): fall back to "right" (statistical majority).

**Output.** Two new columns on `ml_analysis.stroke_events` (coordinate with parallel agent who owns stroke_detector):
- `swing_type TEXT` ∈ {`forehand`, `backhand`, `overhead`}
- `swing_type_confidence FLOAT` ∈ [0, 1]

Volley is **NOT** a class here — the volley analytic (ADR-04) consumes `swing_type` + `ball_bounces` and derives the volley flag separately.

**Tomo's "long stroke / under shoulder / hits a ball" intuition — apply as OUTPUT-SANITY GATES, not classifier inputs.** SOTA consensus is end-to-end learning. The intuition is supported by biomechanics literature but encoding it as model input is brittle on the far player (where the pose primitives we'd rule against are exactly what's sparse). Two safe applications:
1. **Post-classifier validation gate** — if the model predicts `forehand`/`backhand` but pose shows racket-arm above shoulder for ≥80% of window → downgrade `swing_type_confidence` and flag as overhead candidate for review.
2. **Pre-classifier negative filter** — if `stroke_detector` already classified the frame as non-swing (no ball contact, no long backswing) → don't pass to the classifier at all (skip cheaply).

**Top 3 references.**
1. Hovad et al. 2024, *Classification of Tennis Actions Using Deep Learning*, arXiv 2402.02545 — SlowFast on THETIS 74% accuracy benchmark to beat.
2. Martin et al. 2022, *Two Stream Network for Stroke Detection in Table Tennis*, arXiv 2112.12073 — two-stream RGB + optical-flow late fusion template for v2 upgrade.
3. Kondratyuk et al. 2021, *MoViNet*, arXiv 2103.11511 — CPU-inference reference (4 ms/frame mobile CPU at A0-Stream); fallback architecture for Render budget.

---

## Cross-references

- [bronze_silver_18_audit.md](./bronze_silver_18_audit.md) — the model-gap framing.
- [far_player_accuracy.md](./far_player_accuracy.md) — why pose-only fails on far player.
- [ml_pipeline/stroke_classifier/](../../ml_pipeline/stroke_classifier/) — existing scaffold.
- [ADR-04](./adr_04_volley_model_or_analytic.md) — volley analytic depends on this model's output.
- [ADR-05](./adr_05_detector_build_sequencing.md) — sequencing.
