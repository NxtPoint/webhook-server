# Session review — 2026-05-21 afternoon, Phase 5a Stage 2 + Option A delivered

**Owner:** Claude (continuation of 2026-05-20 overnight build + 2026-05-21 morning pivot)
**Status at handover:** **Option A SHIPPED in same session.** Bench locked. All future Batch runs write ROI bounces directly to canonical bronze. Existing task `763c9ee9` migrated via SQL + silver rebuilt — silver went 160 → 183 rows, captured the first NEAR T5 serve we've ever seen.

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

---

## ADDENDUM — Option A executed in this session (2026-05-21 PM continuation)

After parking Option A "for next session", Tomo asked whether we could finish it today. We did.

### What shipped
1. **SQL migration** of existing `763c9ee9` data — added `source` column to `ml_analysis.ball_detections`, tagged existing 1983 rows as `source='main'`, INSERTed the 459 ROI rows from `ball_detections_roi` with `source='roi_prod'`.
2. **`harness rerun-silver`** on `763c9ee9` — silver rebuilt from the now-merged bronze:

   | | Before migration | After rerun-silver |
   |---|---|---|
   | silver rows | 160 | **183** (+23) |
   | silver serves | 16 | **19** (+3) |
   | silver **NEAR** serves | **0** | **1** (id=92, ts=178.76, hit_y=24.05 — first NEAR T5 serve in silver, ever) |
   | silver FAR serves | 16 | 18 (+2) |

3. **Code change in `ml_pipeline/roi_extractors/bounces.py`** — `_init_schema()` now does idempotent `ALTER TABLE ball_detections ADD COLUMN IF NOT EXISTS source TEXT` (was: CREATE TABLE ball_detections_roi). `_persist_rows()` now INSERTs into `ml_analysis.ball_detections` with `source` column instead of `ball_detections_roi`. Dropped `window_serve_ts` from the INSERT (not on canonical `ball_detections`).
4. **Branch `phase-5a/bronze-write-direct`** at commit `7d8bfaa`, merged to main as `5d1e818`. Bench unchanged (`a798eff0=20/24, 880dff02=23/24`).
5. **Docker rebuild + dual-region ECR push + new job-defs**:
   - eu-north-1: **rev 46** pinned to `sha256:87435dbfd…`
   - us-east-1: **rev 28** pinned to `sha256:87435dbfd…`
   - Old revs (45 / 27) kept for rollback.

### What's still untouched

- `serve_detector._load_ball_rows()` still merges `ball_detections_roi` for backwards compat with old data. Once `ball_detections_roi` is fully drained (cleanup migration on existing tasks), this merge logic becomes dead code and can be deleted.
- `ball_detections_roi` table itself: still exists, will no longer accumulate prod data. The diag tool `ml_pipeline/diag/extract_roi_bounces.py` still writes there (intentional — diag isolation).

### Verification a future Batch run will work

The new Batch image (rev 46/28) writes to `ml_analysis.ball_detections` directly. Test in next session by:
- Upload any video as Singles T5 from frontend
- After SUCCEEDED, query `SELECT count(*) FROM ml_analysis.ball_detections WHERE job_id = <new_id> AND source = 'roi_prod'`
- Expect non-zero (this match has reasonable rally bronze coverage). Silver builds automatically since the silver builder already reads `ball_detections`.

### Cleanup work for a later session (not blocking)

- Migrate any remaining `ball_detections_roi` rows on other tasks into `ball_detections` so the legacy table can be dropped.
- Delete `serve_detector._load_ball_rows()`'s ROI merge branch once the legacy table is empty.
- Decide whether to drop the `ball_detections_roi` table or leave it as a diag-tool scratch space.
