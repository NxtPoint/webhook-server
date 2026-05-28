# ADR-05: Detector-model build sequencing

**Status:** DRAFT — awaits Tomo approval. Once approved, becomes the **single ordered roadmap** every future session executes against. North star + CLAUDE.md will reference this ADR.
**Owner:** Tomo decides; any agent works to the queue.
**Last updated:** 2026-05-28.

## Context

[ADRs 01-04](./) define **what** detector models to build. This ADR defines **what order** to build them in, why, and what the corpus + measurement work in parallel looks like.

The drivers:
- **Build-first / train-LAST** ([north_star.md §"OVERARCHING GOAL"](../north_star.md)) — build a model at heuristic / standard-model floor first; train to ceiling later via dual-submit corpus.
- **One model per fact** (north_star rule #2) — each detector lives in its own module, has its own bench, deploys independently.
- **Corpus auto-accumulates** — the parallel agent's work landing video #3 + the serve-corpus dark-launch hook means training data piles up for free as long as we extend the corpus extractor per new model.
- **Far-court ceiling** ([north_star.md line 56](../north_star.md)) — four fields (serve precision, bounce, far-stroke, identity) share a single root cause: far-player sparse signal. Bounce is the lever that lifts the other three indirectly.

## Dependency graph

```
ADR-01 (bounce)     ─────────────┐
ADR-02 (swing-type) ─────────────┤
                                 ↓
ADR-03 (identity)   independent  ADR-04 (volley analytic)
                                 ↑
                                 │ needs bounce + swing-type
```

- ADR-01 and ADR-02 are independent — can be parallelised (two agents).
- ADR-03 is independent of all others — can run in parallel with either.
- ADR-04 is strictly last — needs both ADR-01 and ADR-02.
- Serve training infra (option A from current pickup) is independent — corpus extension + retrain `serve_detector`. Can run any time.

## Leverage ranking

| Build | Direct impact | Indirect impact | Tractability |
|---|---|---|---|
| **ADR-01 bounce model** | Fixes 4.57 m bounce error; raises recall 55→target 80%+, precision 27→80%+ | Lifts serve precision, far-stroke, and is the input to ADR-04 (volley) | Corpus has 488 ball_position labels already; can train soon |
| **ADR-02 swing-type classifier** | Fixes backhand over-count (T5 28 vs SA 18); enables accurate volley derivation | Cleans up the silver pose-inference STOPGAP (deletion-ready once trained) | Needs new `label_kind='stroke_classifier'` extractor (~150 LOC); training data accumulates as matches land |
| **ADR-03 identity rule** | Unlocks A/B across changeovers; unblocks `T5_STROKE_DRIVEN_SILVER` gate | Enables per-player dashboards across sets | No training needed; pure rule first; ~1-2 day build |
| **ADR-04 volley analytic** | Fixes 2× over-count | Cleans up the silver heuristic | Trivial (~30-line algorithm) but blocked on ADR-01 + ADR-02 |
| Serve training infra | Move serve from dev-ceiling to ~90-95% | Lifts far-serve recall | New `label_kind='serve'` extractor + train serve_detector |

## Proposed sequence

### Stream 1 (perception models, sequential):

**Step 1 — ADR-01: Bounce model** (start immediately after approval)
- Why first: corpus is ready; highest leverage (multi-effect); the parallel agent's memory-fix patch lands the streaming pattern we need anyway.
- Done-when: `ml_pipeline/bounce_detector/` shipped; `ml_analysis.ball_bounces` table populated; `bench_bounce` harness green against a Match 1 fixture; T5 bounce recall ≥ 75% / precision ≥ 75% on Match 1.

**Step 2 — ADR-02: Swing-type classifier** (start after Step 1 lands OR in parallel as Stream 2 if a second agent is available)
- Add the corpus extractor first (no training data without it).
- Done-when: corpus accumulates ≥ 10 matches of `stroke_classifier` labels → train v1 → `ml_analysis.stroke_events` carries `swing_type` + `swing_type_confidence` → silver pose-inference STOPGAP can be deleted.

**Step 3 — ADR-04: Volley analytic** (only after Steps 1+2 land)
- ~30-line algorithm; trivial once inputs exist.
- Done-when: `stroke_events.volley_flag` populated; bench agreement with SA on Match 1 ≥ 90%; old heuristic deprecated.

### Stream 2 (independent, parallel-safe):

**Step A — ADR-03: Identity rule** (start any time)
- No corpus extension needed for v1 rule; can run in parallel with Stream 1.
- Done-when: `ml_analysis.player_identity_segments` populated; rule-based correctness ≥ 90% on Match 1 / Match 2.

### Stream 3 (training infra, independent):

**Step T1 — Serve corpus extractor + serve_detector retrain** (start any time)
- Write the corpus extractor for `label_kind='serve'`; once ≥ 10 matches accumulated, train serve_detector v2 with the receiver-FP suppression learned from data.
- Done-when: serve_detector v2 ships; bench ≥ 22/24 on a798eff0 (current 20/24).

## Roadmap-as-a-timeline

A possible execution shape with 1 agent (sequential) vs 2 agents (parallel):

**1 agent, fully sequential:**
```
ADR-01 → ADR-02 → ADR-04 → ADR-03 → Serve training
   ↓        ↓        ↓        ↓           ↓
(weeks)   (weeks)  (days)  (days)    (parallel-able)
```

**2 agents, parallel where possible:**
```
Agent A:  ADR-01 → ADR-02 → ADR-04
Agent B:  ADR-03 →  Serve training infra
```

**Best case (2 agents + corpus pipeline keeps auto-accumulating):**
- Week 1: ADR-01 bounce model lands; ADR-03 identity rule lands.
- Week 2: ADR-02 swing-type classifier extractor in place; corpus auto-accumulating; ADR-03 identity CNN training starts.
- Week 4-6: Enough corpus for swing-type training; v1 ships.
- Week 6: ADR-04 volley analytic falls out trivially.
- Concurrent: serve training extractor lands; ~Week 8-10 enough data to retrain serve_detector v2.

## Coordination protocol

To avoid the patchiness Tomo flagged:

1. **No agent starts a detector build without an APPROVED ADR.** Status field at the top of each ADR must read `APPROVED YYYY-MM-DD`.
2. **Each detector build has its own branch / commits scope.** No agent touches another's module in the same session unless coordinating explicitly via `.claude/next_session_pickup.md`.
3. **Corpus extension lands in the same commit as the detector model it feeds.** Never ship a model without a corpus extractor for its training data.
4. **Each detector ships with a bench.** `bench_bounce`, `bench_swing_type`, `bench_identity`. Local-only initially; promote to CI once stable.
5. **The handover docs (next_session_pickup, north_star, CLAUDE.md) get updated at every detector ship.** Status field in each ADR rolls forward (`APPROVED YYYY-MM-DD` → `SHIPPED YYYY-MM-DD`).

## Recommendation

**Approve the sequence as drafted.** Execution priority:

1. **Immediately on ADR approval:** I (or any agent) starts ADR-01 (bounce model). The other parallel agent finishes corpus video #3, then picks up ADR-03 (identity rule) as Stream 2.
2. **Once ADR-01 lands:** start ADR-02 (swing-type) — corpus extractor first, then accumulate data, then train.
3. **ADR-04 (volley) deferred until ADR-01 + ADR-02 land** — it falls out for free at that point.
4. **Serve training (Stream 3) parallel-able with any of the above** — pick up whenever an agent has bandwidth.

## Open follow-ups

1. **Should the existing `stroke_detector` (velocity-signal, heuristic) be renamed `stroke_timing_detector`** to make it explicit it only emits *timing* (not type)? Names matter; future agents read names.
2. **Should the new analytics tier (`ml_pipeline/analytics/`) be created now**, even if it only contains `volley_derive.py` initially? Or wait until ADR-04 lands?
3. **`docs/_investigation/` ADR numbering** — these are ADRs 01-05. Reserve 06+ for future architectural decisions. Don't conflate with phase numbers in north_star.

## Cross-references

- [ADR-01](./adr_01_bounce_model_architecture.md) — bounce model.
- [ADR-02](./adr_02_swing_type_classifier_plan.md) — swing-type classifier.
- [ADR-03](./adr_03_identity_model.md) — identity model.
- [ADR-04](./adr_04_volley_model_or_analytic.md) — volley analytic.
- [bronze_silver_18_audit.md](./bronze_silver_18_audit.md) — the audit that motivates all five.
- [north_star.md](../north_star.md) — macro plan; will gain a "Current detector build queue" section pointing at this ADR once approved.
