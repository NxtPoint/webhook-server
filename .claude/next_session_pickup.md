# Next-session pickup — 2026-05-28 (session close 3) — Stream 3 SERVE corpus extractor SHIPPED on top of ADR-02

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (close 3)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (re-verified post-change). Identity `100%` (n=14 ITF boundaries) unchanged.

**What shipped this session (across 2 commits):**
1. `06080a0` — **ADR-02 swing-type corpus extractor** (`ml_pipeline/training/label_swing_types.py`) + per-kind `_label_one_kind` helper + dual-kind emission in `_label_pair_now()` + backfill of 3 dual-submit pairs.
2. `(this commit)` — **Stream 3 serve corpus extractor** (`ml_pipeline/training/label_serves.py`) + third `_label_one_kind` call wired into `_label_pair_now()` + `known_kinds` CTE extended in `/ops/backfill-pair-labels` + backfill of 3 dual-submit pairs.

**Corpus state post-this-session (3 kinds × 3 pairs = 9 corpus rows):**

| Pair | T5 | SA | ball_position | stroke_classifier | serve |
|---|---|---|---|---|---|
| Match 1 | 78c32f53 | 0d0514df | 161 ✅ | 94 ✅ | **25 ✅** (NEW) |
| Match 2 | c645a7ee | ee12d918 | 327 ✅ | 341 ✅ | **46 ✅** (NEW) |
| Match 3 | 9378f2dd | 2f355924 | 331 ✅ | 340 ✅ | **47 ✅** (NEW) |
| Corpus 4 | ca475740 *(T5 77%)* | 3922af92 ✅ | pending hook | pending hook | pending hook |
| **Totals (now)** |  |  | 3 rows, 819 labels | 3 rows, 775 labels | **3 rows, 118 labels** |

**Corpus 4 status:** SA done; T5 (`ca475740-9e34-49c3-9b59-0194bfa37013`) at **77%** processing as of 15:49 UTC (ETA ~70 min to 100%). When it lands, the deployed `_dual_submit_pair_complete_hook` will atomically emit **ALL THREE** kinds (ball_position ~664, stroke_classifier 397, serve 114). Verify after hook fires via:
```
SELECT label_kind, COUNT(*), SUM(label_count) FROM ml_analysis.training_corpus GROUP BY 1;
-- Expected: ball_position 4 rows / ~1483 total
--           stroke_classifier 4 rows / ~1172 total
--           serve 4 rows / ~232 total
```

## Honest status of the 5 facts (post-this-session)

| Fact | Status | Honest read |
|---|---|---|
| serve | **DEV CEILING** ✅; **CORPUS EXTRACTOR LIVE 2026-05-28 (Stream 3)** | Bench unchanged (20/24 + 23/24). 118 serve labels in corpus + ~114 once Corpus 4 lands ≈ 232. Below ideal training volume (~500+ wanted to crack receiver-FP) but corpus pipeline is open. |
| bounce (ADR-01) | **v0 SCAFFOLDED — UNTRAINED** | Unchanged. STOPGAP threshold 1.1 still active. Corpus has 411 floor labels reachable (67 + 273 once Corpus 4 lands + 71 backfill if Tomo re-submits 2 unpaired matches). |
| swing_type (ADR-02) | **CORPUS EXTRACTOR LIVE 2026-05-28** | 775 swing labels in 3 backfilled rows; ~1,172 once Corpus 4 lands. Below the ~2-3k volume target ADR-02 §"Volume target" wants for v1 training. Needs ~5-10 more matches before train phase. |
| identity (ADR-03) | **v1 SHIPPED at 100% bench** ✅ | Unchanged. v2 OSNet planned for residual. |
| volley (ADR-04) | **Not built — by design** | Blocked on bounce + swing-type. Falls out trivially once those land. |

## Honest re-ordered roadmap

1. **Wait for Corpus 4 T5 (`ca475740`) to finish** — all 3 kinds will auto-land via the deployed hook. Confirm by SQL above.
2. **(Optional, Tomo's call)** Re-submit `0fa94cf6` + `2c1ad953` (the 2 unpaired Tomo-Rivonia SA matches) → adds 411 floor + 488 swing + ~50 serve labels once T5-paired. **The same hook fires for new pairs — no manual backfill needed.**
3. **Train ADR-01 bounce_detector v1** on accumulated floor labels (411-684 depending on re-submits). Recipe: `.claude/adr01_label_audit_2026-05-28.md`. Lock baseline in `bench_baseline_bounce.json`. Ship + wire into `_do_ingest_t5()` after `serve_detector`.
4. **Accumulate swing-type corpus** (a few weeks of normal uploads). Already at ~1,172 labels; need 2-3k for ADR-02 v1.
5. **Accumulate serve corpus** (free with the same uploads). ~232 labels; need ~500+ to make a dent on receiver-FP via training.
6. **Train ADR-02 R(2+1)D-18 swing-type classifier** once corpus is big enough. Spec: `docs/_investigation/adr_02_swing_type_classifier_plan.md` §"Build spec v1".
7. **Train serve_detector v2** — once serve corpus is big enough (parallel-safe with ADR-02 training; different code path).
8. **ADR-04 volley analytic drops out** — ~30 lines once bounce + swing-type are real.
9. *(Maybe)* upgrade ADR-03 to v2 OSNet later — only if 100% rule-based v1 has problematic edge cases.

## Next session's job — pick ONE (parallel-safe ★)

- **★(A) ADR-01 v1 training** — IFF Corpus 4 has landed (verify via SQL above) AND ideally the 2 unpaired Rivonia matches have been re-submitted. ~2-3 hr. Recipe + label-audit findings in `.claude/adr01_label_audit_2026-05-28.md`. **HIGHEST FORWARD IMPACT** — takes bounce_detector from STOPGAP-clamped-to-zero to actually emitting bronze rows. After this, 3 of the 5 detector models are at production state (serve dev-ceiling, identity v1 100%, bounce v1 trained).
- **★(F) Batch runtime optimisation — L1 + L4 + L5 from `docs/_investigation/batch_optimisation_plan.md`** (NEW 2026-05-28). Current runtime is the actual user-pain ceiling: **183 ms/frame → ~4.79 h per 44-min match** vs the 1-hour target (3.5× speedup needed, "without compromising quality"). Plan is ranked, sourced, quality-gated, ready to execute. Stacked L1+L4+L5 = ~1.7-2.2× speedup (4.79h → ~2.4h); add L3 (FP16 YOLO weights) to land near or under 1h. Parallel-safe with detector-build work (different files) BUT trips BATCH-SIDE CHECKLIST (rule #8) — full Docker rebuild + dual-region ECR push + job-def revisions required. **Daylight-only deploy** per `feedback_overnight_branch_only.md`. Estimated session: ~5 h (3 commits + Docker + ECR + job-defs + one test submission to verify ms/frame drop). Quality gates: serve `bench.yml` MUST stay green (a798eff0=20/24, 880dff02=23/24); local `bench_ball` MUST stay green.
- **★(C) ADR-03 v2 OSNet** — not urgent; only if v1 rule edge cases emerge.
- (D) ADR-04 volley analytic — still blocked on (A).
- (E) Hand-label net-cord / racket-hit FPs for ADR-01 bounce_type enum extension — deferred per `.claude/adr01_label_audit_2026-05-28.md` until v1 training tells us pre-gates aren't enough.

**Recommended:** (A) if Corpus 4 has landed by then (biggest single forward step left); **(F) when Tomo wants the runtime fix prioritised** (he flagged it as "no-brainder, current times are just too long" 2026-05-28 close).

## What this session changed (concrete artefacts)

1. **`ml_pipeline/training/label_swing_types.py`** (NEW commit 1 `06080a0`, ~170 LOC). Mirrors `label_ball_positions.py`. Source: `bronze.player_swing` filtered to `{fh, 1h_bh, 2h_bh, fh_overhead}`. Canonical 3-class output `{forehand, backhand, overhead}` per ADR-02 Q3. Preserves `swing_type_raw`, `is_serve`, `confidence_swing_type`. Role from court half (court_y > 11.885 → NEAR).
2. **`ml_pipeline/training/label_serves.py`** (NEW commit 2, ~190 LOC). Source: `bronze.player_swing WHERE serve = TRUE`. Output: `hit_frame`, `hit_ts`, `player_id`, `court_x/y`, `role`, `swing_type_raw`, `ball_speed` (~50% coverage), `bounce_court_x/y` (~0% coverage — empirical finding: SA doesn't populate `ball_impact_location_x/y` on serve rows today; nullable fields preserved for future SA versions), `confidence`, `confidence_swing_type`.
3. **`upload_app.py::_label_one_kind` (NEW helper, commit 1)** + **`_label_pair_now` (refactored across both commits)** — per-kind idempotency/export/upload/insert extracted into reusable helper; `_label_pair_now` now invokes it THREE times (ball_position, stroke_classifier, serve) with independent idempotency. Back-compat: top-level `label_count`/`label_s3_uri` mirror first newly-labeled kind; per-kind detail under `result['kinds']`.
4. **`upload_app.py::/ops/backfill-pair-labels`** — eligibility query upgraded to a `known_kinds(label_kind) VALUES (...)` CTE; pair eligible if missing AT LEAST ONE known kind. Now includes 'ball_position', 'stroke_classifier', 'serve'. Adding a future label_kind = update the VALUES list + add a fourth `_label_one_kind` call.
5. **3 backfilled `stroke_classifier` corpus rows** (commit 1) — 78c32f53 (94), c645a7ee (341), 9378f2dd (340). 775 total. `s3://nextpoint-prod-uploads/training/labels/{t5}_swing_types.json`.
6. **3 backfilled `serve` corpus rows** (commit 2) — 78c32f53 (25), c645a7ee (46), 9378f2dd (47). 118 total. `s3://nextpoint-prod-uploads/training/labels/{t5}_serves.json`.

## Coordination protocol (per ADR-05) — non-negotiable, unchanged

1. No agent starts a detector build without an APPROVED ADR.
2. Each detector build has its own commit scope.
3. **Corpus extension lands in the same commit as the detector model it feeds.** (Both extractors this session are Step 2 of the prescribed sequence, ahead of their respective model trainings — rule-compliant.)
4. Each detector ships with a bench.
5. This pickup file gets updated at every detector ship.

## Commits this session
- `06080a0` `feat(t5): ADR-02 swing-type corpus extractor + dual-kind _label_pair_now`
- `(this commit)` `feat(t5): Stream 3 serve corpus extractor + 3rd _label_one_kind` — `label_serves.py` + serve emission wired + known_kinds extended + backfill of 3 pairs + pickup update.
- *(Tomo's parallel doc landed mid-session)* `cb4e449` `docs(investigation): batch optimisation plan — 4.79h → ~1h target via player-stage batching + ROI batching/FP16 + NVENC`

## Memory ceiling reference (unchanged from previous session)

| Stage | Peak heap |
|---|---|
| Bronze ingest (streaming ijson) | ~15 MB |
| serve_detector | ~75 MB |
| stroke_detector | ~53 MB |
| silver build | ~79 MB |

Render's 512MB main API has comfortable headroom. End-to-end ingest 44-min match: 3 min 39 sec.

## Runtime ceiling reference (NEW 2026-05-28 — the next inflection point after memory)

| Phase | Wall time (44-min match, ~67k frames) | Bottleneck |
|---|---|---|
| AWS Batch (court + ball + player + ROI + serialisation) | **~4.79 h** | YOLOv8x-pose @ 1280 + SAHI tile-fan, `batch=1` every 5th frame (~75-85% of wall) |
| Render ingest (bronze re-ingest + detectors + silver + AUTO_LABEL hook) | ~3 min 39 sec | Solved this session |
| **Target** | **<1 h** | Plan: `docs/_investigation/batch_optimisation_plan.md` |

Empirical proof the bottleneck is player stage (not ball): tonight's commit `5317c50` added GPU batching for the WASB ball tracker; ms/frame moved from ~183.3 to ~183.1. Ball isn't on the critical path. `BALL_BATCH_SIZE=8` IS live in both regions' active job-defs (eu rev 53 — ca475740 confirmed running on it — and us rev 35). The 183 ms baseline is post-ball-batching. Player batching is the only remaining lever in the same template.

## Read in this order
0. **`docs/north_star.md` §"★ RULES OF THE GAME"** — non-negotiable, every session, before this file.
1. This file (next_session_pickup.md).
2. `docs/north_star.md` §"Current detector build queue (2026-05-28)" — current statuses.
3. `.claude/adr01_label_audit_2026-05-28.md` — corpus state diagnosis (the *why* behind floor-label scarcity + the negative mining recipe). Read if doing Task (A) ADR-01 work.
4. `docs/_investigation/adr_02_swing_type_classifier_plan.md` §"Build spec v1" — model architecture spec. Read if doing ADR-02 training work.
5. `docs/_investigation/adr_03_identity_model.md` §"v1 finding" — tracker-binding pattern. Read if doing any rule+visual work.
6. **`docs/_investigation/batch_optimisation_plan.md`** — Batch GPU inference speedup roadmap (per-stage profile + 7 ranked levers + daylight sequence). Read if doing Task (F) Batch runtime work.

## Suggested opening prompt for next chat (paste verbatim — pick A or B based on intent)

**Option A — corpus / detector training (default):**
```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's task: check the corpus state first (single SQL query — should be
4 rows × 3 kinds if Corpus 4 landed). If yes → ADR-01 v1 training (Task A).
If not yet → close out + suggest Task F (Batch runtime) for the next available
daylight session.
```

**Option B — Batch runtime optimisation (Task F, daylight only):**
```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's task: land L1 + L4 + L5 from docs/_investigation/batch_optimisation_plan.md
against the T5 Batch pipeline. Read the plan first. One commit per lever.
After all three are in, run BATCH-SIDE CHECKLIST end to end (Docker rebuild,
ECR push to eu-north-1 + us-east-1, job-def revisions in both regions). Then
submit ONE test match and confirm ms/frame dropped from ~183 without breaking
the serve bench (a798eff0=20/24, 880dff02=23/24) or bench_ball. Estimated ~5h
session; do not start unless Tomo is awake for the test-submission window.
```

## Suggested task plan for next session

```
1. BOOT (5 min)
   - Read RULES OF THE GAME + this pickup
   - git log --oneline -10
   - Run serve bench — confirm a798eff0=20/24, 880dff02=23/24
   - Acknowledge in one sentence

2. CORPUS STATE CHECK (2 min)
   SELECT label_kind, COUNT(*) AS rows, SUM(label_count) AS total
     FROM ml_analysis.training_corpus
    GROUP BY 1 ORDER BY 1;
   Expected post-Corpus-4 (3 kinds × 4 pairs):
     ball_position 4 / ~1483    stroke_classifier 4 / ~1172    serve 4 / ~232
   - If 4 rows of each → Corpus 4 landed → Task A unblocked.
   - If only 3 rows each → Corpus 4 still pending → close out + recommend Task F.

3a. TASK A: ADR-01 v1 TRAINING (~2-3 hrs, only if Corpus 4 landed)
   - Spec: docs/_investigation/adr_01_bounce_model_architecture.md §"Build spec v1"
   - Context: .claude/adr01_label_audit_2026-05-28.md
   - Pull positives from bronze.ball_bounce type='floor' for the 3-4 Tomo-Rivonia SA tasks
   - Negative mining script (~30 min — recipe in label_audit doc)
   - Train PyTorch loop, AdamW lr=1e-4, label smoothing 0.1
   - Save weights to ml_pipeline/models/bounce_detector_v1.pt
   - Update bounce_detector/cnn.py weight-loading path
   - Remove STOPGAP threshold 1.1; restore to 0.55 per ADR spec
   - Re-run bench_bounce → lock baseline in bench_baseline_bounce.json
   - Wire detect_bounces() into upload_app.py::_do_ingest_t5() after serve_detector

3b. TASK F: BATCH RUNTIME OPTIMISATION (~5 hrs, daylight only)
   - Read: docs/_investigation/batch_optimisation_plan.md (entire doc)
   - Levers L1 + L4 + L5 (~1.7-2.2× speedup): player-stage batching + ROI batching + NVENC
   - Optional L3 (FP16 YOLO weights) to land near or under 1h
   - DOCKER REBUILD + DUAL-REGION ECR PUSH + new job-def revisions in eu-north-1 + us-east-1
   - One test submission post-deploy to confirm ms/frame drop in CloudWatch
   - Quality gates: serve bench.yml + local bench_ball MUST both stay green

4. CLOSE-OUT
   - Update next_session_pickup.md
   - Commit + push
   - 2-line summary

CONSTRAINTS:
- Don't touch parallel-agent files: serve_detector/, stroke_detector/,
  build_silver_match_t5.py, ball_tracker.py, wasb_*.py, roi_extractors/,
  or any file in BATCH-SIDE CHECKLIST per rule #8 (unless doing Task F,
  which explicitly trips it).
- No pytest. No `?key=` query-string auth. Pull-rebase before push.
- Always commit to main (no feature branches).
```

## Scratch
None. All output went into committed code + docs.
