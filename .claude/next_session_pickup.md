# Next-session pickup — paste this verbatim into the next chat

> One canonical "what to do first" doc, overwritten at the end of each session.
> Paste the block below (everything between the `---` markers) as the opening
> message of the next Claude Code chat. Fill in `[DATE]` before pasting.

---

T5 ML pipeline session pickup. Today is [DATE]. Previous session was 2026-05-20 (overnight Phase 5a build).

**TL;DR change since last handover:** Phase 5a `extract_far_bounces` was **built, wired into Batch, and Stage-1 validated locally** during an autonomous overnight session. Bench floor still locked (a798eff0=20/24, 880dff02=23/24). Code is on **branch `phase-5a/roi-bounce-extractor`** — pushed but NOT merged. The remaining steps are gated on a single SQL probe (Step A) and the BATCH-SIDE CHANGE CHECKLIST (Step F). See `.claude/session_2026-05-20_phase5a_overnight.md` for the full session detail.

Stage 1 produced clean signal: SA serve at 178.44s matched to ROI bounce at 178.32s (dt=0.12s, correct service-box half), 15 bounces total across 2 windows on the `a798eff0` fixture in 31 min CPU.

Read in this exact order before doing anything else:

1. `.claude/session_2026-05-20_phase5a_overnight.md` — **REQUIRED.** Detailed session review with: files changed, design decisions, Stage 1 results + one observation worth knowing (cluster granularity is coarser than expected — 6 clusters from 153 anchors).

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

**A. (BLOCKING)** Anchor-source diagnostic on `880dff02`. Single SQL via `/ops/diag/sql` (needs `OPS_KEY`):

    SELECT count(*)                          AS total,
           count(*) FILTER (WHERE is_bounce) AS bounces,
           min(frame_idx) AS first_frame,
           max(frame_idx) AS last_frame,
           count(DISTINCT frame_idx / 250) AS distinct_10s_buckets
    FROM ml_analysis.ball_detections
    WHERE job_id = '880dff02-58bd-412c-9a29-5c5151004447'
      AND court_x BETWEEN -1.5 AND 12.47
      AND court_y BETWEEN 3.985 AND 19.785;

Decision:
- `total ≥ ~30` AND `distinct_10s_buckets ≥ 10` → option (c) confirmed, proceed to **F**.
- `total < 10` → option (c) is wrong on this video. Two cheap fixes (single-file edit each):
  - **Drop anchor filter**: change `_select_anchors` to NOT filter on `_in_service_box_zone` — keep the output filter only.
  - **Use `is_bounce=True` only**: add `bounce_only=True` parameter / filter; sparser anchors with better serve alignment.

  Either is a small edit, but commit + push a new branch revision before Step F.

**F. (BATCH)** BATCH-SIDE CHANGE CHECKLIST (Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1). Both edited prod files are in-container. Then Tomo reruns `880dff02` via the frontend (per the run-monitoring rule — user self-serves).

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
