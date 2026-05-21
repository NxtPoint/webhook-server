# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous sessions: 2026-05-20 (overnight Phase 5a build) + 2026-05-21 morning (anchor-strategy pivot).

**TL;DR change since last handover:** Phase 5a `extract_far_bounces` is **built, anchor strategy pivoted, and pushed**. The pivot — the kickoff doc's default (zone-filtered all-detections) covered only **1/24 SA serves** on the 880dff02 fixture. Defaults flipped to **bounce-only-no-zone** which covers **6/24 (25%)**, a 6× improvement available by flipping two parameter defaults. The 880dff02 fixture's `ball_rows` was snapshotted from the same DB Step A queries, so Step A is effectively answered offline: **153 in-zone anchors, 9 in-zone bounces, 4 distinct 10s buckets**. Bench still locked at a798eff0=20/24, 880dff02=23/24. **Step F (BATCH-SIDE CHANGE CHECKLIST + Tomo reruns 880dff02) is now the sole gating action.** See `.claude/session_2026-05-21_phase5a_pivot.md` for the 4-strategy comparison + rationale.

Stage 1 result that proved the design is sound: SA serve at 178.44s matched to ROI bounce at 178.32s (dt=0.12s, correct service-box half), on the `a798eff0` fixture.

Read in this exact order before doing anything else:

1. `.claude/session_2026-05-21_phase5a_pivot.md` — **REQUIRED.** This morning's pivot review with the 4-strategy diagnostic table and why bounce-only-no-zone wins.

2. `.claude/session_2026-05-20_phase5a_overnight.md` — **REQUIRED.** Original overnight build review with Stage 1 results + design decisions.

2. `docs/north_star.md` — macro plan. Phase 5a is ACTIVE; phase 5b is PARKED.

3. `.claude/phase5a_kickoff.md` — the original scoping doc; useful to cross-reference if you need to understand a design choice.

4. `.claude/handover_t5.md` — BATCH-SIDE CHANGE CHECKLIST section is mandatory before Step F.

Then run the bench locally to confirm the floor is still locked:

    .venv/Scripts/python -m ml_pipeline.diag.bench

Expect: `a798eff0` 20/24, `880dff02` 23/24, no regressions. If anything moved, stop and investigate before touching code.

Then check out the feature branch:

    git fetch origin
    git checkout phase-5a/roi-bounce-extractor

If bench green and branch is checked out, the moves are:

**A. (DONE — offline)** Anchor-source diagnostic on `880dff02`. The 880dff02 bench fixture was snapshotted from `ml_analysis.ball_detections` for this job_id, so its `ball_rows` IS the Step A answer. Pre-flight diagnostic from 2026-05-21 morning:

    .venv/Scripts/python -m ml_pipeline.diag.probe_roi_anchor_strategy \
        ml_pipeline/fixtures/880dff02.pkl.gz

Output:

    total (in service-box zone): 153
    bounces (in zone):           9
    distinct 10s buckets:        4

    strategy                      anchors  clusters  windows   cov_s   serves_covered
    zone=T, bounce=F                  153         6        3    25.2        1/24 (4%)
    zone=T, bounce=T                    9         7        3    20.4        0/24 (0%)
    zone=F, bounce=F                 1983        28       21   123.9       5/24 (21%)
    zone=F, bounce=T (DEFAULT)        162        96       14   175.5       6/24 (25%)

Defaults flipped to `anchor_zone_filter=False, anchor_bounce_only=True` accordingly. If you want a live-DB cross-check (Tomo had connection issues 2026-05-21), the same `/ops/diag/sql` query with `OPS_KEY` should produce matching counts (153 total, 9 bounces). If they differ materially, the fixture is stale — re-snapshot via `snapshot_task.py`.

**F. (BATCH — only remaining gate)** BATCH-SIDE CHANGE CHECKLIST (Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1). Both edited prod files are in-container. Then Tomo reruns `880dff02` via the frontend (per the run-monitoring rule — user self-serves).

**G. (MEASUREMENT)** After 880dff02 rerun completes:

    -- Did the extractor write rows?
    SELECT count(*) AS rows,
           count(*) FILTER (WHERE is_bounce) AS bounces,
           count(DISTINCT window_serve_ts) AS windows
    FROM ml_analysis.ball_detections_roi
    WHERE job_id = '880dff02-58bd-412c-9a29-5c5151004447'
      AND source = 'roi_prod';

Then:

    .venv/Scripts/python -m ml_pipeline.diag.bench
    .venv/Scripts/python -m ml_pipeline.harness audit_points_reconcile 880dff02-58bd-412c-9a29-5c5151004447
    .venv/Scripts/python -m ml_pipeline.diag.reconcile_serves_strict --task 880dff02-58bd-412c-9a29-5c5151004447 --honor-exclude

Success criteria per `docs/north_star.md` Phase 5:
- T5 ball-detection frame coverage ≥ 50% (currently 13%)
- Longest no-ball gap < 5s (currently 91.6s)
- SA point 6 has ≥ 3 T5 ball detections in window (currently 0)
- Phase 4 reconciler per-point match rate ≥ 30% (currently 0/17)

Phase 5a alone may not hit all four — these are Phase-5-wide targets. Measure the **delta** vs pre-5a baseline.

**Things NOT to do** (load-bearing — restating from `CLAUDE.md`, `docs/north_star.md`, `phase5a_kickoff.md`, and `session_2026-05-20_phase5a_overnight.md`):

- Don't merge `phase-5a/roi-bounce-extractor` to main without **Step A's result**. If anchors are sparse, the implementation needs a small change before shipping.
- Don't re-attempt Phase 5b motion-threshold tuning. Round 0 receipts are conclusive; branch `phase-5b/motion-threshold-reduce` is retained on origin as falsified-hypothesis record.
- Don't widen the service-box zone past the current ±1.5 m margin — the upsample-into-service-box trick IS the resolution gain.
- Don't ship a Batch round without the BATCH-SIDE CHANGE CHECKLIST. `__main__.py` AND `roi_extractors/` edits are both in-container.
- Don't ship without bench green.
- Don't skip the non-fatal try/except around the call site in `__main__.py`. 5a is additive; failure must not block silver/trim/notify.
- Don't re-attempt Phase 3 part 2 with any pure-SQL pattern. Same Phase 5 unblocker required.
- Don't touch `ml_pipeline/training/visual_debug/`.
- Don't tune Tier 1 Hough or lower `TRACKNET_HEATMAP_THRESHOLD`.

---

## State at session end (2026-05-20 overnight)

**`origin/main` at `c518bf0`** (Tomo's mobile dashboard fix landed mid-session; unrelated to T5).
**`origin/phase-5a/roi-bounce-extractor`** at session-end commit (Phase 5a feature work).

Bench locked at `a798eff0` 20/24 + `880dff02` 23/24 throughout. Zero detector-quality regression.

Most-relevant recent commits on main (newest first; full log via `git log --oneline -20`):

- `c518bf0` dashboards: mobile H2H — label row 1, both halves row 2 (Tomo, mid-session)
- `014eb67` docs: polish next_session_pickup — tighten TL;DR, fix stale state snapshot
- `590d43b` phase 5: park 5b, promote 5a — session 2026-05-20 pivot
- `833bca4` dashboards: audit pass — Y axis off everywhere, chart-grid widths uniform
- `d26e8cc` phase 5b: Round 0 findings — staged change falsified, candidate list reprioritised

What this overnight session contributed (the Phase 5a build):

- `ml_pipeline/roi_extractors/bounces.py` — replaced 48-line stub with ~320-line production `extract_far_bounces`. Anchor source = in-memory `result.ball_detections` filtered to service-box zone, temporal-clustered, ±2.5s ROI windows with overlap merging. Same DDL as the diag tool. Idempotent via DELETE-then-INSERT on `(job_id, source='roi_prod')`.
- `ml_pipeline/__main__.py` — new "step 2c" call site at line ~215, non-fatal try/except matching the pose extractor pattern.
- `ml_pipeline/diag/replay_roi_bounces.py` — Stage 1 local harness, no DB required.
- `.claude/session_2026-05-20_phase5a_overnight.md` — full session review.
- This file rewritten.

Bench-pre-edit + bench-post-edit confirmed identical (20/24 + 23/24). Stage 1 timing precision confirmed (dt=0.12s on the one SA serve a window covered). The branch is ready for review; the work to do next is Step A (anchor-source diagnostic) + Step F (Batch round).

**Branch `phase-5a/roi-bounce-extractor`:** pushed to origin. NOT merged to main. No PR opened yet (deliberate — let morning-Tomo create the PR after Step A confirms the design).

**Branch `phase-5b/motion-threshold-reduce`:** retained on origin as a falsified-hypothesis record. **Do not merge. Do not reuse the branch name.**

**Working tree at session end:** clean except `ml_pipeline/training/visual_debug/` (untracked, deliberately ignored).
