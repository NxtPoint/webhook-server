# Next-session pickup — 2026-06-05 — bronze-first realignment + bounce bronze model built; NEXT = finish bounce (rebuild + serve consumer) then serve

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN.

### 🧭 THE REFRAME (Tomo, 2026-06-05) — this governs everything now
**Clean silver. Inherit bronze 100%, no exceptions. Bronze is the answer; silver is NEVER the answer.** A **stroke IS a ball-hit** (one event) → silver must be **STROKE/HIT-DRIVEN** (one row per bronze `stroke_events` hit, projected verbatim), NOT bounce-driven. Today's `_t5_pass1_load_bounce_driven` heuristically reconstructs the hit (mirror-fallback, geometric serve, `_infer_swing_type`, gap_break/exclude_d) — that whole pile is DEBT to DELETE once bronze is right. **Overcounts die from correctness, not filtering:** when hit-driven, no valid stroke+hit ⇒ no row, so phantom/racquet/double bounces vanish and T5's ~162/343 collapses toward the real **~84 hits**. Full audit + heuristic-debt checklist: `docs/_investigation/bronze_silver_18_audit.md` §"UPDATE 2026-06-05". Memory: `feedback_silver_must_be_hit_driven`.

**LOCKED ROADMAP (bronze-first, fix one fact at a time, then silver becomes a thin projection):**
1. **Bounce** → CNN bronze model. *(stage BUILT this session — see below; remaining: rebuild + serve consumer)*
2. **Serve** → check + improve model precision + pass-3 inherits `serve_events` (delete geometric gate) + train/lock.
3. **Stroke = ball-hit** → `stroke_events` carries swing_type + ball_hit_location + correct attribution (perspective bias, rule #11). The keystone.
4. **Flip silver STROKE-DRIVEN** (`T5_STROKE_DRIVEN_SILVER`) → DELETE the Pass-1 debt; overcounts die.

## 🎯 NEXT SESSION'S JOB — finish bounce (step 1), then start serve (step 2)
**Bounce bronze model is BUILT + VALIDATED + COMMITTED (`68fdf12`) but NOT rebuilt or consumed yet.** `ml_pipeline/__main__.py` runs the CNN v2 (gravity_residual, thr 0.5, in-memory features, rally≈in_rally) post-pipeline → writes `ml_analysis.ball_bounces`. Validated end-to-end on b008888c: **197 bounces, precision 34%, recall 41%** vs SA 162 (vs the velocity-reversal `is_bounce` rule: 343 / 20% / 43%) — count ≈ SA, precision ~2×. torch+weights are Batch-only (Render main API has neither — confirmed), so it MUST run in Batch.

**Two remaining pieces (couple them — one rebuild covers both):**
1. **Rebuild** (rule #8) to land the Batch bounce stage live (current rev 65/46 predates it). `.claude/handover_t5.md` §BATCH-SIDE CHECKLIST.
2. **Serve-consumer migration (CI-SENSITIVE — do carefully):** point `serve_detector`'s bounce input at `ml_analysis.ball_bounces` instead of `ball_detections.is_bounce`. ⚠️ The locked CI fixtures (a798eff0/880dff02) carry `is_bounce` in their pickled ball_rows, NOT `ball_bounces` → migrating naively breaks the bench (rule #9). Options: regenerate fixtures, or decouple the bench path, or feed both. PLUS the documented pass-3 serve-anchor rework (serve_side_d from `serve_events` hitter, not the bounce on serve rows). This is genuinely step 2's scope — treat bounce-consumer + serve as one focused effort.

## 📦 WHAT SHIPPED THIS SESSION (all on main)
- **Swing classifier**: built + deployed (rev 64) but **FAILED the gate** (per-hit swing agreement vs SA: heuristic 38% > classifier 32%; root = 3-class model with no "other" → forces volleys/serves→forehand). **DISABLED via `SWING_CLASSIFIER_ENABLED=0`** (rev 65/46, image unchanged). Infra fully validated — one env flip to re-enable once the model gets a 4th "other" class (or high min_conf gate) + the far forehand lean fixed, re-validated per-hit vs SA.
- **🔴 Export→reingest leak FIXED** (`f4449b0`): Batch-side enrichments were silently wiped by the Render auto-ingest (DELETE+re-COPY). `bronze_export` now serializes `stroke_class`; ingest COPYs it. (roi_prod preservation was tried then **reverted `9d0a30b`** — it floods the bounce-driven silver 2.4×; it'll be consumed properly once silver is hit-driven.) Memory: `feedback_batch_enrichments_need_export_reingest_carry`.
- **Frame-space fps fixes**: `stroke_events.ts` (`7df8276`) + `serve_events.ts` (`50c0dd3`) now use the SAMPLED fps, not source video_fps (was wrong by source/25 on non-25fps). Serve one is LIVE (serve_events→silver). Bench stayed green. + 2b far-hit-location observability (`931072e`).
- **Silver-heuristic audit + reframe** (`b82f832`) + **bounce root-cause** (apex FPs + perspective confound; cheap fixes empirically rejected) in `bounce_accuracy.md` §"UPDATE 2026-06-05".
- **Bounce CNN bronze stage** (`68fdf12`) — see NEXT JOB above. detect_bounces gained explicit `candidate_mode`/`threshold_override` params.

## 📊 Field status vs SA (fresh, b008888c clean main-only vs ba4812be) — supersedes the stale north_star table
- points 18=18 ✅ · games 4 vs 2 ⚠️over-seg · volley 3≈4 ✅ · serves ~16-22 vs 24 ⚠️far recall · **active rows 121 vs 84 ⚠️over-gen** (dies when hit-driven) · swing: heuristic LIVE (classifier disabled).
- Bounce (bronze, the weak field): velocity-reversal 343/20%/43% → **CNN v2 197/34%/41%** (≈SA 162). Structure/coverage solid; the gaps are over-generation (→hit-driven) + far recall (→training).

## Canonical state
- Batch job-def: **eu rev 66 / us rev 47** (digest `sha256:a60c3909…`, env carried verbatim incl. `SWING_CLASSIFIER_ENABLED=0`). ✅ **Bounce stage rebuild DONE 2026-06-05** — image includes `bounce_detector/` (a missing Dockerfile `COPY` was caught + fixed in `f593ab8`; without it the stage would have import-failed silently). Smoke-tested in-image (module + weights + import OK). **Not yet validated on a live run** — next T5 upload should show `Bounce CNN v2: wrote N ball_bounces` in CloudWatch. g4-primary → g5 → Spot queue.
- Render (auto-deploys main): ingest carries `stroke_class`, roi_prod blanket-delete, fps fixes live, swing-prefer silver cascade live (but classifier disabled in Batch so stroke_class is null → heuristic runs).
- Reference pair: SA `ba4812be` ↔ T5 (heuristic baseline) `a35b37f6`; clean classifier test task `b008888c` (its `ball_bounces` now holds 197 CNN bounces from the stage validation).
- New weights (Batch-bundled, git-ignored): `bounce_detector_v2_7match.pt` (144KB), `swing_classifier_v2.pt` (125MB). GPU dev box `i-0295d636` STOPPED.
- Env knobs (Batch): `SWING_CLASSIFIER_ENABLED`(0), `SWING_CLASSIFIER_MIN_CONF`(0.5), `BOUNCE_CANDIDATE_MODE`, `BOUNCE_DETECTOR_THRESHOLD`(via param).

## 🗂️ Backlog after bounce+serve (deferred, scoped)
- **Swing v2.1**: add 4th "other"/none class (or high min_conf gate) → stop forced-forehand → re-validate per-hit → flip `SWING_CLASSIFIER_ENABLED=1`. (GPU box needed for retrain.)
- **Stroke = ball-hit**: make `stroke_events` carry swing_type + ball_hit_location + correct attribution → unlocks the stroke-driven silver flip (the big cleanup).
- **Far serve recall + far ball_hit_location**: training + far-court calibration (the 2.4-7m overshoot nulling far court_y).
- `bench_silver` stale baseline (pre-existing 6-issue regression from the bounce-proximity guard) — re-baseline.
---
**END OF PICKUP**
