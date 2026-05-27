# Next-session pickup — 2026-05-27 (architecture codified; far-court ceiling reached; serve half-wired)

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" — the non-negotiable T5 architecture (bronze = single source of truth; silver inherits 100% / does no work; one-model-per-fact; build-first/train-last; keep-it-clean). All build happens in that vein.

**Date:** 2026-05-27
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` green; `bench_ball` green. ⚠️ this dev box is **CPU-only** → `bench_ball` ~3h (background it).
**What shipped this session:** Rules of the Game codified in the True North; doc cleanup (active `.claude` 26→7); 18-field architecture audit + **one-model-per-fact** blueprint (`docs/_investigation/bronze_silver_18_audit.md`); ROI Bug 2 fixed + **deployed to Batch** (eu job-def rev50 / us rev32); volley fix (4→2 m, 13→7); bounce diagnosis + proximity guard; speed plan (`t5_pipeline_speed.md`).

**THE BIG FINDING — the far-court ceiling:** serve precision, ball bounce, far-stroke, A/B identity are ALL blocked by one root cause — the far player is ~30 px and far bounces are missed, so the far half lacks corroborating signal. **The build phase has hit its ceiling with standard models on these fields.** Their remaining gains come from **coverage (Phase 5-7, lifts all four) + training**, NOT heuristics. (north_star §"★ The far-court ceiling".)

**Next session's job:** the unifying lever is **coverage (Phase 5-7)** — it lifts all four far-court fields at once. Training-data accumulation (dual-submit corpus) is now **unblocked** (ROI Bug 2 sped runs up). Cleaner non-far build items also remain (`set_number`).

---

## Serve — exactly where it stands (the "did we finish?" answer: NO)
- `serve_detector` (pose-first, bench 23/24 on fixtures) → writes `ml_analysis.serve_events`. ✓ Works, always has (53 rows for M1).
- **Silver still uses the bounce-geometric serve gate** — it does NOT inherit `serve_events`. The wiring was **attempted and reverted** this session (regressed points 17→11; commit history clean — nothing bad shipped).
- **Two blockers:** (a) `serve_events` over-fires on far `pose_only` (24 events, 22 FP on M1) — gating it is **proven-bad** (kills real far serves on other matches; `detector.py:539` NOTE) = far-court ceiling; (b) silver pass-3 needs `serve_side` from the serve, which model-sourced serves lack (the append regression).
- **To finish (later):** far-court coverage so far serves get bounce corroboration + have pass-3 read `serve_side` from the serve's hitter position (a bronze fact). Both = the coverage/training phase.
- The bounce-geometric gate stays a **tagged stopgap** until then. Full detail: `bronze_silver_18_audit.md` §"UPDATE 2026-05-27".

## Building the other models (the user's question)
"One model per fact" — `serve_detector` is the template. **Missing models:** swing-type classifier (fh/bh/overhead), a real bounce model, identity, volley. **But** the far-court ceiling means these can't reach 70-80% on the far half with standard models, so building them IS the coverage+training phase — a **fresh, bigger effort (new session).** Entry point: the dual-submit corpus pipeline is LIVE + unblocked; accumulate matches → train (e.g. stroke classifier Q1-D, weights → `ml_pipeline/models/stroke_classifier.pt`, currently absent).

## 18-field build status (Match 1 vs SA) — full table in north_star
✅ near serves/strokes, court mapping (faithful homography), volley (now 7 vs 6), point structure (17 vs 18), forehand (38/41), overhead (26/30).
❌/⚠️ far-court ceiling: ball bounce (recall 55%, 4.57 m), serve precision (far pose_only FPs), far-stroke fh/bh, A/B identity. `set_number` not populated (a clean buildable item, not far-limited).

## The 40-min validation run (corpus #2)
T5 `c645a7ee` (SA ref `ee12d918`): at ~63% as of ~11:00 UTC, healthy, ETA ~12:20, **under the 6h timeout** → validates ROI Bug 2. `ball_detections` populate AFTER Batch completes (Render ingest via `sweep-t5-orphans` cron). NEXT: confirm it completed + cleared the `roi_bounces` stage (the bug's fix point) + corpus #2 landed in `training_corpus`.

## Open tasks (board)
- **#12 / #13** coverage B1 (short-gap interpolation) / B2 (ball-detector fine-tune) — the unifying far-court lever.
- **#19** serve (BLOCKED — far-court + pass-3 coupling; do not gate pose_only).
- **#20** backhand over-count (28 vs 18) — swing inference, delicate.
- **#22** cleanup Stage 2 (ops-doc consolidation — largely satisfied by Stage 1 + RULES #5 doc structure; physical merge of the big ops docs deemed high-risk/low-value).
- `set_number` population (clean buildable item).

## Doc structure (per RULES #5 — keep it this way)
- `docs/north_star.md` = **True North** (rules + 18-field status + phase ladder + far-court ceiling).
- `.claude/next_session_pickup.md` = **this handover**.
- `.claude/handover_t5.md` = **ops / how-to-run** (bench, Batch deploy, BATCH-SIDE CHECKLIST, task IDs).
- `docs/_investigation/*` = **per-model/topic references** (`bronze_silver_18_audit`, `bounce_accuracy`, `far_player_accuracy`, `t5_pipeline_speed`).
- `.claude/{sop, session_protocol, docs_hygiene}` = general session process. Runbooks: `gpu_dev_box_runbook`, `playbook_aws_batch_ondemand_fallback`.
- Historical → `_archive/`.

## Read in this order
1. This file.
2. `docs/north_star.md` — RULES OF THE GAME → far-court ceiling → 18-field status.
3. `docs/_investigation/bronze_silver_18_audit.md` — the architecture + serve finding.
4. `.claude/handover_t5.md` — ops, if running/deploying.

## Local helpers (gitignored, in `.claude/tmp/`)
Probe scripts from this session (bounce reconcile, serve_events analysis, 18-field status, etc.) — reusable references for the next measurement pass.
