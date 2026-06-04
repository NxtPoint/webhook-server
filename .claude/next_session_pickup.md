# Next-session pickup — 2026-06-04 (PM) — swing classifier DEPLOYED to prod (rev 63/44); AWAITING validation rerun

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (re-verified this session).
**What shipped this session (the swing-classifier deploy — fully done end-to-end):**
- **Bronze inference wired (Batch):** new `ml_pipeline/stroke_classifier/inference_v2.py` runs in `pipeline.py` (`_classify_far_player_strokes` → delegates), classifies BOTH players' swing type per bounce with the trained v2 model (`SwingTypeR2plus1D`, near 0.86 / far 0.61), writes canonical `fh/bh/overhead` to `player_detections.stroke_class`. Frame-space handled (sampled `frame_idx` → source-fps window via `frame_interval`); handedness=1.0 (matches training default); memory micro-batched. Commits `877eaa6` (+ `7962088` detector_v2 lazy-import fix).
- **Silver prefers the model (Render, auto-deployed):** both Pass-1 cascades now PREFER `stroke_class` over the pose/position heuristics (heuristics demoted to STOPGAP fallback). Added `near_sc/far_sc` buckets + a windowed `stroke_class` patch. Commit on `main`.
- **Rule #8 rebuild DONE:** image rebuilt, pushed to both ECRs (amd64 digest `sha256:0eb6ad9c637d4ea2d5fecd1560d5b48a3ff0741b62ebefc101f144e58927978b`), job-defs registered **eu rev 63 / us rev 44** (retryStrategy preserved, g4-primary queue unchanged). Container import smoke test passed; weights bundle + load confirmed in-container.
- **Rollback without rebuild:** set Batch env `SWING_CLASSIFIER_ENABLED=0` (kill-switch) or tune `SWING_CLASSIFIER_MIN_CONF` (default 0.5).

**🔴 CRITICAL FIX THIS SESSION (the deploy was inert without it): export→reingest dropped stroke_class.** First validation run (`db3937fb`) completed but post-ingest `stroke_class=0` — the swing classifier RAN in Batch (436 classified) but `bronze_export._player_detection_to_dict` didn't serialize `stroke_class`, and the Render reingest DELETEs+re-COPYs from that JSON → silver fell back to the heuristic. Same leak wiped `roi_prod` bounces (the blanket ball_detections DELETE) → roi_bounces' ~16 min was 100% wasted on every auto-ingested task. **FIXED (`f4449b0`):** export serializes stroke_class; ingest COPYs it; ingest DELETE now preserves `source='roi_prod'`. **Rebuilt: eu rev 64 / us rev 45** (digest `sha256:108153d7…`). Render ingest auto-deploys.

**⛔ THE ONLY THING LEFT = THE VALIDATION GATE (Tomo's FRESH rerun on rev 64 + my reconcile):**
`db3937fb` ran on rev 63 (pre-fix) so its silver is heuristic-only — **a NEW run is required.** **Tomo triggers a fresh Singles-T5 upload of the same 10-min test video via the frontend** and replies with the new T5 task_id. Then:
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
- Batch job-def: **eu rev 64 / us rev 45** (digest `sha256:108153d7a9df6f5774f0d4fbb545219b1c2b18f1b7fe6b2365187e733730a98a`, g4-primary → g5 → Spot queue). rev 63/44 = swing classifier but export-leak-inert; rev 62/43 = pre-swing.
- **Validation sanity checks after a rev-64 run ingests:** `SELECT count(stroke_class) FROM ml_analysis.player_detections WHERE job_id=:t` should be >0 now; `SELECT count(*) FILTER (WHERE source='roi_prod') FROM ml_analysis.ball_detections WHERE job_id=:t` should survive ingest (was wiped pre-fix). Reference SA teacher = `ba4812be`.
- New weights deployed (in-image, git-ignored): `swing_classifier_v2.pt`. Local-only not-yet-deployed: `bounce_detector_v2_7match.pt`.
- GPU dev box `i-0295d636` (t5-dev-gpu-1b, Tesla T4) — STOPPED.
- Env knobs (Batch): `SWING_CLASSIFIER_ENABLED` (default 1), `SWING_CLASSIFIER_MIN_CONF` (default 0.5).
---
**END OF PICKUP**
