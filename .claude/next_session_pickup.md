# Next-session pickup — 2026-05-28 (session close) — corpus runway open, real status of all 5 facts

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (close)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. Identity `100%` (n=14 ITF boundaries).

## Honest status of the 5 facts (corrected from earlier in the session)

The 5-analysis-model picture is more like **2 of 5 at dev ceiling**, NOT 4 of 5. Be precise:

| Fact | Status | Honest read |
|---|---|---|
| serve | **DEV CEILING** ✅ | Confirmed last week. Receiver-FP is the far-court ceiling. Training is next. |
| bounce (ADR-01) | **v0 SCAFFOLDED — UNTRAINED** | CNN architecture + module + schema + bench wired. No weights. STOPGAP threshold 1.1 hard-clamps "no row written" until weights load. **Not at dev ceiling — it's plumbing.** |
| swing_type (ADR-02) | **Not built** | Only an ADR spec. No module, no scaffold. Pre-existing `stroke_classifier/` directory is unrelated untrained scaffolding. |
| identity (ADR-03) | **v1 SHIPPED at 100% bench** ✅ | This IS dev ceiling — but it's a rule, not a model. 100% because tennis rules are deterministic. v2 OSNet remains the planned upgrade. |
| volley (ADR-04) | **Not built — by design** | Pure analytic, ~30 LOC. Blocked on bounce + swing_type being real. |

## Corpus state (post-Corpus 4)

Corpus 4 just landed on the SA side and confirmed Tomo's hypothesis that the floor-bounce gap was a **recording setup issue**, NOT a court/venue issue.

| Corpus task | T5 | SA | Duration | Floor labels | Swing labels |
|---|---|---|---|---|---|
| Match 1 | 78c32f53 | 0d0514df | 8.5 min | **67** | 94 |
| Match 2 | c645a7ee | ee12d918 | 37 min | 0 (bad recording) | 327 |
| Match 3 | 9378f2dd | 2f355924 | 37 min | 0 (bad recording) | 331 |
| **Corpus 4** (T5 still running) | ca475740 | **3922af92** | TBD | **273** ✅ | 391 |

**Corpus 4 status:** SA done 2026-05-28 12:03:53. T5 (`ca475740-9e34-49c3-9b59-0194bfa37013`) still running. Pair will auto-land via `_label_pair_now()` hook once T5 completes. **No manual action needed.**

**Tomo's call (2026-05-28 close):** Matches 2 + 3 are scrap for floor-bounce training (bad recording setup). Useful videos = Match 1 + Corpus 4 only. Swing-label data from all 4 stays valid for future swing-type training.

**Two unpaired SA matches still available** for re-submit if Tomo wants to grow the floor-bounce training set further:
- `0fa94cf6-7cdd-4a8f-9bf9-c603ce31e872` — 277 floor labels (Tomo / Rivonia / 2026-05-23)
- `2c1ad953-b65b-41b4-9999-975964ff92e1` — 67 floor labels (Tomo / Rivonia / 2026-04-27)

Combined with Match 1 (67) + Corpus 4 (273), maxing the floor corpus would give **~684 floor labels across 4 tasks** — comfortable v1 training base.

## Honest re-ordered roadmap

1. **Wait for Corpus 4 T5 (`ca475740`) to finish** — floor corpus jumps 67 → ~340 automatically.
2. **(Optional, Tomo's call)** Re-submit `0fa94cf6` + `2c1ad953` → ~684 floor labels.
3. **Build ADR-02 corpus extractor** for `label_kind='stroke_classifier'` — `ml_pipeline/training/label_swing_types.py` + 1-2 lines in `_label_pair_now()`. Unblocks all future swing-type accumulation. ~150 LOC.
4. **Train ADR-01 bounce_detector v1** on accumulated floor labels. Negative mining recipe + label-audit findings: `.claude/adr01_label_audit_2026-05-28.md`. Lock baseline in `bench_baseline_bounce.json`. Ship.
5. **Accumulate swing-type corpus** (a few weeks of normal uploads), then train ADR-02 R(2+1)D-18. Ship.
6. **ADR-04 volley analytic drops out** — ~30 lines once bounce + swing-type are real.
7. *(Maybe)* upgrade ADR-03 to v2 OSNet later — only if 100% rule-based v1 turns out to have problematic edge cases in production.

## Next session's job — pick ONE (parallel-safe ★)

- **★(A) ADR-02 swing-type corpus extractor** (~150 LOC + hook lines). Parallel-safe with any other work. Single biggest forward move because it unblocks ALL swing-type training. 1,143 swing labels across 4 matches already waiting.
- **★(B) ADR-01 v1 training** — IFF Corpus 4 (and ideally the 2 unpaired matches) have landed in the corpus. Negative mining + train + lock bench baseline + ship.
- **★(C) Serve corpus extractor** for `label_kind='serve'`. Independent Stream 3, parallel-safe.
- (D) ADR-04 volley analytic — still blocked on (B).
- (E) Identity v2 OSNet — not urgent.

**Recommended:** start with (A) because it doesn't depend on anything else and unblocks the swing-type future. Run (B) in parallel if Corpus 4 has landed.

## Commits this session (5)
- `f2c4258` CLAUDE.md streamlined 23% (431→360 lines)
- `9b19e0f` 5 ADRs APPROVED + research-grounded specs
- `6154de9` ADR-01 v0 + ADR-03 v1 scaffold (with parallel-agent constraints)
- `5c5cfe0` ADR-03 ITF-default patch (0% → 100% bench)
- `0cd9787` ADR-01 label audit + corpus floor-coverage diagnosis (Streams B + C)
- *(this commit)* session close — pickup overwrite + memory entries

## Coordination protocol (per ADR-05) — non-negotiable
1. No agent starts a detector build without an APPROVED ADR.
2. Each detector build has its own commit scope.
3. **Corpus extension lands in the same commit as the detector model it feeds.**
4. Each detector ships with a bench.
5. This pickup file gets updated at every detector ship.

## Memory ceiling reference (post corpus-auto-land productionisation)

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
3. `.claude/adr01_label_audit_2026-05-28.md` — Stream B + C diagnosis (the *why* behind the corpus state). Read if doing ADR-01 work.
4. `docs/_investigation/adr_03_identity_model.md` §"v1 finding" — tracker-binding pattern. Read if doing any rule+visual work.
5. Whichever ADR (01-04) you're about to advance — that's the spec for the task you've claimed.

## Suggested opening prompt for next chat (paste verbatim)

```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything.

Today's task: ADR-02 swing-type corpus extractor (Task A in the pickup).
After it lands, if Corpus 4 (ca475740) has shown up in training_corpus,
optionally tackle ADR-01 v1 training (Task B). Otherwise commit + close out.
```

## Suggested task plan for next session

```
1. BOOT (5 min)
   - Read RULES OF THE GAME + this pickup
   - git log --oneline -10
   - Run serve bench — confirm a798eff0=20/24, 880dff02=23/24
   - Acknowledge in one sentence

2. VERIFY CORPUS 4 LANDED (2 min)
   - Query training_corpus for t5_task_id='ca475740-9e34-49c3-9b59-0194bfa37013'
   - If landed → Task B is unblocked (parallel option after A)
   - If not landed → check ml_analysis.video_analysis_jobs status; no action

3. TASK A: ADR-02 SWING-TYPE CORPUS EXTRACTOR (~2-3 hours)
   - Spec: docs/_investigation/adr_02_swing_type_classifier_plan.md
   - Template: ml_pipeline/training/label_ball_positions.py (mirror the shape)
   - Build: ml_pipeline/training/label_swing_types.py
     • Source: bronze.player_swing where swing_type is not null
     • Output JSON schema: matches build_serve_bounce_dataset.py
     • Constants: DEFAULT_INCLUDE_TYPES = ('forehand', 'backhand', 'overhead')
   - Wire: upload_app.py::_label_pair_now() — add a second corpus row
     emission for label_kind='stroke_classifier' alongside ball_position
   - Backfill: one-off script for all 4 existing dual-submit pairs
     (78c32f53, c645a7ee, 9378f2dd, ca475740 if landed)
   - Verify: new training_corpus rows with label_kind='stroke_classifier'
     and ~1,143 total swing labels across matches
   - Bench: NONE for v0 (extractor, not model). Serve bench must stay green.

4. OPTIONAL TASK B: ADR-01 V1 TRAINING (~2 hours, IFF Corpus 4 landed)
   - Spec: docs/_investigation/adr_01_bounce_model_architecture.md §"Build spec v1"
   - Context: .claude/adr01_label_audit_2026-05-28.md (audit + negative
     mining recipe)
   - Audit Corpus 4's 273 floor labels (~15 min DB query)
   - Negative mining script (~30 min)
   - Training: PyTorch loop, AdamW lr=1e-4, label smoothing 0.1, early
     stop on val macro-F1. Save to ml_pipeline/models/bounce_detector_v1.pt
   - Update bounce_detector/cnn.py to load weights; remove STOPGAP threshold
     1.1 → restore to 0.55 per ADR spec
   - Re-run bench_bounce; lock baseline in bench_baseline_bounce.json
   - Wire detect_bounces() into upload_app.py::_do_ingest_t5() after
     serve_detector

5. CLOSE-OUT
   - Update next_session_pickup.md
   - Commit + push
   - 2-line summary

CONSTRAINTS:
- Don't touch parallel-agent files: serve_detector/, stroke_detector/,
  build_silver_match_t5.py, ball_tracker.py, wasb_*.py, roi_extractors/,
  or any file in BATCH-SIDE CHECKLIST per rule #8.
- No pytest. No `?key=` query-string auth. Pull-rebase before push.
```

## Scratch
None. All output went into committed code + docs.
