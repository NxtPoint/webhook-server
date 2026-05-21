# Session review — 2026-05-21 afternoon, Phase 5a Stage 2 + architectural pivot

**Owner:** Claude (continuation of 2026-05-20 overnight build + 2026-05-21 morning pivot)
**Status at handover:** Phase 5a deployed + measured on `763c9ee9-e5ea-42ab-820a-7d53f6a7316c`. Works on its own axis but **writes to the wrong table**. Fix scheduled for next session as **Option A** (single-line target change). Parked deliberately, not broken.

---

## TL;DR

Phase 5a's ROI extractor ran end-to-end on a fresh upload of the same video:
- ✅ Wrote 459 rows / 23 bounces / 9 windows to `ml_analysis.ball_detections_roi`
- ✅ Render-side `serve_detector._load_ball_rows()` merged them → +6 serves got `bounce_frame` attached (3→9)
- ❌ Silver row count UNCHANGED (160 silver rows, 16 serves) — silver builder doesn't read `ball_detections_roi`

After reflection with Tomo, **the right fix is at the bronze layer, not silver.** Phase 5a should write to `ml_analysis.ball_detections` (the canonical bronze) instead of a parallel `ball_detections_roi` table. Silver then "just works" by derivation, no silver code change, and `serve_detector`'s ROI merge logic becomes redundant.

## What we measured

Same video, two T5 runs (pre-5a vs post-5a):

| Metric | 880dff02 (pre-5a) | 763c9ee9 (post-5a) | Delta |
|---|---|---|---|
| `ml_analysis.ball_detections` rows | 1983 | 1983 | 0 |
| `ml_analysis.ball_detections` bounces | 162 | 162 | 0 |
| `ml_analysis.ball_detections_roi` rows | 0 | **459** | **+459** |
| `ml_analysis.ball_detections_roi` bounces | 0 | **23** | **+23** |
| `ml_analysis.serve_events` total | 107 | 109 | +2 |
| `ml_analysis.serve_events` with `bounce_frame` | 3 | **9** | **+6** |
| `silver.point_detail` (t5) rows | **160** | **160** | **0** |
| `silver.point_detail` (t5) serves | 16 | 16 | 0 |

Phase 5a's additive payload IS reaching `serve_detector` (proving the merge logic works), but the serve_detector's improvement is invisible in `silver.point_detail` because silver is built independently from a different code path.

## Why silver row count didn't move — the architectural issue

`build_silver_match_t5.py:438` loads bounces from one place only:

```python
SELECT frame_idx, x, y, court_x, court_y, speed_kmh, is_in
FROM ml_analysis.ball_detections
WHERE job_id = :jid AND is_bounce = TRUE
```

It does **not** read `ml_analysis.ball_detections_roi`. It also does **not** read `ml_analysis.serve_events` — silver does its own serve detection geometrically from bounces + player positions.

So the ROI extractor's output bypasses silver entirely. The +6 better serve_events that the Render-side detector produced never become silver rows, because silver isn't asking the detector — it's asking the raw `ball_detections` table.

This isn't a silver bug. It's a Phase 5a architecture bug: **we created a second bronze table.** That forces every downstream consumer (silver, serve_detector, any future tool) to know about both tables and merge them. The architectural rule should be: **there's one canonical bronze. Every consumer reads it. Period.**

## Option A — what to do next session (~10 min code + standard deploy)

Change Phase 5a's write target. Single-file edit:

**`ml_pipeline/roi_extractors/bounces.py`** — in `_persist_rows()` and `_init_schema()`:

Replace `ml_analysis.ball_detections_roi` with `ml_analysis.ball_detections`. The column shapes mostly match (frame_idx, x, y, court_x, court_y, is_bounce, job_id are common). The ROI-specific fields (`source`, `window_serve_ts`) don't exist on `ball_detections` — either:

- (i) Drop them on insert (lose traceability of "which ROI window produced this row")
- (ii) Add `source TEXT NULL` to `ball_detections` (idempotent ALTER) and write `source='roi_prod'` so we can still distinguish (and so we don't dedupe-overwrite main-pass rows)

Recommendation: **(ii)**. Tiny schema bump, preserves diagnostics, gives us a clean way to query "T5 rows from main pass" vs "T5 rows from ROI pass".

Then the rest of the system unchanged:
- `serve_detector._load_ball_rows()` keeps working (it already reads `ball_detections` first); its ROI merge logic becomes dead code we can drop in a cleanup pass.
- `build_silver_match_t5.py` reads `ball_detections` and now sees ROI bounces too — silver row count moves mechanically.
- `ball_detections_roi` table sits idle. Drop it in a later cleanup migration.

## Validation paths for Option A

| Path | Time | Notes |
|---|---|---|
| **Code edit + Docker rebuild + ECR push + job-def + frontend rerun** | ~90-120 min | Proper end-to-end. The "real" deploy. |
| **Code edit + ad-hoc SQL migration of existing 763c9ee9 ROI rows → ball_detections + harness rerun-silver** | ~30 sec on Render shell | Fast validation of the silver-side hypothesis. Lets us see whether silver row count actually grows BEFORE committing to the Docker round-trip. |

I'd do the SQL migration first to prove the silver hypothesis, then ship the code change. Migration looks like:

```sql
INSERT INTO ml_analysis.ball_detections
    (job_id, frame_idx, x, y, court_x, court_y, is_bounce, source)
SELECT job_id, frame_idx, x, y, court_x, court_y, is_bounce, source
FROM ml_analysis.ball_detections_roi
WHERE job_id = '763c9ee9-e5ea-42ab-820a-7d53f6a7316c';
```

(after `ALTER TABLE ml_analysis.ball_detections ADD COLUMN IF NOT EXISTS source TEXT;`)

Then `harness rerun-silver 763c9ee9-...` and re-query `silver.point_detail` for that task. If silver row count grows materially → ship the Docker change.

## What this doesn't solve

Phase 5a + Option A together would close part of the ball-coverage gap (the 23 bounces in service-box zones, with a 3× boost in serves with attached bounce frames). The deeper coverage gap (13% → ~70% to match SportAI) needs WASB integration and finetuning — both flagged in the infrastructure audit and unblocked by the GPU dev box. Phase 5a was always a partial win.

## Open admin task — Render Postgres re-lock

We opened Render Postgres to `0.0.0.0/0` to unblock the Batch DB connection. Tomo to re-lock back to his home IP (`105.214.8.31/32`) at end of session OR keep open until Option A lands. Long-term proper fix: NAT Gateway + static EIP for the Batch compute environment, then allowlist only that EIP. See `feedback_render_postgres_ip_allowlist.md`.
