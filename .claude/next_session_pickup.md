# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-22 (afternoon — WASB Step 5 verified, Phase 5e CLOSED)
**Phase active:** Phase 5 — Ball detection coverage. **5e VERIFIED IN PROD** + **5c.2 SHIPPED** + **Silver bench SHIPPED**. Activation work + 3 follow-ups remain.
**Bench:** `a798eff0=20/24, 880dff02=23/24` — **green** (serve). Ball-bench v2 locked at `7100792`. Silver-bench has empty fixture set — first capture pending Render shell.
**What shipped last session:** WASB Step 5 verified end-to-end on Batch task `1d6feb3a` (CloudWatch + bronze SQL + env var + job-def rev all confirm WASB running). Phase 5c.2 silver-bench tests all pass. Three follow-ups identified — none blocking.
**What's blocked:** Nothing. All shipped builds are working; activation is Tomo-side (env-flips + first fixture capture).
**Next session's job:** Pick one of (a) activate what's shipped — env flips + first fixture, (b) fix one of the 3 WASB follow-ups (filter chain-rejection, source='main' tag, fixture capture of task `1d6feb3a`), (c) Phase 5c.3 `harness build-corpus`.

If the above is enough, stop reading this file and go.

If you need depth (inheriting a blocker, verifying a claim, picking next move), continue.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session: 2026-05-22 afternoon (WASB Step 5 verified, Phase 5e CLOSED, three follow-ups documented).

**TL;DR — where we are:**
- **Phase 5e WASB integration — VERIFIED IN PRODUCTION.** Batch task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` ran end-to-end on `ten-fifty5-ml-pipeline:47` (image `sha256:8fe82a3…`, `BALL_TRACKER=wasb`). CloudWatch confirmed `WASBBallTracker` ran the main pipeline: 15,298 frames inferred, 8,303 detected raw (54.3%), 17 valid bounces, pipeline complete in 2,258s. Bronze SQL matches the log. Production verification PASS.
- **Phase 5c.2 shipped + tested.** Pair-completion hook + corpus table + view + backfill endpoint. Local tests pass (`training_corpus` 11-column schema, UNIQUE constraint enforced, hook wired in `_do_ingest_t5`). All dark behind `AUTO_LABEL_DUAL_SUBMIT_PAIRS=0` until flipped.
- **Silver bench shipped + tested.** `snapshot.py` + `bench.py` orchestrator end-to-end (CLI smoke, schema init creates all 24 tables on fresh Docker Postgres, container lifecycle clean). Empty fixture set — first capture pending Render shell.
- **Ball-tracker bench DONE.** v2 metric, locked baseline at `7100792`.
- **Phase 5c.0+5c.1 ready to flip.** `AUTO_DUAL_SUBMIT_T5=1` is the first flip; `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` is the second.

**Three follow-ups from WASB verification (none blocking, all worth investigating):**

1. **Chain-rejection in `_filter_outliers`** (ball_tracker.py:551, copied to wasb_ball_tracker.py). WASB processed all 15,298 frames but bronze rows only span frames 2-3329. `_filter_outliers` is dropping all detections >150px from the last kept one, and a single "teleport" can lock the reference position early in the match and reject everything after. Pre-existing BallTracker bug; WASB inherited it verbatim. Fix shape: when a candidate is rejected, allow it to BECOME the new `prev` if N consecutive subsequent detections are all close to it (indicating the ball really did move there). ~1 session.

2. **`source='main'` tag missing.** All 483 rows from task `1d6feb3a` have `source IS NULL`. Phase 5a Option A only tagged historical rows during the migration; new main-pass writes via `ml_pipeline/db_writer.py` don't set source. Loses the `main` vs `roi_prod` diagnostic distinction going forward. One-line fix: pass `source='main'` default in the main-pass INSERT. ~10 min.

3. **First silver-bench fixture should be task `1d6feb3a`.** This was the WASB production verification run. Capturing it as a silver-bench fixture validates both (a) silver builder against post-WASB bronze, and (b) catches the filter chain-rejection in a reproducible local form. Replaces or augments `880dff02` as the canonical first fixture.

**Open admin items:**
- 5c.2 boot verification on Render — confirm `ml_analysis.training_corpus` table + `gold.vw_dual_submit_pairs` view exist after redeploy (SQL at §"5c.2 verification" below).
- Silver bench first-fixture capture — needs one Render-shell run. Playbook: `.claude/strategy/silver_bench_design_2026-05-21.md` §11.
- Render Postgres still open to `0.0.0.0/0` (since 2026-05-21 Phase 5a). Re-lock to `105.214.8.31/32` or build NAT Gateway + EIP.
- Option A Batch verification: task `6a8a344f-93bb-49af-8456-88d81a5dd7e3` — older in-flight, still open.
- Old GPU box `i-0fb3983fa555c16e3` (eu-north-1a) parked stopped (~$3.70/mo EBS).

Read in this order before doing anything else:

1. `.claude/strategy/silver_bench_design_2026-05-21.md` — silver bench design + §11 bootstrap playbook.
2. `.claude/strategy/dual_submit_status_2026-05-20.md` — Phase 5c.2 design (shipped) + 5c.3-5c.5 ahead.
3. `docs/north_star.md` — macro plan; Phase 5e SHIPPED + VERIFIED.
4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST + silver-bench subsection.

Then run the locked serve-bench locally to confirm the floor:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one (recommended order: 1 → 2 → 3 → 4 → 5):**

**Option 1: Activate what's shipped.** Tomo-side actions: (a) flip `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render's main API service; (b) run snapshot for first silver-bench fixture from Render shell + upload to S3 + pull locally + bench; (c) run `/ops/backfill-pair-labels` to retro-seed existing pairs. Each is <10 min once started. Closes both shipped builds to fully-live state.

**Option 2: Capture task `1d6feb3a` as the first silver-bench fixture.** Combines two follow-ups — exercise the new silver bench end-to-end AND create the reproducible artefact for investigating the chain-rejection bug. Same workflow as Option 1(b) but pointed at `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` instead of `880dff02`.

**Option 3: Fix the source='main' tag in db_writer.py (~10 min).** One-line addition. Preserves the Phase 5a diagnostic distinction. Risk: low (additive). Tests via the silver bench after Option 2.

**Option 4: Fix `_filter_outliers` chain-rejection (~1 session).** Design the "candidate becomes new prev after N consistent neighbours" rule, implement in ball_tracker.py + wasb_ball_tracker.py (mirror both), test via ball-tracker bench. **Recommended after Option 2** so we have a reproducible failure case to validate against.

**Option 5: Phase 5c.3 — `harness build-corpus` subcommand (~3-4 hr).** Pure local; reads `training_corpus`, pulls labels + videos from S3, assembles dataset for training. Spec at `.claude/strategy/dual_submit_status_2026-05-20.md` §4. **Note:** consumer ships before producer fires meaningfully — speculative until 5c.2 is activated and has rows.

**Strategic frame (Tomo's):** silver is derived from bronze; the goal is to make bronze 100% correct and aligned to SportAI. WASB shipping was the biggest single bronze-quality move this quarter. **Activation (Option 1) is the highest-leverage move because both shipped builds are otherwise dormant.** Then Options 2-4 attack the WASB follow-ups — directly improves bronze quality further.

**Things NOT to do** (load-bearing):

- **Don't merge `ball_tracker.py`, `wasb_ball_tracker.py`, `wasb_hrnet.py`, `config.py`, `pipeline.py`, or `Dockerfile` changes without BATCH-SIDE CHANGE CHECKLIST.** See CLAUDE.md item #8.
- **Don't rollback WASB without running the bench against TrackNetV2 first.** Rollback = `aws batch update-job-definition` clearing the `BALL_TRACKER` env var; previous revs (`:46`/`:28`) kept on standby.
- **Don't try to add the stroke-classifier hook (G10) to the Render API process.** `export_training_data.py` imports cv2, opens the video locally, runs optical flow — none of which works on Render. G10 belongs in Phase 5c.5 on the GPU box.
- **Don't change the `_dual_submit_pair_complete_hook` env-flag default to ON without an explicit go from Tomo.** Default-OFF is the safety; flip is a Render-side action.
- **Don't bypass the `_filter_outliers` filter when investigating follow-up #1.** The filter exists because raw TrackNetV2 output has wild noise; the fix is making the filter *smarter* (re-anchor when a new position has consistent neighbours), not removing it.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD` (motion-fallback noise is structural).
- Don't drop `test_videos/` from the GPU rsync.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys.
- Don't create parallel bronze tables. One canonical bronze, distinguished by `source`.

---

## State at session end (2026-05-22 afternoon)

**`origin/main` at `83e1ab7` (or whatever the close-checklist commit lands at — see git log).** Commits relevant to current state:

- `83e1ab7` silver bench: implement snapshot + orchestrator (steps 2+4 of the design)
- `4fba821` docs: pickup refresh — Phase 5c.2 shipped, next move = G10 stroke-classifier hook
- `d7718e0` phase 5c.2: pair-completion hook + ml_analysis.training_corpus
- `0aa5a79` session close (2026-05-21 deep eve): pickup refresh + north_star Phase 5e
- `5e3e746` silver bench: scaffolding + Docker Postgres lifecycle helper
- `afe4a56` docs: pickup refresh — WASB swap shipped to production (rev 47 / rev 29)
- `4a39588` WASB swap: env-gated drop-in for BallTracker (default still tracknet_v2)
- `7100792` ball-bench v2 baseline: WASB wins on 880dff02 SA point 6 (0/9 -> 2/9)
- `5319ed7` ball-bench metric v2: post-filter + trajectory coherence + tier breakdown
- `98d20bf` phase 5c.1: add /ops/dual-submit-t5-backfill endpoint

**Ball-bench baseline locked at `7100792`.** Serve bench: a798eff0 20/24, 880dff02 23/24.

**Batch state — VERIFIED 2026-05-22:**
- eu-north-1 `ten-fifty5-ml-pipeline:47` → `sha256:8fe82a3…` (with `BALL_TRACKER=wasb`)
- us-east-1 `ten-fifty5-ml-pipeline:29` → same digest, same env var
- Previous active revs (eu :46 / us :28) kept for instant rollback
- Lambda submits by job-def name — new jobs auto-resolve to these revs

**Silver-builder bench:** snapshot + orchestrator implemented (`83e1ab7`). Schema init verified locally — all 24 expected tables on fresh Docker Postgres including the new 5c.2 `training_corpus`. Empty fixture set — first capture pending.

**GPU dev box:** `i-0295d636f6bf957eb` (eu-north-1b), stopped. Old `i-0fb3983fa555c16e3` (1a) parked stopped.

**Render Postgres allowlist:** `0.0.0.0/0` (open admin item).

---

## WASB Step 5 verification — receipts (Batch task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3`)

```
Batch job:           t5-tenni-1d6feb3a (12bebf4c-0c24-40b5-981d-c8a77c41f6e2) — SUCCEEDED
Image:               sha256:8fe82a361023be8db4f50dd188bab74d12700740ed0d0c208d8c6458b94b34fa
BALL_TRACKER env:    wasb
Job-def used:        ten-fifty5-ml-pipeline:47
Video:               wix-uploads/1779386702_match.mp4 (Tomo vs Jimbo Ma, ~10 min)

CloudWatch log (excerpt):
  18:49:05 INFO ml_pipeline.wasb_ball_tracker: === WASBBallTracker diagnostics ===
  18:49:05 INFO ml_pipeline.wasb_ball_tracker: frames_inferred:       15298
  18:49:05 INFO ml_pipeline.wasb_ball_tracker: detected:              8303 (54.3%)
  18:49:05 INFO ml_pipeline.wasb_ball_tracker: detect_bounces (wasb): found 17 bounces (after validation)
  18:49:05 INFO ml_pipeline.pipeline:           Pipeline complete in 2258.9s (147.6 ms/frame)

Bronze SQL:
  total_detections:      483
  bounces:               17    (matches log)
  with_court_coords:     136
  max_speed_kmh:         243.69
  avg_real_shot_speed:   96.6 km/h
  source breakdown:      all NULL (follow-up #2)
  frame range in DB:     2-3329 (follow-up #1 — WASB processed all 15298)
```

**Verdict: Phase 5e PASS.** WASB live in production, producing physically plausible bronze.

---

## Phase 5c.2 — verification (run after Render redeploys `83e1ab7`)

Boot-init creates these idempotently. Verify via `/ops/diag/sql`:

```sql
-- 1. Corpus table exists?
SELECT count(*) AS column_count
  FROM information_schema.columns
 WHERE table_schema = 'ml_analysis' AND table_name = 'training_corpus';
-- Expect: 11

-- 2. Dual-submit view exists?
SELECT count(*) AS column_count
  FROM information_schema.columns
 WHERE table_schema = 'gold' AND table_name = 'vw_dual_submit_pairs';
-- Expect: 11

-- 3. Any existing pairs? (Won't exist until AUTO_DUAL_SUBMIT_T5=1 is flipped
--    AND a tennis_singles match is uploaded, OR a backfill is run.)
SELECT sa_task_id, t5_task_id, s3_key, pair_complete, paired_at
  FROM gold.vw_dual_submit_pairs
 WHERE pair_complete = TRUE
 ORDER BY paired_at DESC
 LIMIT 5;
```

Activation sequence:
1. Flip `AUTO_DUAL_SUBMIT_T5=1` on Render → next SA upload spawns a paired T5.
2. After T5 completes → confirm two `submission_context` rows for the same `s3_key`.
3. Flip `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` on Render → next pair-completion fires the hook.
4. Confirm one row in `ml_analysis.training_corpus`.
5. `POST /ops/backfill-pair-labels` with `{"dry_run": true}` to find any pre-flip pairs needing retroactive labeling. Then `dry_run=false` to label them.

---

## Silver bench — first-fixture capture (run on Render shell)

Full playbook in `.claude/strategy/silver_bench_design_2026-05-21.md` §11. Quick version:

```bash
# On Render shell (in webhook-server's project dir):
python -m ml_pipeline.diag.bench_silver.snapshot \
    --task 1d6feb3a-4624-47ae-b8f5-44246b6d0eb3

# Output: ml_pipeline/fixtures_silver/1d6feb3a_bronze.sql.gz + _silver_baseline.json
# Upload to S3:
python -c "
import boto3
s3 = boto3.client('s3')
for f in ['1d6feb3a_bronze.sql.gz', '1d6feb3a_silver_baseline.json']:
    s3.upload_file(f'ml_pipeline/fixtures_silver/{f}',
                   'nextpoint-prod-uploads', f'fixtures/silver/{f}')
"

# Then locally:
aws s3 cp s3://nextpoint-prod-uploads/fixtures/silver/ \
          ml_pipeline/fixtures_silver/ --recursive
.venv/Scripts/python -m ml_pipeline.diag.bench_silver --setup
.venv/Scripts/python -m ml_pipeline.diag.bench_silver
# Expect: green for the fixture (silver builder is deterministic).
```

Once green, commit the baseline JSON (the `.sql.gz` stays gitignored):
```bash
git add ml_pipeline/fixtures_silver/1d6feb3a_silver_baseline.json
git commit -m "silver bench: lock baseline from production capture"
```

Repeat for `880dff02` (the canonical bench reference) for a second fixture.
