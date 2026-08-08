# Next-session pickup — 2026-08-08 — SportAI HEVC failure + ingest OOM, both gated

> **Three parallel threads.** This session was **prod incident + docs**. The
> **SportAI business-analytics pipeline** thread is parked at "everything
> reconciles" (see §Prior threads). The **T5 ML pipeline** is parked at "bronze
> DEV complete, training is the incremental remainder" (`.claude/handover_t5.md`).

## ⚡ Executive summary (read first)

An ingest-worker OOM alert unwound into **two unrelated faults**, one of them a
vendor bug that had been silently costing customers money.

**★ SportAI cannot analyse HEVC/H.265.** Measured across all 10 archived
payloads: **0/4 HEVC matches produced usable analysis; 6/6 H.264 did.** SportAI's
own `final_confidences.final` separates them with no overlap (h264 0.618–0.699,
hevc 0.408–0.526). It is **not the footage** — on `42280d38` both real players
were tracked across 82k/85k frames at 0.71 pose confidence; only the ball
detector returned nothing. Not bitrate either (`df594aea` is 15.3 Mbps h264 and
works). iPhones record HEVC by default. **OPEN: raise with SportAI; consider an
upload-time warning.**

**★ Failed analyses were billed and announced as ready.** A failed SportAI run
returns **HTTP 200 with a well-formed empty payload**, so ingest "succeeded",
silver got nothing, the customer got a "video is ready" email, and a credit was
consumed. `VIDEO_QUALITY_CHECK_ENABLED` only gates the video *before* analysis.
**Fixed** by `ingest_quality/` (commit `40d4fbe`).

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
| (docs) | `docs/_investigation/sportai_hevc_and_ingest_memory_2026-08-08.md` (canonical evidence), `ingest_quality/README.md`, CLAUDE.md, `operations.md`, `env-vars.md`. |

**Infra:** ingest worker upgraded `starter` (512 MB) → **`standard` (2 GB)**.

## Open items

1. **Raise HEVC with SportAI.** 4/4 broken is a vendor bug. Highest value here.
2. **Warn on HEVC at upload** (Media Room) or transcode before submit — this
   prevents the whole class rather than catching it after the spend.
3. **Refund `42280d38` + `6abd37ca`** — Tomo said he'd credit them. `6abd37ca`
   (2026-08-08 13:00) was never flagged by anything: it has only 2 phantom
   players so it didn't OOM, and nothing noticed the empty dashboard.
4. **Verify the gate fires in prod** on the next HEVC upload — it has only been
   validated offline against the 10 archived payloads (rejects exactly the 2
   empties, passes all 6 h264 clean).
5. **Agent prod access is blocked**: Render Postgres rejects the dev box
   (`105.214.16.76` not allowlisted; no VPN active) and there is no local
   `OPS_KEY`, so all DB diagnosis this session went via the S3 raw-json archive.
   Allowlist the IP or provide `OPS_KEY` to unblock self-service.

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
- **Don't re-key the sanity gate** on `ball_positions` (misses `6abd37ca`, which
  has 496 with zero rallies) or rallies-per-minute (rejects `df594aea`, which
  reports 9 rallies over 74 min yet has 100 hand-verified points).

## Prior threads (unchanged, still current)

- **SportAI silver/gold:** everything reconciles to the spine
  (`exclude_d IS NOT TRUE`); `docs/_investigation/silver_gold_filter_contract.md`
  §"★ THE RULES OF THE GAME". The locked gate is
  `python -m ml_pipeline.diag.recon_bench` (c8b77210 18/18 AND no df594aea
  regression) — **now documented in CLAUDE.md's Testing & CI, it was missing.**
  Parked: finer placement/depth heatmap detail, pending Tomo's video annotation.
- **T5:** bronze deterministic DEV complete; only training remains
  (`.claude/handover_t5.md`, `.claude/audit_bronze_build_2026-06-16.md`).
