# T5 ML Pipeline — North Star

**2026-06-06 — FAR-COURT FOUNDATION FIXED: FAR POSITION ERROR -0.43m == NEAR -0.42m (was +11.0m). Deployed rev 72/53.** The 2026-06-04 entry's residual far items are RESOLVED — and the "far-baseline projection overshoots 2.4-7m" was root-caused: **every calibration fitting stage amputated the far keypoints** (RANSAC/rejection thresholds sized for near-court pixel noise; the far keypoints were detected 14/14 and discarded), so the far half was extrapolated from near-only fits. Fix chain, each probe-validated vs SportAI on the reference video: **(1) calibration** (`b08a858`, far bias +11.0→~-1m, bounce xy 4.9→**0.90m**, in-flight phantom bounces now auto-killed by honest projection); **(2) identity** (`e9ae36f` `PLAYER_SPLIT_BY_NET` — near/far split by court NET line not frame midline; pid-1 pollution **46%→0%**); **(3) ROI rally-gate on CNN ball_bounces** (`328d3b8`, bounce stage reordered before ROI sweep; far-ROI usable poses **391→1019**); **(4) tracker warp-decomp** (`b696c26`, -10m widenings→5m; far error → **-0.43m**). Also: serve detector consumes CNN bounces (`05fe85d`), SAHI_BATCHED accuracy regression found+fixed (feed −26% near-pose, rev 67), CI bench repaired (red since May 28, `f24f4f5`), serve model v1 scaffold (`61b677b`, trained-on-warped-coords → retrain pending). **Serve: near 13/14 = ceiling; far 3/12 heuristic-saturated → training on the now-clean feed is the path to ≥20/26.** The scorecard below is DOUBLY STALE (pre-warp-fix measurements) — **next: fixture regen → fresh 18-field scorecard → serve model retrain** (`.claude/next_session_pickup.md`).

**2026-06-04 — FAR-SIDE WAS BUGS, NOT PHYSICS + STROKE-TYPE MODEL BUILT (was 0%).** A root-cause investigation overturned the "far is just 30px physics" assumption: two FIXABLE bugs were dropping far data. (1) **Frame-space mismatch** (`fab487a`) — SportAI hit_frames (source-fps) matched against 25fps detections dropped **62% of swing training hits** (bimodal by fps, not court distance — the recurring `feedback_t5_two_frame_spaces` bug). (2) **Far NULL court_y** (`353a6cc`) — `map_to_court`'s strict ±5m bound nulls ~50% of far detections (the far-baseline projection overshoots 2.4-7m). Combined recovery: swing training data **786 → 1588 hits, FAR 207 → 595 (~2.9×)** — WITHOUT relaxing map_to_court (precise far coords stay a calibration task, not corrupted into silver). **The stroke-TYPE classifier went from UNTRAINED (0%, heuristic stopgap) to a real model**: v2 (far-rich) val macro-F1 0.77 (fh 0.77 / bh 0.68 / overhead 0.87); per-role NEAR/FAR eval added (`b07be60`). **Bounce v2** retrained on 7 matches: F1 0.40 → 0.54. **Pipeline confirmed FULLY AUTO** SportAI→T5→corpus (both AUTO_ gates ON); training manual by design. **Queue switched g4-primary** (validated A/B: g4 1.45× slower but 24% cheaper/match + matches the A4000 migration target). **NEXT: re-measure the 18-field reconciliation vs SportAI — the far-side fixes likely materially improved bronze-vs-SA; the scorecard below is STALE (pre-fix).** See `.claude/next_session_pickup.md` + memory `project_farside_mostly_fixable_bugs`.

**2026-06-03 — MAIN-LOOP RUNTIME CYCLE DONE: 118 → ~109 min, ACCURACY-CLEAN (job-def eu rev 62 / us rev 43).** Three g5 runs on the 60fps Jimbo match (`b2f16f55`/`ce048588`/`d39a6f07`): **B1 decode-skip** (main `VideoPreprocessor` grab/retrieve — main loop 57.5→~50 min, byte-identical), **D1** (free GPU cache at main→ROI boundary — proved GPU is COMPUTE-bound, 380MB/24GB), **B2** (`MOG2_DOWNSCALE=4`, accuracy-neutral) all SHIPPED. **Batching REJECTED** (no-op — GPU compute-bound). **imgsz960 REJECTED** (−18.5% far-player for −3.6 min — bad trade vs far-court priority). **Sub-1h NOT reached and now gated only behind accuracy-sensitive far-court levers** (C1 bounce-CPU, pose-stride) — decision for Tomo: is <1h worth ~15-20% far-court signal? See `.claude/next_session_pickup.md` + `docs/_investigation/t5_runtime_backlog.md` §"Honest sub-1h math". [Superseded plan below — kept for the per-stage profile.]

**2026-05-31 — BATCH RUNTIME OPTIMISATION DEPLOYED + PROVEN (rev 59). 4.79h → ~118 min; far-pose cross-fps misalignment FIXED.** Shipped + validated in prod on match 4 (`624e0b36`): player/ROI batching + FP16 + SAHI-batched + MOG2-downscale + CPU/GPU overlap + g5/A10G (main loop 183 → **48 ms/frame**), the **ROI-sweep 25fps-alignment fix** (far-pose was indexed in 60fps source space, misaligned with 25fps bronze on every non-25fps match — now `frame_idx[24..71914]`; also a 2.4× over-decode removed → sweep 91→52 min), pose-fp16-crash + bounce-OOM fixes. **Total 157→118 min, court 88%, 0 errors.** Frame-space gotcha now in MEMORY (`feedback_t5_two_frame_spaces`). [Superseded plan below — kept for the per-stage profile.]

**2026-05-28 (close 3) — BATCH GPU RUNTIME OPTIMISATION PLAN ON DISK.** Memory is solved; **AWS Batch runtime is the next inflection point.** Current: ~183 ms/frame → **~4.79 h per 44-min match**. Target: **<1 h per 45-min match** (3.5× speedup). Bottleneck empirically isolated: YOLOv8x-pose @ 1280 + SAHI tile-fan, both running `batch=1` every 5th frame — ~75-85% of per-frame wall. Tonight's WASB ball-batching (`5317c50`) delivered ~zero ms/frame change, proving ball is NOT on the critical path (confirmed: `BALL_BATCH_SIZE=8` is live on eu-north-1 rev 53 + us-east-1 rev 35, and ca475740 is running on rev 53). Player batching is the only remaining lever in the same template. **Ranked roadmap + per-stage profile + quality gates: `docs/_investigation/batch_optimisation_plan.md` (`cb4e449`).** Top three levers: L1 player-stage GPU batching (25-40% cut, zero accuracy risk), L4 ROI ViTPose + TrackNet batching + FP16 (3-5× on the ~29% ROI slice), L5 NVENC transcode (~5-10 min). Stacked: 4.79h → ~2.4h. Adding L3 (FP16 YOLO) likely lands under 1h. L7 (G5.xlarge / A10G) is the no-code closer. **Trips BATCH-SIDE CHECKLIST rule #8 → daylight-only deploy.** Morning-prompt Option B in `.claude/next_session_pickup.md`.

**2026-05-28 — CORPUS AUTO-LAND VERIFIED END-TO-END ON LONG MATCHES (training runway open).** Corpus #3 (`9378f2dd` ↔ SA `2f355924`, 44-min match) landed via the proper auto-spawn → sweep → ingest → AUTO_LABEL flow in **3 min 39 sec**. Three stacked OOMs on Render's 512MB main API are now all fixed by the same streaming + numpy(17,3) keypoint compaction template: bronze ingest (`8dc3b31`, 250→15 MB), detectors (`859718d`, 210→75/53 MB), and tonight's silver-build allocation (`781a4cb`, 269→79 MB — the `_build_player_buckets` `.fetchall()` of 72k player_detections with nested-list keypoints). Worst-stage peak Python heap end-to-end is now ~80 MB; Render has comfortable headroom. Three corpus rows have shipped through the same hands-off path across two days (Match 1 `78c32f53` 2026-05-22; Match 2 `c645a7ee` 2026-05-27; Match 3 `9378f2dd` 2026-05-28). **Bulk training-video load is unblocked.** Full detail: `MEMORY.md`→`project_t5_may28_corpus_autoland_unblocked.md`. **NEXT: the detector-build queue (one-model-per-fact) — ADR-01 bounce + ADR-03 identity scaffolds shipped 2026-05-28 (commit `6154de9`); the v1 wins (Path A ITF-default for ADR-03 + Path B label-audit for ADR-01) are the next session's job. See `.claude/next_session_pickup.md`.**

**Last updated:** 2026-05-27 — **Build-first/train-last goal codified** (see "★ THE OVERARCHING GOAL" below) + live 18-field status snapshot. ROI Bug 2 deployed (eu rev50/us rev32). Earlier 2026-05-25 — **Phase 7 MEASURED + REFRAMED.** Ball-bounce reconciliation vs SA on Match 1 (live-DB, `docs/_investigation/bounce_accuracy.md`): the bounce problem is **detection precision + timing, NOT coordinate calibration** — the court calibration is a faithful homography (0.11m self-consistency), and the ~177 nulled bounces are ~84% airborne false-positives, not lost ground bounces. Phase 7 work reframed from "recalibrate" to "reject airborne `is_bounce` FPs + fix ~0.5s timing." (Prior: Phase 5e WASB production verification PASSED, task `1d6feb3a`, 54.3% detection; Phase 5c.2 SHIPPED `d7718e0` + silver bench `83e1ab7`.)
**Last verified:** 2026-05-22 — serve bench green (a798eff0 20/24, 880dff02 23/24); ball-bench v2 locked at `7100792`; silver-bench schema init verified locally (24 tables on fresh Docker Postgres including `ml_analysis.training_corpus`); WASB pipeline verified in production on Batch task `1d6feb3a`. 5c.2 hook still gated behind `AUTO_LABEL_DUAL_SUBMIT_PAIRS=0` until Tomo flips it.
**Previous version archived:** `docs/_archive/north_star_2026-05-07_phantom-bounce-era.md`
**This is the single place where the T5 macro plan lives.** Phase work happens against this ladder. Don't invent new directions — pick a phase, claim it, deliver, update.

---

## ★ RULES OF THE GAME — read first, every session. ALL build happens in this vein.

Non-negotiable. A change that violates one of these is going backwards — stop and rethink.

1. **Bronze is the single source of truth. Silver inherits it 100% and does NO work.** Silver only *projects* bronze + adds analytics (score, serve location 1-8, zones, aggression, depth). If you catch silver *computing a base fact* (serve, swing, bounce, identity), that's a bug to fix — not to extend.
2. **One model per fact.** Pipeline: raw detectors (TrackNet / WASB / YOLOv8-pose / ViTPose) → **analysis models** (serve_detector, stroke_detector, …) → **bronze** normalised answers (`ml_analysis.*`) → silver projects. A fact is "done" only when a *model* emits it. A fact with no model is a **model to build/train** — tag it `STOPGAP-until-model-X`, never a silent silver heuristic. (Audit: `docs/_investigation/bronze_silver_18_audit.md`.)
3. **Build-first, train-LAST.** Build all 18 base fields to ~70-80% with the standard models *now*; train to 90-95% *later* — it's free + automatic via SportAI dual-submit. **Train selectively** (don't cap us at SportAI where our heavier models may be better). Pipeline-speed / throughput is a *training-stage* lever, never a reason to pause building.
4. **Measure-first, bench-green.** Validate against live data (`db_init.engine`) before committing. Keep the serve bench green (`ea1e500c 12/26, 880dff02 23/24` — re-baselined 2026-06-06 on rev-72 clean coordinates). No Batch push without the **BATCH-SIDE CHECKLIST** (`.claude/handover_t5.md`).
5. **Keep it clean — always.** Doc structure is fixed: **this file = True North** (rules + 18-field status + phase ladder); `.claude/next_session_pickup.md` = handover; `.claude/handover_t5.md` = ops / how-to-run; `docs/_investigation/*` = per-model references; everything historical → `_archive/`. **Don't create a new doc when an existing one fits.** Every session reads this file + the handover before touching code.

---

## ★ THE OVERARCHING GOAL — build the 18, THEN train (build-first, train-LAST)

The objective is an in-house pipeline whose **bronze** (`ml_analysis.*` → silver Pass 1) reproduces SportAI's **18 base facts materially.** Silver derives *everything else* (zones, aggression, serve location 1-8, rally analytics) off those same 18 — so **bronze-t5 ≈ bronze-sportai is the whole game.**

**Sequence — do NOT reorder:**
1. **BUILD** all 18 base fields to **~70-80%** using the standard models we already have (YOLOv8-pose, TrackNet/WASB, ViTPose). ← **current + dominant priority.**
2. **THEN TRAIN** to 90-95% — and this is **FREE + automatic:** every production SportAI dual-submit accumulates a training pair, so once productionised we get hundreds of labelled games for nothing. **Training is LAST and self-funding.** Don't spend build-phase effort chasing accuracy that training will deliver by default.

> **⚠️ Training caution (Tomo, 2026-05-27):** SportAI-as-teacher **caps us at SportAI's accuracy** on any field where our (more detailed) models could exceed it — TrackNet/WASB + ViTPose are heavier than SportAI's "good-enough" pretrained stack. So **don't blindly fit to SportAI** on fields where we may already be as good or better (e.g. fine-grained pose-driven swing type, hit location). Train **selectively** — teacher-train the fields where SportAI is clearly the better signal (it's fast + accurate enough, not yet line-calling), and **manually verify / hold out** the fields where we might be ahead. The free auto-corpus is for *volume*; what we train *on* is a deliberate per-field choice, not "match SportAI everywhere."

**Implication:** pipeline-speed / corpus-throughput optimisation is a *training-stage* lever (it makes the free training faster) — **NOT** a reason to pause building. Build the 18 first; sign off "dev done" before training.

**The 18 base fields** = the Pass-1 projection in `build_silver_match_t5.py`: WHO hit (player_id/side), WHAT (serve, swing_type, volley), WHEN (ball_hit_s), WHERE-hit (ball_hit_location_x/y), WHERE-bounced (court_x/y), ball_speed, ball_player_distance, rally membership — plus the point/game/set structure passes 3-5 derive from them.

**★ SILVER ROW ARCHITECTURE = HIT-DRIVEN. SETTLED 2026-06-14 (probe ladder on live SA data).** One silver row = one SHOT = one HIT. The bounce is an *outcome attribute* of the shot (where it landed), matched in as an UPDATE — NEVER the row key. Evidence: (1) every SA swing carries hit coords (100% of 2,380 swings across 6 matches); the bounce stream is multi-typed (floor/net/racket) at 1.12× swing count, not 1:1 with shots; (2) prod SA silver is ALREADY hit-driven — `build_silver_v2.py:357` inserts one row per `bronze.player_swing`, Pass-2 (`:376`) matches a bounce in as an UPDATE; (3) only `build_silver_match_t5.py` Pass-1 is bounce-driven — the inconsistent outlier. **Consequence:** the T5 target is Pass-1 inserts from the HIT stream (`ml_analysis.stroke_events`, mirror SA) + bounce MODEL (`ml_analysis.ball_bounces`) demoted to the Pass-2 coordinate enricher. The "deprecate is_bounce → inherit ball_bounces" lever was polishing the wrong axle (keeps T5 bounce-driven). **ENABLEMENT is gated** on the T5 hit model's far-side WHO-attribution (far ~6/51 — the 2026-05-25 stroke-driven flip overshot on this, not on the architecture); emission is fine (B1 anchor recall 94-96%, balanced near/far). Flip when sharp-far retrain lifts far attribution. Memory `silver_must_be_hit_driven`.

**★ DEFINITION OF DONE — HIT + BOUNCE (the gate before SWING). Verified live 2026-06-14: BUILD done (both models emit); we are at TRAIN-LAST.** The far-side accuracy gap on BOTH facts is one root cause (far ball/player too coarse), proven un-movable by heuristics → training-gated. DONE: bounce model emits (`ball_bounces`) ✅, hit model emits (`stroke_events`) ✅, bounce precision lever (thr 0.70, rev 79/60) ✅, architecture decided hit-driven ✅. REMAINING: (5) verify bounce output survives re-ingest — 0 rows on all existing tasks, claim unverified [verify next upload]; (6) locked `bench_hit` — hit has no repeatable gate yet [no-data build, do anytime]; (7) **sharp-far footage accrues — new FULL-RES uploads through rev-79** [DATA, Tomo]; (8) retrain bounce CNN + hit model on sharp-far → far reaches ~70-80% bar [TRAIN, gated on 7]; (9) flip silver hit-driven (Pass-1 from `stroke_events`, bounce→Pass-2 enricher) [LAST, after 8 — early flip regresses silver]. THEN → swing (2nd-last model). **Forward progress now = #7 (upload full-res) → #8 retrain; #6 is the only no-data build item left.** Full checklist: `.claude/next_session_pickup.md` §"DEFINITION OF DONE".

### ★ FRESH 18-FIELD SCORECARD — rev 72 clean coordinates (p9b `ea1e500c` vs SA `ba4812be`), 2026-06-06 PM

First field-by-field measurement on post-far-court-fix data (calibration `b08a858` + identity `e9ae36f` + ROI gate `328d3b8` + decomp `b696c26`). Tool: `python -m ml_pipeline.diag.scorecard <job_id>` (promoted `12aad57`) + `harness eval-serve`. All prior accuracy numbers were measured against the warped map — treat this table as the new canonical baseline.

| field | measure (rev 72) | vs 70-80% build bar |
|---|---|---|
| WHO — identity (near-half pollution) | 0% | ✅ **SIGNED OFF** |
| WHO — identity (off-court static FP) | **FIXED 2026-06-07 (p14, rev 77/58): 45% → 1%.** Drop on STORED court_x ∉ [-2, 12.97] at BOTH write boundaries (db_writer `3f04f21` + bronze_export `c991f2a` — the re-ingest wipes a db_writer-only drop). ⚠️ Selection-level bounds were tried twice and killed 8,146 real far rows (`feedback_stored_rows_blind_to_scoring_population`) — do NOT re-add tier bounds. | ✅ **D1 DONE** |
| WHERE — player position NEAR | med -0.42m (p10 -0.79 / p90 +0.20) | ✅ **SIGNED OFF** |
| WHERE — player position FAR | **p90 +8.07 → +0.36 (D1 fixed, p14).** ⚠️ Honest median is **-2.17m** — the earlier "-0.43 == NEAR" was spectator-flattered; the residual is real behind-baseline extrapolation bias (the documented recurring tail). | ✅ spread **DONE**; median bias = known calibration item (training/calibration, not heuristics) |
| serve NEAR | 13/14 recall (eval-serve), 12/14 strict bench | ✅ **SIGNED OFF** (dev ceiling) |
| serve FAR | **7/12 eval (was 3/12)** — serve model v1 live end-to-end (p10, rev 73, 2026-06-06 PM): Batch `serve_candidates` stage → detector `model_far` merge → silver verbatim 48/48 both directions. C1 ROI gate fixed same day. | ✅ **SIGNED OFF at build bar** — further gains = training (corpus growth retrains the model free) + ROI serve-window detection depth (2/10 windows still thin) |
| stroke WHEN/WHO NEAR | 13/51 @1.0s (7/51 @0.5s); 55 emitted vs SA 51 — count parity masked event-level misalignment; consistent across rev 67/68/72 (never worked, not a regression) | ❌ TRAIN |
| stroke WHEN/WHO FAR | 19/51 @1.0s; 161 emitted vs SA 51 (3.2× over) | ❌ TRAIN |
| swing_type | classifier DISABLED (per-hit 32% vs heuristic 38%, both below bar) | ❌ TRAIN (v2.1 + 4th class) |
| bounce — recall/precision | **2026-06-14: precision lever SHIPPED (thr 0.5→0.70, env `BOUNCE_CNN_THRESHOLD`, eu rev 79/us rev 60). Offline (5 labeled tasks): recall 20.7%→18.2%, precision 11.0%→23.3% (2.1×), over-emission 1.88×→0.78×.** Floor was thr 0.5 = rec 20.7/prec 11.0 (1416 emitted/752 labels). Sharp ball (de-risk) lifts far recall only 26→32% — CNN fit to COARSE distribution → **headline recall is TRAINING-GATED** (`feedback_far_roi_payoff_is_scorer_training_gated`). | 🟡 precision lever DONE; recall TRAIN-gated (sharp-far retrain) |
| bounce — xy accuracy | med 0.90m on matched (26/68 within 0.6s); corpus medErr 0.26-0.50m | ✅ accuracy at bar |
| bounce — NULL coords | 72% → **61%** (D2, `aba54ad`: ball image xy projected at the bounce frame — valid exactly then, ball on ground). Matched bounces 26→30. Residual NULLs are strict-bounds rejections of far projections — honest. | 🟡 **D2 partial** — residual rides the far-calibration tail |
| ball speed | 2040/8011 detections carry speed_kmh; accuracy unmeasured | ⚠️ presence only |
| ball_hit_location x/y | blocked on stroke ts alignment | ❌ blocked by stroke |
| rally / point / game / set structure | not measurable on a probe job (no silver rows) | ⏸ measure on next real upload |

**Dev-ceiling sign-off list (what "finish the build" means now):**

- **★ FAR-BALL ROI DEPLOYED 2026-06-13 (eu rev 78 / us rev 59).** Far ball re-detected on a high-res far-court crop (`roi_extractors/far_ball.py`, env `ROI_FAR_BALL_ENABLED`), source-preference merged for trajectory readers (`ml_pipeline/ball_merge.py`, roi_far_ball>roi_prod>main; silver stays main-only), carried through re-ingest. **Bounce-coupling PROVEN end-to-end:** far-bounce candidate recall **40%→87%** on the deployed pipeline (the candidate stage was the bounce-recall bottleneck). This is the upstream **bronze** fix that unblocks BOTH far-bounce recall (bounce #4) and the far hit gate (far hits = non-bounce far events, via the existing `cnn_bounce_gap` hit feature). Far-gate hit-model retrain now accrues from new full-res uploads (corpus originals deleted post-trim — no manual re-run). See `docs/_investigation/far_ball_roi.md`.
- **SIGNED OFF — no further heuristics:** near serve, **far serve (2026-06-06 PM — see below)**, near position, far position median, near-half identity, court mapping. Touch these only via training.
- **★ SERVE: FULLY SIGNED OFF 2026-06-06 PM (p10, rev 73).** The complete chain is live and verbatim: Batch serve-model stage (`ml_analysis.serve_candidates`, `SERVE_MODEL_STAGE=1`) → Render detector merge (`model_far` additive, `SERVE_MODEL_ENABLED` default-on) → bronze `serve_events` → silver inherits 100% verbatim (`T5_SERVE_FROM_EVENTS` default-on, min-conf 0). p10 numbers: near 13/14, **far 7/12 (was 3/12)**, total **20/26 at eval tolerance**; silver↔bronze trace **48/48 both directions**. ⚠️ Same audit found the May-27 inheritance had NEVER been live in prod (default-OFF flag, Render env flip never landed — `feedback_count_alignment_is_not_provenance`); fixed by flipping code defaults ON. Accuracy from here = free training (every SA dual-submit upload retrains the serve model on clean labels) — NOT detector heuristics.
- **Dev items remaining (cheap + deterministic, do before/alongside training):**
  - **D1 — pid-1 off-court static-FP gate**: kill detections with court_x outside [-1.5, 12.5] persisting in a tight y-band. Deterministic rule (trust-the-rule pattern), Batch-side tracker or Render-side filter. Cleans far position spread AND stroke far over-emission inputs.
  - **D2 — bounce NULL-coord projection**: 72% of CNN bounces carry no court coords; the projection exists (calibration is fixed) — wire it through the bounce stage.
  - **C1 — ROI sweep serve-window coverage (Batch-side, next deploy cycle)**: the rally gate (`328d3b8`) leaves **0-1 far ROI pose rows in ALL far serve windows** (measured p9b); ROI rows are the only far rows carrying court_y, so the far pose path starves upstream of every serve gate. Pad the rally gate backward ~5-8s (or anchor on near-half CNN bounces) so serve wind-ups are swept. This also feeds the serve model better far features (Job 3).
- **Training territory (bronze-first, SA-as-teacher where SA is reliable):** far serve (serve_model v1 retrain — gate: ≥ heuristic far 3/12 eval + bench green), stroke event alignment (heuristic detector confirmed at ceiling on three revisions), swing type v2.1, bounce recall.
- **Deferred to next real upload:** point/game/set structure, silver-level fields (probe jobs have no silver).

### Build status vs SportAI — Match 1 (`78c32f53` vs `0d0514df`), 2026-05-27
> ⚠️ **STALE (pre-far-side-fix).** Table is from 2026-05-27. Swing_type now has a TRAINED classifier (v2 macro-F1 0.77; near 0.86 / far 0.61), far training data ~2.9×.
>
> **★ CORRECTED SCORECARD 2026-06-04 (reconcile tool was distorting it — `3577601`).** The reconcile counted `exclude_d=True` rows, badly over-stating T5. Respecting `exclude_d` (ACTIVE rows), the current-rev pair `a35b37f6` vs SA `ba4812be` is **much more aligned than the old table implies**: active 71 vs 84; **backhand 14 vs 15 ✅** (NOT the "+10 over-count" — that was the artifact); serve 24 vs 26 ✅; volley 4 vs 4 ✅; points 17 vs 18 ✅. **Real remaining gaps:** forehand UNDER-count (20 vs 39), overhead over-label (9 vs 0, some fh mislabelled), game over-segmentation (4 vs 2), and the silver exclusion IS over-aggressive — **CONFIRMED ROOT (2026-06-04): the `gap_break` cascade in `build_silver_v2.py:725-735`.** A >5s gap between shots after the last serve flags the shot, and the cumulative `BOOL_OR ... ROWS UNBOUNDED PRECEDING` then excludes that shot AND every subsequent shot in the point — so ONE mid-rally gap kills the whole point tail (pt6: 19/21 excluded). Far player hit hardest (45/66 excluded) because its detection is intermittent → apparent gaps → cascade. This IS the forehand under-count (T5 detects 59 fh, excludes 39, keeps 20 vs SA 39). Greedy-chain-rejection bug (`feedback_greedy_chain_rejection`). **✅ FIXED + VALIDATED 2026-06-04 (`2ef26bd`):** replaced the cascade with a `last_connected_s` re-anchor (gap-exclude only the trailing tail beyond the last live-rally shot). Real pair `a35b37f6` vs SA: active **forehand 20 → 40 (= SA 39)** — recovered exactly onto SA's real count. bench_silver fixture improved (1→4 active). Residual swing over-count (bh 23 vs 15) is the HEURISTIC mislabel → the swing classifier deploy fixes it. SHARED prod silver but change only makes gap_break LESS aggressive (low SA risk; no SA bench fixture yet — add one). The swing-classifier deploy separately helps forehand/overhead labeling. Lesson: **always reconcile on `exclude_d IS NOT TRUE`.**
| base field | T5 | SA | read |
|---|---|---|---|
| active rows (overall) | 97 | 94 | ✅ aligned |
| court mapping | faithful homography (0.11 m self-consistent) | — | ✅ silent-degeneracy + degenerate-lock tail **FIXED & PROVEN IN PROD 2026-05-29** (match 4 landed: radial lock, 334 silver rows all with court_x). Fix G/B frame-selection + projection self-test (deployed rev 57) + lock-best & projection-quality candidate select (on `main`, rides next rebuild → match 4 43%→~94% local). Camera-agnostic lens model (Fix E) built but dormant (needs fisheye fixture). See `_investigation/court_calibration_silent_degeneracy.md`. |
| player side (near/far) | 2 pids, clean split | 2 | ✅ side ok — **A/B identity NOT solved** (Q2-B blocked) |
| ball_hit_location x/y | 94 populated | 94 | ✅ populated; accuracy unmeasured (far sparse) |
| swing — forehand | 38 | 41 | ✅ close |
| swing — overhead/serve-motion | 26 | 30 | ✅ close |
| swing — backhand | 28 | 18 | ⚠️ over-counts (~+10) |
| volley | 13 | 6 | ⚠️ over-counts (net-distance proxy too loose) |
| serve (silver count) | 26 (was 15) | 26 | ✅ COUNT-aligned via `T5_SERVE_FROM_EVENTS` — T5 silver now **inherits bronze `serve_events` verbatim** (2026-05-27, `fc9bc6b`). Composition 17near/6far+3ambig vs SA 14/12 → far recall is the residual (far-court ceiling, train). Detector bench: 20/24 & 23/24 |
| point structure | pts 17 / games 3 | 18 / 2 | ✅ close |
| ball bounce x/y | recall 55%, precision 27%, 4.57 m err | — | ❌ **weakest field** (this chat's focus) |
| set_number | not populated | 1 | ⚠️ missing |

**Verdict:** counts are already close to SA on most fields (active 97/94, fh 38/41, pts 17/18). **Below the 70-80% build bar:** ball bounce (worst), serve recall, volley over-count, backhand over-count, set numbering, A/B identity. **That list IS the "finish the build" backlog before train-sign-off.**

> **★ The far-court ceiling (confirmed 2026-05-27).** Four of the weak fields — **serve precision, ball bounce, far-player stroke, A/B identity** — all fail for the **same root reason**: the far player is ~30 px and far bounces are missed, so the far half lacks the corroborating signal. No heuristic/threshold separates real-far from FP-far (both lack the far bounce). Proven repeatedly: bounce-precision filters underdeliver; the near-stroke gate is provisional; Q2-B identity needs changeover detection we can't do; and gating `pose_only` serves was already reverted (kills real far serves — detector.py:539 NOTE). **Implication:** the build phase has hit its ceiling *with standard models* on the far-court fields. Their remaining gains come from **coverage (Phase 5-7, which lifts all four at once)** + **training** — NOT from more silver/detector heuristics. The non-far fields (near serves/strokes, court mapping, volley, point structure) are in good shape. **So: stop tweaking far-court fields; the path is coverage + train-later.**
>
> **★ UPDATE 2026-06-04 — the "ceiling" was PARTLY fixable bugs, not pure physics.** A root-cause investigation found the far-side data loss was *dominated* by two code bugs, not resolution: a frame-space mismatch (SA source-fps hit_frames vs 25fps detections) and a far-NULL-court_y strict-bound (the far projection overshoots 2.4-7m and gets nulled). Fixing both recovered far swing training data **207 → 595 (~2.9×)** and let the stroke-TYPE classifier train for the first time. So the revised view: the far player IS resolution-limited for *pose keypoints* (genuinely fundamental, mitigated by the ViTPose ROI extractor), BUT the far *data pipeline* had fixable alignment/coordinate bugs that were masquerading as the ceiling. **Before declaring any far field "at its ceiling," check frame-space + court_y NULL-rate first.** Precise far *coordinates* remain a real calibration task (the 2.4-7m overshoot). See memory `project_farside_mostly_fixable_bugs`.

> **★ Serve precision — DEV CEILING re-confirmed with receipts (2026-05-27).** The dominant serve FP is the **receiver standing at their baseline** (geometrically identical to a server). Two structural suppression attempts — drop the local-minority server by raw event count, then by serve-cluster count — BOTH regressed the bench near-recall (13/14→1/14, then →5-7/14), because the far player **over-emits** events (pose + bounce paths) so any count signal is biased toward far and drops the cleanly-detected near serves. With the per-event bounce/source filters already proven-bad (detector.py:539 NOTE), receiver-FP suppression is **not achievable by dev heuristics → training territory**. ACTION TAKEN INSTEAD: T5 silver now **inherits `serve_events` verbatim** (`T5_SERVE_FROM_EVENTS`), so silver honestly mirrors bronze and bronze improvements (via training) flow through automatically. **SportAI's serve mapping is generally GOOD (Tomo, 2026-05-27)** — the geometric `serve_d` "custom label" exists for ONE camera-setup-affected video, not an SA failure; so SA is a reliable serve/stroke **teacher** for training, and the symmetric end-state is `serve_d` inheriting each flow's bronze serve (retire the geometric gate; SA-side, future).

---

## How to run / query (quick reference)

- **Serve bench** (mandatory pre-push on any detector edit; floor ea1e500c 12/26, 880dff02 23/24): `.venv/Scripts/python -m ml_pipeline.diag.bench`
- **Per-run scorecard** (after any probe/run of a dual-submit pair): `python -m ml_pipeline.diag.scorecard <job_id> [--sportai-tid <sa>]`
- **Ball bench** (local; ~3h on this CPU-only box — background it): `python -m ml_pipeline.diag.bench_ball`
- **Query the prod DB directly** (this dev box's IP is allowlisted — measure against live data, don't paste shell output): `PYTHONPATH=<repo> .venv/Scripts/python` then `from db_init import engine; engine.connect()`.
- **Rebuild a T5 silver match**: `from ml_pipeline.build_silver_match_t5 import build_silver_match_t5; build_silver_match_t5('<task_id>', replace=True)`
- **Measure a field vs SA / hand-truth**: `python -m ml_pipeline.diag.bounce_xy_accuracy --sa-task <sa> --t5-task <t5>` (or `--ground-truth <json>`); `harness reconcile <sa> <t5>`; `harness eval-serve <task>`.
- **Batch deploy** (after a Batch-side change — see the file-list trigger in `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST"): Docker rebuild → dual-region ECR push → new job-def revisions in **eu-north-1 + us-east-1** (pin the amd64 sub-manifest digest; retryStrategy preserved).
- **Run status** (no frontend visibility): query `ml_analysis.video_analysis_jobs` (`status, current_stage, progress_pct, updated_at`); `ball_detections` populate only AFTER Batch completes + the `sweep-t5-orphans` cron fires the Render ingest.
- **Full ops/how-to-run reference**: `.claude/handover_t5.md`.

---

## Product goal

A tennis match analytics dashboard where:

1. Every event shown corresponds to a real point-stroke (no pre-serve racquet-bouncing leaking in, no between-points walking).
2. Every coordinate is geometrically accurate within the dashboard's tolerance: hitter location, bounce location, ball trajectory.
3. Per-stroke claims (forehand winner, backhand depth, attack/defence, serve-side) are correct on validated points.
4. The user can trust per-rally and per-set aggregates because the underlying events are clean.

We are NOT trying to hit 100% serve detection. We're trying to get the dashboard data trustworthy enough that a coach using it would draw the same conclusions as if they'd watched the match.

---

## Status snapshot — 2026-05-07 EOD

**What shipped today (validated end-to-end on `880dff02-58bd-412c-9a29-5c5151004447` vs SA `2c1ad953-...`):**

- **Phase 1 — bounce-validity rule** → DONE. Strict reconcile **23/24 (10/10 FAR)**, all three target FAR misses recovered. Bench locked at 880dff02 23/24 + a798eff0 20/24.
- **Phase 3 part 1 — warm-up filter** → 35-row noise reduction; **Backhand crushed 62→10**; T5 active silver 49 vs SA 85.
- **Phase 4 — reconciler tool** → shipped (`audit_points_reconcile.py` + baseline + `--honor-exclude` flag).
- Tier 2 SQL endpoint + Tier 3 bench CI live. Batch deploy protocol documented.

**What today's investigation revealed:**

- The per-point reconciler floor of **0/17** isn't a noise problem and isn't a Phase 3 problem. Root cause classified in `docs/_archive/may07_sa_point6_gap.md`: T5's bronze ball detection sits at **~13% frame coverage** match-wide, with **six >40-second gaps**. SA point 6 (9 strokes, ~16s rally) falls inside a 61.8-second ball-detection blackout. Player tracking is fine — it's purely the ball.
- This blocks Phase 6 (stroke classification), Phase 7 (coordinate reconciliation), and Phase 8 (final serve cleanup). All three depend on T5 strokes existing at the right times, which depends on ball detection coverage.

**Renumbered ladder:** what was old "Phase 5/6/7" is now "Phase 6/7/8". A new Phase 5 — **Ball detection coverage** — is inserted as the top bottleneck.

---

## Current bottleneck

**Ball-bounce accuracy — RECONCILED 2026-05-25 against SA on Match 1; the problem is NOT calibration.** Full diagnosis in `docs/_investigation/bounce_accuracy.md`. Headline: the court calibration is a **faithful planar homography** (reconstructable Render-side from player-feet correspondences to 0.11m self-consistency), so Phase 7-as-"recalibration" is **not** the lever. Event recall is fine (85% of SA floor bounces within ±0.8s). The real levers are **bounce-detection precision** (T5 fires 303 events vs SA's 161; ~177 nulled `is_bounce` flags are ~84% airborne false-positives, correctly clamped — not lost ground bounces) and **~0.5s timing jitter** (downstream of 52% ball coverage). The far baseline is **resolution-limited** (~1px ≈ metres) — a physical cap recalibration can't remove; near-half placement is well-conditioned and likely already good. The old "3-7m off / 10-17m far-baseline" framing conflated airborne FPs and the far-court resolution limit with a calibration error that isn't there.

Phase 1 is closed; the phantom-bounce era described in the archived north_star is over.

---

## ★ BRONZE-FIRST PRINCIPLE — Tomo decision 2026-05-25 (supersedes the B→C→A order)

**T5 reconciliation to SportAI is a BRONZE (`ml_analysis.*`) accuracy problem, not a silver-derivation problem. Silver row-generation is FROZEN until the 18 base fields align with SportAI in the bronze layer.**

Layering reminder (the naming hides it): the T5 "bronze" is `ml_analysis.*` (ball/player detections, serve/stroke events). `build_silver_match_t5.py` **Pass 1 is the bronze→base-fact projection** that must reconcile to SportAI (the 18 columns: player_id, serve, swing_type, volley, hit location, court_x/y, ball_speed, …). Passes 3-5 are the silver analytics (serve location 1-8, zones, aggression) layered on top — garbage-in/garbage-out if the Pass-1 rows are wrong.

**Why this is now the rule (proven 2026-05-25):** We wired `ml_analysis.stroke_events` into Pass 1 (the old Option B) — stroke-driven row generation, one stroke → one silver row. It overshot badly on Match 1: **141 vs SA's 84 active; near 114 / far 27 vs SA's balanced 43 / 41; far Forehands got *worse* (6 vs SA's 18)**. Root cause is bronze: the stroke detector's hitter attribution is perspective-biased to the near player (208/34 vs true ~50/50), far-player pose is sparse (1,105 vs 11,755 keypoint rows on M1), and bounce coords are off (Phase 7). The code is **committed but gated OFF** behind `T5_STROKE_DRIVEN_SILVER` (bounce-driven stays the live path) — **do not flip it on, and do not chase reconciliation by reorganising silver, until bronze is right.** See CLAUDE.md "Things not to do" #11.

### Progress 2026-05-25 — far player fixed, near precision provisionally gated (all Render-side, gated path)

The stroke-driven silver bronze gaps were attacked end-to-end (stroke-driven path still gated OFF; no live impact):
- **Far player FIXED** (3 commits): ROI ViTPose pose wired into silver + stroke buckets (`ead857a`); far fh/bh camera-mirror (`a8479a8`); far wrist-velocity size-normalisation (`956b65a`). Gated stroke-driven far active 27→43 (SA 41); stroke attribution 208/34 → 165/106.
- **Near precision PROVISIONAL gate** (`9a4ab0a`): near-only wrist swing-path ≥0.75 torso-lengths (cuts pre-serve ball-bounce + recovery twitches — small-arc, high-velocity false peaks). Gated stroke-driven near 108→43 (=SA 43); total 151→78 (SA 84). **Single-match-calibrated → re-validate on a 2nd match or supersede with Q1-D.**

Full detail: `docs/_investigation/far_player_accuracy.md`, `.claude/next_session_pickup.md`.

### Priority order (reframed): fix bronze accuracy first

| # | Work | Where | Why this position |
|---|---|---|---|
| **1** | **Validate the near swing-path gate (2nd match) / Q1-D trained classifier; Q2-B A/B identity** | Render-side | The near gate is single-match-calibrated — needs a 2nd match's SA truth or the trained stroke classifier (Q1-D, `training_corpus` accumulating) before it's trustworthy. Player A/B identity (Q2-B) still unaddressed (silver assigns by court SIDE; can't hold A/B across an end-change). Both gate the stroke-driven flip. |
| **2** | **Phase 7 (reframed) — bounce-detection PRECISION, not recalibration** | `roi_extractors/bounces.py` (Batch) and/or a silver-side `is_bounce` guard (Render) | RECONCILED 2026-05-25 (`docs/_investigation/bounce_accuracy.md`): calibration is a faithful homography — recalibration is NOT the lever. Reject airborne `is_bounce` FPs (~177/303, ~84% above the court plane); fix the ~0.5s timing jitter (ball coverage). Far-baseline accuracy is resolution-capped (~1px≈m). **Don't trigger a Batch recalibration to chase the 3m number.** |
| **C** | **Fix `roi_bounces` per-window slowdown (Bug 2)** | Batch-side, `ml_pipeline/roi_extractors/bounces.py` | Contained, well-diagnosed. Unblocks long matches (Match 2 timed out at 6h). One-file change (load TrackNet outside the window loop). |

**Deferred / lower-priority backlog:**

- **Bug 1 — far-ROI region misalignment** — now folded into priority #1 (far-player accuracy); it's a concrete symptom of the same problem.
- **Option D — retune `excl_chain.gap_break` 5s → 8s** — silver-side; moot under the bronze-first freeze. Wait for a silver-bench fixture *and* clean bronze.
- **Phase 8 — final serve-detection cleanup** (4 a798eff0 + 1 880dff02 misses) — lower priority than Phase 7.

Full strategic analysis is in `.claude/next_session_pickup.md`.

---

## Strategy update 2026-05-24 — Path 0 (no training) confirmed viable for Phase 6

**Headline:** Three diag probes today on the `0d0514df ↔ 78c32f53` dual-submit pair answered the "should we train?" question with concrete numbers. Pose-only stroke detection is viable; training is now insurance / runway to 90%+, not a load-bearing prerequisite.

**The three probes (commit history preserves the algorithms; only the winner is in the tree):**

| Probe | Approach | Recall vs 106 SA hits | Why it died / lives |
|---|---|---|---|
| 1. `ball_hit_baseline.py` (deleted 2026-05-24) | y-reversal on ball trajectory (port of ameynarwadkar repo) | **0%** | Heuristic assumes broadcast/top-down camera; our amateur side-cam has horizontal ball motion. Dead end. |
| 2. `ball_hit_fusion.py` (deleted 2026-05-24) | ball position vs wrist-keypoint distance | **15%** | At the millisecond of contact, the ball is OCCLUDED by racquet/player. WASB coverage gap aligns exactly with SA truth frames (median 477px ball-to-wrist distance AT truth). No ball-based heuristic can recover. |
| 3. `ml_pipeline/diag/ball_hit_pose.py` (KEPT) | wrist-velocity peaks, no ball signal | **63-67%** | Pose IS detectable at hit moments where ball is occluded. The signal exists; remaining gap is algorithm sophistication. |

**Coverage measurement on `78c32f53` (post-WASB + chain-rejection):** 8,005 ball detections / 15,296 frames = **52% coverage**, up from the 13% pre-WASB baseline. That's a 4× improvement — Phase 5's done-when ("≥50% frame coverage") is materially met. Coverage is no longer the dominant constraint.

**Why pose-only beats fusion:** The ball occludes against the racquet at contact. Pose data (wrist keypoint from YOLOv8x-pose) remains detectable at 87% coverage including the hit frames where the ball is invisible. This is the same architectural choice our shipped serve detector uses (Silent Impact 2025 / TAL4Tennis pattern) — 20/24 + 23/24 on bench.

**Realistic ceiling without training** (1-2 weeks of refinement on top of `ball_hit_pose.py`'s 63%):
- Peak+offset correction (velocity peak fires 4-6 frames before SA's contact frame): recall +25pp at ±3 tolerance → ~63%
- Tighten `--min-gap-frames` 15→25 to suppress backswing+follow-through double-detection: precision 21% → ~40%+
- Add acceleration / swing-template matching: another +10pp precision
- Better FAR pose extraction (currently 6,130 entries vs NEAR's 10,115): +5-10pp recall
- **Target: 75-80% recall, 50-60% precision** — production-grade for the dashboard / coaching surfaces

**Where training fits now (Phase 5c / 5d):** The corpus accumulation pipeline (auto-spawn + auto-label, LIVE per Phase 5c.0-5c.3) is not wasted. It's the runway from heuristic-ceiling (~80%) to ML-ceiling (~90-95%). Once we have 5-10 matches of corpus, a small pose-feature classifier (24-frame window of pose features → hit/not-hit) is trainable and would close the heuristic-to-ML gap. **No longer urgent; no longer load-bearing.** Keep accumulating passively.

**What hasn't moved:** ~~Phase 7 (bounce x,y coordinate accuracy in meters)~~ — **MEASURED 2026-05-25.** The geometric error vs SA is now reconciled (`docs/_investigation/bounce_accuracy.md`) and the conclusion flips: it's a bounce-detection *precision* + timing problem, not a coordinate-calibration problem. Calibration is a faithful homography.

**Implication for the phase ladder:** Phase 5 partial → mostly DONE (coverage met). Phase 6 BLOCKED → UNBLOCKED (pose-only viable). Phase 7 BLOCKED → MEASURABLE (next probe). Phase 8 unchanged.

---

## Current detector build queue (2026-05-28) — single source of truth

**Five ADRs APPROVED 2026-05-28** define what to build next and in what order. **Every future session that touches a detector module reads ADR-05 first to claim the next available build.**

| ADR | Topic | Status | Build dependency |
|---|---|---|---|
| [ADR-01](./_investigation/adr_01_bounce_model_architecture.md) | Bounce model — Render-side standalone, 1D temporal CNN + geometric pre-gates | **v1 TRAINING INFRA + GRAVITY-RESIDUAL CANDIDATE GENERATOR SHIPPED 2026-05-28** (`a2bf4b8` + `4a36f34`). Match 1 bench @ thr=0.5 with GR-retrained weights: recall **23.9%** / precision 9.1% / spatial err 0.30m (vs is_bounce-mode 3.0%/3.6%/0.55m → **6.5× recall lift, 47% spatial-err reduction**). Env-gated `BOUNCE_CANDIDATE_MODE=is_bounce`(default)/`gravity_residual` per memory `feedback_env_var_rollback_pattern`. Next ceiling = training-data diversity; awaits Match 4 re-ingest post-calibration-fix (273 more clean labels → 340 total). | Independent |
| [ADR-02](./_investigation/adr_02_swing_type_classifier_plan.md) | Swing-type classifier — R(2+1)D-18 on 16-frame optical-flow ROI | **CORPUS EXTRACTOR + DATASET BUILDER LIVE 2026-05-28.** Extractor: `label_swing_types.py` (3 rows / 775 labels + Corpus 4 ~397). Builder: `build_swing_type_dataset.py` produces architecture-agnostic `(N, 16, 112, 112, 2)` flow tensors from corpus + 720p trimmed video; Match 1 smoke=66/94 hits (28 FAR labels lost to upstream T5 far-detection gap; ±5 fallback recovers what it can). Model class + training loop still pending — need ~2-3k labels (~5-10 more matches) before v1 training. | Independent of bounce |
| [ADR-03](./_investigation/adr_03_identity_model.md) | Player identity — rule v1 (changeover detector), CNN v2 (OSNet) | **v1 SHIPPED 2026-05-28** at 100% bench (n=14 ITF boundaries). Tracker-binding-aware ITF-default. v2 OSNet planned for residual. | Independent of bounce + swing-type |
| [ADR-04](./_investigation/adr_04_volley_model_or_analytic.md) | Volley analytic — pure bronze derivation from bounce + swing events | APPROVED, **BLOCKED** by ADR-01 + ADR-02 | Must wait |
| [ADR-05](./_investigation/adr_05_detector_build_sequencing.md) | Build sequencing + coordination protocol | APPROVED | — |

**Three parallel streams maximum.** Stream 1 (Tomo's option): ADR-01 → ADR-02 → ADR-04. Stream 2: ADR-03 (independent). Stream 3: serve training infra — **corpus extractor for `label_kind='serve'` SHIPPED 2026-05-28** (`ml_pipeline/training/label_serves.py`, 3 backfilled rows / 118 serve labels + 114 once Corpus 4 lands); `serve_detector` retrain awaits ~500+ accumulated labels.

**Coordination rule (ADR-05):** no agent starts a detector build without an APPROVED ADR + a pickup-file claim. Corpus extension lands in the same commit as the detector model it feeds.

---

## Phase ladder

| # | Phase | Done-when | Owner / Status |
|---|---|---|---|
| 0 | Doc cleanup + this file | handover ≤700 lines, ≤5 active T5 memory files, this file exists with phase ladder | DONE 2026-05-07 |
| 1 | Bounce-validity rule | net-crossing filter applied; bench 20/24 floor; new fixture confirms 458/463/584 movement | DONE 2026-05-07 — 880dff02 fixture **23/24 (10/10 FAR)** |
| 2 | Point boundary detection | `detect_point_boundaries()` function exists; per-point match ≥80% on `a798eff0` | PARTIAL — function landed (POINT 2026-05-07); IOU 17.6% pre-Phase-3, **pending re-measurement on post-Phase-3 active silver** |
| 3 | Pre-/between-point filter | Active T5 silver ±5% of SA event count; stroke distribution within ±10% per class | PARTIAL — warm-up half shipped; **between-point empirically blocked by Phase 5 (2026-05-20)** |
| 4 | Point-completeness reconciler | Diag tool shipped with baseline alongside `bench_baseline.json` | DONE 2026-05-07 — tool live, baseline 0/17 (root cause classified as Phase 5 territory) |
| 5 | Ball detection coverage | T5 ball-detection frame coverage ≥50%; longest gap <5s | **MOSTLY DONE 2026-05-24** — 52% coverage on `78c32f53` post-WASB + chain-rejection (was 13%). See Strategy update above. |
| 6 | Stroke detection (was "classification reconciliation") | Pose-only detector ≥75% recall vs SA truth at ±3 frame tolerance, ≥50% precision | **UNBLOCKED 2026-05-24** — pose-only path validated (`ball_hit_pose.py` probe at 63-67%; refinement target 75-80% without training). Production module at `ml_pipeline/stroke_detector/` is the next concrete piece. |
| 7 | Coordinate reconciliation | Per-event `bounce_court_x/y` populated; geometric error vs SA <2m | **MEASURABLE 2026-05-24, UNMEASURED** — coverage prerequisite met. THE next critical measurement. Tomo flagged this as the most important metric. |
| 8 | Final serve-detection cleanup | Revisit 4 a798eff0 misses with all upstream fixes in place | BLOCKED by 5 → now unblocked but lower priority than 7 |

---

## Per-phase detail

### Phase 1 — Bounce-validity rule — DONE 2026-05-07
**What landed:** `ml_pipeline/serve_detector/bounce_validity.py` exposing `validate_bounces()` (HALF_Y=11.885), wired into `RallyStateMachine.build_from_db`, `extract_far_pose`'s in-memory rally-gate block, and `detect_serves_offline` so bench mirrors prod. Image rebuilt + pushed to both ECRs (eu-north-1 rev 44, us-east-1 rev 26, amd64 sub-manifest digest `sha256:3f2a3fa1...c6b8`).
**Validation:** Fixture `880dff02` ran end-to-end on the new image: bench reports **23/24 (13/14 NEAR, 10/10 FAR)** vs the locked a798eff0 baseline of 20/24. All three target FAR misses (458.08, 463.52, 584.92) flipped to MATCH on the strict reconciler. New baseline locked in `ml_pipeline/diag/bench_baseline.json`.
**Residual:** 1/24 still missing — 148.52 NEAR. Bucket C class (bronze pose-amplitude gap, `arm_ext` distribution caps at 0.1px), independent of phantom-bounce class. Backlog. Not worth chasing without a pose model swap.
**Key learning:** `extract_far_pose` lives in the Batch container. The first push of Phase 1 was Render-only — Batch jobs ran the OLD image silently. Pre-merge checklist + on-demand-priority queue swap added to `handover_t5.md` + CLAUDE.md as a result.

### Phase 2 — Point boundary detection — PARTIAL 2026-05-07
**What:** Function `detect_point_boundaries(serves, ball_events, fps) -> [(point_start_frame, point_end_frame)]`.
**Where:** `ml_pipeline/point_structure/point_boundaries.py` (function), `ml_pipeline/diag/audit_points.py` (audit tool).
**Status:** Function landed. Audit reported 17.6% IOU≥0.5 / 64.7% IOU≥0.3 on the noisy pre-Phase-3 silver. **Re-measurement on post-Phase-3 active silver is the next step** — should rise materially with 35 noise rows removed and active T5 only 49 rows.
**Done-when:** Per-point match rate ≥80% IOU≥0.5 on `880dff02` post-Phase-3.
**Blocker:** None for re-measurement. Integration into silver is Phase 3 part 2.

### Phase 3 — Pre-/between-point filter — PARTIAL 2026-05-07
**What:** Filter pass in `build_silver_v2.py` that drops T5 silver rows outside detected point boundaries via `exclude_d=TRUE`.
**Where:** `build_silver_v2.py` pass 3 + (eventually) consumes Phase 2's `detect_point_boundaries()`.

**Part 1 — warm-up filter — DONE 2026-05-07.** New `first_serve_task` CTE + OR clause in the `final` CTE flips `exclude_d=TRUE` on rows where `ball_hit_s < per-task MIN(ball_hit_s) FILTER (serve_d)`. Predicted 35-row impact on `880dff02` confirmed via direct query (76 pre-existing exclusions + 35 new = 111 TRUE). Backhand count on active silver dropped from 62 → 10 (now slightly *under* SA's 15). Bench unchanged.

**Part 2 — between-point filter — DONE 2026-05-24 night.** Bounce-driven rally-window filter shipped + verified live on Match 1 (`78c32f53`). v3 implementation (commits `b68e33e` → `0201531`) added 5 new CTEs (`vaj / ball_bounces / point_starts / point_window_bounds / rally_windows / in_rally_flag`) in pass 3 of `build_silver_v2.py`. Rally windows are TIME-based (derived from `ml_analysis.ball_detections` bounces), so v2's forward-fill bug can't recur. Rally end = `GREATEST(last_bounce + 1s, rally_start + 20s)`, capped at `next_rally_start - 3s`; 2s pre-buffer on the start. Three safety gates (`has_bounce_data`, `has_serves`, per-window 10/20s fallback) make the filter a no-op for SportAI tasks and edge-case T5 tasks.

**Live results on Match 1 (`78c32f53` post-filter):**
- T5 silver: 139 total → **60 active, 79 excluded** (SA: 94 total → 84 active, 10 excluded)
- Active stroke distribution: T5 Backhand=14 vs SA=15 (exact match within 1), T5 Volley=0 vs SA=4 (over-detection eliminated), T5 Serve=28 vs SA=26 (within 8%)
- Bench unchanged (a798eff0=20/24, 880dff02=23/24)

**Known ceiling — Forehand undercount is upstream of this filter.** T5 active Forehand=17 vs SA's 38 (gap of 21). Per-row breakdown showed the binding constraints are:
- The existing `excl_chain.gap_break` 5s-gap rule (pre-existing in pass 3) excludes 24 rows match-wide — 10 Forehands among them. That rule was tuned for SportAI's denser bronze and gets aggressive when Phase 5 ball coverage is at 50% (long-tail strokes after a 5+ second silver-row gap get killed).
- T5's silver builder is bounce-driven (one bounce = one silver row). When TrackNet misses bounces for forehands, those strokes never become silver rows in the first place — no filter can conjure them.

Three tuning iterations on the between-point filter (`+10s fallback → +20s + 2s pre-buffer → 20s minimum window`) moved the active count by 1 row. The filter is functionally correct; the remaining gap is structural and addressed in two future tracks: (a) retune `excl_chain.gap_break` 5s → 8s for sparse-bounce regimes (small Render-side change, but touches load-bearing pre-existing logic — needs care); (b) pivot the T5 silver builder to consume `ml_analysis.stroke_events` directly (now populated — see Phase 6 below).

**Original 2026-05-20 attempts (preserved below for reference)** — two pure-SQL attempts shipped + reverted; both flawed for the same upstream reason that's now resolved.

  - **v1 (commit 00b8639, reverted)** — Pattern A from .claude/_archive/session_2026-05-20_review.md: anchor on every `serve_d=TRUE` row in `with_try_ff`, window = `LEAST(hit+30s, next_serve-2s)`. Result on 880dff02: **no-op**. T5's geometric serve detector emits 107 detections on an 18-point match (any overhead-type swing within EPS of a baseline qualifies). 107 dense anchors create windows that cover the entire match → nothing falls outside any window → 0 rows excluded. Active T5 rows held at 49.
  - **v2 (commit f0b104e, reverted)** — anchor on first `serve_d=TRUE` per `point_number` (~18-30 anchors), 20s cap. Result on 880dff02: **wrong rows dropped**. Active T5 49 → 34 (-15 by count) but the reconciler's "T5 strokes outside ANY SA point window" held at 20 — all 15 dropped rows were INSIDE real SA windows. Per-point: pt 5 (SA [178.44–195.96]) 8 T5 → 1; pt 14 (SA [458.08–468.00]) 9 T5 → 1. Forward-fill of `point_number` assigns rows in the [SA_point_start, T5_serve_detection] gap to the PREVIOUS point_number; those rows then fall outside that previous point's 20s window and get excluded — even though they're real strokes of the current point.
  - **Pattern B (Python `detect_point_boundaries()` integration) — inherits the same start-of-window limit.** `detect_point_boundaries()` improves the END of windows via `idle_gap_s=4.0s` (bounce-driven, tighter than v2's 20s cap), but `start_frame = serve.frame_idx` is identical to v2. The structural problem is "T5's serve detection lands later than SA's true point start" — Pattern B doesn't address this.
  - **Root cause confirmed empirically: this work requires reliable bounce evidence to distinguish 'real stroke before serve detection' from 'between-point noise'. That's Phase 5.** Don't re-attempt Phase 3 part 2 until Phase 5 ball-detection coverage is materially better.

  Revert lives at `de06d41` on main. Phase 3 part 1 (warm-up filter at line 713-718 of `build_silver_v2.py`) is unaffected and still shipping. Restart the design when Phase 5 has produced ≥30% ball-coverage on 880dff02.

**How to verify (when re-attempted):** Active T5 silver row count within ±5% of SA's. **AND** the reconciler's "T5 strokes outside ANY SA point window" count drops. Don't trust row-count alone — v2 hit the row-count target but dropped real strokes; the reconciler's window-overlap metric is the load-bearing signal.

### Phase 4 — Point-completeness reconciler tool — DONE 2026-05-07
**What landed:** `ml_pipeline/diag/audit_points_reconcile.py` + `ml_pipeline/diag/points_reconcile_baseline.json`. CLI: `python -m ml_pipeline.diag.audit_points_reconcile --task <T5_TID> [--honor-exclude]`. Reports per-SA-point match/partial/missing per stroke; produces a single number "X/Y points fully reconcile."
**Baseline:** **0/17 points fully reconcile** on `880dff02`. Today's investigation classified this as ball-coverage-limited (Phase 5 territory), not a tool problem.
**Future use:** Re-run after each Phase 5 milestone to track how per-point reconciliation moves. Re-run after Phase 3 part 2 lands to measure noise→accuracy tradeoff.
**Done-when:** Tool committed (✓), baseline file committed (✓), `--honor-exclude` flag for active-view (✓).

### Phase 5 — Ball detection coverage — TOP BOTTLENECK
**What:** Get T5's bronze `ml_analysis.ball_detections` to ≥50% frame coverage, with longest gap <5s on the validation match. Currently ~13% coverage with six >40s gaps.

**Why this is the bottleneck (evidence from `docs/_archive/may07_sa_point6_gap.md`):**
- SA point 6 (9 strokes, ~16s rally, frames 5599-6003) has **0 T5 ball detections** in window
- Match-wide T5 has 1,983 ball detections across 15,300 frames = 13% coverage
- Six gaps >40s; top three are 91.6s, 73.2s, 61.8s
- Player tracking is fine through these windows (490/400 court-coord rows in SA point 6)
- 10 of 17 SA points have zero T5 strokes in their windows because of this — Phase 6 + 7 cannot proceed

**Sub-tasks (parallelizable, all in `ml_pipeline/`):**

- **5a — Finish ROI bounce extractor — DONE 2026-05-21.** `ml_pipeline/roi_extractors/bounces.py` rewritten from stub to production extractor (~320 lines). Anchor strategy: bounce-only no-zone-filter (chosen after fixture diagnostic showed the kickoff doc's default would cover only 1/24 SA serves vs 6/24 for bounce-only). Anchor windows are ±2.5s around clustered bronze bounces, TrackNet rerun on tight service-box crop, results merged INTO canonical `ml_analysis.ball_detections` (NOT a parallel `_roi` table — architectural pivot to Option A on 2026-05-21 PM). Validated on task `763c9ee9`: 459 ROI rows / 23 bounces added; silver row count 160 → 183 (+23); first NEAR T5 serve in silver ever (id=92, ts=178.76s, hit_y=24.05). Bench unchanged at 23/24 + 20/24. Production image: eu-north-1 job-def rev 46, us-east-1 rev 28, both `sha256:87435dbfd…`. Phase 5 done-when targets only PARTIALLY met (frame coverage gain is modest — bigger gains need WASB integration / Phase 5d).
- **5b — Frame-delta Hough fallback gain-up. PARKED 2026-05-20 with empirical receipts.** Round 0 baseline diagnostics (CloudWatch on 880dff02 + local Tier-4 sweep on a798eff0) showed: (i) Tier 4 already returns a position on ~99.93% of TrackNet-empty frames — there's no headroom to "fire more often"; (ii) the staged motion-threshold change 25→15 regresses post-`_filter_outliers` survival by 11.6% (local exp on a798eff0), because lowering the gate makes Hough's strongest-circle pick noisier rather than catching more real balls; (iii) `tier2_cc_rejected = 0` on 880dff02 — the Tier 2 area-gate change is a no-op too; (iv) the dominant filter is `_filter_outliers` (150px from previous-kept) which eats ~79% of Tier-4 returns. Source-aware filter surrogate (Option α) showed -3.0pp rally-precision and the deeper finding that `ball_rows` aren't strongly concentrated in rally windows even pre-filter (Tier-1 fires across the whole match, not just in rallies) — so "gate Tier-4 by recent Tier-1 anchor" doesn't get the concentration boost the design assumed. Full BallTracker local validation aborted (40-min estimate was off by ~30×; actual ~21 hrs on CPU without GPU). Receipts: `.claude/_archive/phase5b_ball_tracker_characterisation.md` (Tuning rounds + reprioritised candidates) + commit `d26e8cc`. Branch `phase-5b/motion-threshold-reduce` retained on origin as a falsified-hypothesis record; do not merge.
- **5c — Dual-submit training data pipeline. Phase 5c.0 / 5c.1 / 5c.2 / 5c.3 ALL LIVE + VERIFIED IN PROD 2026-05-22.** End-to-end verification on SA task `0d0514df-...` → auto-spawned T5 sibling `78c32f53-...` → `ml_analysis.training_corpus` row with 161 ball-position labels (48 NEAR / 47 FAR / 66 other). `AUTO_DUAL_SUBMIT_T5=1` + `AUTO_LABEL_DUAL_SUBMIT_PAIRS=1` are flipped. `harness build-corpus` subcommand shipped (commit `2ac4a64`) with companion `verify-corpus-row` (`36f18d5`), `--upload-s3` (`4272c5e`), `--task` filter (`b48230c`). **Architectural fix shipped same session:** `/ops/sweep-t5-orphans` endpoint (commit `a1a7e96`) — auto-spawned T5 tasks have no polling browser to open the `/upload/api/task-status` ingest gate, so they sit in `last_status='queued'` indefinitely despite Batch having succeeded. The sweep catches them. Tonight's pair needed a manual GET to unblock; future runs are covered by the sweep (Render cron wiring is the only remaining piece — see Open admin items in the pickup). Full breakdown: `.claude/_archive/dual_submit_status_2026-05-20.md`.
- **5d — TrackNetV3 retrain.** Architecture ported (`ml_pipeline/tracknet_v3.py`); weights not trained. Blocks on 5c. Once weights exist, swap them in via the existing config path — no architectural changes needed. **Lower urgency post-5e:** if WASB delivers production-equivalent F1 gains, the 5d retrain story collapses to "WASB beats us out the gate; finetune V3 only if WASB plateaus."
- **5e — WASB-SBDT integration. SHIPPED 2026-05-21 + VERIFIED 2026-05-22.** WASB (HRNet backbone, BMVC 2023) wired into `ml_pipeline/pipeline.py` as a drop-in alternative to `BallTracker`, env-gated via `BALL_TRACKER` (default `tracknet_v2`; both prod job-defs set to `wasb`). Validated by ball-tracker bench (`ml_pipeline/diag/bench_ball_baseline.json`, commit `7100792`): WASB recovers 2/9 vs TrackNetV2's 0/9 SA point 6 strokes on the canonical bronze-coverage-gap regime. Production verification on Batch task `1d6feb3a-4624-47ae-b8f5-44246b6d0eb3` (Tomo vs Jimbo Ma, `wix-uploads/1779386702_match.mp4`, 2026-05-22): CloudWatch confirms `WASBBallTracker` ran on all 15,298 sampled frames at 54.3% raw detection rate (8,303 detected), 17 valid bounces, pipeline complete in 2,258s. Bronze SQL matches log exactly. Image `sha256:8fe82a3…`, eu-north-1 rev 47, us-east-1 rev 29. **Rollback path:** unset `BALL_TRACKER` env on the job-def, no code change needed. **Three follow-ups (none blocking):** (1) `_filter_outliers` chain-rejection — pre-existing BallTracker bug; in `1d6feb3a` it locked the reference position early and rejected detections past frame 3329 (out of 15298 processed); fix shape: re-anchor when N consistent neighbours appear; (2) `db_writer.py` doesn't set `source='main'` on new writes, so the Phase 5a `source` diagnostic distinction is lost — one-line fix; (3) capture `1d6feb3a` as the first silver-bench fixture to lock in a reproducible artefact for follow-up (1).

**How to verify:**
- Match-level: ball-detection frame coverage ≥50% (up from 13%)
- Worst-gap: longest contiguous no-ball frames <5s (down from 91.6s)
- SA point 6 specifically: ≥3 T5 ball detections in window
- Phase 4 reconciler: per-point match rate ≥30% (up from 0%)

**Blocker:** 5a DONE 2026-05-21. 5b parked (2026-05-20). 5e SHIPPED 2026-05-21 + VERIFIED IN PROD 2026-05-22. **5c.0 / 5c.1 / 5c.2 / 5c.3 ALL LIVE + VERIFIED 2026-05-22 evening.** 5d blocks on 5c. **5e follow-ups (1) chain-rejection + (2) `source='main'` SHIPPED 2026-05-22 late evening** — re-anchor fix in commit `7863a66`, deployed to Batch eu-north-1 `:48` / us-east-1 `:30` (amd64 `bc8f7d72…`); ball-bench post_filter_sa_recall verdict: 100% on 3/4 (fixture, tracker) combos, 67% on a798eff0/tracknet (was 33% pre-fix). Next moves: (a) wire `/ops/sweep-t5-orphans` into a 5-min Render cron so future auto-spawned T5 tasks no longer need manual unblocking, (b) follow-up (3) re-capture `1d6feb3a` silver-bench fixture against new Batch image to see post-fix bronze density, (c) accumulate more training_corpus rows from organic uploads before attempting a fine-tune run.

### Phase 6 — Stroke detection — MODULE DONE 2026-05-24 night, silver consumption is next

**Module shipped (commits `2cedc4c` → `aaba134`):** `ml_pipeline/stroke_detector/` — 5-file production module mirroring `serve_detector/` shape (`__init__.py`, `models.py`, `schema.py`, `velocity_signal.py`, `detector.py`). Wired into the T5 ingest path in `upload_app.py::_do_ingest_t5` right after `detect_serves_for_task`. Schema auto-created on first call; delete+reinsert per task on re-detection (same lifecycle as `serve_events`).

Three refinements from the `ball_hit_pose.py` probe applied:
1. **Peak-to-contact offset +4 frames.** Velocity peak fires on the backswing-to-contact transition; SA's truth contact frame is 4-6 frames later. `predicted_hit_frame = peak_frame + 4`.
2. **`min_gap_frames` 15 → 25.** Probe over-fired on backswing + forward + follow-through inside 15 frames at 25fps.
3. **Deceleration filter.** Reject peaks where smoothed `v[i+3] > peak * 0.5` (single-frame check per probe spec, NOT a mean — first implementation used mean and zaped 100% of peaks on real video; fixed in `aaba134`).

**Live results on Match 1 (`78c32f53`):** 249 stroke events persisted, avg confidence 0.95, span ts=3-608s. Probe baseline (no refinements) scored 63-67% recall on 161 SA hits. Production module emits ~150 more events than SA's truth (false-positive surplus) — expected per the pickup; the precision gap is what training-based refinement closes (Phase 5d after corpus accumulation).

**Silver consumption — NOT YET WIRED.** The current T5 silver builder (`build_silver_match_t5.py::_t5_pass1_load`) is bounce-driven: one bounce = one silver row. `stroke_events` is populated but not consumed. This is the next Phase 6 step and a direct lever on the Forehand-undercount ceiling that Phase 3 part 2 exposed:
- A stroke that produces no detectable bounce (TrackNet miss / ball wide / out) currently has no silver row.
- Wiring `stroke_events` into Pass 1 would generate one silver row per detected stroke contact, with bounce coords joined when available — recovering forehands lost to bounce-detection gaps.
- Estimate: 1-2 days. Render-side, no Batch redeploy. Risk: changes the row-generation contract for T5 silver, so gold views may need a sanity pass.

**Where training fits (later):** 5-10 corpus rows of paired SA+T5 labels enables training a small pose-feature classifier to close the heuristic ceiling to ~90%+. Phase 5c is now passively accumulating; no urgency.

**See:** Strategy update 2026-05-24 above for the full three-probe story.

### Phase 7 — Bounce accuracy — MEASURED + REFRAMED 2026-05-25

**Measured against SA on Match 1 (full diagnosis: `docs/_investigation/bounce_accuracy.md`).** The
critical measurement Tomo flagged is done, and it **reframes the phase**: ball-bounce accuracy is a
**detection-precision + timing** problem, **not a coordinate-calibration problem.**

**Findings (live-DB reconciliation, both 25fps, time-matched):**
  - Calibration is a **faithful planar homography** — a fit on 14,198 player-feet correspondences
    reproduces stored bounce coords to **0.11m**. Recalibration is not the lever.
  - SA's 161 bounces = 67 floor (ground) + 94 swing (racquet). T5 fires **303** `is_bounce` in the
    match window: 126 with coords, **177 nulled** by the strict ±5m clamp (`court_detector.py:887`).
  - The 177 are **~84% airborne false-positives** (detected above the far-baseline image row), not
    lost ground bounces. Ground-bounce trajectory signature 15% (vs 43% on kept bounces).
  - Event recall is fine (85% of SA floor within ±0.8s); **timing jitter ~0.5s** (ball coverage).
  - Far baseline is **resolution-limited** (~1px ≈ metres) — accuracy cap independent of calibration.

**Reframed work (Phase 7'):**
  1. **Bounce-detection precision** — reject airborne `is_bounce` (require near-court-plane contact /
     descending→ascending image_y inflection / ball–floor proximity). Cuts the ~177 FPs.
  2. **Timing** — pin bounce frames via better ball coverage (overlaps Phase 5 / WASB).
  3. **Persist the per-job homography** (cheap) for re-projection + audit without a rerun.
**Where:** `roi_extractors/bounces.py` (Batch — trips BATCH-SIDE CHECKLIST) and/or a silver-side
`is_bounce` guard (Render — faster to validate; mind bronze-first #11). De-risk Render-side on
Match 1 first, then port to Batch — the pattern that produced the diagnosis.
**Do NOT:** trigger a Batch court-recalibration to chase the old "3-7m off" number — the calibration
is faithful; the error is precision, not projection.

### Phase 8 — Final serve-detection cleanup — UNBLOCKED but LOWER PRIORITY than 7
**What:** With ball coverage, point boundaries, and clean silver in place, revisit the 4 a798eff0 misses + 1 880dff02 miss (148.52 NEAR). Whichever still don't recover gets a one-line memo in the Backlog + parked.
**Status:** Coverage prerequisite met 2026-05-24. Not the next critical move — Phase 7 (bounce x,y accuracy) is the load-bearing measurement. Pick this up after Phase 7 + Phase 6 production module.

---

## Backlog (issues we know about but aren't in the phase ladder)

- **2.4-7m y-axis offset.** Calibration extrapolation behind the far baseline produces court_y -3 to -7m for players who are visually at the baseline. Apr 29 verified naive widening (-3.5→-5.0) loses 2 PASS. Likely needs a pixel-y-based far-baseline check (replacing `_baseline_zone(court_y)`) — touches multiple call sites; deferred.
- **148.52 NEAR pose-amplitude gap.** Real serve, real keypoints (0.95 conf), but dominant wrist physically never clears avg shoulder line by more than 0.1px. Needs pose-model swap or training data — deferred to Phase 8.
- **Stroke classifier (optical flow CNN) training.** `ml_pipeline/stroke_classifier/` exists with model + flow extractor, but no trained weights. Unblocks once Phase 5c (dual-submit training data) lands.
- **Custom T5 skill** (`.claude/skills/t5/`). Marginally helpful for new sessions; ~1 hour of work; not blocking. Add when the project enters a calmer phase.
- **Silver should consume `ml_analysis.serve_events`** (branch `silver/connect-serve-events` / 2026-05-07, **NOT shipped** — backlog entry only). Naive OR overshoots the impact band because `serve_events` holds all 107 detector candidates, not just the 23 reconciler-validated ones. Two viable paths: (a) persist strict-reconciler MATCH verdict to a column on `serve_events`; (b) gate EXISTS on `rally_state` ∈ ('pre_point','in_rally') AND `confidence ≥ 0.7`. Belongs in Phase 6 (with Phase 5 bench harness as the safety net).
- **TrackNetV3 retraining moved to Phase 5d** (was here).
- **`extract_roi_bounces.py` integration moved to Phase 5a** (was here).

---

## Progress measurement

These are the metrics this file is tracking:

| Metric | Phase | Today's value | Target |
|---|---|---|---|
| Bench MATCH (strict reconcile) on `880dff02` | 1 | **23/24** | 24/24 (Phase 8) |
| Bench MATCH on `a798eff0` | 1 | 20/24 | unchanged baseline |
| Active T5 silver row count vs SA on `880dff02` | 3 | 49 vs 85 | within ±5% (≈ 81-89) |
| T5 active stroke distribution: Backhand | 3 | **10 vs SA 15** | within ±10% (13-16) |
| T5 active stroke distribution: Forehand | 3 | 21 vs SA 40 | within ±10% (36-44) |
| T5 ball-detection frame coverage | 5 | **13%** | ≥50% |
| Longest no-ball gap | 5 | **91.6s** | <5s |
| Per-point reconciler full_match | 4 → 5/6 | **0/17** | ≥8/17 after Phase 5; ≥14/17 after Phase 6 |
| Coordinate error vs SA | 7 | unmeasured | <2m |

The single-number metrics that matter most for "is the dashboard trustworthy" are bottom three. All blocked by Phase 5.

---

## Autonomy infrastructure (separate track)

| Tier | What | Status |
|---|---|---|
| 1 | Local diag where possible; user only intervenes on Batch reruns | Already there |
| 2 | Read-only `/api/diag/sql` Flask endpoint | **DONE 2026-05-07** (`infra/tier-2-sql-endpoint`) |
| 3 | GitHub Actions runs `bench` on push + PR | **DONE 2026-05-07** (`infra/tier-3-bench-ci`) |
| 4 | All diag tools DB-aware via the SQL endpoint | Ongoing — comes naturally as Phase 5/6/7 tools land |
| 5 | Render→Batch automation: trigger reruns from agent context, watch CloudWatch | Deferred — schedule during a Phase 5 lull, scope tighter than original brief (just SubmitJob + DescribeJobs, no streaming) |

---

## Operating rules

1. **No detector edit without `bench` green first.** Hard rule from CLAUDE.md.
2. **No T5 detector branch merges without the Batch-side change check.** `git diff --stat` against `ml_pipeline/roi_extractors/`, `__main__.py`, `pipeline.py`, `Dockerfile`, `requirements.txt`, `serve_detector/`. Non-empty diff → Docker rebuild + dual-region ECR push + new job-def revisions before user reruns. See `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST".
3. **Phase work updates this file.** Anyone closing a phase: bump status, write a 3-line "what changed" entry under the phase. Anyone starting: claim it (write your name + date in Status column).
4. **New ideas → Backlog, not into phases.** New directions get triaged by Tomo before they become phases. Keeps scope contained.
5. **One agent per phase, isolated worktrees.** No file conflicts.
6. **Validation that requires Batch reruns is a Tomo-trigger step.** Agent commits + pushes; Tomo reruns Batch when convenient. Not real-time.
7. **Don't ship code that depends on SA truth at runtime.** The strict reconciler is a diag tool; production has no SA counterpart. Filters and rules need to work without it.

---

## How to update this file

- **Closing a phase:** flip Status to DONE with date; write 3 lines under the phase explaining what shipped + key learnings. Update Progress measurement metrics table.
- **Starting a phase:** flip Status from UNCLAIMED to `<your session ID> <YYYY-MM-DD>`; commit before work starts.
- **Major restructuring (new bottleneck, new phases):** copy current file to `docs/_archive/north_star_YYYY-MM-DD_<context>.md` first, then rewrite. Don't lose history.
- **Bench baseline shifts:** mention here (not just in `bench_baseline.json` commit message). The single-number metric for the dashboard's data quality is what this file is tracking, not just the detector's.
