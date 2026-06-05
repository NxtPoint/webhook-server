# Next-session pickup — 2026-06-04 (PM) — swing classifier DEPLOYED to prod (rev 63/44); AWAITING validation rerun

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".

## 🧭 ARCHITECTURE REALIGNMENT (Tomo, 2026-06-05) — READ THIS, it reframes everything
**Clean silver. Inherit bronze 100%, no exceptions. Bronze is the answer; silver is NEVER the answer.** The unlocking insight: **a stroke IS a ball-hit (one event)** → silver must be **STROKE/HIT-DRIVEN** (one row per bronze `stroke_events` hit, projected verbatim), NOT bounce-driven. Today's bounce-driven Pass 1 (`_t5_pass1_load_bounce_driven`) heuristically reconstructs the hit (mirror-fallback, geometric serve, `_infer_swing_type`, gap_break, exclude_d) — that whole pile is DEBT to DELETE once bronze is right. **Overcounts die automatically when hit-driven:** no valid stroke+hit ⇒ no row, so phantom/racquet/double bounces vanish and T5's ~162/343 collapses toward the real **~84 hits** as a consequence of correctness. Full audit + heuristic-debt checklist + locked roadmap: `docs/_investigation/bronze_silver_18_audit.md` §"UPDATE 2026-06-05".
**Locked order (bronze-first):** 1) **bounce** → promote CNN v2 to the bronze bounce model; 2) **serve** → pass-3 inherits `serve_events` + serve-model precision, delete geometric gate; 3) **stroke=ball-hit** → `stroke_events` carries swing_type + ball_hit_location + correct attribution; 4) **flip silver STROKE-DRIVEN** (`T5_STROKE_DRIVEN_SILVER`), delete Pass-1 debt. Build bronze fact-by-fact; never extend a silver heuristic.
**Bounce CNN v2 validated this session:** gravity_residual + `bounce_detector_v2_7match.pt` @thr 0.5 → precision 20%→37%, count 343→**172 (≈SA 162)**, recall held. Weights 144KB (committable). Next: promote to bronze bounce model (not into the doomed bounce-driven silver).

**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (re-verified this session).
**What shipped this session (the swing-classifier deploy — fully done end-to-end):**
- **Bronze inference wired (Batch):** new `ml_pipeline/stroke_classifier/inference_v2.py` runs in `pipeline.py` (`_classify_far_player_strokes` → delegates), classifies BOTH players' swing type per bounce with the trained v2 model (`SwingTypeR2plus1D`, near 0.86 / far 0.61), writes canonical `fh/bh/overhead` to `player_detections.stroke_class`. Frame-space handled (sampled `frame_idx` → source-fps window via `frame_interval`); handedness=1.0 (matches training default); memory micro-batched. Commits `877eaa6` (+ `7962088` detector_v2 lazy-import fix).
- **Silver prefers the model (Render, auto-deployed):** both Pass-1 cascades now PREFER `stroke_class` over the pose/position heuristics (heuristics demoted to STOPGAP fallback). Added `near_sc/far_sc` buckets + a windowed `stroke_class` patch. Commit on `main`.
- **Rule #8 rebuild DONE:** image rebuilt, pushed to both ECRs (amd64 digest `sha256:0eb6ad9c637d4ea2d5fecd1560d5b48a3ff0741b62ebefc101f144e58927978b`), job-defs registered **eu rev 63 / us rev 44** (retryStrategy preserved, g4-primary queue unchanged). Container import smoke test passed; weights bundle + load confirmed in-container.
- **Rollback without rebuild:** set Batch env `SWING_CLASSIFIER_ENABLED=0` (kill-switch) or tune `SWING_CLASSIFIER_MIN_CONF` (default 0.5).

**🔴 CRITICAL FIX THIS SESSION (the deploy was inert without it): export→reingest dropped stroke_class.** First validation run (`db3937fb`) completed but post-ingest `stroke_class=0` — the swing classifier RAN in Batch (436 classified) but `bronze_export._player_detection_to_dict` didn't serialize `stroke_class`, and the Render reingest DELETEs+re-COPYs from that JSON → silver fell back to the heuristic. Same leak wiped `roi_prod` bounces (the blanket ball_detections DELETE) → roi_bounces' ~16 min was 100% wasted on every auto-ingested task. **FIXED (`f4449b0`):** export serializes stroke_class; ingest COPYs it; ingest DELETE now preserves `source='roi_prod'`. **Rebuilt: eu rev 64 / us rev 45** (digest `sha256:108153d7…`). Render ingest auto-deploys.

**✅ VALIDATED OVERNIGHT (b008888c, rev 64) — leak fixed, but classifier FAILED the gate → DISABLED.**
- **Plumbing fix CONFIRMED working:** post-ingest `stroke_class`=211 (was 0 on rev 63), `roi_prod`=3502 survived (was wiped). The export→reingest leak is closed.
- **Swing gate: classifier LOSES to the heuristic → DISABLED via `SWING_CLASSIFIER_ENABLED=0` (eu rev 65 / us rev 46, image unchanged).** Definitive per-hit swing agreement vs SA: **heuristic 38% (16/42) vs classifier 32% (15/47)**. Root cause: the v2 model has only 3 classes (fh/bh/overhead), **no "other"/volley**, so every hitter-candidate near a bounce is forced to a groundstroke → forehand over-prediction (clean-silver Forehand 62 vs SA 39 vs heuristic 40; top classifier errors `Serve→Forehand` ×8, `Backhand→Forehand` ×7). The heuristic actually nails forehand (40≈39); its weakness is bh/overhead over-count, which the classifier only marginally improved while breaking forehand. **Aggregate fh+bh+oh error: heuristic ≈22 vs classifier ≈42.**
- **To RE-ENABLE the classifier (next iteration):** (1) add a 4th "other/none" class (or a high `SWING_CLASSIFIER_MIN_CONF` gate, e.g. 0.7-0.8) so non-groundstrokes fall back to the heuristic instead of defaulting to forehand; (2) address the far forehand lean (far eval was 0.61); (3) re-validate per-hit agreement vs SA. The DEPLOY INFRA is fully validated + one env flip from live — flip `SWING_CLASSIFIER_ENABLED=1` on the job-def when the model is fixed.
- **`roi_prod` REVERTED (`9d0a30b`):** preserving it flooded silver (bounce-driven: 254 active rows vs 121 main-only vs SA 84) → worse reconciliation. roi_bounces can only feed silver behind a **bounce dedup/merge** step (merge roi_prod into the main bounce set, don't add net-new shot rows). Until then it stays wiped on ingest (its pre-tonight state — no regression). **This is now the highest-value bounce task:** roi_bounces does ~16 min of work that's discarded; a dedup step would turn that into real far/service-box recall (the weakest field).

**(stale gate instructions, kept for the re-enable path):** A fresh Singles-T5 run on a classifier-enabled rev, then:
```
python -m ml_pipeline.harness reconcile <sa_task> <new_t5_task>   # exclude_d-correct
```
**GATE:** swing_type must reconcile BETTER than the heuristic — backhand toward 15 (not over-counting), overhead toward 0. **Heuristic baseline to beat:** T5 `a35b37f6` vs SA `ba4812be` (north_star corrected scorecard: bh 14 vs 15, overhead 9 vs 0 ← the over-label the classifier should fix; fh recovered 40 vs 39). **Deploy STAYS only if it wins; else flip `SWING_CLASSIFIER_ENABLED=0`.**
Recent dual-submit pairs (SA ← T5): `ba4812be ← a35b37f6` (reference), `2c1ad953 ← 17e2da3a`, `0336b82b ← 63a0130d`.

If that's enough, go. Depth below.

## 🔍 Validation specifics (do this when the rerun lands)
1. `harness reconcile <sa> <new_t5>` — read backhand/overhead/forehand active counts (exclude_d-correct).
2. Confirm the model actually fired: `SELECT count(*) FROM ml_analysis.player_detections WHERE job_id=:t5 AND stroke_class IS NOT NULL` (expect tens–hundreds). If 0 → check Batch CloudWatch logs for `swing_classifier_v2:` lines (look for "weights not present", bounce count, classified count).
3. Compare swing distribution of new T5 vs the heuristic baseline (`a35b37f6`) both vs SA `ba4812be`. Classifier wins if bh over-count shrinks (toward 15) and overhead over-label drops (toward 0) WITHOUT regressing forehand.
4. If it wins: keep; update north_star scorecard (swing_type now model-owned, heuristic = fallback). If it loses: `SWING_CLASSIFIER_ENABLED=0` on both job-defs' env (no rebuild) and record why.

## 📊 FRESH 18-field scorecard — 2026-06-04 overnight (b008888c CLEAN main-only vs SA ba4812be)
Re-measured with all today's fixes live (far-side bug fixes + fps fixes + leak fix). **Supersedes the stale north_star table.**
| field | SA | T5 clean | read |
|---|---:|---:|---|
| active rows | 84 | **121** | ⚠️ **over +44% — bounce OVER-GENERATION is the #1 reconciliation gap** (phantom/pre-point/duplicate bounces → extra shot rows). roi_prod made it 254 → reverted. |
| points | 18 | 18 | ✅ aligned |
| games | 2 | 4 | ⚠️ over-segmented (known) |
| volley | 4 | 3 | ✅ close |
| serves | 24 | ~16–22 | ⚠️ under — far serve recall (known ceiling → training) |
| swing fh/bh/oh | 39/15/0 | 62/22/12 (clf) · 40/23/13 (heur) | heuristic LIVE; fh≈SA, bh/oh over. classifier disabled (see above). |
| hit_x/y, court_x/y, pid | — | 100% pop | ✅ coverage solid |
| ball_hit_location far accuracy | — | 2b: far court_y NULL-driven stale/mirror | calibration task |

**Verdict — where we are vs "dev done":** structure (points/games/volley/coverage) is GOOD. **Three remaining gaps, ranked:**
1. **🔴 Bounce precision (NEW #1 priority, ROOT-CAUSED 2026-06-05).** Bounce-vs-bounce (not bounce-vs-stroke): **SA 162 vs T5 343 (2.1× over-detect); precision 20%, recall 43%** @0.3s. **ROOT pinned:** `ball_tracker.detect_bounces` (ball_tracker.py:641) flags every image-y velocity sign-flip as a bounce — but `vel<0→vel>0` is the **APEX of the ball's arc**, not a ground bounce. Counting apexes ≈ the 2× over-count = the documented "airborne false-positives." **Two cheap fixes EMPIRICALLY REJECTED (don't retry):** (a) `validate_bounces` cross-net filter in silver → recall 43%→27% for nothing (T5 court_y too noisy); (b) directional clause (down-then-up only) → recall 43%→35% for nothing (image-y velocity is confounded by near/far perspective drift, not pure vertical). **The real fix needs a better SIGNAL:** gravity-residual (`bounce_detector/feature_extractor._gravity_residual`, parabola-fit, perspective-robust) + the trained **bounce CNN v2** (`models/bounce_detector_v2_7match.pt`, F1 0.54, undeployed). **✅ Validated the signal has the headroom:** gravity-residual candidates hit **70% recall** (vs velocity-reversal's 43% ceiling) — the real bounces ARE recoverable; precision is the CNN's job. **Concrete next step (supervised):** run `detect_bounces_offline(weights_path=models/bounce_detector_v2_7match.pt)` on b008888c (assemble wrists/rally/court features via the bounce harness), measure post-CNN precision/recall vs SA's 162; if precision ≥~40% at recall ≥50%, deploy `BOUNCE_CANDIDATE_MODE=gravity_residual` + v2 (Batch rebuild). Batch-side (rule #8) + tradeoff call → **daytime session.** Full detail: `docs/_investigation/bounce_accuracy.md` §"UPDATE 2026-06-05". (Secondary: T5 silver is bounce-driven vs SA stroke-driven — the gated `T5_STROKE_DRIVEN_SILVER` addresses the event-type mismatch but only once bronze stroke attribution is right, rule #11.)
2. **🟠 Swing classifier v2.1** — add 4th "other/none" class (or high min_conf gate) to stop the forced-forehand; re-validate per-hit vs SA (must beat heuristic's 38%). Infra is live, one env flip to re-enable.
3. **🟠 Far serve recall + far ball_hit_location** — both far-court ceiling (training + calibration), as documented.

## 🔬 Far-side ball-hit / bounce investigation (2026-06-04 PM — 2 agents, while validation ran)
Tomo asked whether the far-player frame-space bug (that we fixed for swings) also hits **ball bounce** or **ball hit**. Findings (measured on prod, not just code-read):
- **Ball BOUNCE = NOT a bug.** The bounce corpus builder (`bounce_detector/dataset.py::_sa_label_to_t5_frame`) was already fixed for this exact frame-space bug on 2026-05-29 (it converts via `timestamp × FRAME_SAMPLE_FPS`) — it actually *templated* the swing fix. `build_serve_bounce_dataset.py` works purely in source-frame space, never crosses into 25fps. Far-bounce weakness is precision/coverage/resolution (`docs/_investigation/bounce_accuracy.md`), not frame-space. **No action.**
- **(2a) stroke_events.ts fps bug = FIXED this session (`7df8276`).** `detect_strokes_for_task` divided sampled `predicted_hit_frame` by source `video_fps`. Confirmed on 63a0130d (30fps): phf=906 stored 30.20s vs correct 36.24s. Render-side; latent (live silver recomputes ts) but a foot-gun. Now uses sampled fps (total_frames/duration).
- **(2c) serve_detector ts/fps = FIXED this session (`50c0dd3`).** Same bug class, but LIVE (`serve_events.ts` inherited by silver via `T5_SERVE_FROM_EVENTS`) and the fps also drove seconds→frame detection windows (8s interval → ~9.6s at 30fps). `serve_detector/detector.py` now derives sampled fps (total_frames/duration, fallback FRAME_SAMPLE_FPS). Render-side. **Bench-neutral & re-run GREEN (20/24, 23/24)** — verified both CI fixtures are 25fps (sampled==video_fps there), and the offline/bench path takes fps from the fixture so it's untouched. ⚠️ **When a non-25fps bench fixture is added, store the SAMPLED fps in it** so detect_serves_offline keeps matching prod.
- **(2b) FAR `ball_hit_location` = OBSERVABILITY SHIPPED (`931072e`); accuracy = calibration task (Tomo's call 2026-06-04).** Confirmed real/live/variable (minor on 25fps a35b37f6 ~3% null; **~28% NULL heavily-far on 30fps 63a0130d**). Silver CANNOT fix it — far `court_y` is NULL (map_to_court ±5m rejects the 2.4–7m far-baseline overshoot) so there's no accurate far location to read; relaxing the bound would store known-bad coords (deliberately rejected per north_star). Shipped MEASUREMENT only: `hit_resolve_diag` logs `T5 hit-resolve by side: {...} (far approx/total = N%)` per build — watch this to quantify the gap. **The accuracy fix is the far-court CALIBRATION work** (homography/lens overshoot), the documented far-court task — not a silver/builder change. Validate any future calibration fix with far-only `harness reconcile` median Euclidean error.

## 🧹 Cleanup flagged (not blocking — Tomo decision)
**Redundant swing path:** `stroke_classifier/detector_v2.py` (wired in `upload_app.py::_do_ingest_t5`) writes a SEPARATE `ml_analysis.swing_type_events` table that **silver Pass 1 does not read**, and it runs on Render where the git-ignored weights never exist → permanent no-op. The LIVE path is now `inference_v2` (Batch) → `stroke_class` → silver. Candidate for retirement (detector_v2 + swing_type_events table) once the deploy is validated — "one model per fact / keep it clean."

## 🐛 Known stale baseline (not mine, pre-existing)
`bench_silver` fixture `1d6feb3a` reports REGRESSION (row_count 7→4 from the bounce-proximity guard, downstream distributions). **Confirmed identical on clean HEAD** before my changes — it's a stale baseline from recent guard/gap_break commits, NOT this deploy. `bench_silver` is local-only (not a CI gate). Re-baseline it in a future session.

## 🎯 ALSO ON THE RADAR (after validation)
1. **RE-MEASURE 18-field reconciliation vs SA** — `harness reconcile`. Today's far-side fixes + the swing model likely improved bronze-vs-SA alignment; the north_star table is stale (pre-fixes).
2. **Ball bounce** (weakest field) — lift recall: gravity-residual candidate-gen + check `build_serve_bounce_dataset` for the SAME frame-space bug the swing builder had (`feedback_t5_two_frame_spaces`).
3. **A/B identity** — product call: changeover detection OR formally accept Near/Far.
4. **Per-role far swing eval** — finish to know far swing F1 → decide if stroke-TYPE is build-done.

## Canonical state
- Batch job-def: **eu rev 65 / us rev 46** (image digest `sha256:108153d7…` unchanged from rev 64; **rev 65/46 add `SWING_CLASSIFIER_ENABLED=0`** — classifier off pending the 4th-class/threshold fix). g4-primary → g5 → Spot queue. History: rev 64/45 = leak-fixed export + classifier ON (failed gate); rev 63/44 = classifier but export-leak-inert; rev 62/43 = pre-swing.
- **Validation sanity checks after a rev-64 run ingests:** `SELECT count(stroke_class) FROM ml_analysis.player_detections WHERE job_id=:t` should be >0 now; `SELECT count(*) FILTER (WHERE source='roi_prod') FROM ml_analysis.ball_detections WHERE job_id=:t` should survive ingest (was wiped pre-fix). Reference SA teacher = `ba4812be`.
- New weights deployed (in-image, git-ignored): `swing_classifier_v2.pt`. Local-only not-yet-deployed: `bounce_detector_v2_7match.pt`.
- GPU dev box `i-0295d636` (t5-dev-gpu-1b, Tesla T4) — STOPPED.
- Env knobs (Batch): `SWING_CLASSIFIER_ENABLED` (default 1), `SWING_CLASSIFIER_MIN_CONF` (default 0.5).
---
**END OF PICKUP**
