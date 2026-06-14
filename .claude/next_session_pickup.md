# Next-session pickup — 2026-06-14 — ✅ BOUNCE PRECISION (lever A) DEPLOYED. ⛔ SILVER PURITY (lever B) MEASURED + GATED (do NOT flip — read the gate). eu rev 79 / us rev 60. main @ HEAD. Serve signed off.

> **Resume:** lever A (bounce precision) is DONE + deployed. Lever B (silver purity) was investigated and is BLOCKED on 3 preconditions — it is NOT a flip you can land; the next real bounce work is the **architecture decision** (is silver bounce-driven or hit-driven?) + the **sharp-far retrain** (training-gated, accrues from new full-res uploads). Read §"⛔ LEVER B" before touching silver.

## ⚡ Executive summary (read first — 30 seconds)
**Phase:** bronze-first; **SERVE SIGNED OFF**. Bounce push: precision lever shipped, purity lever gated.
**Deployed:** **eu rev 79 / us rev 60** (amd64 `1069f87e`), cloned rev 78/59 config, cross-region digest equal.
**Bench:** floor `ea1e500c=12/26` + `880dff02=23/24`. Green, CI green.
**What shipped:** bounce CNN threshold 0.5→0.70 (env `BOUNCE_CNN_THRESHOLD`, `b4bf5ac`) — offline-proven precision 11%→23% (2.1×), over-emission 1.88×→0.78×, −2.5pp recall (training-gated).
**What's blocked:** lever B (silver inherits ball_bounces) — 3 preconditions unmet (carry + accuracy + architecture). See below.
**Next session's job:** silver architecture is now SETTLED (hit-driven, see §"⛔ LEVER B"). The one remaining bottleneck for both bounce recall AND the hit-driven flip = **sharp-far retrain** (lift the hit model's far WHO-attribution from ~6/51) — accrues from new full-res uploads. After that: flip T5 Pass-1 to insert from `stroke_events`, demote bounce to a Pass-2 enricher. Training/architecture work, not quick build levers.

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

## ⛔ LEVER B — SILVER PURITY (MEASURED, GATED — DO NOT FLIP)
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
**→ LEVER B REDIRECTED:** "deprecate is_bounce → inherit ball_bounces" was the WRONG axle (keeps T5 bounce-driven). Real fix = **T5 Pass-1 inserts from the HIT stream `ml_analysis.stroke_events`** (mirror SA's player_swing insert) + bounce MODEL `ml_analysis.ball_bounces` demoted to the **Pass-2 coordinate enricher** (matched into each hit row, like SA Pass-2). Lever A's precision feeds this. This session did NOT flip silver / add speculative plumbing (rule #11).
**→ STILL GATED on enablement:** flip is gated on the T5 hit model's far-side WHO-attribution (far ~6/51; emission fine — B1 anchor recall 94-96% balanced near/far) — same sharp-far retrain bottleneck. NEXT bounce/silver work = lift far attribution (sharp-far retrain), THEN flip T5 Pass-1 hit-driven + demote bounce to Pass-2.

---

## Canonical state
- main @ `b4bf5ac` (+ this pickup/doc commit on top) synced with image **eu rev 79 / us rev 60** (amd64 `1069f87e`).
- Bench floor: ea1e500c 12/26 + 880dff02 23/24 (green, CI green).
- Bounce floor post-lever-A (offline, 5 labelled tasks @ thr 0.70): rec 18.2% / prec 23.3% / over_x 0.78.
- Tools: `.claude/tmp/bounce_precision_sweep.py` (threshold sweep, labelled-task-filtered), `.claude/tmp/register_jobdefs.py` (job-def deploy helper).
- Reference video local: `ml_pipeline/test_videos/a798eff0_sa_video.mp4`. SA companion `ba4812be` (26 serves: 14N/12F).

## What's NOT a quick build lever anymore (both bounce levers spent)
- **Bounce/hit RECALL** = training-gated. Only the sharp-far CNN retrain lifts it — accrues from NEW full-res uploads (corpus originals deleted; use `trimmed/{task}/practice.mp4`). Re-run the 7 corpus jobs through Batch rev79 + re-ingest (now carries roi_far_ball) so the model trains on sharp-far, then rebuild hit dataset + retrain.
- **Silver bounce purity** = gated on the architecture decision (above) + carry + accuracy.
- Remaining 18-field items: swing v2.1 (4th "other" class, purity-corrected — delete silver swing heuristics, classifier owns fh/bh/overhead/volley), set_number, point/game structure on next real upload.

## Memory entries this session
`feedback_silver_heuristic_to_model_swap_gate` (the 3-precondition gate for replacing a silver heuristic with a model fact).
---
**END OF PICKUP**
