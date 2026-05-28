# Next-session pickup — 2026-05-28 (session close 2) — ADR-02 corpus extractor SHIPPED, swing-type runway open

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (close 2)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (re-verified post-change). Identity `100%` (n=14 ITF boundaries) unchanged.

**What shipped this session:** ADR-02 swing-type corpus extractor (`ml_pipeline/training/label_swing_types.py`) + dual-kind emission wired into `_label_pair_now()` + backfilled the 3 existing dual-submit pairs. Per ADR-05 Step 2: corpus extractor first, classifier model later when volume accumulates.

**Corpus state post-this-session:**

| Pair | T5 | SA | ball_position | stroke_classifier |
|---|---|---|---|---|
| Match 1 | 78c32f53 | 0d0514df | 161 ✅ | **94 ✅** (NEW) |
| Match 2 | c645a7ee | ee12d918 | 327 ✅ | **341 ✅** (NEW) |
| Match 3 | 9378f2dd | 2f355924 | 331 ✅ | **340 ✅** (NEW) |
| Corpus 4 | ca475740 *(T5 65%)* | 3922af92 ✅ | pending hook | pending hook |
| **Totals (today)** |  |  | **3 rows, 819 labels** | **3 rows, 775 labels** |

**Corpus 4 status:** SA done; T5 (`ca475740-9e34-49c3-9b59-0194bfa37013`) at 65% processing as of 15:13 UTC. When it lands, the deployed `_dual_submit_pair_complete_hook` will atomically emit BOTH kinds (ball_position 273 floor+391 swing, stroke_classifier 397). Verify after deploy via:
```
SELECT label_kind, COUNT(*), SUM(label_count) FROM ml_analysis.training_corpus GROUP BY 1;
```

## Honest status of the 5 facts (post-this-session)

| Fact | Status | Honest read |
|---|---|---|
| serve | **DEV CEILING** ✅ | Unchanged. Training is the next move. Stream 3 (serve corpus extractor) parallel-safe whenever an agent has bandwidth. |
| bounce (ADR-01) | **v0 SCAFFOLDED — UNTRAINED** | Unchanged. STOPGAP threshold 1.1 still active. Corpus has 411 floor labels reachable (67 + 273 once Corpus 4 lands + 71 backfill if Tomo re-submits 2 unpaired matches). |
| swing_type (ADR-02) | **CORPUS EXTRACTOR LIVE 2026-05-28** | 775 swing labels in 3 backfilled rows; ~1,172 once Corpus 4 lands. Below the ~2-3k volume target ADR-02 §"Volume target" wants for v1 training. Needs ~5-10 more matches before train phase. |
| identity (ADR-03) | **v1 SHIPPED at 100% bench** ✅ | Unchanged. v2 OSNet planned for residual. |
| volley (ADR-04) | **Not built — by design** | Blocked on bounce + swing-type. Falls out trivially once those land. |

## Honest re-ordered roadmap

1. **Wait for Corpus 4 T5 (`ca475740`) to finish** — both kinds will auto-land via the deployed hook. Confirm by SQL above.
2. **(Optional, Tomo's call)** Re-submit `0fa94cf6` + `2c1ad953` (the 2 unpaired Tomo-Rivonia SA matches) → adds 411 floor + 488 swing labels once T5-paired. **The same hook fires for new pairs — no manual backfill needed any more.**
3. **Train ADR-01 bounce_detector v1** on accumulated floor labels (411-684 depending on re-submits). Recipe: `.claude/adr01_label_audit_2026-05-28.md`. Lock baseline in `bench_baseline_bounce.json`. Ship + wire into `_do_ingest_t5()` after `serve_detector`.
4. **Accumulate swing-type corpus** (a few weeks of normal uploads). Already at ~1,172 labels; need 2-3k for ADR-02 v1.
5. **Train ADR-02 R(2+1)D-18 swing-type classifier** once corpus is big enough. Spec: `docs/_investigation/adr_02_swing_type_classifier_plan.md` §"Build spec v1".
6. **ADR-04 volley analytic drops out** — ~30 lines once bounce + swing-type are real.
7. *(Maybe)* upgrade ADR-03 to v2 OSNet later — only if 100% rule-based v1 has problematic edge cases.

## Next session's job — pick ONE (parallel-safe ★)

- **★(A) ADR-01 v1 training** — IFF Corpus 4 has landed (verify via SQL above) AND ideally the 2 unpaired Rivonia matches have been re-submitted. ~2 hr. Recipe + label-audit findings in `.claude/adr01_label_audit_2026-05-28.md`.
- **★(B) Stream 3 serve corpus extractor** — clone the exact pattern from this session for `label_kind='serve'`. Source: `bronze.player_swing` where `serve = TRUE` (or `ml_analysis.serve_events` — pick whichever has cleaner ground truth). Independent of bounce/swing. ~1-2 hr (the template is now battle-tested).
- **★(C) ADR-03 v2 OSNet** — not urgent; only if v1 rule edge cases emerge.
- (D) ADR-04 volley analytic — still blocked on (A).
- (E) Hand-label net-cord / racket-hit FPs for ADR-01 bounce_type enum extension — deferred per `.claude/adr01_label_audit_2026-05-28.md` until v1 training tells us pre-gates aren't enough.

**Recommended:** (A) if Corpus 4 has landed by then; (B) otherwise.

## What this session changed (concrete artefacts)

1. **`ml_pipeline/training/label_swing_types.py`** (NEW, ~165 LOC). Mirrors `label_ball_positions.py`. Source: `bronze.player_swing` filtered to `{fh, 1h_bh, 2h_bh, fh_overhead}`. Canonical 3-class output `{forehand, backhand, overhead}` per ADR-02 Q3. Preserves `swing_type_raw`, `is_serve`, `confidence_swing_type`. Role from court half (court_y > 11.885 → NEAR).
2. **`upload_app.py::_label_one_kind` (NEW helper)** + **`_label_pair_now` (refactored)** — extracts the per-kind idempotency/export/upload/insert into a reusable helper; `_label_pair_now` now calls it twice (ball_position + stroke_classifier). Back-compat: top-level `label_count` / `label_s3_uri` mirror first newly-labeled kind. Per-kind detail under `result['kinds']`.
3. **`upload_app.py::/ops/backfill-pair-labels`** — eligibility query upgraded to a `known_kinds(label_kind) VALUES (...)` CTE; pair eligible if missing AT LEAST ONE known kind. Endpoint docstring updated. Adding a new label_kind in future = update the VALUES list in 2 places (the CTE here + the dual emission in `_label_pair_now`).
4. **3 backfilled `stroke_classifier` corpus rows** — 78c32f53 (94 labels), c645a7ee (341), 9378f2dd (340). 775 total. Lives in `s3://nextpoint-prod-uploads/training/labels/{t5}_swing_types.json`.

## Coordination protocol (per ADR-05) — non-negotiable, unchanged

1. No agent starts a detector build without an APPROVED ADR.
2. Each detector build has its own commit scope.
3. **Corpus extension lands in the same commit as the detector model it feeds.** (This session is rule-compliant: extractor is Step 2 of the prescribed sequence, ahead of the model per ADR-05 line 52.)
4. Each detector ships with a bench.
5. This pickup file gets updated at every detector ship.

## Commits this session (1 planned)
- *(this commit)* `feat(t5): ADR-02 swing-type corpus extractor + dual-kind _label_pair_now` — `label_swing_types.py` + helper refactor + backfill of 3 pairs + endpoint eligibility update + pickup overwrite.

## Memory ceiling reference (unchanged from previous session)

| Stage | Peak heap |
|---|---|
| Bronze ingest (streaming ijson) | ~15 MB |
| serve_detector | ~75 MB |
| stroke_detector | ~53 MB |
| silver build | ~79 MB |

Render's 512MB main API has comfortable headroom. End-to-end ingest 44-min match: 3 min 39 sec.

## Read in this order
0. **`docs/north_star.md` §"★ RULES OF THE GAME"** — non-negotiable, every session, before this file.
1. This file (next_session_pickup.md).
2. `docs/north_star.md` §"Current detector build queue (2026-05-28)" — current statuses.
3. `.claude/adr01_label_audit_2026-05-28.md` — corpus state diagnosis (the *why* behind floor-label scarcity + the negative mining recipe). Read if doing ADR-01 work.
4. `docs/_investigation/adr_02_swing_type_classifier_plan.md` §"Build spec v1" — model architecture spec. Read if doing ADR-02 training work.
5. `docs/_investigation/adr_03_identity_model.md` §"v1 finding" — tracker-binding pattern. Read if doing any rule+visual work.

## Suggested opening prompt for next chat (paste verbatim)

```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's task: check the corpus state first (single SQL query). If Corpus 4
(ca475740) landed, do ADR-01 v1 training (Task A in the pickup). If not yet,
do Stream 3 serve corpus extractor (Task B). Either way, commit + close out.
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
   Expected post-Corpus-4: ball_position rows=4 total=1092; stroke_classifier rows=4 total=1172.
   - If matches → Corpus 4 landed → Task A unblocked.
   - If only 3 rows each → Corpus 4 still pending → Task B.

3a. TASK A: ADR-01 v1 TRAINING (~2 hrs, only if Corpus 4 landed)
   - Spec: docs/_investigation/adr_01_bounce_model_architecture.md §"Build spec v1"
   - Context: .claude/adr01_label_audit_2026-05-28.md
   - Negative mining script (~30 min — recipe in label_audit doc)
   - Train PyTorch loop, AdamW lr=1e-4, label smoothing 0.1
   - Save weights to ml_pipeline/models/bounce_detector_v1.pt
   - Update bounce_detector/cnn.py weight-loading path
   - Remove STOPGAP threshold 1.1; restore to 0.55 per ADR spec
   - Re-run bench_bounce → lock baseline in bench_baseline_bounce.json
   - Wire detect_bounces() into upload_app.py::_do_ingest_t5() after serve_detector

3b. TASK B: STREAM 3 SERVE CORPUS EXTRACTOR (~1-2 hrs, if Task A blocked)
   - Template: ml_pipeline/training/label_swing_types.py (THIS session's deliverable; pattern is fresh)
   - Build: ml_pipeline/training/label_serves.py
     • Source: bronze.player_swing WHERE serve = TRUE (cleaner than ml_analysis.serve_events because SA is the teacher)
     • Output fields: hit_frame, hit_ts, player_id, court_x/y, role, confidence
     • Constants: DEFAULT_FRAME_W/H/FPS = 1920/1080/30, HALF_Y = 11.885
   - Wire: upload_app.py::_label_pair_now() — add a third _label_one_kind() call for label_kind='serve'
   - Also extend the known_kinds VALUES list in /ops/backfill-pair-labels eligibility CTE
   - Backfill (script pattern same as this session): 3 existing pairs → serve corpus rows
   - Verify: 3 new training_corpus rows with label_kind='serve' (~25-50 serves per match expected)
   - Bench: NONE for v0 (extractor, not model). Serve bench must stay green.

4. CLOSE-OUT
   - Update next_session_pickup.md
   - Commit + push
   - 2-line summary

CONSTRAINTS:
- Don't touch parallel-agent files: serve_detector/, stroke_detector/,
  build_silver_match_t5.py, ball_tracker.py, wasb_*.py, roi_extractors/,
  or any file in BATCH-SIDE CHECKLIST per rule #8.
- No pytest. No `?key=` query-string auth. Pull-rebase before push.
- Always commit to main (no feature branches).
```

## Scratch
None. All output went into committed code + docs.
