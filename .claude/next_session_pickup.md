# Next-session pickup — 2026-05-27 (late) — corpus auto-land UNBLOCKED; first hands-off run in flight

## ⚡ Executive summary (read first — 60 seconds)

**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME" — bronze = single source of truth; silver inherits 100% / does no work; one-model-per-fact; build-first/train-last; keep-it-clean.

**Date:** 2026-05-27 (late evening)
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` green; `bench_ball` green at BALL_BATCH_SIZE=8 (output-identical to baseline).
**What shipped tonight (all about productionising the dual-submit corpus pipeline so we can bulk-load training videos):**
1. **ROI single-decode** (`75c377d`) — 3→2 video decodes on Batch (fuse pose+bounce ROI passes). Batch-deployed.
2. **Silver pass-3 hang FIXED** (`70766c5`+`fdf0697`) — route-B staged temp-table + ANALYZE; the pass-3 correlated-subquery chain hung 21min on a 44-min match → now **2.1s**. Render-side.
3. **Sweep self-heal** (`fa0bba0`) — `/ops/sweep-t5-orphans` now re-fires ingests that died mid-flight (started_at set + finished_at NULL + stale), not just never-started orphans.
4. **GPU ball-batching** (`5317c50`+`1c286ed`) — env-gated `BALL_BATCH_SIZE`, **ACTIVATED on Batch** (job-def eu rev53 / us rev35, BALL_BATCH_SIZE=8). bench_ball green.
5. **T5 ingest OOM FIXED** (`8dc3b31`) — `ingest_bronze_t5` did json.loads of the WHOLE export → ~400-540MB peak → OOM-killed the 512MB main API on long matches (the *real* blocker). Now streams via ijson (peak ~15MB). Adds `ijson==3.5.0`.

**Proof:** `c645a7ee` (the 44-min T5 match) FULLY RECOVERED end-to-end — streaming ingest (35653 ball / 72565 player) → serve 261 / stroke 597 → silver 371 rows. corpus #2 landed (id=2). **AUTO_LABEL_DUAL_SUBMIT_PAIRS=1 confirmed live.**

**What's IN FLIGHT (don't disturb):** Video 3 — SA side `2f355924` (sport_type=tennis_singles) — the **first fully-hands-off dual-submit run**. The T5 counterpart auto-spawns (`_auto_dual_submit_t5`) when the SA ingest fires (~SportAI finish + ingest gate). A **watch (`be5ja755m`) is running in the ORIGINAL chat** (Tomo keeping it open) to report the T5 batch time + whether corpus #3 auto-lands. **⚠️ Do NOT push to main / redeploy Render while video 3 is mid-pipeline — a redeploy kills in-flight ingests** (this exact thing re-bit c645a7ee tonight).

**Next session's job:** (a) confirm video 3 → corpus #3 auto-lands cleanly = license to bulk-load training videos; then (b) the **detector-build phase** — see "What's next" below.

**Uncommitted / deferred:** `frontend/media_room.html` (+414/−230, resumable-upload WIP — NOT this session's; left untouched for Tomo). Tonight's close-doc `git push` was **DEFERRED** to avoid a Render redeploy interrupting video 3 — commits are local; push when video 3 is past ingest.

---

## What's next — the detector-build phase (Tomo's plan, confirmed against north_star)

Tomo's mental model is correct and matches the RULES: **one-model-per-fact → build all 18 bronze base fields to ~70-80% with standard models → THEN train (free/automatic via the dual-submit corpus) to 90-95%, selectively.** `serve_detector` is the template; build the others the same way.

**The one nuance north_star adds — the far-court ceiling (north_star §"★ The far-court ceiling"):** four fields — **far serve precision, ball bounce x/y, far-player stroke (fh/bh), A/B identity** — CANNOT reach 70-80% with standard models, because the far player is ~30px and far bounces are missed (no corroborating signal). Their gains come from **coverage (Phase 5-7) + training**, NOT heuristics. So "build to 70-80% with standard models" applies cleanly to the NEAR/buildable fields; the far-court four are gated on the corpus+training path — which is exactly why tonight's corpus-pipeline unblock matters (it's the runway from heuristic-ceiling ~80% to ML-ceiling ~90-95%).

**Concrete next moves (Tomo's instinct first):**
1. **Validate the serve detector** (Tomo's call — right per RULES #4). NEAR serves are solid (bench 20/24, 23/24). The remaining BUILD task is the **silver-inheritance gap**: silver still uses an old bounce-based shortcut and does NOT consume `serve_events` (the detector's output). Wiring it was attempted+reverted (pass-3 needs `serve_side` from the serve). Far-serve precision is far-court-ceiling — don't chase it with heuristics (gating `pose_only` is proven-bad, `detector.py:539`).
2. **Build the missing detectors, one-model-per-fact** (`serve_detector` is the template): swing-type classifier (fh/bh/overhead), volley, a real bounce model, A/B identity. Prioritise the buildable/near fields; the far-court-limited ones accumulate corpus → train (stroke classifier scaffold exists, awaiting weights → `ml_pipeline/models/stroke_classifier.pt`).
3. **Reference:** `docs/_investigation/bronze_silver_18_audit.md` — the 18-field inherit-vs-rederive audit + one-model-per-fact blueprint. `docs/north_star.md` §"Build status vs SportAI" — the live 18-field scorecard (what's ✅ vs ⚠️/❌).

---

## Read in this order
1. This file.
2. `docs/north_star.md` — RULES OF THE GAME → 18-field build status → far-court ceiling.
3. `MEMORY.md` → `project_dual_submit_autoland.md` — the full corpus-pipeline state + tonight's 4 fixes + how dual-submit auto-spawn works.
4. `docs/_investigation/bronze_silver_18_audit.md` — the detector blueprint for the build phase.
5. `.claude/handover_t5.md` — ops / how-to-run / BATCH-SIDE CHECKLIST, if deploying.

## Watching video 3 (if you're the chat that kept be5ja755m open)
- T5 batch time vs c645a7ee baseline: batch 17504s/4.86h, main-loop 12255s/3.4h, ROI tail 5249s. GPU-batch + single-decode should cut these.
- Corpus #3 lands when both sides' silver build + `pair_complete=TRUE` + the AUTO_LABEL hook fires (idempotent via the training_corpus UNIQUE constraint).
- If the T5 ingest sticks: the sweep self-heals it within ~30min now (no manual reset).

## Local helpers (gitignored, `.claude/tmp/`)
`test_streaming_mock.py` / `test_streaming_ingest.py` (OOM-fix validation), `silver_validate.py` + `rebuild_ee12_timed.py` (route-B output-identity + timing), `test_wasb_batch_equiv.py` (GPU-batch equivalence), `explain_pass3.py` (pass-3 EXPLAIN probe), `register_jobdef.py` / `activate_ball_batch.py` (Batch job-def deploy/env), `watch_video3.py` (the live watch). psycopg3-for-local-COPY: `engine.url.set(drivername='postgresql+psycopg')`.
