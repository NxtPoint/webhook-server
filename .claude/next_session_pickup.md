# Next-session pickup — 2026-07-26 — post-incident, pipeline hardened

> **Two parallel threads.** This is the **SportAI (`tennis_singles`)
> business-analytics pipeline**. The **T5 ML pipeline** is parked at "bronze DEV
> complete, training is the incremental remainder" (`.claude/handover_t5.md`).

## ⚡ Executive summary (read first)

The last session was a long **incident + hardening** sprint triggered by the
**Erin v Jolanda** upload (`df594aea`) getting stuck. It is now **fully ingested**
(bronze + silver + the new analytics tables) and the whole ingest→silver→trim
path was hardened. **Everything is shipped to `main`.** Bench is green.

**The one thing still open on `df594aea`: the video TRIM** (highlight reel) is
parked at `trim_status='accepted'` — not blocking (the dashboard/analytics are
done; the trim is polish). See "Parked" below.

**THE job for next session = build the two-match silver reconciliation bench and
strengthen silver (logic-only) → full plan in
`docs/_investigation/silver_recon_bench_plan.md`.** The owner has hand-mapped the
full Erin v Jolanda match on video (Excel: `…OneDrive\Documentos\Tenfifty5\erin_v_yolanda recon.xlsx`;
Erin=low id ~427, Yolanda ~770; point start/end marked in cell colours). Baseline
numbers below. **Start by extracting + getting Tomo to SIGN OFF the ground truth**
(the colour scheme is non-uniform — verify before building on it), then the bench,
then sound silver levers under the iron rule: improve df594aea WITHOUT moving
c8b77210 off 18/18 (anti-overfit), no manufacturing, bronze is the ceiling.

## THE JOB — validate df594aea against the owner's video

The owner (Tomo) is watching the Erin v Jolanda footage and recording the real
point winners / outcomes, exactly like the **18/18** validation we did on
`c8b77210` (Tomo v Jimbo). Then reconcile against silver.

**df594aea silver baseline (measured 2026-07-26):**
| metric | value |
|---|---|
| `silver.point_detail` shots | **611** |
| distinct points | **95** (owner flagged 95 as maybe-low — verify vs video; plausible for a full match) |
| spine shots (`exclude_d IS NOT TRUE`) | 389 |
| points with a winner | 95 / 95 |
| `match_quality` tier | **medium** (ball 0.29 / pose 0.68 / swing 0.76 / final 0.62) |
| `match_player_summary` rows | 2 · `player_movement_grid` cells | 762 |

Method: same as the filter-contract doc §Verification — vw_point export, compare
point winners to the video, triage any gap as **bronze-accuracy vs silver-bug**
(RULE 6 / bronze-first). This is a *badly-tracked* match (ball_conf 0.29), so
expect it to degrade vs the 18/18 clean match — the goal is to find WHERE it
degrades and whether it's a bronze ceiling or a silver bug.

**quality_tier calibration (open):** both `c8b77210` (good) and `df594aea` (bad)
read **medium** — the thresholds don't discriminate. Calibrate once the video
validation shows how bad `df594aea` really is; it should read **low**.

## Parked — video-worker trim (df594aea + a latent URL bug)

1. **df594aea trim stuck at `accepted`** (since ~10:10, no completion, no error).
   It's on the real worker with the new streaming+SigV4 fixes. To resume: pull the
   **real video-worker service Logs** (NOT `video-worker.onrender.com` — see #2)
   for `FFMPEG TRIM task_id=df594aea` around the last re-fire; see if it's
   encoding, died (`/tmp`? instance failed?), or errored. Re-fire with
   `POST /ops/retrim {"task_id":"df594aea-…"}`. Low priority (no customers, test match).
2. **`video-worker.onrender.com` serves a FOREIGN Node/Express app** (`"Cannot GET
   /trim"` = Express, not our Flask worker). The committed `render.yaml`
   `VIDEO_WORKER_BASE_URL` value is therefore **wrong/squatted**. Trims still work
   because the main API's *actual* env points to the real worker (else trims would
   404-fail, not `accept`). **Fix:** confirm the video-worker service's real URL +
   the main API's `VIDEO_WORKER_BASE_URL`, correct the `render.yaml` value, and
   don't trust external curls of `video-worker.onrender.com`.

## What shipped this session (all on `main`)

| commit | what |
|---|---|
| `7b15768` | `/ops/sweep-sa-orphans` now also recovers **STUCK** (started-then-died) SportAI ingests, not just never-started; poison-match attempt cap (`SWEEP_SA_MAX_ATTEMPTS`). |
| `b0ead43` | ingest worker: **retry on transient DB errors** (Render PG failover / "in recovery") with backoff; sweep give-up no longer overwrites the worker's real `ingest_error`. |
| `56b75db` | **debug_data blob size cap** (`DEBUG_DATA_MAX_BYTES`, ~2 MB) — a 7.6 MB `bronze.debug_event` JSONB insert was severing the DB connection and aborting the whole bronze txn (the primary df594aea root cause; also forced PG crash-recovery → the misleading "in recovery mode"). |
| `6cbe94c` | silver: the retired-column `DROP COLUMN shot_q/…` runs in a **SAVEPOINT** — it was blocked by legacy `ss_.*` + gold `SELECT *` views (`DependentObjectsStillExist`), poisoning the txn and failing **every** ingest's silver build. |
| `f9b8a4c` | **streaming single-pass ffmpeg trim** — was download-full-source + N per-segment files + output → blew Render's **2 GB `/tmp`** limit on long matches → instance killed → trim orphaned. Now streams source from S3 + one `trim`+`concat` pass; only the output touches `/tmp`. |
| `bfd6a57` | `POST /ops/retrim` + `POST /ops/sweep-stale-trims` + `trim_attempts` column — recover trims killed mid-encode (attempt-capped + ops alert). |
| `75dfc33` | trim source presigned with **SigV4 in the bucket's real region** (bucket is **eu-north-1**; SigV2/us-east-1 → 400). Region auto-detected via the `x-amz-bucket-region` header. |
| `0b98d8d` | `_mark_trim_accepted` stamps `trim_requested_at=NOW()` so a fresh re-fire isn't seen as stale and double-fired by the sweep. |

**Ingest worker instance was moved back to $7 (standard)** — the failures were
never memory (they were the debug_data blob + silver DROP), so the $25 bump was
unjustified. If a *truly enormous* match ever pressures 512 MB it will now fail
**visibly** (real error + the stale-ingest sweep), and you can bump just-in-time.

## New env vars (all have safe defaults)

`DEBUG_DATA_MAX_BYTES`=2000000 · `INGEST_DB_RETRY_MAX`=5 / `INGEST_DB_RETRY_BASE_S`=5 ·
`SWEEP_SA_MAX_ATTEMPTS`=4 · `TRIM_STALE_AFTER_S`=1800 / `TRIM_SWEEP_MAX_ATTEMPTS`=3 ·
`TRIM_STREAM_INPUT`=1 (0 = download fallback) · `TRIM_ENCODE_TIMEOUT_S`=3600 ·
`TRIM_PRESIGN_EXPIRY_S`=21600 · `S3_BUCKET_REGION` (override; auto-detected otherwise).

## New ops surface + schema

- `POST /ops/retrim {task_id, force?}` — reset + re-fire one trim.
- `POST /ops/sweep-stale-trims {dry_run, limit}` — recover trims stuck at
  accepted/queued/processing (cron-wired; attempt-capped).
- `POST /ops/sweep-sa-orphans` — now covers stuck-stale ingests too.
- New column `bronze.submission_context.trim_attempts INT`.
- Cron `cron_sweep_t5_orphans.py` now also POSTs `/ops/sweep-stale-trims`.
- `tf_readonly` was GRANTed SELECT on `silver.*` (so dev can read the analytics tables).

## Open audit P1s (unchanged — none touched this session)

All billing/frontend/gold, outside the silver-derivation work. Ranked:
1. **deuce/ad midline** (`build_silver_v2.py:653`) — splits on drifting AVG instead of fixed 5.485.
2. **hollow ingest bills the customer** — zero-row ingest marked completed + consumes a credit.
3. **NULL rendered as `0%`** across Match Analytics (frontend).
4. **Serve Strategy totals double-count** (frontend re-keys + sums).
5. **soft-deleted matches never leave `vw_player`** (`gold_init.py`, no `deleted_at IS NULL`).
6. P2: serve-speed KPIs average a partial sample; `_validate_rally_count` false-alarms.

## Dashboard data layer — BUILT + wired + first prod run confirmed

`silver_analytics/` (fitness / movement-grid / quality) is wired into the ingest
worker (STEP 3b) and populated on df594aea's real ingest (762 grid cells, 2
player summaries, 1 quality row). **Dashboards NOT built yet** — that's the next
build after validation. Roadmap: `.claude/plans/twinkly-seeking-bentley.md`
(momentum curve needs no new table; fitness/heatmap read the new tables).

## Reference matches

| task | who | note |
|---|---|---|
| `c8b77210` | Tomo v Jimbo Ma | **primary reference — 18/18 vs video.** Protect from orphan sweep. |
| `df594aea` | **Erin v Jolanda** | this session's match — 611 shots / 95 points, badly tracked (ball 0.29). **Next: validate vs video.** |
| `0336b82b` | Erin v Jolanda (earlier run) | different SportAI run of the same footage |

## Canonical docs

- Pipeline logic + filter contract → `docs/_investigation/silver_gold_filter_contract.md`
- Audit closeout → `docs/_investigation/pipeline_end_to_end_audit_2026-07-19.md`
- Bench (mandatory before serve_detector edits): `.venv/Scripts/python -m ml_pipeline.diag.bench` (ea1e500c=12/26, 880dff02=23/24).

## Key lesson from this session

**Don't guess Render/infra failures — get the actual service log.** The df594aea
cause was misread as OOM (→ a wasted memory bump) and as a generic failover before
the worker's own log named it (`debug_event` 7.6 MB insert → severed connection).
Each real log line was decisive. See memory `feedback_get_real_logs_not_guess`.
