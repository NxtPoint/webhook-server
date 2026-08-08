# `ingest_quality/` — post-analysis sanity gate

One question: **did SportAI actually analyse this match?**

`VIDEO_QUALITY_CHECK_ENABLED` gates the video *before* analysis. Nothing checked
what came back — and a failed SportAI analysis returns **HTTP 200 with a
well-formed but empty payload**. So the ingest "succeeded", silver got nothing,
the customer was emailed a ready dashboard with no data, and a credit was
consumed. Two real matches hit this (`42280d38`, `6abd37ca`).

Full evidence: `docs/_investigation/sportai_hevc_and_ingest_memory_2026-08-08.md`.

## Where it runs

`ingest_worker_app._do_ingest`, **STEP 1c** — after the raw archive (so a
rejected payload is kept for diagnosis), before bronze.

On reject it stamps `last_status='failed'` + `ingest_error`, emails ops, and
returns **before** bronze, silver, trim, the billing sync (STEP 5) and the
customer notify (STEP 6). An unanalysable match is neither billed nor announced.

## What it rejects — and what it deliberately doesn't

**REJECT** on one unambiguous signal: **0 rallies AND 0 floor bounces** — no
point structure for silver to derive from. Not a tuned threshold; a structural
fact. Healthy matches measured ≥ 9 rallies and ≥ 162 bounces; the two rejects
have 0/0.

Three axes were considered and rejected — **don't reintroduce them**:

| axis | why not |
|---|---|
| `ball_positions == 0` | `6abd37ca` has **496** ball positions with 0 rallies and 0 bounces — just as empty. A ball rule misses it. |
| rallies per minute | `df594aea` reports 9 rallies over 74 min yet has **100 hand-verified points**. A rate rule rejects a match we hold ground truth for. |
| codec (HEVC) | HEVC is the *cause* (4/4 broken vs 6/6 h264) but the decision is made on the **output**, so a SportAI fix needs no change here. Warning only. |

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
