# Session review — 2026-05-21 morning, Phase 5a anchor-strategy pivot

**Owner:** Claude (continuation of overnight 2026-05-20 build session)
**Status at handover:** anchor defaults changed, bench green, pushed; **still gated on Step A confirmation + Step F batch round**.

---

## TL;DR

The overnight 2026-05-20 build implemented the kickoff doc's option (c) verbatim: anchors = ball detections in service-box zone, clustered at gap=0.5s. Stage 1 on a798eff0 showed it WORKS (precise dt=0.12s SA-T5 match where a window overlapped), but it covers very few serves.

This morning, using infrastructure from the parallel agent session (the GPU dev box wasn't needed yet, but the strategy docs caused the analysis), the **Step A diagnostic was answered offline from the 880dff02 bench fixture** — saving the live-DB round-trip. The answer revealed the kickoff doc's anchor strategy is poorly chosen for this match: it covers only **1/24 SA serves (4%)** because bronze detections are temporally concentrated in 4 distinct 10s buckets.

A four-strategy comparison (table below) found **bounce-only-no-zone covers 6/24 (25%)** — a 6× improvement available by flipping two parameter defaults. Both defaults changed, behaviour preserved as opt-in flags for diagnostic flexibility. Bench unchanged. Branch updated and pushed.

## What the 2026-05-21 morning session did differently

| Action | Where |
|---|---|
| Read other agent's strategy docs (infra audit + dual-submit + T5-vs-SA + GPU box runbook) | `.claude/strategy/`, `.claude/infrastructure/` |
| Confirmed sequencing: silver bench AFTER 5a Step F lands (commit 91e9558 caveat) | `.claude/strategy/infrastructure_audit_2026-05-20.md` §9 |
| Realised the 880dff02 bench fixture HAS the same data Step A queries | `ml_pipeline/fixtures/880dff02.pkl.gz` |
| Ran 4-strategy diagnostic on the fixture | new script `ml_pipeline/diag/probe_roi_anchor_strategy.py` |
| Found current default covers 1/24 serves; bounce-only-no-zone covers 6/24 | output captured in §"Anchor strategy comparison" below |
| Changed defaults in `extract_far_bounces` + call site | `bounces.py` + `__main__.py` |
| Made strategy configurable via two kwargs for future tuning | `anchor_zone_filter` + `anchor_bounce_only` |
| Re-ran bench (still green at 20/24 + 23/24) | no change required, both fixtures' silver/serve pipelines unaffected |

## Anchor strategy comparison (the load-bearing table)

```
=== 880dff02 (fps=25.0, window_s=2.5s, cluster_gap_s=0.5s) ===
  total ball_rows: 1983
  bounces:         162
  match length:    590.1s (14753 frames)
  SA serves:       24

=== Step A SQL diagnostic equivalent (from fixture) ===
  total (in service-box zone): 153
  bounces (in zone):           9
  distinct 10s buckets:        4

strategy                      anchors  clusters  windows   cov_s   serves_covered
------------------------------------------------------------------------------------
zone=T, bounce=F                  153         6        3    25.2        1/24 (4%)
zone=T, bounce=T                    9         7        3    20.4        0/24 (0%)
zone=F, bounce=F                 1983        28       21   123.9       5/24 (21%)
zone=F, bounce=T (DEFAULT)        162        96       14   175.5       6/24 (25%)
```

The kickoff doc proposed `zone=T, bounce=F`. The data says `zone=F, bounce=T` is **6× better at the metric that actually matters** (SA serves with at least one ROI window overlapping their timestamp).

### Why bronze detections are clumped

The bronze ball-detection pipeline produces a long stream of TrackNet hits during long rallies (frame-by-frame in service-box zone) but goes silent between rallies. With cluster_gap_s=0.5s, an entire rally collapses to one cluster centred in its middle — and the OUTSIDE of those clusters (where most serves actually happen) gets no window. Bounce-only anchoring works because bronze bounces are temporally sparse (162 bounces in 14753 frames vs 1983 detections) and roughly correlate with discrete ball-strike events.

### Why the fixture diagnostic is trustworthy

`ml_pipeline/diag/snapshot_task.py` writes the fixture by querying the same `ml_analysis.ball_detections` table that Step A would query, filtered by the same job_id. The two fixtures happen to share IDENTICAL `ball_rows` because both T5 tasks (`a798eff0` and `880dff02`) processed the SAME dual-submit video — bronze detection is deterministic, same input → same output. The bench numbers differ between fixtures (20/24 vs 23/24) because the *pose* data differs (different runs of YOLO/ViTPose), not the ball data.

So the offline answer for 880dff02 = the live answer for 880dff02 = 153 in-zone anchors / 9 in-zone bounces.

## Files changed this morning

| File | Change |
|---|---|
| `ml_pipeline/roi_extractors/bounces.py` | `_select_anchors` gains `zone_filter: bool=False` + `bounce_only: bool=True` kwargs (new defaults). Docstring tells the diagnostic story. `extract_far_bounces` exposes the two flags as kwargs. Empty-anchor log message updated. |
| `ml_pipeline/__main__.py` | Production call site passes `anchor_zone_filter=False, anchor_bounce_only=True` explicitly. Comment block explains why. |
| `ml_pipeline/diag/probe_roi_anchor_strategy.py` | **New** — pure-Python pre-flight diagnostic. Loads a bench fixture, replicates the Step A SQL count, runs four anchor strategies through clustering + windowing, reports SA-serve coverage. Use for future fixtures or tuning attempts. |
| `.claude/session_2026-05-21_phase5a_pivot.md` | This file. |

## What this changes for Step F

- The BATCH-SIDE CHANGE CHECKLIST is unchanged — same files touched, Docker rebuild + dual-region ECR push + new job-def revisions still required.
- Stage 2 measurement on 880dff02 will now likely show ~14 ROI windows (vs the ~3 the original default would have produced).
- Expected `ball_detections_roi` row count: hundreds to low thousands (vs the dozens we'd have seen with 3 windows × ~150 frames).
- The 0/17 per-point reconciler baseline should move IF the bounce-yield per window is comparable to Stage 1's 5-10 bounces × 14 windows = ~70-140 fresh bounces in service-box zone.

## What this does NOT solve

- **Coverage is still only 25% of serves on 880dff02.** Phase 5 done-when criteria are ≥50% ball-coverage frame-by-frame, longest gap <5s, SA point 6 ≥3 detections, ≥30% point reconciler match. ROI bounces alone won't hit all four — it's an additive +X% contribution per Phase 5a's scope.
- The remaining 18/24 serves the bounce-only-no-zone strategy doesn't cover have NO bronze bounce signal anywhere near them. To cover those, you'd need:
  - WASB integration (better bronze detector → more bronze bounces in the right places) — strategy doc identifies this as the highest-leverage next move
  - Or option (b): anchor on serve_detector pose events (Render-side stage)
  - Or option (a): scan entire video — manageable on GPU dev box (~150s/match)

These are post-5a discussions; don't shoehorn them into 5a now.

## Validation done

1. ✅ Bench green at start (carry-over from overnight session): `7fb9bdd` → 20/24 + 23/24
2. ✅ Probe diagnostic on 880dff02 fixture → 4-strategy comparison reproducible
3. ✅ Code change: `extract_far_bounces` defaults pivoted, call site updated explicitly
4. ✅ Bench green post-change: `7fb9bdd` (HEAD before my new commit) — unchanged because both fixtures' bench-relevant inputs (pose) are unaffected by 5a edits. After my new commit it'll show new HEAD.

## What still needs Tomo

1. **Step A confirmation (optional now).** The fixture diagnostic IS Step A's answer. But if you want a live-DB cross-check, the same `/ops/diag/sql` query is still in `.claude/next_session_pickup.md`. Expected output matches the fixture: total ≈ 153, bounces ≈ 9, distinct_10s_buckets ≈ 4.

2. **Step F — BATCH-SIDE CHANGE CHECKLIST.** Docker rebuild + dual-region ECR push + new job-def revisions in eu-north-1 + us-east-1. Then rerun `880dff02` via the frontend.

3. **Stage 2 measurement** — query `ml_analysis.ball_detections_roi` for the 880dff02 rerun, re-run `bench`, `audit_points_reconcile`, `reconcile_serves_strict`.

## Risks

- Strategy assumes the temporal distribution on 880dff02 generalises. Other matches may behave differently. The probe script makes it trivial to verify per-fixture; future Claude can run it on any new fixture.
- 14 windows on 880dff02 means ~14× the TrackNet compute on the Batch GPU vs the original 3-window design. Estimated 14 × ~30s/window on GPU = 7 min added to Batch runtime — well within the 3600s timeout, but worth noting.
- If Step F's Stage 2 measurement shows 0 row delta (bench stays at 23/24 with no improvement), the bounce-only-no-zone default may also be wrong for some reason and we'd revisit options (a) or (b).
