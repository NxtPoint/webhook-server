# Next-session pickup — 2026-06-13 PM — ✅ FAR-ROI DEPLOYED (eu rev 78 / us rev 59) + BOUNCE-COUPLING PROVEN end-to-end (far-bounce candidate recall 40%→87%). main @ `2066b6d`. Far-gate hit-model retrain accrues from NEW full-res uploads (corpus originals deleted; see the ✅ section below). Serve still signed off.

> **Resume:** read the "✅ 2026-06-13 PM — FAR-ROI DEPLOYED" section directly below — it supersedes the older threads. The far-gate number now grows automatically as new dual-submit matches flow through rev 78 (which captures sharp-far via the export-carry). No manual re-run is possible (corpus originals are 404 post-trim, only 720p trimmed survive).

## ✅ 2026-06-13 PM — FAR-ROI DEPLOYED + COUPLING PROVEN END-TO-END (resume = corpus re-run for the far-gate read)
**Shipped (main @ `fa2acf9`, bench green):** `506c986` source-preference merge for ball_detections readers (`ml_pipeline/ball_merge.py`; bounce_detector/hit_model/serve readers dedup roi_far_ball>roi_prod>main>NULL; silver stays main-only) + `fa2acf9` far_ball wired into unified.py/__main__ (env `ROI_FAR_BALL_ENABLED` default-on) + export-reingest carry (roi_far_ball survives via bronze_export `extra_ball_rows` + bronze_ingest_t5 `source` COPY) + Dockerfile COPY for ball_merge (rule #8).
**Deployed (rule #8 full cycle):** amd64 `316c1c4c…`, cross-region digest equal, **eu rev 78 / us rev 59** latest-ACTIVE.
**Reference re-run:** probe `p15_far_roi` = job `75d9dd7e` (SA `ba4812be`), SUCCEEDED ~54min, 2237 roi_far_ball rows.
**★ COUPLING PROVEN on the DEPLOYED pipeline** (`.claude/tmp/p15_measure_export.py`, reads the S3 export — main 8011 + roi_far_ball 2237 both carried): far-BOUNCE candidate recall **(A) main-only 12/30 (40%) → (B) merged sharp 26/30 (87%)**. The candidate stage was THE bottleneck → fixed. far-ROI fixes both halves of the far gate.
**NEXT = far-gate retrain, gated on a CORPUS RE-RUN.** The hit dataset reads `ml_analysis`, which only gets the main ball via the Render re-ingest (probes don't — see memory `feedback_probes_no_main_ball_in_ml_analysis`). Re-run the 7 corpus jobs through the FULL flow (Batch rev78 + re-ingest, which now carries roi_far_ball) so the model TRAINS on sharp-far, then rebuild hit dataset + retrain + read far gate @1.0s pid-strict vs heuristic far 19/51. Features already support it (`cnn_bounce_gap`/`near_cnn_bounce`, fed by the now-reliable far bounces). Originals deleted post-trim → use the frame-aligned `trimmed/{task}/practice.mp4` (memory `reference_t5_video_retention`).

## ▶️ PRIOR THREAD (superseded by the above) — bounce-coupling
**Why:** far-ROI sharp ball fixes far-ball TRACKABILITY but NOT hit-vs-bounce DISCRIMINATION (angle/speed/proximity all fail — see below). The far gate's 2nd half = reliable far-bounce marking so far hits = non-bounce far events. The bounce candidate generator (`bounce_detector/detector.py::_candidate_frames_from_gravity_residual`) keys off the ball's IMAGE-Y trajectory → the sharp far ball should lift its recall (coarse-ball ceiling was ~36-58%; 38% end-to-end).
**✅ RESULT CAPTURED 2026-06-13 PM (`.claude/tmp/far_bounce_coupling.log`):** far-BOUNCE candidate recall @0.2s on 30 SA far bounces — **(A) WASB ball 12/30 (40%) vs (B) sharp far-ROI 24/30 (80%). B ≫ A (2×).** COUPLING IS REAL. The sharp far ball directly improves far-bounce candidate recall → bounce #4 improves AND the hit model gets reliable far-bounce marks. **One far-ROI deploy fixes both halves of the far gate** (emission already proven 25/25 trackable; discrimination via bounce-coupling now proven). This is the "sharp≫WASB" branch → proceed with the far-ROI deploy (do NOT pivot downstream).
**Decision tree (resolved → branch 1):** sharp≫WASB → coupling real: far-ROI deploy lifts bounce #4 recall AND feeds the hit model reliable far-bounce marks (one deploy, both halves). sharp≈WASB → far-bounce bottleneck is downstream (CNN scorer/pre-gates/labels) → pivot there.
**Bounce pipeline:** gravity-residual candidates → trained CNN (v2 7-match, `models/bounce_detector_v2_7match.pt`, thr 0.55) → `ml_analysis.ball_bounces`. Candidate stage is the bottleneck.



## 🌙 OVERNIGHT far-ROI session (read first — Tomo asleep, autonomous)
**Mandate:** complete B2-far + train. **Honored constraint:** no Batch deploy / no Batch-side merge to main overnight (`feedback_overnight_branch_only`) — so the bronze rebuild (needs Batch+GPU+corpus videos) and retrain are DAYLIGHT work, teed up.

**Decision chain this session:** B2 far proven UPSTREAM (not labeling/features/data — 3 probes) → option 2 chosen → research: **WASB dethroned** (RacketVision: TrackNetV3+BM+4F 1.66px vs WASB 3.62px) BUT **WASB stays as the global tracker**; far fix = **far-court ROI re-detection with TrackNet on a high-res crop** (hybrid, same as bounces.py). v3+BM+4F = a SEPARATE future global-tracker decision, NOT this build.

**Built + validated (LOCAL, reference video):**
- `ml_pipeline/roi_extractors/far_ball.py` — far-ball ROI extractor. **On branch `far-ball-roi`** (Batch-bundled dir → branch per overnight rule). Smoke-tested (124 rows/3 windows).
- A/B: far trajectory residual **298px→45px (6.7× sharper)**. Real `candidates.py`: far hits+bounces **25/25 matched** with clean ~169° discontinuities (baseline: feature-weak noise). **FAR-BALL TRACKABILITY FIXED.**
- ⚠️ **KEY FINDING — far-ROI is NECESSARY, NOT SUFFICIENT for the far GATE.** Proximity de-risk (image-space, no calib needed): far-HIT ball→far-player gap 445px vs far-BOUNCE 391px — **proximity does NOT separate** (confirmed vs fork probe ~500px both). Angle (169° both), speed (overlapping), proximity (~400px both) ALL fail to separate far hit from far bounce even on the SHARP ball — at distance they genuinely look alike. The sharp ball fixes EMISSION (trackable candidates) but not DISCRIMINATION.
- **What the far gate ALSO needs:** (1) reliable far-bounce marking (bounce #4 — far hits = non-bounce far discontinuities; the sharp ball feeds the bounce model too → **strokes-far and bounce #4 are COUPLED**); and/or (2) a temporal/sequence head (hit→bounce→hit alternation — current hit model is per-candidate). Temper the far-gate expectation: ship far-ROI as infrastructure, plan the bounce/sequence piece as the second half.

**⚠️ KEY OPEN DECISION before wiring:** `roi_far_ball` rows OVERLAP WASB rows in `ball_detections` (2 rows/far-frame → corrupts the trajectory readers). Merge strategy (recommend: read-time `source` preference `roi_far_ball>roi_prod>main`, audit ALL readers) must be resolved first. **Full design + daylight checklist: `docs/_investigation/far_ball_roi.md`.**

**DAYLIGHT next (in order):** (1) resolve merge strategy + audit `ball_detections` readers (`docs/_investigation/far_ball_roi.md`); (2) wire `FarBallProcessor` into `unified.py`/`__main__.py`; (3) Batch deploy (rule #8 full cycle); (4) re-run reference → rebuild hit dataset → retrain → read far gate — EMISSION should rise, but DISCRIMINATION won't fully resolve on the sharp ball alone (temper expectation); (5) **bounce #4 is now the SECOND HALF of the far-gate fix** (not free): reliable far-bounce marking → far hits = non-bounce far events; sharp ball feeds the bounce model too; (6) consider a sequence head (hit→bounce→hit) on the hit model; (7) THEN swing (scoped below, purity-corrected).

**SWING (banked, not started — Tomo: focus one thing):** 4th "other" class. **Purity correction (Tomo):** silver swing heuristics (`_infer_swing_type_from_keypoints/_from_position` + volley-distance) VIOLATE the end-state architecture — DELETE them (mirror serve deletion), classifier owns fh/bh/overhead/volley to ceiling, "other"=non-groundstroke (not "heuristic guesses"). Gate = classifier vs SA STANDALONE (no heuristic crutch). Full scope in this session's transcript.


## ⚡ Executive summary (read first — 30 seconds)
**Phase:** bronze-first; **SERVE IS SIGNED OFF** (north_star sign-off list updated). Deployed: **eu rev 76 / us rev 57** (amd64 `cb444b47`).
**Bench:** floor `ea1e500c=12/26` (CI) + `880dff02=23/24` (legacy guard). Green, CI green.
**Serve final (p10, rev 73):** near 13/14, **far 7/12 (was 3/12)**, total **20/26** at eval tol; silver↔bronze trace **48/48 BOTH directions**. Chain: Batch serve-model stage (`serve_candidates`) → detector `model_far` merge → bronze `serve_events` → silver verbatim (min-conf **0**).
**⚠️ D1 SAGA (read before touching tracker gates):** v1 (x+y bound on tier-500, p11) and v2 (x-only, p12) each killed the SAME 8,146 real far-player rows — the far player's strict=False scoring projections are off-domain in BOTH axes and aren't persisted to design against (`feedback_stored_rows_blind_to_scoring_population`). Far serve collapsed 7/12→3/12 both times. **v3 (`3f04f21`, rev 76/57): selection reverted to p10 behaviour; spectator dropped at db_writer on STORED court_x ∉ [-2, 12.97]** — the strict-bounded population the predicate was validated on. p11/p12 also showed: near 14/14 (148.52 recovered when spectator gone), FAR p90 +0.24 (was +8.07), honest FAR median is **-2.17m** (the old -0.43 was spectator-flattered — residual behind-baseline bias is real, on the list), bounce NULL 72%→61% (D2 partial: strict bounds reject most far fills — honest).
**p13 result (rev 76):** far 7/12 RESTORED ✓ + 22,080 rows back ✓ — but the spectator survived (45%): the db_writer drop was wiped by the Render re-ingest because `bronze_export` builds the payload from the IN-MEMORY result (the export+reingest-carry rule, AGAIN). **v3b (`c991f2a`, rev 77/58): same drop predicate added to bronze_export** — both boundaries now filtered. Offline replay confirms the drop is serve-neutral (12/26 identical with spectator rows removed from the fixture).
**p14 RESULT (rev 77/58) — ALL BARS HIT, D1+D2 CLOSED:** far 7/12 ✓, near 13/14 ✓, F1 55.6 (best yet), pid-1 off-court **1%** ✓ (survives re-ingest), player rows 20,936 (exact predicate count), FAR p90 **+0.36** ✓, bounce NULL 61% + matched 30/68 (D2 partial, residual = strict-bounds rejections). North_star scorecard rows updated (D1 DONE, D2 partial, far-median honest at -2.17m = known calibration item). Nothing in flight.

## The day's chain (all on main, all bench-green, CI green)
1. **Fixture regen + re-baseline** (`f28a4d9`,`08b5b13`): harness drift fixed — fixtures now carry CNN bounces (schema v2, prod-parity); CI fixture a798eff0→ea1e500c (12/26); a798eff0 retired (S3 archived). All old fixtures were the SAME video, warp-era.
2. **Zone tighten** (`3b33c9c`): `_baseline_zone` far (-3.5..4.5)→(-5.0..2.0); P 39→45.7.
3. **Scorecard promoted** (`12aad57`): `python -m ml_pipeline.diag.scorecard <job_id>`; fresh 18-field table + sign-off list in north_star (`197bccc`).
4. **Serve model v1 retrained** (`ccc3c6d`): clean held-out eval via EXTRA_EVAL; gate met (far 4/10 @ P 0.40).
5. **C1 ROI gate** (`a841c6d`): rally gate on validated PROJECTED bounces — far wind-ups 11/12→0/12 blocked (NULL-coord pre-serve ball-bouncing was the blocker, validity rule keeps NULLs).
6. **Batch serve stage** (`399712c`): `ml_analysis.serve_candidates` (survives re-ingest like ball_bounces); `SERVE_MODEL_STAGE=1` on job-defs.
7. **Detector wire-in** (`63e2f5b`,`f2be8b4`): `model_far` additive merge; **SERVE_MODEL_ENABLED default ON** post-p10.
8. **⚠️ RULE-1 AUDIT FINDING** (`d4ebb95`,`a54d11a`): T5_SERVE_FROM_EVENTS had NEVER been live in prod (default-OFF, Render env flip never landed) — silver ran the legacy geometric serve path for 10 days while docs said "inherits verbatim"; the "24v26 count-aligned" was coincidence (1/24 traced). Fixed: **default ON in code** + overlay inherits by event player_id (NULL hitter coords tolerated — mandatory for model_far events) + **min-conf 0 (Tomo: "literally everything verbatim")**. See memory `count-alignment-is-not-provenance`.
9. **D1** (`49ef908`): tier-500 got a geometric domain — the standing spectator at (-4.8,+6.1) was pid-1 in 45% of its non-NULL frames (tier-500 had NO bounds; pose-carrying off-court people qualified). Predicate validated: kills 950/969 FP rows, 0 real.
10. **D2** (`aba54ad`): NULL-coord CNN bounces get court coords by projecting ball image xy at the bounce frame (ball is ON the ground plane exactly then); was 72% NULL, 140/140 fillable. Feeds the ROI gate density too.

## Deploy state
- **eu rev 74 / us rev 55** @ amd64 `ac33fc04` (D1+D2). rev 73/54 @ `606a5c7d` (serve stack). Cross-region digests VERIFIED equal (a tag/push race on the 73 deploy briefly pushed stale bits to us-east-1 — caught by the digest check; handover step 3 now mandates cross-region digest equality, `c2f8f65`).
- Env knobs (all default-ON in code, env = rollback): `SERVE_MODEL_ENABLED`, `T5_SERVE_FROM_EVENTS`, `SERVE_CNN_BOUNCES`; `T5_SERVE_EVENTS_MIN_CONF=0.0`; Batch-side `SERVE_MODEL_STAGE=1`. All documented in docs/env_vars.md.

## STROKE ARC (started 2026-06-07 — the serve recipe, replayed)
**Silver purity DONE (`46c8a91`):** legacy geometric serve path DELETED from build_silver_match_t5 (-376 lines): per-bounce serve decision, _serve_geometric_check (+HITTER_FAR_MAX warp tolerances), _is_overhead_pose, _check_hitter_stationary_pre_hit, T5_SERVE_FROM_EVENTS flag — overlay now UNCONDITIONAL. Validated: rebuilds identical (a35b37f6 13/13, p10 48/48 both ways).
**B1 DONE (probe ladder, 4 probes on p14 clean data):**
- Ball-trajectory discontinuity (velocity-vector angle >90°, speed>1px/f) = THE anchor signal: 94-96% recall of 102 SA swings, BALANCED near/far (47/47). The heuristic stroke_detector adds ZERO anchors beyond it (strictly dominated).
- ⚠️ Design-critical: bounce-discontinuity and hit-discontinuity are 0.3-0.7s neighbours — clustering CONFLATES them (gap=0.3 collapsed recall to 73/102). v1 = PER-CANDIDATE classification (hit/bounce/noise), tiny dedup ~0.1s only, bounce-CNN pattern. ~900-1500 candidates/match vs ~100 hits.
- Residual: 1 true ball-gap miss (334.72s); other misses are cluster-absorption artifacts that per-candidate design recovers. Anchor ceiling ≈ 99%.
- Probes: `.claude/tmp/stroke_b1_p{1..4}.py`.
**B2 IN FLIGHT — hit model v1 scaffold LANDED (`c06a198`), gate NOT met yet:**
- `ml_pipeline/hit_model/` (candidates/features/dataset/model/train, serve-model recipe). Trains in ~3 min CPU; weights `models/hit_model_v1.pt`.
- **Three label bugs found+fixed during the build** (each general): (1) SA player_id filter was reference-video-specific → 5/6 train tasks had ZERO labels; (2) every-candidate-in-tolerance positives taught "hit-adjacent" not "hit" → nearest-only + ignore-zone (w=0) labeling; (3) **SA player_id is a PERSON and swaps ends at changeovers** → side labels must be positional PER SWING (ball_hit_location_y > 11.885 = near), never a person mapping (person mapping scored 40-57% on long matches).
- **State after 2026-06-12 probe ladder**: near **31/51 BEATS heuristic 13/51** (was 24, +perspective features `c84851f`), F1 0.444→0.491; far **3-6/51 vs 19/51 STILL FAILS**. Gate not met (far blocker). **Far is now PROVEN UPSTREAM, not fixable in the hit model** — three probes:
  1. **Labeling is not the blocker** (`.claude/tmp/hit_sidematch.log`): side-consistent positive selection via the SA pid lifted label WHO 67%→94% but held-out far only 6→8 and F1 regressed 0.444→0.410. Reverted.
  2. **Emission/attribution decomposition** (`.claude/tmp/emission_vs_attribution.py`): far FIRES 22/51 (near 32/51) and of those only 8/22 attribute right (near 28/32). Both far losses share one root — the scorer fires on the stronger BOUNCE, not the weak far hit.
  3. **Fork probe** (`.claude/tmp/far_fork_probe.py`): far is **56% of training positives** (NOT data-starved → reweighting won't help), and far-hit vs far-bounce are **feature-INDISTINGUISHABLE** (angle 135 vs 139, speed 5-8px both, density 8 both, player-gap ~500px both — vs NEAR-hit's sharp 30px/264px signature). The far ball/player trajectory is too coarse at distance for ANY scorer to separate a hit reversal from a bounce reversal.
- **VERDICT → option 2**: far needs sharper upstream far ball (+ far player) tracking. Candidates EXIST (46/51 far hits have one) so the far ball IS detected → **runtime-neutral TrackNet fine-tune** (same model/resolution, better weights via `ml_pipeline/training/` + `bench_finetuned`), NOT a resolution/tiling/fps increase (Tomo's runtime budget: <2h for a 45-min match must hold). Option 1 (perspective features) and labeling are EXHAUSTED — don't re-try them.

## NEXT (in order)
1. **B2 far = option 2 (Tomo-approved direction, 2026-06-12).** Runtime-neutral TrackNet far-ball fine-tune. Sequence: (a) build/extend far-ball training set from the dual-submit corpus (`ml_pipeline/training/`); (b) fine-tune at CURRENT resolution (no runtime add); (c) validate `bench_ball` + `bench_finetuned` (must not regress near-ball); (d) Batch re-run the reference video (rev rebuild + dual-region ECR + job-defs — rule #8) to regenerate cleaner candidates; (e) rebuild hit dataset + retrain hit model, re-measure far on clean heldout (86ade942). Bar: far-hit becomes feature-separable from far-bounce (speed/angle gap opens) → far emission+attribution rise toward 19/51. **If fine-tune plateaus → resolution/tiling is the only remaining lever and is a Tomo runtime-budget decision (<2h hold).**
2. **Near side is shippable now** (31/51 ≫ 13/51 heuristic). Option: wire hit model in as near-side-only with far gated known-limited, in parallel with the far work.
3. **p11 validation** (if not done): `.claude/tmp/p11_validate.py 90bba646-2745-4d4a-8e03-10c0b8ad4ad3`. Bars: pid-1 off-court ≪45%, FAR p90 tightens from +8.07, bounce NULL ≪72%, far serve ≥7/12, near 13/14.
4. **bench_silver baseline regen** (stale + the serve-inheritance flip shifts it).
5. Remaining 18-field items: **swing v2.1** (4th class), **bounce recall** (38% — note: low bounce recall also degrades hit-vs-bounce labeling, ties into B2), set_number, point/game structure on next real upload.
6. Corpus retrains as Tomo uploads.

## Canonical state
- main @ `aba54ad` synced with image rev 74/55. Bench floor: ea1e500c 12/26 + 880dff02 23/24.
- Probe rows in ml_analysis: p9b `ea1e500c` (scorecard source + fixture), p10 `432c3ff3` (serve-stack validation + silver build exists), p11 `90bba646` (in flight).
- Reference video local: `ml_pipeline/test_videos/a798eff0_sa_video.mp4`. SA companion `ba4812be` (26 serves: 14N/12F).
- Probe harness: `.claude/tmp/probe_{submit,measure}.py`, `p10_validate.py`, `p11_validate.py`; per-run scorecard now in `ml_pipeline/diag/scorecard.py`.

## Memory entries this arc
`nat-idle-drop-long-db-connections` (dataset build hang), `count-alignment-is-not-provenance` (the rule-1 audit), handover deploy step 3 cross-region digest check (`c2f8f65`).
---
**END OF PICKUP**
