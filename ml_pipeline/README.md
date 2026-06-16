# ml_pipeline

> In-house ("T5") tennis video analysis. AWS Batch GPU runs court / ball / player detection into `ml_analysis.*` (bronze); Render then runs serve detection + the T5 silver build; gold views feed the dashboards. **Status: BRONZE DETERMINISTIC DEV COMPLETE (2026-06-16) — training is the only remaining (incremental) phase.** Dev-only, gated to `tomo.stojakovic@gmail.com`.

This README is a **router**, not a spec. It points you at the canonical docs; it does not duplicate them. Read `docs/north_star.md` §"★ RULES OF THE GAME" **first**, then `.claude/handover_t5.md`.

## Data flow

```
video.mp4 → AWS Batch GPU (court/ball/player detection) → ml_analysis.*  (bronze)
          → Render (serve_detector)        → ml_analysis.serve_events
          → Render (build_silver_match_t5) → silver.point_detail (model='t5')
          → gold.* views → dashboards
```

Bronze is the single source of truth; silver inherits 100% and does no work; one model per fact; build-first / train-last.

## Submodules

Where each runs matters: **Batch** modules ship in the Docker image (changing them trips rule #8 — Docker rebuild + dual-region ECR push + new job-defs). **Render** modules deploy by `git push` to `origin/main`. Only weight-based inference runs in Batch (weights are git-ignored, so Render never has them).

| Dir | Runs | Purpose | Doc |
|---|---|---|---|
| `serve_detector/` | Render | Pose-first serve detection + rally state machine + `serve_events` schema | `.claude/handover_t5.md` TEST HARNESS |
| `stroke_detector/` | Render | Velocity-signal stroke (ball-hit) detection → `stroke_events`; near-side swing-path precision gate | `docs/_investigation/far_player_accuracy.md` |
| `bounce_detector/` | Batch | CNN bounce model (gravity-residual candidates → CNN scorer) → `ml_analysis.ball_bounces` | `docs/_investigation/adr_01_bounce_model_architecture.md` |
| `serve_model/` | Batch infer + Render merge | Far-serve candidate anchors + MLP scorer; merges `model_far` additively | `docs/_investigation/adr_01_*` (recipe port) |
| `hit_model/` | local/CPU (not in image) | Per-candidate ball-hit classifier (hit/bounce/noise); gate not yet met | `docs/_investigation/adr_05_detector_build_sequencing.md` |
| `stroke_classifier/` | Batch | Optical-flow R(2+1)D swing-type CNN (4-class) → bronze `stroke_class`; silver projects it verbatim | `docs/_investigation/adr_02_swing_type_classifier_plan.md` |
| `identity_detector/` | Render | A/B player identity (changeover rule + game boundaries) | `docs/_investigation/adr_03_identity_model.md` |
| `roi_extractors/` | Batch | Unified single-decode ROI sweep (`unified.py::run_unified_roi`): `pose.py` (far ViTPose → `player_detections_roi`) + `bounces.py` (service-box TrackNet) + `far_ball.py` (far-half ball → `source='roi_far_ball'`) | `.claude/handover_t5.md` §ROI extractor integration |
| `training/` | GPU dev box / Batch | Model training (TrackNet fine-tune, bounce/swing/hit), corpus + manual labelling | `.claude/training_environment.md` |
| `diag/` | local | The bench family (`bench`/`bench_ball`/`bench_silver` + `bench_hit`/`bench_bounce`/`bench_swing_type`/…), `recon_line`, serve viewer, pose probes | `.claude/handover_t5.md` TEST HARNESS |
| `point_structure/` | — | `point_boundaries.py` — point/game derivation; **not** used by the silver builders (they derive structure in `build_silver_v2` pass-3 SQL). Kept for `diag/audit_points.py` only | — |
| `ground_truth/` | — | SA-independent hand-labelled truth (bounce labels) for accuracy where SA can't be the yardstick | `ground_truth/README.md` |

The serve/silver builders also live at repo root or here: `build_silver_match_t5.py` (T5 match silver, shares passes 3-5 with root `build_silver_v2.py`), `build_silver_practice.py`, `bronze_ingest_t5.py`, `bronze_export.py`, `db_writer.py`, `harness.py`.

## Bench floor + the rule

Run before any `serve_detector/` edit:

```bash
.venv/Scripts/python -m ml_pipeline.diag.bench   # floor: ea1e500c=12/26, 880dff02=23/24
```

A red bench is a real regression — never relax or work around it (CLAUDE.md rule #9). **Batch-side changes additionally trip rule #8** — bench green ≠ Batch in sync; the canonical Batch-bundled file list is the `COPY` lines in `ml_pipeline/Dockerfile`. Full checklist: `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST".

## Pointers out (don't duplicate these here)

- `docs/north_star.md` — RULES OF THE GAME + macro plan + current status (authoritative).
- `.claude/handover_t5.md` — how to run / validate / ship (ops); TEST HARNESS is the serve-detector operating manual.
- `.claude/training_environment.md` — training environment (Batch GPU) + corpus generation. The next move is training.
- `docs/_investigation/bronze_silver_18_audit.md` — field-by-field 18-base-field bronze↔SA audit.
- `docs/_investigation/adr_0*.md` — the model-architecture ADRs (bounce, swing-type, identity, volley, detector sequencing).
- CLAUDE.md §"T5 ML pipeline" — the key-directories table and the load-bearing "things not to do".
