# Next-session pickup — 2026-08-08 — empty-analysis gate + ingest OOM

> **Three parallel threads.** This session was **prod incident + docs**. The
> **SportAI business-analytics pipeline** thread is parked at "everything
> reconciles" (see §Prior threads). The **T5 ML pipeline** is parked at "bronze
> DEV complete, training is the incremental remainder" (`.claude/handover_t5.md`).

## ⚡ Executive summary (read first)

An ingest-worker OOM alert unwound into three separate faults.

> **★ I got the headline wrong first, and Tomo caught it.** I claimed "SportAI
> cannot analyse HEVC (0/4 vs 6/6 h264)". That was built on `meta.n_rallies`,
> which does not track usability. Scored against actual silver rows, **2 of the 4
> HEVC matches analysed fine**. Codec is a weak signal at n=2 failures, not a
> cause. **Don't gate on codec.** Lesson: validate a proxy against ground truth
> before building a narrative on it.

**★ An empty match is an all-swings-invalid match.** Ball tracking collapses
(usable matches have ≥5,591 `ball_positions`; the two failures had 0 and 496) →
SportAI validates swings against ball proximity, so it flags **every** swing
`valid:false` → `_resolve_two_players` counts `valid IS TRUE` only → raises
`Cannot resolve 2 players (found 0)` → zero silver rows. **That error does NOT
mean player detection failed** — `6abd37ca` had exactly 2 correct players and 228
all-invalid swings. Across all 10 payloads **valid swings == silver rows,
exactly**. **Fixed** by `ingest_quality/` (`40d4fbe`), which rejects iff the
match would produce zero silver rows.

**★ `last_status='completed'` outlives a later failure.** Bronze sets it; nothing
resets it when silver fails. Both broken matches read `completed` to
`gold.vw_client_match_summary` (customer sidebar, 0 points) **and to
`billing_import_from_bronze`, which selects exactly that** — running the bulk
usage sync would bill them. **STILL OPEN.**

**★ The OOM was payload pathology, not payload size.** `42280d38` is **5.8 MB
gz** — smaller than four payloads that ingested fine — but parses to a **558 MB
dict (637 MB peak)**, because 74 phantom players carry `location_heatmap` grids
that are **97.7 % zeros** (15.6 M cells, 504 MB = 90 % of the payload). Zeros
gzip to nothing and expand 7× in RAM. **The fix was the `standard` (2 GB)
upgrade**, done. A code fix shipped alongside (`bf986b0`) but did **not** fix
this task — record it accurately.

## What shipped

| commit | what |
|---|---|
| `bf986b0` | Three whole-payload re-serializations streamed (`raw_archive.archive_raw`, `ingest_bronze._compute_session_uid`, `_persist_raw` — which encoded to utf-8 **twice**). Worst peak above the live dict **111.4 → 13.4 MB**; worker traced peak **293 → 195 MB**. Byte-identical: sha/len/gzip/**session_uid** all verified on the real 39 MB payload. Headroom for every match, but runs *after* the parse that killed this one. |
| `40d4fbe` | `ingest_quality/` post-analysis sanity gate + `python -m ingest_quality.tests.test_gate`. |
| (docs) | `docs/_investigation/empty_analysis_and_ingest_memory_2026-08-08.md` (canonical evidence), `ingest_quality/README.md`, CLAUDE.md, `operations.md`, `env-vars.md`. |

**Infra:** ingest worker upgraded `starter` (512 MB) → **`standard` (2 GB)**.

## Open items

1. **Fix the `last_status` inconsistency** — stamp `last_status='failed'` in
   `_do_ingest`'s exception handler so a post-bronze failure stops reading as
   completed. Closes the billing trap + sidebar lie for EVERY failure mode.
   Proposed, not built.
2. **Correct the two existing rows** (`42280d38`, `6abd37ca`) — needs a write
   role; `tf_readonly` cannot.
3. **Billing check, NOT a refund.** I told Tomo to credit both customers; that
   was wrong. Neither was emailed (`ses_notified_at` NULL) and neither was billed
   (billing sync is STEP 5, after the silver build that raised; the bulk sweep is
   a manual script, not cron-wired). Verify `billing.*` before crediting —
   `tf_readonly` cannot see that schema.
4. **Why did ball tracking collapse on those two?** Unknown, n=2. Watch
   `ball_positions` as uploads accumulate; revisit codec only with a bigger sample.
5. **Verify the gate fires in prod** on the next bad upload — validated offline
   against 10 payloads, not yet observed live.
6. **Agent prod access:** `tf_readonly` now works from the dev box (bronze/silver/
   gold only — `billing.*` and `core.*` are not visible). Credentials in
   `devenv/.env.local`; `ml_pipeline.diag.recon_bench._prod_url()` reads them.

## Gotchas worth carrying

- **`aws s3 ls` is not a memory proxy.** A degenerate payload compresses BETTER
  and costs MORE RAM. Measure `json.loads` with `tracemalloc`.
- **A best-effort step must not be an allocation spike.** `archive_raw` was
  wrapped in `try/except` and called "never fatal" — but an OOM is SIGKILL, not
  an exception, so the caller could never contain it.
- **The poison-guard is one-way.** Once `/ops/sweep-sa-orphans` gives up at
  `SWEEP_SA_MAX_ATTEMPTS` it stamps `last_status='failed'` and never retries.
  Recover with `POST /ops/ingest-task {"task_id":…,"mode":"worker"}`, which
  ignores the attempt count and re-resolves a fresh `result_url`.
- **Don't re-key the sanity gate** on `ball_positions` (best-separating number
  but an 11× gap fitted to 10 samples), rallies-per-minute (rejects `df594aea`,
  which has 611 silver rows and 100 hand-verified points), or **codec** (2 of 4
  HEVC matches are fine).
- **A misleading error message cost two wrong diagnoses.** "Cannot resolve 2
  players" is emitted by a `valid IS TRUE` filter, not by player detection.

## Prior threads (unchanged, still current)

- **SportAI silver/gold:** everything reconciles to the spine
  (`exclude_d IS NOT TRUE`); `docs/_investigation/silver_gold_filter_contract.md`
  §"★ THE RULES OF THE GAME". The locked gate is
  `python -m ml_pipeline.diag.recon_bench` (c8b77210 18/18 AND no df594aea
  regression) — **now documented in CLAUDE.md's Testing & CI, it was missing.**
  Parked: finer placement/depth heatmap detail, pending Tomo's video annotation.
- **T5:** bronze deterministic DEV complete; only training remains
  (`.claude/handover_t5.md`, `.claude/audit_bronze_build_2026-06-16.md`).
