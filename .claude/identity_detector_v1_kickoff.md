# identity_detector v1 — kickoff (2026-05-28)

**Status:** v1 (rule-based, no training) shipped to working tree, awaiting parent-session review. **Not committed, not registered in `upload_app.py` boot.**

**Spec:** `docs/_investigation/adr_03_identity_model.md` §"Build spec v1".

## What's built (v1, rule-based, fully shippable)

- `ml_pipeline/identity_detector/` — new module mirroring `serve_detector` shape:
  - `models.py` — `IdentitySegment`, `GameBoundary`, `Side`, `IdentitySource` dataclasses/enums.
  - `game_boundaries.py` — server-alternation game-window derivation + tiebreak detection (cumulative games_in_set ≥ 12 → next is tiebreak). Includes a small de-glitch step for noisy serve_events.
  - `changeover_rule.py` — per-game decision matrix (ITF expected × dual-cross observed) → `(swapped, confidence, source)`.
  - `detector.py` — orchestrator; prod entry `detect_identity_for_task(conn, tid)` + pure offline `detect_identity_offline(...)`.
  - `db.py` — `init_identity_schema()` with two idempotent DDLs (the new column + the new table).
  - `__init__.py` — public API including the spec-required `detect_identity_segments(task_id)` wrapper.
- `frontend/media_room.html` — new singles step-3 toggle "Player A is on the camera side at the start of the match" (defaults Yes); flows into `submit_s3_task` body as `a_starts_near: true|false`.
- `upload_app.py` — minimal additive change (6 lines): one ALTER COLUMN in the existing schema-ensure block, four lines wiring `a_starts_near` through the existing `_store_submission_context` INSERT, and one line carrying it from request body into the meta dict.
- `ml_pipeline/diag/bench_identity.py` — local-only bench. Runs the detector against three T5 tasks (the two locked CI-bench fixtures `880dff02`/`a798eff0` plus the spec's "Match 1" `78c32f53`), reports per-game segments, confidence dist, and ITF-expected-vs-detected changeover-fire rate.

## What's NOT built (deferred — v2 / future sessions)

- **v2 OSNet CNN re-id.** The whole training pathway (corpus extension for `label_kind='identity'`, dual-submit pairs, OSNet fine-tuning, embedding-vs-centroid inference). Spec is in ADR-03 §"v2 re-id CNN architecture" — needs ≥ 10 dual-submit matches before training is meaningful.
- **Registration in `upload_app.py` boot.** Parent will register `init_identity_schema()` in the on-boot init sequence (same try/except pattern as the other schema-init functions).
- **Wiring identity_detector into the T5 ingest flow** so `detect_identity_for_task()` actually runs after serve_detector. Currently the function exists but nothing calls it in prod.
- **Silver-builder join.** `build_silver_match_t5.py` doesn't yet read from `ml_analysis.player_identity_segments` — silver still uses near/far for the T5 model. The join (per ADR §"Confidence + uncertainty model": ≥ 0.9 use A/B labels; 0.5-0.9 keep near/far + flag uncertain; < 0.5 surface in dashboard) is the next session's silver-side work.
- **Dashboard surfacing of `source = needs_review`** segments (the "identity uncertain — review and tag manually" UI).

## How to run the bench

```bash
.venv/Scripts/python -m ml_pipeline.diag.bench_identity
# or scope to one task:
.venv/Scripts/python -m ml_pipeline.diag.bench_identity --task 880dff02-58bd-412c-9a29-5c5151004447
# or dump full JSON for post-mortem:
.venv/Scripts/python -m ml_pipeline.diag.bench_identity --json out.json
```

Local-only by design — hits the live Render Postgres via `db_init.engine`, so CI won't run it. The serve bench (`python -m ml_pipeline.diag.bench`) remains the only CI gate. Identity bench is to the identity module what `bench_silver` is to silver: a local regression check the human runs before pushing.

## v1 floor reality (bench output)

Per-game agreement on Match 1 (`78c32f53`) and the two locked fixtures: **0% changeover detection** at the 14 ITF-expected boundaries across all three tasks. This is **the expected v1 ceiling**, not a bug. The YOLOv8 tracker already binds `player_id=0` to "whoever is on the near side right now" and `player_id=1` to "whoever is on the far side" — so after a changeover the same physical player simply gets re-assigned. The court_y signal the rule looks for (dual-cross from near→far + far→near) is invisible to the rule because the tracker has already "fixed" it.

What the rule *does* do correctly on v1:
- Derives sensible per-game time windows from server alternation (with de-glitching against single-serve FP flips).
- Honestly emits `source = rule_v1_terminated` with conf=0.5 at every expected changeover where the rule can't observe a flip — those get the NEEDS_REVIEW promotion at threshold so silver knows not to trust them.
- Anchors game 1 from the upload-form `a_starts_near` field.
- Persists `ml_analysis.player_identity_segments` idempotently (ON CONFLICT DO UPDATE).

The v2 CNN is exactly the upgrade specified to break this ceiling.

## What the v2 session needs

1. Extend `ml_analysis.training_corpus` with `label_kind='identity'` rows — one row per inter-game gap per dual-submit match, carrying the SA-confirmed (player_a_pid, near/far) mapping as ground truth.
2. Build the OSNet_x1_0 fine-tune harness in `ml_pipeline/identity_classifier/` (NEW, separate from `stroke_classifier/`).
3. Replace `changeover_rule.py`'s dual-cross check with OSNet-centroid cosine-similarity per game.
4. Add a `reid_cnn_v1` source value (already an allowed value in the DDL comment).
5. Extend `bench_identity.py` with a `--use-cnn` flag and a CNN-vs-rule comparison column.

## File index

| File | Purpose |
|---|---|
| `ml_pipeline/identity_detector/__init__.py` | Public API: `detect_identity_for_task`, `detect_identity_segments`, `init_identity_schema` |
| `ml_pipeline/identity_detector/models.py` | Side / IdentitySource enums + GameBoundary + IdentitySegment dataclasses |
| `ml_pipeline/identity_detector/game_boundaries.py` | Server-alternation derivation with tiebreak + de-glitch |
| `ml_pipeline/identity_detector/changeover_rule.py` | Per-gap dual-cross decision matrix |
| `ml_pipeline/identity_detector/detector.py` | Orchestrator: prod + offline entry points |
| `ml_pipeline/identity_detector/db.py` | `init_identity_schema()` + `delete_identity_for_job()` |
| `ml_pipeline/diag/bench_identity.py` | Local-only bench harness |
| `frontend/media_room.html` | Singles step-3 form: a_starts_near toggle |
| `upload_app.py` | submission_context column + INSERT wiring (additive) |
