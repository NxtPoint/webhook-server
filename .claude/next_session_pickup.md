# Next-session pickup — 2026-06-14 — ✅ HIT+BOUNCE BUILD COMPLETE: bounce precision DEPLOYED, silver FLIPPED hit-driven, bench_hit locked, architecture SETTLED. Only TRAIN-LAST remains (needs Tomo full-res uploads). eu rev 79 / us rev 60. main @ HEAD. Serve signed off.

> **Resume:** the hit+bounce BUILD + ARCHITECTURE is DONE this session — nothing to "finish building" here. Silver is now HIT-DRIVEN (one row per stroke event), bounce precision is deployed, and `bench_hit` is a locked gate. The ONLY remaining work for hit+bounce is ACCURACY = the **sharp-far retrain**, which is **gated on Tomo uploading new FULL-RES matches** (corpus then auto-accrues sharp-far data → retrain → measure with `bench_hit`/`bench_bounce`). If no new uploads yet: there is no productive hit/bounce build work to do — consider starting **SWING** (the 2nd-last model) or the verify items. Read §"DEFINITION OF DONE" first.

## ⚡ Executive summary (read first — 30 seconds)
**Phase:** bronze-first; **SERVE SIGNED OFF**; **HIT + BOUNCE build/architecture COMPLETE — at train-last.**
**Deployed:** **eu rev 79 / us rev 60** (amd64 `1069f87e`), cross-region digest equal. main @ `472b244`.
**Bench:** serve floor `ea1e500c=12/26` + `880dff02=23/24` green, CI green. `bench_hit` baseline locked (NEAR gate 67% / FAR gate 19% / prec 54%). `bench_bounce` floor (thr 0.70): rec 18% / prec 23%.
**What shipped this session:** (1) bounce CNN thr 0.5→0.70 DEPLOYED (`BOUNCE_CNN_THRESHOLD`, rev 79/60) — precision 11%→23%; (2) silver row architecture SETTLED = HIT-DRIVEN (probe ladder on live SA data); (3) silver FLIPPED hit-driven (`T5_STROKE_DRIVEN_SILVER` default ON — Tomo, T5 unused; validated 110→174 rows); (4) `bench_hit` built + baseline locked; (5) rule #11 updated; CLAUDE.md `/init` drift fixed.
**What's blocked:** hit+bounce ACCURACY (far-side) — purely TRAIN-LAST, gated on new FULL-RES uploads accruing sharp-far corpus data. No code lever left.
**Next session's job:** if Tomo has uploaded full-res matches → retrain bounce CNN + hit model on sharp-far, measure with the benches. If not → start SWING (2nd-last model, scope below) or do the verify items (#5). The hit+bounce build is done.

---

## ✅ LEVER A — BOUNCE PRECISION (DEPLOYED 2026-06-14)
**What:** raised the Batch bounce-stage CNN cutoff `__main__.py:336` from 0.5 → **0.70**, made it env-configurable (`BOUNCE_CNN_THRESHOLD`, default 0.70) so future tuning is a job-def env flip with no rebuild (env_var_rollback_pattern). Commit `b4bf5ac` (code + docs/env_vars.md).
**Why 0.70:** offline corpus threshold sweep (`.claude/tmp/bounce_precision_sweep.py`, prod config gravity_residual + bounce_detector_v2_7match.pt, **5 labelled corpus tasks**) reproduces the floor EXACTLY (thr 0.5 = emit 1416 / match 156 / rec 20.7% / prec 11.0% / over_x 1.88) and shows recall flat to ~0.70 while precision climbs:
```
 thr   emit  match  rec%  prec%   F1  over_x
0.50  1416   156  20.7  11.0  14.4   1.88  <- old floor
0.65   758   141  18.8  18.6  18.7   1.01
0.70   589   137  18.2  23.3  20.4   0.78  <- DEPLOYED (Tomo picked)
0.75   419   126  16.8  30.1  21.5   0.56  (best F1)
0.80   293   109  14.5  37.2  20.9   0.39
```
⚠️ **Sweep population fix (memory stored_rows_blind_to_scoring_population):** the corpus now has 8 tasks, 3 with ZERO floor labels (c645a7ee, 9378f2dd, 63a0130d) — their emissions can never match → must be EXCLUDED from precision or it reads ~3× too low (3.6% not 11%). The sweep tool filters to labelled tasks; re-use it as-is.
**Deploy (rule #8 full cycle, clean):** Docker build (no requirements change, fast) → in-image verify (`BOUNCE_CNN_THRESHOLD` present) → SEQUENTIAL ECR push both regions (not parallel — digest race) → cross-region manifest digest equal `a19bab1a` → amd64 sub-manifest `1069f87e` → `register_jobdefs.py` → eu rev 78→79 / us rev 59→60. Old image was `316c1c4c` (far-ROI deploy).
**VERIFY on next real upload:** (1) prod bounce precision rises; (2) the coupling — fewer emitted bounces feed the far-pose ROI rally-gate density (`__main__.py:338+`); FP bounces there were noise so density should be fine, but eyeball `ms_per_frame`-style ROI pose counts. To recover recall later, LOWER `BOUNCE_CNN_THRESHOLD` once the CNN is retrained on sharp-far.

## ✅ LEVER B — SILVER PURITY: ARCHITECTURE SETTLED + FLIPPED HIT-DRIVEN (history below; flip done 2026-06-14)
> This section records how the answer was reached. BOTTOM LINE: silver is now HIT-DRIVEN (`T5_STROKE_DRIVEN_SILVER` default ON). The "DO NOT FLIP" framing below was the pre-flip analysis; Tomo then chose to flip early (T5 silver not prod-consumed). See §"DEFINITION OF DONE" for current state.
**Goal as stated:** deprecate `is_bounce`, make silver inherit `ml_analysis.ball_bounces` verbatim (mirror serve-purity `46c8a91`).
**Where silver reads bounces today:**
- `build_silver_match_t5.py:699` — `ball_detections.is_bounce=TRUE`, **bounce-DRIVEN row gen** (1 row/bounce). The rule-#11 site.
- `build_silver_v2.py:1191` — local CTE *named* `ball_bounces` from `is_bounce`, rally-END timing only (NOT the model table — name collision is misleading).
- **Nothing reads `ml_analysis.ball_bounces` (the model) into silver.**
**★ THREE blockers (full writeup: `docs/_investigation/bounce_accuracy.md` §"Lever B"; memory `feedback_silver_heuristic_to_model_swap_gate`):**
1. **Empty table.** `ml_analysis.ball_bounces` = **0 rows on all 8 corpus tasks** (measured). "Survives re-ingest" (`__main__.py:295`) only holds for NEW post-rev-66 tasks; older/re-ingested have none. Flip → silver collapses to 0 rows on the existing corpus. Needs the export+reingest carry (like roi_far_ball got).
2. **Not accurate enough.** Model @ 0.70 = 18% recall, ~589 rows vs is_bounce's ~2,669 (5 tasks) — different + 4.5× smaller population. Driving row-gen off an 18%-recall fact regresses. Recall is training-gated.
3. **★ Wrong axle.** Silver should be HIT-driven not bounce-driven (memory `silver_must_be_hit_driven`). Swapping is_bounce→ball_bounces makes a *better-sourced but still bounce-driven* Pass-1 — it does NOT achieve purity. Real fix = hit-driven row-gen, gated on the hit model (B2, gate not met). Stroke-driven path is committed OFF behind `T5_STROKE_DRIVEN_SILVER`.
**✅ ARCHITECTURE QUESTION SETTLED 2026-06-14 (precondition c — probe ladder on live SA data): HIT-DRIVEN.** Every SA swing has hit coords (100%/2,380 swings); bounce stream is multi-typed at 1.12× swings (not 1:1 with shots); **prod SA silver is ALREADY hit-driven** (`build_silver_v2.py:357` inserts 1 row/`player_swing`, Pass-2 `:376` matches bounce in as UPDATE) — only T5 Pass-1 is bounce-driven (the outlier). Writeup: north_star §"SILVER ROW ARCHITECTURE", memory `silver_must_be_hit_driven`.
**→ LEVER B REDIRECTED:** "deprecate is_bounce → inherit ball_bounces" was the WRONG axle (keeps T5 bounce-driven). Real fix = **T5 Pass-1 inserts from the HIT stream `ml_analysis.stroke_events`** (mirror SA's player_swing insert) + bounce MODEL `ml_analysis.ball_bounces` demoted to the **Pass-2 coordinate enricher** (matched into each hit row, like SA Pass-2). Lever A's precision feeds this. (This path was then ENABLED — see below.)
**→ FLIPPED 2026-06-14 (Tomo: T5 silver not prod-consumed → no regression risk):** `T5_STROKE_DRIVEN_SILVER` now defaults ON. Pass-1 inserts from `stroke_events` (validated 78c32f53 110→174 rows, clean passes 1-5). Rule #11 updated. ACCURACY still train-gated (far attribution ~19% per `bench_hit`) but that's the retrain, not a code lever. Rollback `T5_STROKE_DRIVEN_SILVER=0`. Bounce coords still read `is_bounce` (model table empty on existing tasks) — swaps to the model once it's carried + accrued (residual, not blocking).

---

## ✅ DEFINITION OF DONE — HIT + BOUNCE (the gate before SWING)
Verified on live data 2026-06-14. **Verdict: BUILD is done (models emit both facts); we are at the TRAIN-LAST stage.** The remaining accuracy gap on BOTH facts is ONE root cause — far ball/player too coarse — proven un-movable by heuristics → training-gated. "Done building" = no code lever moves accuracy, only data.

| # | Item | Type | Status |
|---|------|------|--------|
| 1 | Bounce model emits → `ml_analysis.ball_bounces` | build | ✅ |
| 2 | Hit model emits → `ml_analysis.stroke_events` (108-810 rows/task) | build | ✅ |
| 3 | Bounce precision lever (thr 0.5→0.70, rev 79/60) | build | ✅ DONE this session |
| 4 | Silver row architecture decided = HIT-DRIVEN | decision | ✅ DONE this session |
| 5 | **Bounce model output survives re-ingest** (0 rows on ALL existing tasks — vintage/carry; "survives" claim unverified) | verify | ⬜ check on next real upload |
| 6 | **Locked hit bench** (`bench_hit`) — repeatable accuracy gate | build | ✅ DONE 2026-06-14 (`bench_baseline_hit.json`: NEAR gate 67%, FAR gate 19%, prec 54%) |
| 7 | **Sharp-far footage accrues** — new FULL-RES SA uploads re-run through rev-79 (carries `roi_far_ball`); old corpus originals deleted | DATA (Tomo) | ⬜ gating dependency |
| 8 | **Retrain** bounce CNN + hit model on the sharp-far distribution → far recall/attribution reach the ~70-80% bar | TRAIN | ⬜ gated on #7 (measure with `bench_hit` / `bench_bounce`) |
| 9 | **Flip silver HIT-DRIVEN** — Pass-1 from `stroke_events`; bounce → enricher | build | ✅ DONE 2026-06-14 (Tomo: T5 unused → flipped early; `T5_STROKE_DRIVEN_SILVER` default ON; validated 78c32f53 pass1 110→174 rows clean). Rule #11 updated. |
| → | THEN: SWING (4th "other" class, purity-corrected) — the second-last model | next | — |

**STATE NOW:** architecture is fully in place — silver is HIT-DRIVEN (default on), bounce-precision deployed, hit bench locked. The ONLY thing left for hit+bounce DoD = ACCURACY, which is purely TRAIN-LAST: #7 (Tomo uploads full-res matches → corpus accrues sharp-far) → #8 retrain (measure via `bench_hit`/`bench_bounce`). #5 = one-glance verify on next upload.
**Residual (not blocking, documented):** the hit-driven path still reads bounce coords from `is_bounce` (the bounce MODEL `ball_bounces` is empty on existing tasks — column mismatch + zero benefit until carried/accrued); swing_type still STOPGAP-falls to heuristic when the bronze `stroke_class` model didn't classify (swing = next model). Both are correct-by-design holds, not debt to flip blind.

---

## Canonical state
- main @ `472b244` synced with image **eu rev 79 / us rev 60** (amd64 `1069f87e`). Silver hit-driven flip + bench_hit are Render-side/local (no Batch rebuild).
- Bench floor: serve ea1e500c 12/26 + 880dff02 23/24 (green, CI green). `bench_hit` baseline locked (`bench_baseline_hit.json`: NEAR gate 67% / FAR gate 19% / prec 54%). `bench_bounce` @ thr 0.70: rec 18.2% / prec 23.3% / over_x 0.78.
- Env flags (all default-ON in code, env = rollback): `BOUNCE_CNN_THRESHOLD=0.70`, `T5_STROKE_DRIVEN_SILVER=1` (NEW this session), plus the serve set (`SERVE_MODEL_ENABLED`, `SERVE_CNN_BOUNCES`, `T5_SERVE_EVENTS_MIN_CONF=0.0`).
- Tools: `bench_hit` / `bench_bounce` (diag); `.claude/tmp/bounce_precision_sweep.py` (threshold sweep, labelled-task-filtered), `.claude/tmp/register_jobdefs.py` (job-def deploy helper).
- Reference video local: `ml_pipeline/test_videos/a798eff0_sa_video.mp4`. SA companion `ba4812be` (26 serves: 14N/12F).

## The hit+bounce build is DONE — what the NEXT session does (in priority order)
1. **IF Tomo has uploaded new FULL-RES matches → the retrain (the only thing that finishes hit+bounce):** corpus auto-accrues sharp-far data; re-run corpus jobs through Batch rev79 + re-ingest (carries roi_far_ball), rebuild hit dataset + retrain bounce CNN + hit model on the sharp-far distribution, measure with `bench_hit` (move FAR gate from 19%) + `bench_bounce` (move recall from 18%). Originals deleted → use `trimmed/{task}/practice.mp4` (memory `reference_t5_video_retention`).
2. **IF no new uploads yet → start SWING (the 2nd-last model).** Scope (Tomo, purity-corrected): a 4th "other" class; **DELETE the silver swing_type heuristics** (`_infer_swing_type_from_keypoints/_from_position` + volley-distance) — the classifier owns fh/bh/overhead/volley to ceiling, "other" = non-groundstroke. Gate = classifier vs SA STANDALONE (no heuristic crutch). Swing model = `stroke_classifier/` (in Batch image, `SWING_CLASSIFIER_ENABLED=0` — lost the per-hit gate, needs the 4th class). Mirror the hit+bounce recipe: build to dev ceiling → bench → train-last.
3. **Verify items:** (#5) on the next real T5 upload, confirm `ml_analysis.ball_bounces` is non-zero post-ingest (the "survives re-ingest" claim is unverified — 0 on all existing tasks). Also eyeball that the higher bounce threshold didn't starve the far-pose ROI rally-gate density.

## Residuals (correct-by-design holds, NOT debt to flip blind)
- Hit-driven silver path reads bounce coords from `is_bounce`, not the bounce MODEL (`ml_analysis.ball_bounces` empty on existing tasks + column mismatch). Swap in once it's carried through re-ingest AND accrued from new uploads.
- `swing_type` STOPGAP-falls to heuristic when bronze `stroke_class` absent — resolved by the swing model (item 2 above).

## Memory entries this session
- `feedback_silver_heuristic_to_model_swap_gate` (3-precondition gate for replacing a silver heuristic with a model fact).
- `feedback_silver_must_be_hit_driven` updated — architecture SETTLED with live-data evidence + the lever-B redirect.
---
**END OF PICKUP**
