# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-22 (late evening — Phase 5c END-TO-END VERIFIED + orphan-sweep endpoint shipped)
**Phase active:** Phase 5 — Ball detection coverage. **5c.0 / 5c.1 / 5c.2 / 5c.3 ALL LIVE + VERIFIED IN PROD.** 5e VERIFIED. 5d remains blocked on corpus volume.
**Bench:** Serve `a798eff0=20/24, 880dff02=23/24` — green. Ball-bench post-fix baseline intact. Silver-bench `1d6feb3a` 7 rows (pre-fix bronze; recapture pending).
**What shipped today (8 commits):** CLAUDE.md doc edit (`ad60eda`); Phase 5c.3 `harness build-corpus` (`2ac4a64`); `verify-corpus-row` (`36f18d5`); `build-corpus --upload-s3` (`4272c5e`); `build-corpus --task` (`b48230c`); `/ops/sweep-t5-orphans` endpoint (`a1a7e96`); plus the two doc commits closing this session.
**End-to-end proof:** SA `0d0514df-68aa-4346-9e2d-64413429e47f` → auto-spawned T5 `78c32f53-5580-4a88-a4e7-7506e59b2b52` → `ml_analysis.training_corpus` row with **161 ball-position labels (48 NEAR / 47 FAR / 66 other)** created `2026-05-22 20:20:54` UTC, 0.6 s after `_do_ingest_t5` completed.
**What's blocked:** Nothing on Phase 5c. 5d (TrackNetV3 finetune) needs more corpus rows (~5+ matches) before training is worth attempting.
**Next session's job:** Pick (a) wire `/ops/sweep-t5-orphans` into a Render cron, (b) re-capture `1d6feb3a` silver-bench fixture, (c) accumulate organic corpus rows + smoke-test `harness build-corpus` once we have 2+ matches, or (d) Phase 5c.4 — bench-gate-before-promotion (needs `ball_tracker.py --weights-path` ⇒ Batch deploy).

If the above is enough, stop reading this file and go.

If you need depth (inheriting a blocker, verifying a claim, picking next move), continue.

---

## Today's big architectural learning — the orphan-trigger gap

**The bug:** `_auto_dual_submit_t5` submits a Batch job + creates a `bronze.submission_context` row for the T5 sibling. The ingest gate that calls `_do_ingest_t5` lives inside `/upload/api/task-status` and only fires when a browser polls. **Auto-spawned T5 tasks have no polling browser**, so they sit in `last_status='queued'` indefinitely despite Batch having succeeded.

**Tonight's evidence:** `78c32f53-...` Batch completed at 16:42 UTC, sat orphaned for 3h45m until a single manual `GET /upload/api/task-status?task_id=78c32f53-...` unblocked the ingest at ~20:20 UTC. Pair-completion hook fired correctly downstream of that.

**The shipped fix (this session):** `POST /ops/sweep-t5-orphans` — OPS_KEY-gated endpoint that scans for the exact gap and fires `_start_ingest_background` for each orphan via a background thread. Idempotent (inner ingest gate checks `ingest_started_at` + staleness; `training_corpus` has a UNIQUE constraint). Documented in CLAUDE.md §Diagnostics & Ops. Dry-run default.

**What's still needed:** wire it into a Render cron. Without that, every future auto-spawned T5 still needs manual triggering. **Recommended:** a 5-min cron hitting `/ops/sweep-t5-orphans {"dry_run": false}` — that's the minimal closure of the loop. Owner: next session.

---

## State at session end (2026-05-22 late evening)

`origin/main` at **`a1a7e96` `ops: /ops/sweep-t5-orphans` — fire ingest for stuck auto-spawned T5 tasks**. Recent session commits (most recent first):

```
a1a7e96 ops: /ops/sweep-t5-orphans — fire ingest for stuck auto-spawned T5 tasks
b48230c harness: build-corpus --task <t5_task_id> filter
4272c5e harness: add --upload-s3 flag to build-corpus
36f18d5 harness: add verify-corpus-row subcommand for ml_analysis.training_corpus
2ac4a64 phase 5c.3: harness build-corpus subcommand — assemble dataset from training_corpus
ad60eda docs: CLAUDE.md — document silver/ball benches + db_writer in T5 section
7fff997 docs: north_star — mark 5e follow-ups #1 + #2 SHIPPED 2026-05-22
7863a66 fix(t5): _filter_outliers chain-rejection — re-anchor on coherent cluster
```

**Phase 5c artefacts in prod:**
- `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render (Sport AI - API call service)
- `gold.vw_dual_submit_pairs` populated (1 complete pair so far)
- `ml_analysis.training_corpus` has 1 row (`label_kind='ball_position'`, 161 labels)
- S3: `s3://nextpoint-prod-uploads/training/labels/78c32f53-5580-4a88-a4e7-7506e59b2b52_ball_positions.json`

**Batch state:**
- eu-north-1 `ten-fifty5-ml-pipeline:48`, us-east-1 `:30` — amd64 `bc8f7d72…` — INCLUDES chain-rejection fix + `source='main'` follow-up #2
- Previous active revs (eu :47 / us :29) kept ACTIVE for rollback

**Serve bench:** `a798eff0` 20/24, `880dff02` 23/24, no regressions
**Silver bench:** `1d6feb3a` OK (7 silver rows — frozen pre-fix bronze)
**Ball bench:** post-fix baseline locked

**Render auto-deploy status at session close:** `a1a7e96` pushed; deploy should have completed by the time next session starts. To verify post-deploy:
```bash
curl -sS -X POST https://api.nextpointtennis.com/ops/sweep-t5-orphans \
     -H "X-Ops-Key: $OPS_KEY" \
     -H "Content-Type: application/json" \
     -d '{"dry_run": true}'
```
Expected: `{"ok": true, "dry_run": true, "found": 0, ...}` — no orphans since tonight's was manually resolved.

---

## Read in this order before doing anything else

1. `.claude/strategy/dual_submit_status_2026-05-20.md` §1-3 (status) + §4 5c.4-5c.5 (next phases).
2. `docs/north_star.md` — 5c.0-5c.3 marked LIVE + VERIFIED tonight.
3. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST. Still load-bearing.
4. `CLAUDE.md` §Diagnostics & Ops — `/ops/sweep-t5-orphans` documented.

Then run the locked benches to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench
    .venv/Scripts/python -m ml_pipeline.diag.bench_silver

Expect: serve `a798eff0` 20/24, `880dff02` 23/24; silver `1d6feb3a` OK (7 rows).

---

## Next move — pick one (recommended order: 1 → 2 → 3 → 4)

**Option 1: Wire `/ops/sweep-t5-orphans` into a Render cron (~30 min).** Add a new Render Cron Job (similar pattern to `cron_capacity_sweep.py` / `cron_monthly_refill.py`) that POSTs to `/ops/sweep-t5-orphans` every 5 min with `dry_run=false`. Without this, every future auto-spawned T5 still needs manual unblocking. **This closes the dual-submit loop.**

**Option 2: Re-capture `1d6feb3a` silver-bench fixture against post-fix Batch image (Tomo-side, ~15 min Render + 5 min local).** From Render shell: `python -m ml_pipeline.diag.bench_silver.snapshot --task <task_id> --upload-s3`; locally pull, run silver bench, `--update-baseline`, commit. If silver row count jumps from 7 → 30+, that's direct evidence the chain-rejection fix is structurally repairing T5 bronze density.

**Option 3: Smoke-test `harness build-corpus` end-to-end (~15 min, requires 2+ corpus rows).** Need to wait until another tennis_singles upload happens (or do one manually). Then `python -m ml_pipeline.harness verify-corpus-row <t5_id>` to confirm S3 artefacts, followed by `python -m ml_pipeline.harness build-corpus --output-dir ml_pipeline/training/datasets/dual_submit_first --limit 5`. Confirms the assembly path works on real data.

**Option 4: Phase 5c.4 — bench-gate-before-promotion (~4-6 hr, Batch deploy required).** Extend `ml_pipeline/ball_tracker.py` to accept `--weights-path` constructor arg, add `bench_ball.py --weights-path` flag, write `bench_finetuned.py` comparison script + promotion playbook. **Trips guardrail #8** (Batch-side change checklist) — Docker rebuild + dual-region ECR push + new job-def revs required. Don't ship this without bandwidth to do the Batch deploy carefully.

---

## Open admin items

- `/ops/sweep-t5-orphans` shipped but NOT wired to a cron yet. Manual until that lands.
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).
- Silver-bench has only 1 fixture (`1d6feb3a`). Adding `880dff02` would give a denser regression target. Both spec'd in `.claude/strategy/silver_bench_design_2026-05-21.md` §11.
- Tonight's verification curl flow doesn't auto-fire ingest for tasks queued more than `min_age_minutes=5` ago — sweep takes care of those. Edge: a brand-new auto-spawned T5 within the 5-min window will not be picked up until the next sweep tick. Acceptable for now (T5 takes >25 min on Batch; first sweep tick will always be ready).

---

## Things NOT to do (load-bearing)

- **Don't add new columns to `bronze.submission_context` in production** without explicit need and a documented reason.
- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, `db_writer.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.**
- **Don't rollback WASB without running the bench against TrackNetV2 first.** Previous revs (eu :46-:47 / us :28-:29) kept on standby.
- **Don't change the `AUTO_DUAL_SUBMIT_T5` / `AUTO_LABEL_DUAL_SUBMIT_PAIRS` env-flag defaults to ON in code.** They are explicitly flipped on Render so the deploy default stays OFF (dark by design).
- **Don't ship a `bench_ball.py --weights-path` flag without the Batch deploy.** Even an additive `weights_path=None` kwarg on `BallTracker.__init__` trips guardrail #8.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural).
- Don't drop `test_videos/` from the GPU rsync.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. T5 bronze in `ml_analysis.*`, SportAI in `bronze.*`, distinguished at the silver layer by `model` column.

---

## Verification commands (tonight's specific pair)

```bash
# 1. Confirm the corpus row is still there (should always return 1+ row)
psql "$DATABASE_URL" -c "
SELECT sa_task_id::text, t5_task_id::text, label_kind, label_count, role_breakdown, created_at
FROM ml_analysis.training_corpus
ORDER BY created_at DESC LIMIT 3;
"

# 2. Confirm S3 artefacts are intact (run from a machine with OPS_KEY + boto3)
.venv/Scripts/python -m ml_pipeline.harness verify-corpus-row 78c32f53-5580-4a88-a4e7-7506e59b2b52

# 3. Smoke-test the assembly path on this single pair (~5 min, downloads 50 MB video)
.venv/Scripts/python -m ml_pipeline.harness build-corpus \
    --output-dir ml_pipeline/training/datasets/dual_submit_first \
    --task 78c32f53-5580-4a88-a4e7-7506e59b2b52 \
    --limit 1

# 4. Sweep endpoint dry-run (expect 0 orphans, the gap is closed for tonight's task)
curl -sS -X POST https://api.nextpointtennis.com/ops/sweep-t5-orphans \
     -H "X-Ops-Key: $OPS_KEY" \
     -H "Content-Type: application/json" \
     -d '{"dry_run": true}'
```

---

## Phase 5c.2 / 5c.3 activation playbook — DONE; kept for reference

(env-flag flip + backfill + verification — already executed this session)

1. Render dashboard "Sport AI - API call" → Environment:
   - `AUTO_DUAL_SUBMIT_T5` = `1` ✓
   - `AUTO_LABEL_DUAL_SUBMIT_PAIRS` = `1` ✓
2. Confirm fresh tennis_singles upload spawns a paired T5: ✓ (`0d0514df` → `78c32f53`)
3. After both halves complete, verify `training_corpus` row: ✓ (161 labels)
4. `/ops/backfill-pair-labels` ran with 0 eligible (no pre-existing pairs): ✓

Future activations on different services would follow the same recipe.
