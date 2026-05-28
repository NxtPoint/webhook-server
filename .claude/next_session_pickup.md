# Next-session pickup — 2026-05-28 (session close 4) — Swing-type DATASET BUILDER shipped + full v1 built

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (close 4)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. Identity `100%` unchanged.

**What shipped this session (across 3 commits):**
1. `06080a0` — **ADR-02 swing-type corpus extractor** (`label_swing_types.py`) + per-kind `_label_one_kind` helper + dual-kind `_label_pair_now()` + backfill 3 pairs.
2. `2f67bbd` — **Stream 3 serve corpus extractor** (`label_serves.py`) + third `_label_one_kind` call + `known_kinds` CTE + backfill 3 pairs.
3. `(this commit)` — **ADR-02 dataset builder** (`build_swing_type_dataset.py`) converts the swing corpus + 720p trimmed video into architecture-agnostic `(N, 16, 112, 112, 2)` Farneback optical-flow tensors. Full v1 dataset built locally (368 hits / 775 labels = 47.5% survival; gitignored output).

**Corpus state post-this-session (3 kinds × 3 pairs = 9 corpus rows):**

| Pair | T5 | SA | ball_position | stroke_classifier | serve |
|---|---|---|---|---|---|
| Match 1 (Tomo / Rivonia) | 78c32f53 | 0d0514df | 161 ✅ | 94 ✅ | 25 ✅ |
| Match 2 (Erin / ccj)     | c645a7ee | ee12d918 | 327 ✅ | 341 ✅ | 46 ✅ |
| Match 3 (Dejan / ccj)    | 9378f2dd | 2f355924 | 331 ✅ | 340 ✅ | 47 ✅ |
| Corpus 4 (Tomo / Rivonia) | ca475740 *(T5 78%, roi_extract stage)* | 3922af92 ✅ | pending hook | pending hook | pending hook |
| **Totals (now)** |  |  | 3 rows / 819 | 3 rows / 775 | 3 rows / 118 |

**Corpus 4 status:** SA done; T5 progressed 65%→77%→78% during this session but ROI extract stage age is now ~23 min without status update — may be stuck or just slow. Won't land this session.

## ADR-02 v1 dataset receipts (NEW)

Local build at `ml_pipeline/training/datasets/swing_type_v1/` (gitignored). Per-match survival:

| Match | n_in | n_out | NEAR | FAR | forehand | backhand | overhead |
|---|---|---|---|---|---|---|---|
| 78c32f53 (Rivonia) | 94 | **66 (70%)** | 48 | 18 | 28 | 12 | 26 |
| c645a7ee (ccj)     | 341 | **152 (45%)** | 100 | 52 | 56 | 32 | 64 |
| 9378f2dd (ccj)     | 340 | **150 (44%)** | 100 | 50 | 55 | 31 | 64 |
| **Total** | **775** | **368 (47.5%)** | **248 (67%)** | **120 (33%)** | **139** | **75** | **154** |

**Drop reason — 100% are "no role-matching T5 player detection within ±5 frames"** at hit_frame. Same root cause flagged in north_star line 56 (far-player coverage gap). Match 1 (Tomo/Rivonia, good camera setup) loses 30%; Matches 2+3 (ccj courts) lose 55% — drop rate is court-setup correlated, not random. This is the EXACT pattern called out by [memory `feedback_diagnose_corpus_before_assuming_data_scarcity_fixable_by_bulk_load`](../../memory/feedback_diagnose_corpus_before_assuming_data_scarcity_fixable_by_bulk_load.md).

**Implications for ADR-02 training:**
- 368 hits split train/val = 216 train / 152 val (2 matches train, 1 match val to avoid cross-match leakage)
- Class balance per training fold: ~80 forehand / ~45 backhand / ~85 overhead. Backhand is the minority — focal loss or WeightedRandomSampler may be needed.
- Role balance: 67% NEAR / 33% FAR. Model will see far less FAR motion data; production accuracy on FAR will lag.
- This corpus is below ADR-02's stated 2-3k volume target; weights trained today would overfit. Wait for ~5-10 more matches before firing v1 training.

## Honest status of the 5 facts (post-this-session)

| Fact | Status | Honest read |
|---|---|---|
| serve | **DEV CEILING** ✅; corpus extractor LIVE (Stream 3) | 118 labels + ~114 from Corpus 4 ≈ 232. Need ~500+ for receiver-FP training. |
| bounce (ADR-01) | **v0 SCAFFOLDED — UNTRAINED** | STOPGAP threshold 1.1 active. Corpus has 411 floor labels reachable once Corpus 4 lands + 2 unpaired re-submits. |
| swing_type (ADR-02) | **CORPUS EXTRACTOR + DATASET BUILDER LIVE** | 368 training-ready hits today; will be 500+ once Corpus 4 lands. Below 2-3k v1 target. Model class + training loop NOT YET BUILT. |
| identity (ADR-03) | **v1 SHIPPED at 100% bench** ✅ | v2 OSNet planned. |
| volley (ADR-04) | **Not built — by design** | Blocked on bounce + swing-type. |

## Honest re-ordered roadmap

1. **Wait for Corpus 4 T5 to finish** — all 3 corpus kinds auto-land via the deployed hook. Will also add ~150-200 more training-ready hits to swing dataset on next builder re-run.
2. **(Optional, Tomo's call)** Re-submit 2 unpaired Tomo-Rivonia matches → another ~300 floor + ~500 swing + ~50 serve labels.
3. **Train ADR-01 bounce_detector v1** on accumulated floor labels (recipe in `.claude/adr01_label_audit_2026-05-28.md`). Highest forward impact — gets bounce_detector from STOPGAP-clamped-to-zero to actually emitting rows.
4. **Accumulate swing-type corpus to ~2-3k labels** (5-10 more matches). Then train R(2+1)D-18.
5. **Accumulate serve corpus to ~500+ labels** (a few more matches). Then retrain `serve_detector` for receiver-FP.
6. **ADR-04 volley analytic drops out** for free.
7. *(Maybe)* ADR-03 v2 OSNet.

## Next session's job — pick ONE (parallel-safe ★)

- **★(A) ADR-01 v1 training** — IFF Corpus 4 has landed AND ideally 2 unpaired Rivonia matches re-submitted. ~2-3 hr. **HIGHEST IMPACT** (unblocks bounce from STOPGAP zero).
- **★(F) Batch runtime optimisation** — L1+L4+L5 from `docs/_investigation/batch_optimisation_plan.md`. 4.79h → ~1h target. ~5 hr daylight-only deploy.
- **(G) ADR-02 v1 model + training scaffold** (no weights yet) — mirror bounce_detector's STOPGAP pattern: R(2+1)D-18 model class + dataset wrapper that reads our 368-hit .pt files + training loop + bench harness + STOPGAP-flagged inference wiring. ~4-5 hr. Spec-compliant. Weights fire when corpus crosses 2-3k. **Note:** the existing `ml_pipeline/stroke_classifier/` is pre-ADR-02 (50K-param scaffold, 5-class, far-only) — decide whether to replace or evolve before building.
- (C) ADR-03 v2 OSNet — not urgent.
- (D) ADR-04 volley — still blocked on (A).
- (E) Hand-label net-cord / racket-hit FPs — deferred.

**Recommended:** (A) if Corpus 4 has landed by then; otherwise (F) for daylight, or (G) for night.

## What this session changed (concrete artefacts)

### Commit 1 (`06080a0`)
- `ml_pipeline/training/label_swing_types.py` (~170 LOC). Source: `bronze.player_swing` mapped to canonical {forehand, backhand, overhead}.
- `upload_app.py::_label_one_kind` helper + `_label_pair_now` refactor (dual kinds).
- `upload_app.py::/ops/backfill-pair-labels` known_kinds CTE.
- 3 backfilled `stroke_classifier` corpus rows (775 labels).

### Commit 2 (`2f67bbd`)
- `ml_pipeline/training/label_serves.py` (~190 LOC). Source: `bronze.player_swing WHERE serve = TRUE`.
- Third `_label_one_kind` call for `label_kind='serve'`.
- `known_kinds` CTE extended.
- 3 backfilled `serve` corpus rows (118 labels).

### Commit 3 (this commit)
- `ml_pipeline/training/build_swing_type_dataset.py` (~450 LOC). Architecture-agnostic: outputs `(N, 16, 112, 112, 2)` Farneback flow tensors + per-hit metadata. Resolves player bbox via role + court-coord matching with ±5-frame fallback search (recovers 9 of 28 missing FAR detections on Match 1). 1080→720 bbox rescale baked in.
- `.gitignore` adds `_dataset_cache/`.
- Full v1 dataset built locally: 3 .pt files + manifest.json under `ml_pipeline/training/datasets/swing_type_v1/` (all gitignored). Cached videos under `_dataset_cache/` (~750 MB local).
- `docs/north_star.md` ADR-02 row updated.

## Coordination protocol (per ADR-05) — unchanged

1. No agent starts a detector build without an APPROVED ADR.
2. Each detector build has its own commit scope.
3. **Corpus extension lands in the same commit as the detector model it feeds.** (All 3 this session: corpus-side prep ahead of model — Step 2 of prescribed sequence.)
4. Each detector ships with a bench.
5. This pickup file gets updated at every detector ship.

## Commits this session
- `06080a0` `feat(t5): ADR-02 swing-type corpus extractor + dual-kind _label_pair_now`
- `2f67bbd` `feat(t5): Stream 3 serve corpus extractor + 3rd _label_one_kind`
- `(this commit)` `feat(t5): ADR-02 swing-type dataset builder + full v1 build`
- *(Tomo's parallel doc landed mid-session)* `cb4e449` `docs(investigation): batch optimisation plan`
- *(Tomo's note landed mid-session)* `79f5947` `docs: document Batch runtime optimisation plan as the next inflection point`

## Memory ceiling reference (unchanged)

| Stage | Peak heap |
|---|---|
| Bronze ingest (streaming ijson) | ~15 MB |
| serve_detector | ~75 MB |
| stroke_detector | ~53 MB |
| silver build | ~79 MB |

Render's 512MB main API has comfortable headroom. End-to-end ingest 44-min match: 3 min 39 sec.

## Runtime ceiling reference (NEW 2026-05-28)

| Phase | Wall time (44-min match, ~67k frames) | Bottleneck |
|---|---|---|
| AWS Batch (court + ball + player + ROI + serialisation) | **~4.79 h** | YOLOv8x-pose @ 1280 + SAHI tile-fan, `batch=1` every 5th frame (~75-85% of wall) |
| Render ingest (bronze re-ingest + detectors + silver + AUTO_LABEL hook) | ~3 min 39 sec | Solved this session |
| **Target** | **<1 h** | Plan: `docs/_investigation/batch_optimisation_plan.md` |

Empirical proof the bottleneck is player stage (not ball): tonight's commit `5317c50` added GPU batching for the WASB ball tracker; ms/frame moved from ~183.3 to ~183.1. Player batching is the only remaining lever in the same template.

## Read in this order
0. **`docs/north_star.md` §"★ RULES OF THE GAME"** — non-negotiable.
1. This file.
2. `docs/north_star.md` §"Current detector build queue (2026-05-28)".
3. `.claude/adr01_label_audit_2026-05-28.md` — for Task (A) ADR-01 work.
4. `docs/_investigation/adr_02_swing_type_classifier_plan.md` §"Build spec v1" — for Task (G) ADR-02 model work.
5. `docs/_investigation/batch_optimisation_plan.md` — for Task (F) Batch runtime work.

## Suggested opening prompt for next chat (pick A, F, or G)

**Option A — ADR-01 v1 training (default if Corpus 4 landed):**
```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's task: check the corpus state first (SQL — should be 4 rows × 3 kinds
if Corpus 4 landed). If yes → ADR-01 v1 training (Task A in the pickup).
If not yet → consider Task G (ADR-02 model scaffold using already-built
swing dataset) or close out + recommend Task F for next daylight.
```

**Option F — Batch runtime optimisation (daylight only):**
```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's task: land L1 + L4 + L5 from docs/_investigation/batch_optimisation_plan.md
against the T5 Batch pipeline. Read the plan first. One commit per lever.
After all three are in, run BATCH-SIDE CHECKLIST end to end (Docker rebuild,
ECR push to eu-north-1 + us-east-1, job-def revisions in both regions).
Then submit ONE test match. ~5h session; daylight only.
```

**Option G — ADR-02 v1 model scaffold (no weights yet):**
```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's task: ADR-02 v1 model scaffold per docs/_investigation/adr_02_swing_type_classifier_plan.md
§"Build spec v1". First, decide whether to replace or evolve the existing
ml_pipeline/stroke_classifier/ (pre-ADR-02 50K-param 5-class far-only scaffold).
Then build: R(2+1)D-18 model class, PyTorch Dataset wrapper for the existing
swing_type_v1 .pt files (368 hits / 16-frame Farneback flow), training loop,
bench harness, STOPGAP-flagged inference wiring into _do_ingest_t5 after
stroke_detector. NO TRAINING RUN today — weights wait until corpus crosses
~2-3k labels (~5-10 more matches). ~4-5 hr. Mirror bounce_detector pattern.
```

## Constraints (unchanged)

- Don't touch parallel-agent files: `serve_detector/`, `stroke_detector/`, `build_silver_match_t5.py`, `ball_tracker.py`, `wasb_*.py`, `roi_extractors/`, or any file in BATCH-SIDE CHECKLIST per rule #8 (unless doing Task F).
- No pytest. No `?key=` query-string auth. Pull-rebase before push.
- Always commit to main (no feature branches).

## Scratch
None. All output went into committed code + docs.
