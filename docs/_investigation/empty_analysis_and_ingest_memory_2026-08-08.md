# Empty SportAI analyses + the ingest OOM (2026-08-08)

**Status: DIAGNOSED + GATED.** One alert unwound into three separate faults.

> **★ CORRECTION NOTICE.** The first version of this document claimed *"SportAI
> cannot analyse HEVC — 0/4 hevc broken vs 6/6 h264"*. **That was wrong**, and
> Tomo was right to push back on it. It was built on `meta.n_rallies`, which does
> not track usability: `df594aea` (h264) reports **9** rallies and has **611**
> silver rows. Measured against silver rows instead, **2 of the 4 HEVC matches
> analysed perfectly well**. Codec is a weak risk signal at n=2 failures, not a
> cause. The real discriminator is ball-tracking volume. Do not reinstate a
> codec-based rejection.

Trigger: `Web Service nextpoint-ingest-worker exceeded its memory limit`, and
task `42280d38` (`stoker.neil@gmail.com`) stamped `ingest died repeatedly
(4 attempts) with no terminal state` by the SA orphan sweep's poison-guard.

---

## ★ Finding 1 — an empty analysis is an all-swings-invalid analysis

Measured over **every** payload in `s3://nextpoint-prod-uploads/raw-json/` (10),
scored against the silver rows each actually produced in prod:

| task | codec | ball_positions | swings | **valid swings** | **silver rows** |
|---|---|---|---|---|---|
| 299013b3 | h264 | 62,863 | 576 | 491 | 491 |
| df594aea | h264 | 47,754 | 734 | 611 | 611 |
| e4a74645 | h264 | 34,870 | 647 | 596 | 596 |
| f7223270 | **hevc** | 24,234 | 182 | 155 | **155** |
| 36625d04 | **hevc** | 17,419 | 289 | 154 | **154** |
| 079d2c62 | h264 | 5,821 | 108 | 94 | 94 |
| c8b77210 | h264 | 5,597 | 114 | 100 | 100 |
| 052786b4 | h264 | 5,591 | 112 | 100 | 100 |
| **6abd37ca** | hevc | **496** | 228 | **0** | **0** |
| **42280d38** | hevc | **0** | 458 | **0** | **0** |

**`valid swings` equals silver rows exactly, in all ten** — silver is hit-driven,
one row per valid swing. So the payload predicts the outcome perfectly *before*
bronze.

### The failure chain

1. **Ball tracking collapses** — usable matches have ≥ 5,591 `ball_positions`;
   the two failures have 0 and 496 (an 11× gap).
2. SportAI validates a swing against **ball proximity** (`swing_confidences`
   carries a `ball_nearby` term), so with no ball it flags **every** swing
   `valid: false` — 458 of 458, and 228 of 228.
3. `build_silver_v2._resolve_two_players` counts distinct `player_id` over
   **`valid IS TRUE` only** → finds 0 → raises
   `Cannot resolve 2 players (found 0)`.
4. Silver writes zero rows. Bronze had already succeeded.

### The error message misleads — read it carefully

`6abd37ca` carries **exactly 2 correctly-identified players** and 228 swings.
Nothing at all was wrong with the player IDs. "Cannot resolve 2 players" is an
artefact of the `valid IS TRUE` filter, not a statement about player detection.
Both of us initially misread it — one way (codec), then the other (SportAI
couldn't identify the players).

### On the codec

| codec | n | broken | fine |
|---|---|---|---|
| h264 | 6 | 0 | 6 |
| hevc | 4 | **2** | **2** |

Both failures were HEVC, but half the HEVC set was fine. With two failures total
this does not support a causal claim — and `f7223270` (HEVC, 24k ball positions,
155 silver rows) is a direct counterexample. Worth keeping an eye on as uploads
accumulate; **not** worth gating on. It remains a warning only.

What is *not* the cause: bitrate (`df594aea` is 15.3 Mbps h264 and works),
duration, or player-detection quality (on `42280d38` both real players tracked
across 82,511 / 84,822 frames at ~0.71 pose confidence — the footage is fine).

---

## ★ Finding 2 — a failed match still reads as `completed`

`last_status='completed'` is set by **bronze** (STEP 2). When a later step fails,
nothing resets it. Both broken matches therefore sit at:

```
last_status = 'completed'   ingest_error = 'ValueError: Cannot resolve 2 players…'
```

Consequences:

- **Customer-visible.** Both appear in `gold.vw_client_match_summary` — the
  `/api/client/matches` sidebar — as completed matches with `total_points: 0`.
  An empty dashboard that claims to be finished.
- **A live billing trap.** `billing_import_from_bronze.sync_usage_from_submission_context`
  selects exactly `last_status='completed'` with no consumption record. Running
  that bulk sync would bill both retroactively.

**What did NOT happen** (verified in prod, correcting an earlier claim in this
file): neither customer was emailed — `ses_notified_at` is NULL for both,
because silver raised before STEP 6 — and neither was billed, because
`sync_usage_for_task_id` is STEP 5, also after silver. The bulk sweep is a manual
`__main__` script, not cron-wired (grep: its only callers are `upload_app.py:2071`
and `ingest_worker_app.py:415`, both per-task and both post-silver).

Platform-wide, exactly **2** matches are in this state — both from 2026-08-08.

**Open:** the inconsistency itself is unfixed. The sanity gate prevents this
*cause* recurring, but any post-bronze failure (e.g. a silver bug on a good
match) still leaves a match reading `completed`.

### The gate (`ingest_quality/`)

Runs as **STEP 1c** in `ingest_worker_app._do_ingest` — *after* the raw archive,
so a rejected payload is retained for diagnosis.

**REJECT** on either structural, threshold-free fact:
- **zero valid swings** (with swings present) — the direct cause above; and
- **no rallies AND no floor bounces** — no point structure to derive.

Validated against all 10 archived payloads: **rejects iff the match produced zero
silver rows.** No false positives, no false negatives.

Rejected gate designs — **don't reintroduce**:

| axis | why not |
|---|---|
| `ball_positions` | Best-separating *number* (≥5,591 vs 0/496) but that is an 11× gap fitted to 10 samples, and a count threshold on a 10-min match ≠ a 2-hour one. `valid == 0` is the same evidence with no knob. |
| rallies per minute | `df594aea` reports 9 rallies over 74 min yet has 611 silver rows and 100 hand-verified points. |
| codec | 2 of 4 HEVC matches are fine. See Finding 1. |

**WARN** (still ingests): suspect codec, low `final` confidence, > 10 players.
**Fails open**: distinguishes `"rallies": []` (ran, found none) from a *missing*
key, so a SportAI rename cannot reject every match at once; `assess()` returns OK
on any internal error.

On reject: stamps `last_status='failed'` + `ingest_error`, emails ops, returns
before bronze/silver/trim/billing/customer-notify.

Rollback `INGEST_SANITY_GATE_ENABLED=0`.
Check `.venv/Scripts/python -m ingest_quality.tests.test_gate`.

---

## ★ Finding 3 — compressed size is not a proxy for ingest memory

The OOM was **not** caused by a big payload, but by a *degenerate* one.

| task | gz | JSON | parsed dict | parse peak |
|---|---|---|---|---|
| `299013b3` (ingested fine) | 11.0 MB | 39.1 MB (×3.5) | 181 MB | 221 MB |
| `42280d38` (OOM ×4) | **5.8 MB** | 78.9 MB (**×13.5**) | **558 MB** | **637 MB** |

**The failing payload is half the compressed size of one that succeeded.**

Cause: 74 phantom players each carry a `location_heatmap` (424-row float grid).
Across all 74 that is **15.6 M cells, 97.7 % of them `0.0`** = **504 MB parsed =
90 % of the payload**. Runs of `0.0,0.0,0.0` gzip to almost nothing, but each
becomes a 24-byte Python float plus an 8-byte pointer. **The worse the tracking,
the better it compresses and the more RAM it costs.**

At 637 MB the parse alone exceeded the whole 512 MB `starter` instance, so it
died in `json.load` at STEP 1. **The fix was upgrading the ingest worker to
`standard` (2 GB)** — done 2026-08-08.

### Shipped alongside, but did NOT fix this task (commit `bf986b0`)

Three whole-payload re-serializations, all running while the parsed dict was
still live:

| site | cost |
|---|---|
| `raw_archive.archive_raw` — `gzip.compress(json.dumps(p).encode())` | +78.2 MB |
| `ingest_bronze._compute_session_uid` — full `json.dumps` purely to hash it | — |
| `ingest_bronze._persist_raw` — `json.dumps`, then `.encode("utf-8")` **twice** (sha, then gzip) | +111.4 MB |

All three now stream over `JSONEncoder.iterencode`: worst peak above the live
dict **111.4 MB → 13.4 MB**, worker traced peak **293 MB → 195 MB**. Verified
byte-identical on the real 39 MB payload — `payload_sha256`, `payload_len`,
gzip round-trip and **`session_uid`** all match (sha256 is a streaming
construction, so session identity is unchanged). `payload_len` stays a
*character* count, guarded with a non-ASCII case.

Real headroom for every match — but it runs *after* the parse that killed this
one. Don't record it as the fix.

> **Sub-lesson:** the archive step advertised itself as best-effort/never-fatal
> inside a `try/except`. An OOM is **SIGKILL, not an exception** — the caller
> could never contain it. A best-effort step must not be an allocation spike.

---

## Recovery

The poison-guard stamps `last_status='failed'`, so **`/ops/sweep-sa-orphans` will
not retry it** (it re-hits the `attempts >= SWEEP_SA_MAX_ATTEMPTS` branch). Use
the manual bypass, which ignores the attempt count and re-resolves a fresh
SportAI `result_url`:

```bash
curl -X POST https://api.nextpointtennis.com/ops/ingest-task \
  -H "X-Ops-Key: $OPS_KEY" -H "Content-Type: application/json" \
  -d '{"task_id":"<task_id>","mode":"worker"}'
```

## Open items

- **Fix the `last_status` inconsistency** — a post-bronze failure should not
  leave a match reading `completed` (Finding 2). Closes the billing trap and the
  sidebar lie for every failure mode, not just empty analyses.
- **Correct the two existing rows** (`42280d38`, `6abd37ca`) — needs a write
  role; `tf_readonly` cannot.
- **Why did ball tracking collapse on these two?** Unknown, and n=2. Watch
  `ball_positions` as uploads accumulate; if HEVC keeps appearing among the
  failures with a larger sample, revisit the codec question *then*.
- **Verify the gate fires in prod** on the next bad upload — it is validated
  offline against 10 payloads, not yet observed live.

## How to reproduce any of this

```bash
aws s3 sync s3://nextpoint-prod-uploads/raw-json/ ./all/
```

Then score each payload with `ingest_quality.assess`, and join to
`silver.point_detail` counts (`--db prod` credentials live in
`devenv/.env.local`; `ml_pipeline.diag.recon_bench._prod_url()` reads them).
Memory-profile with `tracemalloc` around `json.loads` — **never** infer memory
from `aws s3 ls`.
