# Next-session pickup — 2026-05-28 (late evening) — 3-stream wrap-up landed

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" + §"Current detector build queue (2026-05-28)".

**Date:** 2026-05-28 (late evening)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN throughout. Identity bench `100.0%` agreement (n=14 ITF-expected changeovers).

**This session delivered 3 streams + parallel-agent coordination:**

### Stream A — ADR-03 identity rule patched to v1 SHIPPED (`5c5cfe0`)
Tracker-binding-aware decision matrix. **0% → 100% bench**. Tennis rules are deterministic; visual dual-cross is the CHECK, not the source. ADR-03 status flipped to v1 SHIPPED.

### Stream B — ADR-01 label audit (the 67 Match-1 floor labels)
Cross-checked against T5 `ball_detections`. Median pixel error **80 px** (acceptable). 95% coverage in ±5-frame window; 53% strong-positive agreement (within 50 px). Current T5 `is_bounce` heuristic recall: 9% — large training headroom. Full receipts: `.claude/adr01_label_audit_2026-05-28.md`.

### Stream C — Why c645a7ee had 0 floor labels (NOT an extractor bug)
**Floor-label scarcity is an SA-side LOCATION pattern.** All 3 Tomo-at-Rivonia matches emit floor labels (411 total: 67 + 67 + 277). All 3 non-Rivonia matches (Erin/ccj, Dejan/ccj, Erin/Bloem) emit 0. SA's pipeline appears to recognize Rivonia's court setup and only enables floor-bounce detection there. **Bulk-loading from other courts will NOT add floor labels** — the morning pickup's "bulk-load to grow corpus" advice was wrong for floor data specifically.

**Hidden upside:** 2 Tomo-Rivonia SA matches (`0fa94cf6` = 277 floor labels, `2c1ad953` = 67) **exist in SA bronze but have no T5 partner.** Tomo re-submitting them to T5 → auto-corpus → instant 6× uplift (67 → 411) without any new recording.

## ⚠️ Three things Tomo can do to unblock ADR-01 training

| What | Why | Cost |
|---|---|---|
| **(1) Re-submit `0fa94cf6` video to T5** | Adds 277 floor labels to corpus (4× current). Largest single lever. | One upload via Media Room |
| **(2) Re-submit `2c1ad953` video to T5** | Adds 67 floor labels. | One upload |
| **(3) Investigate why SA only emits floor at Rivonia** | If we can get ccj + Bloem courts producing floor data, the auto-corpus suddenly works for all uploads. Out of our pipeline — but knowable. | SA-side investigation; talk to SportAI? |

## Next session's job — pick ONE (parallel-safe choices marked ★)

- **★(A) ADR-02 swing-type corpus extractor** — write `ml_pipeline/training/label_swing_types.py` for `label_kind='stroke_classifier'`. Mirror `label_ball_positions.py`. Source: `bronze.player_swing.swing_type`. Wire into `_label_pair_now()`. ~150 LOC + 1-2 hook lines. Unblocks swing-type training data accumulation. Parallel-safe.
- **★(B) ADR-01 v1 training** — IFF Tomo has re-submitted at least 1 of the unpaired Rivonia matches AND the corpus extension has landed for that pair. Negative-mining recipe in `.claude/adr01_label_audit_2026-05-28.md`. Train, lock baseline in `bench_baseline_bounce.json`, ship. Parallel-safe with (A).
- **★(C) Serve corpus extractor** — Stream 3, independent of everything. `label_kind='serve'`. Parallel-safe.
- (D) ADR-04 volley analytic — STILL blocked on ADR-01 v1 (real bounce events) + ADR-02 v1 (swing_type column).
- (E) Identity v2 (OSNet CNN) — wait until corpus has identity labels accumulating; not urgent now that v1 hits 100%.

**Recommended:** if Tomo re-submits 0fa94cf6 + 2c1ad953 this week → (B) is highest leverage. If not → (A) keeps the training queue moving while waiting.

## Commits this session
- `f2c4258` CLAUDE.md streamlined 23%
- `9b19e0f` 5 ADRs APPROVED + research-grounded specs
- `6154de9` ADR-01 v0 + ADR-03 v1 scaffold (with parallel-agent constraints respected)
- `5c5cfe0` ADR-03 ITF-default patch (0% → 100% bench)
- (current commit landing now: ADR-01 label-audit doc + ADR-01 training-data section updated + this pickup overwrite)

## Architecture status — current snapshot

| Field | Producer | Status |
|---|---|---|
| serve | `serve_detector` | DEV CEILING; T5 silver inherits via `T5_SERVE_FROM_EVENTS` |
| **bounce** | **`bounce_detector` v0** | SCAFFOLDED (untrained); 411 floor labels accessible once 2 SA tasks T5-paired |
| swing_type | stroke_classifier (scaffold, no weights) | BLOCKED on corpus extractor |
| **identity** | **`identity_detector` v1** | **SHIPPED 100% bench** (tracker-binding-aware) |
| volley | (analytic — ADR-04) | BLOCKED on bounce + swing_type |

## Coordination protocol (per ADR-05) — non-negotiable
1. No agent starts a detector build without an APPROVED ADR.
2. Each detector build has its own branch / commit scope.
3. **Corpus extension lands in the same commit as the detector model it feeds.**
4. Each detector ships with a bench.
5. This pickup file gets updated at every detector ship.

## Memory ceiling reference (post-Stream-A of morning session)

| Stage | Peak heap |
|---|---|
| Bronze ingest (streaming ijson) | ~15 MB |
| serve_detector | ~75 MB |
| stroke_detector | ~53 MB |
| silver build | ~79 MB |

Render's 512MB main API has comfortable headroom. End-to-end ingest 44-min match: 3 min 39 sec.

## Read in this order
1. This file.
2. `.claude/adr01_label_audit_2026-05-28.md` — full Stream B + C receipts.
3. `docs/_investigation/adr_03_identity_model.md` (now v1 SHIPPED) — for context on the ITF-default win.
4. `docs/north_star.md` §"Current detector build queue (2026-05-28)" — updated statuses.
5. Whichever ADR (01-04) you're about to advance.

## Scratch
None — all output went into committed code + docs.
