# Session review — 2026-05-20 overnight, Phase 5a build

**Owner:** Claude (autonomous overnight session)
**Status at handover:** built + locally validated; NOT pushed.

---

## TL;DR

Phase 5a `extract_far_bounces` is **built, wired into Batch entry point, and Stage-1 validated locally**. Bench floor still locked (a798eff0=20/24, 880dff02=23/24). Branch not pushed — needs human-authorised `git push`. After push, the only remaining steps are Step A (anchor source diagnostic — needs `OPS_KEY`) and Step F (BATCH-SIDE CHANGE CHECKLIST + 880dff02 rerun).

## What shipped (uncommitted, on `main` working tree)

| File | Change |
|---|---|
| `ml_pipeline/roi_extractors/bounces.py` | **Replaced the 48-line stub with ~320-line production extractor.** `extract_far_bounces(video_path, job_id, engine, *, court_detector, bounces, fps=25.0, window_s=2.5, cluster_gap_s=0.5, max_windows=None, source_tag='roi_prod', replace=True, return_rows=False)`. Anchor source = in-memory `result.ball_detections` filtered to service-box zone, temporal-clustered, ±window_s ROI windows with overlap merging. Same DDL as the diag tool (`ml_analysis.ball_detections_roi`). Idempotent on `(job_id, source)` via DELETE-then-INSERT when `replace=True`. |
| `ml_pipeline/__main__.py` | **New "step 2c" call site** at ~line 215 (right after the existing pose extractor). Non-fatal try/except matches the pose pattern exactly. `on_progress("roi_bounces", 80)`. Practice mode skipped (matches pose extractor). |
| `ml_pipeline/diag/replay_roi_bounces.py` | **New** Stage 1 local harness. Loads bench fixture, mocks BallDetection from `ball_rows`, calibrates court detector against local test video, calls `extract_far_bounces(engine=None, return_rows=True)`. Prints per-SA-truth nearest-bounce proximity table. |
| `.claude/next_session_pickup.md` | Rewritten as the next chat opener — points at this review for detail. |
| `.claude/session_2026-05-20_phase5a_overnight.md` | This file. |

Source tag: **`roi_prod`** (distinct from the diag tool's `roi_far` for traceability). The `serve_detector` merge logic at `detector.py:289-298` reads by `job_id` only, so the source distinction is bookkeeping.

## Design decisions worth remembering

1. **Service-box zone**: `−1.5 ≤ court_x ≤ 12.47` and `3.985 ≤ court_y ≤ 19.785` (1.5 m margin on both axes around the doubles-width × service-line-to-service-line rectangle). Used for both anchor filtering AND output filtering. Matches the diag tool.

2. **Anchor clustering**: anchors within `cluster_gap_s=0.5s` collapse to a single centroid. After that, windows are built as `[centroid − window_s, centroid + window_s + 1)` and overlapping windows are merged (so we never run TrackNet twice on the same frame).

3. **`engine=None` semantics**: skips the DB write but still runs the full TrackNet pass and returns row count (and rows if `return_rows=True`). This is what the Stage 1 harness uses — Stage 1 needs no DB at all.

4. **Non-fatal at the caller**: every code path inside `extract_far_bounces` that can't produce results (missing video, no anchors, no court detector, projection failure) logs + returns 0 instead of raising. Combined with the `try/except` in `__main__.py` that's a belt-and-braces guarantee for the additive-only contract.

5. **`court_detector` is REQUIRED** at the entry point. Unlike the pose extractor which falls back to re-calibrating from the video, we refuse to run without a detector — re-calibration would cost 10-20 s for zero gain, the pipeline already produces one.

## Validation done

1. ✅ **Bench green pre-edit** (014eb67): a798eff0=20/24, 880dff02=23/24, no regressions.
2. ✅ **Import smoke test**: `from ml_pipeline.roi_extractors.bounces import extract_far_bounces` clean; harness imports clean.
3. ✅ **Stage 1 harness — completed** (`--max-windows 2`, a798eff0 fixture, 31 min on CPU):

   - 1983 ball detections → 153 service-box-zone anchors → 6 temporal clusters → 2 merged windows (capped at 2)
   - Window 1: frames [55, 217), center=4.68s → 52 dets in zone, **5 bounces**
   - Window 2: frames [4439, 4669), center=180.04s → 202 dets in zone, **10 bounces**
   - Total: 254 rows, 15 bounces (engine=None — no DB write)

   **The temporal precision is the key result.** SA serve at 178.44s matched to ROI bounce at 178.32s — **dt=0.12s**, court_x=8.96, court_y=6.99 (correct half for a NEAR serve landing in the FAR service box). Where windows cover the SA serve, bounces land within a couple of frames. The pipeline does what the kickoff doc intended.

4. ✅ **Bench green post-edit** (c518bf0, same numbers): a798eff0=20/24, 880dff02=23/24.

### Observation worth noting — coarse cluster granularity

153 anchors collapsed to only 6 clusters with `cluster_gap_s=0.5s`. Reason: during rallies TrackNet fires nearly every frame in service-box zones, so an entire rally folds into one cluster centered in its middle. We get rally-period windows, not serve-period windows.

For this video that means ~6 ROI windows for 24 SA serves. That's still useful (each window covers a rally → produces bounces for ALL the serves in that rally), but it does mean some compute goes to "warmup noise" — Window 1 at ts=4.68 was 50 s before any SA serve (pre-match practice swings).

**Possible future refinements** (out of scope for tonight, raised for morning-Tomo):

| Lever | Effect |
|---|---|
| Filter anchors to `is_bounce=True` only | Bounces are sparse; would split rallies into per-event clusters → more, smaller windows targeted at actual ball-strike events |
| Require minimum cluster size (e.g. ≥10 anchors) | Drops warmup noise (1-2 stray detections) without touching the bounce-only design choice |
| Tighten `cluster_gap_s` (e.g. 0.2 s) | Splits long rallies but doesn't help the warmup case |

Decision deferred. The kickoff doc explicitly says "all detections" (not "bounces only"), and the Stage 2 measurement on 880dff02 will tell us whether the bounce yield is good enough as-is.

## What still needs to happen (in order)

### 1. Inspect Stage 1 output

Read `b3wx8vbpd.output`. Look for:
- `roi_bounces: N bounces → A anchors → C clusters → W merged windows` — confirms anchor pipeline produced sensible counts.
- `roi_bounces: service-box pixel ROI (...)` — confirms the court projection found pixel ROI.
- `roi_bounces: [k/W] frames [...) center=... -> N dets in zone, M bounces (T s)` — per-window output.
- `=== Per SA-truth proximity ===` table — for the windows we ran, are the closest ROI bounces within ~1-2 s of SA serve times? If yes, anchor logic is correct.

If the harness errored, common causes:
- Court calibration failed on a798eff0_sa_video.mp4 — would surface as `court calibration failed — no detection produced`. Unlikely (this video calibrates fine for the pose extractor).
- TrackNet weights missing — would surface inside `BallTracker.__init__`. We verified `tracknet_v2.pt` is present.
- A BallTracker config tweak between the diag tool (last touched a while ago) and now. Compare diag tool's `_run_roi_window` output to ours.

### 2. Step A — anchor source diagnostic on 880dff02

Decision-blocking SQL query. Needs `OPS_KEY`.

```bash
curl -sS -X POST https://api.nextpointtennis.com/ops/diag/sql \
  -H "X-Ops-Key: $OPS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "SELECT count(*) AS total, count(*) FILTER (WHERE is_bounce) AS bounces, min(frame_idx) AS first_frame, max(frame_idx) AS last_frame, count(DISTINCT frame_idx / 250) AS distinct_10s_buckets FROM ml_analysis.ball_detections WHERE job_id = ''880dff02-58bd-412c-9a29-5c5151004447'' AND court_x BETWEEN -1.5 AND 12.47 AND court_y BETWEEN 3.985 AND 19.785",
    "limit": 1
  }'
```

**Decision tree:**
- `total ≥ ~30` AND `distinct_10s_buckets ≥ 10` → option (c) confirmed, proceed to Step E + F.
- `total` < 10 → option (c) is wrong for this video. Two choices:
  - Drop the service-box-zone filter on anchors (use ANY ball detection), keep service-box-zone filter on outputs. Doubles anchor count but keeps output noise low.
  - Switch to option (b): anchor on `ml_analysis.serve_events` from serve_detector — requires moving extract_far_bounces out of Batch into a Render-side stage.

Recommendation if borderline: drop anchor filter, keep output filter. Single-file change.

### 3. Step E — commit + push

Branch suggestion: `phase-5a/roi-bounce-extractor`. Commit message:

```
phase 5a: production ROI bounce extractor

Replace the stub in ml_pipeline/roi_extractors/bounces.py with a working
extract_far_bounces that runs TrackNet on tight service-box crops around
in-memory result.ball_detections anchors. Mirrors the architectural shape
of extract_far_pose: same calibrated court_detector reuse, same in-memory
bounces input, same non-fatal try/except call site.

Anchor source is option (c) from the stub docstring — bronze ball_detections
filtered to the service-box zone, temporally clustered, ±2.5 s windows
with overlap merging.

Writes to ml_analysis.ball_detections_roi with source='roi_prod' (distinct
from diag tool's 'roi_far'). serve_detector merge logic reads by job_id
only and is unaffected.

Local Stage 1 validated on a798eff0 fixture. Bench unchanged
(a798eff0=20/24, 880dff02=23/24).

Phase 5a tracking: docs/north_star.md, .claude/phase5a_kickoff.md.
```

Files to add:
- `ml_pipeline/roi_extractors/bounces.py` (M)
- `ml_pipeline/__main__.py` (M)
- `ml_pipeline/diag/replay_roi_bounces.py` (??)
- `.claude/next_session_pickup.md` (M)
- `.claude/session_2026-05-20_phase5a_overnight.md` (??)

`ml_pipeline/training/visual_debug/` stays untouched per Tomo's instruction.

### 4. Step F — BATCH-SIDE CHANGE CHECKLIST

Both edited prod files (`__main__.py`, `roi_extractors/bounces.py`) are in-container. Per `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST": Docker rebuild + dual-region ECR push (eu-north-1, us-east-1) + new job-def revisions in both regions. THEN ask Tomo to rerun 880dff02 via the frontend.

### 5. Step G — Stage 2 measurement (after 880dff02 rerun completes)

```bash
# Did the extractor write anything?
curl ... /ops/diag/sql -d '{
  "sql":"SELECT count(*) AS rows, count(*) FILTER (WHERE is_bounce) AS bounces, count(DISTINCT window_serve_ts) AS windows FROM ml_analysis.ball_detections_roi WHERE job_id = ''880dff02-58bd-412c-9a29-5c5151004447'' AND source = ''roi_prod''",
  "limit":1
}'

# Bench still green?
.venv/Scripts/python -m ml_pipeline.diag.bench

# Per-point match rate vs Phase 4 baseline (0/17)
.venv/Scripts/python -m ml_pipeline.harness audit_points_reconcile 880dff02-58bd-412c-9a29-5c5151004447

# Optional: reconcile vs the bench baseline
.venv/Scripts/python -m ml_pipeline.diag.reconcile_serves_strict --task 880dff02-58bd-412c-9a29-5c5151004447 --honor-exclude
```

## Risks + open items

- **OPS_KEY not in this Claude session.** Step A needs it; can't run autonomously. If Tomo set up the gitignored `~/.ops_key` file for himself, the bash one-liners above will pick it up via `$(cat ~/.ops_key)`. Otherwise paste `OPS_KEY` directly.
- **Stage 1 timed out / errored?** Re-run with `--max-windows 1` for a quick sanity check. The code path is identical — fewer windows just means faster.
- **Practice mode**: skipped entirely (matches pose extractor's `if not practice` guard). Open question per kickoff doc — leave as-is for now.
- **Source tag bookkeeping**: if you want to delete the diag-tool's `roi_far` rows on the same task before Stage 2, the `replace=True` default only deletes rows with the same source tag, so legacy `roi_far` rows would coexist with new `roi_prod` rows. Not a bug — both are merged at serve_detector — but worth knowing.

## What is NOT on the menu

- Don't retry Phase 5b motion-threshold tuning. `phase-5b/motion-threshold-reduce` is the falsified-hypothesis branch.
- Don't lower `TRACKNET_HEATMAP_THRESHOLD`.
- Don't touch Tier-1 Hough config in `config.py`.
- Don't widen the service-box zone past the current ±1.5 m margin.
- Don't merge to `main` without Step A's result.
