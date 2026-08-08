# `ingest_quality/` — post-analysis sanity gate

One question: **did SportAI actually analyse this match?**

`VIDEO_QUALITY_CHECK_ENABLED` gates the video *before* analysis. Nothing checked
what came back — and a failed SportAI analysis returns **HTTP 200 with a
well-formed but empty payload**. So the ingest "succeeded", silver wrote nothing,
and the match still showed as `completed` with 0 points in the customer's
sidebar. Two real matches hit this (`42280d38`, `6abd37ca`, both 2026-08-08).

Full evidence: `docs/_investigation/empty_analysis_and_ingest_memory_2026-08-08.md`.

## Where it runs

`ingest_worker_app._do_ingest`, **STEP 1c** — after the raw archive (so a
rejected payload is kept for diagnosis), before bronze.

On reject it stamps `last_status='failed'` + `ingest_error`, emails ops, and
returns **before** bronze, silver, trim, the billing sync (STEP 5) and the
customer notify (STEP 6). An unanalysable match is neither billed nor announced.

## What it rejects — and what it deliberately doesn't

**REJECT** on either structural, threshold-free fact:

1. **0 valid swings** (with swings present) — the direct cause. SportAI validates
   a swing against ball proximity, so when ball tracking collapses it flags every
   swing invalid; `build_silver_v2._resolve_two_players` counts distinct
   `player_id` over `valid IS TRUE` only, finds 0, and raises "Cannot resolve 2
   players". Measured across all 10 archived payloads, **valid swings == silver
   rows exactly**.
2. **0 rallies AND 0 floor bounces** — no point structure to derive from.

Validated against all 10 real payloads: **rejects iff the match produced zero
silver rows.**

Three axes were considered and rejected — **don't reintroduce them**:

| axis | why not |
|---|---|
| `ball_positions` | The best-separating *number* (usable ≥ 5,591; broken 0 and 496) — but an 11× gap fitted to 10 samples, and a count threshold on a 10-min match ≠ a 2-hour one. `valid == 0` is the same evidence with no knob. |
| rallies per minute | `df594aea` reports 9 rallies over 74 min yet has 611 silver rows and **100 hand-verified points**. A rate rule rejects a match we hold ground truth for. |
| codec (HEVC) | **2 of the 4 HEVC matches analysed fine** (155 and 154 silver rows). Both failures happened to be HEVC, but at n=2 that is a weak risk signal, not a cause. An earlier version of this file claimed "every HEVC upload was broken" — it was built on `n_rallies`, which doesn't track usability. Warning only. |

**WARN** (still ingests): suspect codec, `final` confidence below
`INGEST_SANITY_MIN_FINAL_CONF`, or > 10 players (phantom storm — the shape that
OOM-killed the 512 MB worker).

## Fails open, by design

It only judges a payload it *recognises*. `"rallies": []` (SportAI ran, found
none) and a **missing** `rallies` key (a shape we don't understand) are
distinguished — conflating them would mean a SportAI key rename rejects every
match on the platform at once. `assess()` also catches everything and returns an
OK verdict on error: a bug here must never be able to fail an ingest.

## Env

| var | default | meaning |
|---|---|---|
| `INGEST_SANITY_GATE_ENABLED` | `1` | `0` = assess and log, never reject (rollback, no deploy) |
| `INGEST_SANITY_MIN_FINAL_CONF` | `0.55` | warn-only threshold; sits inside the observed h264/hevc gap (0.526 → 0.618) |
| `INGEST_SANITY_SUSPECT_CODECS` | `hevc,h265` | warn-only codec list |

## Check

```bash
.venv/Scripts/python -m ingest_quality.tests.test_gate
```

Pure logic — no DB, no AWS, no deps, instant. Fixtures encode the shapes measured
across all 10 real payloads, including `df594aea` as an explicit must-not-reject.
Run it after touching this package.
