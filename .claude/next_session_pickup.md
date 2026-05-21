# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous sessions: 2026-05-20 (overnight Phase 5a build), 2026-05-21 morning (anchor-strategy pivot), 2026-05-21 PM (Stage 2 measured + **Option A delivered in same session**).

**TL;DR — where we are:**
- **Phase 5a is fully shipped end-to-end.** ROI bounce extractor writes directly into canonical `ml_analysis.ball_detections` with `source='roi_prod'`. Silver and serve_detector see the rows through their existing single-table loaders — no downstream code change needed.
- **Validated on task `763c9ee9-e5ea-42ab-820a-7d53f6a7316c`** via SQL migration + `harness rerun-silver`: silver went **160 → 183 rows (+23)**, serves **16 → 19 (+3)**, including the **first NEAR T5 serve we've ever seen in silver** (id=92, ts=178.76s, hit_y=24.05). Bench unchanged: a798eff0=20/24, 880dff02=23/24.
- **Batch is on the new image:** eu-north-1 rev 46, us-east-1 rev 28, both pinned to `sha256:87435dbfd…`. Every future Batch run writes to canonical bronze.
- **Full session detail:** `.claude/session_2026-05-21_phase5a_stage2.md` (including the ADDENDUM showing Option A execution).

**Open admin item:** Render Postgres is still open to `0.0.0.0/0` (we opened it 2026-05-21 to unblock Batch). Re-lock to home IP `105.214.8.31/32` OR build the NAT-Gateway-with-EIP proper fix. See `feedback_render_postgres_ip_allowlist`.

Read in this order before doing anything else:

1. `.claude/session_2026-05-21_phase5a_stage2.md` — **REQUIRED.** The full Stage 2 + Option A story. ADDENDUM section shows what shipped.
2. `docs/north_star.md` — macro plan. Phase 5a is now ACTIVE → DONE; next phases.
3. `.claude/strategy/infrastructure_audit_2026-05-20.md` — the prioritised roadmap for what comes next. Top items now unblocked: silver-builder bench harness (#2), WASB integration (#3), ball-tracker bench (#4).
4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST is still required for future Batch deploys.

Then run the bench locally to confirm the floor is still locked:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions.

**Next move — pick one:**

**Option 1: Silver-builder bench harness (highest leverage from the audit).** Per the 2026-05-21 sequencing caveat in `infrastructure_audit_2026-05-20.md` §9, this should be baselined now that Phase 5a Step F has landed. The harness converts silver-builder iteration from 5-10 min Render round-trips to seconds. ~1 session of build. Files to scaffold:
- `ml_pipeline/diag/bench_silver.py` (mirrors `bench.py`'s shape)
- `ml_pipeline/diag/silver_baseline.json` (per-task expected row counts + serve counts)
- Snapshot fixtures via existing `snapshot_task.py` pattern
- CI: extend `.github/workflows/bench.yml` to gate silver too

**Option 2: Verify Option A with a real Batch run.** Upload any video as Singles T5 from the frontend (Tomo's action). After SUCCEEDED, confirm `ml_analysis.ball_detections WHERE job_id = <new_id> AND source = 'roi_prod'` returns non-zero. This is a low-risk integration test — proves the new image (rev 46/28) behaves as designed without depending on validation infra. ~30-60 min of Batch wait.

**Option 3: WASB integration (the +9pp F1 win from the market scan).** Uses the GPU dev box for the first time. ~1-2 sessions. Item #3 in the audit. Depends on having Option A's coverage gain measured, then adding WASB as a parallel ball-detection path.

**Option 4: NAT Gateway + static EIP for Batch + re-lock Render Postgres.** The proper security fix. 30-60 min VPC networking task. Removes the `0.0.0.0/0` hole. Useful before any non-trusted environment changes.

**Recommendation:** start with Option 2 (cheap proof Option A's deploy works on a fresh task), then Option 1 (silver bench — biggest leverage). Option 4 can run in parallel with anything since it's pure infra. Option 3 is the medium-term unlock.

**Things NOT to do** (load-bearing — restating from `CLAUDE.md`, `docs/north_star.md`, and the feedback memories):

- Don't re-attempt Phase 5b motion-threshold tuning. Round 0 receipts are conclusive; branch `phase-5b/motion-threshold-reduce` retained on origin as falsified-hypothesis record.
- Don't create parallel bronze tables. **One canonical bronze, distinguished by `source` column.** See `feedback_t5_single_canonical_bronze` for the architectural rationale we landed 2026-05-21 PM.
- Don't ship a Batch round without the BATCH-SIDE CHANGE CHECKLIST. `roi_extractors/`, `__main__.py`, `serve_detector/` are all in-container.
- Don't ship without bench green.
- Don't skip the non-fatal try/except around the call site in `__main__.py`. 5a is additive; failure must not block silver/trim/notify.
- Don't merge a feature branch to main without verifying Batch is in sync (`git diff origin/main HEAD --stat` against the in-container file list).
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD`.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't ask Tomo to do Docker work — agent handles deploys. See `feedback_agent_handles_deploys`.

---

## State at session end (2026-05-21 PM)

**`origin/main` at `5d1e818`** (Option A merge).
**`origin/phase-5a/roi-bounce-extractor`** — original build, kept as history.
**`origin/phase-5a/bronze-write-direct`** at `7d8bfaa` — Option A code change.
**`origin/phase-5b/motion-threshold-reduce`** — falsified-hypothesis record.

Most-relevant recent commits on main:

- `5d1e818` Merge phase 5a Option A: write ROI bounces to canonical ball_detections
- `7d8bfaa` phase 5a Option A: write ROI bounces to canonical ball_detections
- `90288ef` docs: 2026-05-21 PM — Phase 5a Stage 2 measured + Option A planned
- `c1370ed` Merge branch 'phase-5a/roi-bounce-extractor' (original Phase 5a build to main)
- `a2854db` docs: 2026-05-21 — Phase 5a anchor pivot session review + pickup refresh
- `91e9558` strategy: infra audit - sequence silver bench AFTER 5a Step F lands

**Batch state:**
- eu-north-1 job-def `ten-fifty5-ml-pipeline:46` → image `@sha256:87435dbfd…` (Option A)
- us-east-1 job-def `ten-fifty5-ml-pipeline:28` → image `@sha256:87435dbfd…` (Option A)

**Bench at session end:** `commit=5d1e818`, a798eff0=20/24, 880dff02=23/24, no regressions.

**Working tree at session end:** clean except `ml_pipeline/training/visual_debug/` (untracked, deliberately ignored).

**Render Postgres allowlist:** `0.0.0.0/0` (temporary — needs re-lock).

**DB migration done in this session** (for `763c9ee9` only):
- `ALTER TABLE ml_analysis.ball_detections ADD COLUMN IF NOT EXISTS source TEXT`
- Tagged 1983 main-pass rows as `source='main'`
- INSERTed 459 rows from `ball_detections_roi` with `source='roi_prod'`

Other tasks (e.g. 880dff02) still have their ROI data in `ball_detections_roi` only — not migrated. Cleanup migration is a future-session task (low priority — old data, no active impact).
