# Next-session pickup — 2026-06-06 PM — FAR-COURT FOUNDATION FIXED (calibration+identity+ROI+decomp); next = fixture regen → fresh scorecard → serve model retrain

## ⚡ Executive summary (read first — 30 seconds)
**Phase:** bronze-first; the far-court structural defects are FIXED and probe-validated. Deployed: **rev 72/53**.
**Headline:** FAR player position error **-0.43m == NEAR -0.42m** (was +11.0m this morning). Identity pollution **46% → 0%**. Bounce xy **4.9m → 0.90m**.
**Bench:** serve 20/24, 23/24 GREEN (every push gated). CI green.
**The day's chain (each measured vs SA on the reference video, probe ledger in .claude/tmp/probe_results.md):**
1. **Calibration** (`b08a858`): far keypoints were amputated by every fitting stage (RANSAC thresholds sized for near-court noise) → far court extrapolated. Far-player bias +11.0m → ~-1m; bounce xy 4.9 → 0.90m; in-flight phantom bounces now auto-killed by honest projection.
2. **Identity** (`e9ae36f`, env `PLAYER_SPLIT_BY_NET`): near/far split by court NET line, not frame midline. pid-1 pollution 46% → 0% (p8). Near pose rows 7535 → 11767.
3. **ROI gate** (`328d3b8`): bounce-CNN stage reordered BEFORE ROI sweep; rally gate consumes CNN events. Far-ROI usable poses 391 → 1019 (det 598 → 2008, p7).
4. **Tracker decomp** (`b696c26`): warp-era -10m widenings (tier-2 + SAHI skip-B) → 5m; spectator suppression. FAR error -1.05 → **-0.43m** (p9b).
5. Serve consumer on CNN bounces (`05fe85d`, env `SERVE_CNN_BOUNCES`); SAHI_BATCHED=0 feed fix (rev 67); CI repaired (`f24f4f5`).
**Serve now:** near 13/14 (ceiling), far 3/12 — far heuristic is signal-saturated (more coverage → FPs not recall; 41 emitted, P 39): **TRAINING territory, now with clean data.**
**Next jobs (order):** (1) serve fixture regen + zone decomp, (2) fresh 18-field scorecard, (3) serve model retrain on clean corpus.

## ⚙️ Deploy-chain gotcha learned today (p9 incident)
A one-shot build→push→register chain MISSED the `docker tag` step → pushed the previous image, registered rev 71/52 against stale bits, probe p9 measured nothing. Caught by comparing pushed manifest digests; fixed as rev 72/53. **Always: build → `docker tag` BOTH regional names → push → verify in-image (`docker run --entrypoint python ... inspect.getsource`) → extract amd64 digest → register.** Rev 71/52 are orphaned stale revisions — do not use.

## 🧹 JOB 1 — Remaining warp-compensation cleanup
DONE today: tracker tier-2 + SAHI skip-B (-10 → -5, `b696c26`, validated p9b).
Remaining:
- ⚠️ `serve_detector/detector.py` `_baseline_zone` far range (-3.5..4.5) — **CI-SENSITIVE and BLOCKED on fixture regeneration**: the locked bench fixtures carry WARP-ERA court coords; tightening zones breaks the bench falsely. Correct path: regenerate fixtures from a rev-72 run (snapshot_task on p9b job ea1e500c or next real upload) + re-baseline in the same commit (rule #9-compliant, justified), THEN tighten zones.
- `build_silver_match_t5.py:191-192` HITTER_FAR_MAX +6m / HITTER_NEAR_MIN -6m — function is on the roadmap's DELETION path (silver inherits serve_events); don't polish, delete with the stroke-driven flip.
- `roi_extractors/pose.py:41` FAR_ROI_Y_LO=-8.0 — KEEP: image-geometric (raised-arms coverage), not warp debt.

## 📊 JOB 2 — Fresh 18-field scorecard
All historical accuracy numbers were measured against the warped map. Re-run field-by-field on rev-72 data (p9b job `ea1e500c` rows KEPT in ml_analysis as the source; or next real upload). Tools: `.claude/tmp/scorecard.py <job_id>` + `harness eval-serve`. Output: per-field build-bar status → dev-ceiling sign-off list. Promote scorecard.py into ml_pipeline/diag/ as the automated per-run scorecard (high value — kills silent-regression class).

## 🎓 JOB 3 — Serve model retrain (after 1+2)
Scaffold `61b677b` (ml_pipeline/serve_model/). v1 weights trained on WARPED far coords → held-out parity only (gate not met, NOT wired). Retrain needs clean-coordinate features: old corpus task coords are warped — only new runs / dual-submits carry honest coords (Tomo uploading matches as SportAI accumulates clean labels automatically). Candidate recall 98.5%; far ROI 2.6× + identity 0% = better features. Target: far 3/12 → 8+/12 ⇒ ≥20/26.

## Canonical state
- Batch: **eu rev 72 / us rev 53** (amd64 `1d41e8ff`; env carried: SAHI_BATCHED=0, SWING_CLASSIFIER_ENABLED=0, PLAYER_SPLIT_BY_NET default-on in code). main @ `b696c26` fully synced with image (rev 71/52 = stale orphans, ignore).
- Env knobs: `PLAYER_SPLIT_BY_NET`(1), `SERVE_CNN_BOUNCES`(1), `BOUNCE_CANDIDATE_MODE`, `SWING_CLASSIFIER_ENABLED`(0).
- Probe harness: `.claude/tmp/probe_{submit,measure}.py` + `scorecard.py`; ledger `.claude/tmp/probe_results.md`. Probe rows: p9b (`ea1e500c`) KEPT as scorecard source; p7/p8/stale-p9 deleted from ml_analysis.
- Real-task runs of reference video: 60b11b09 (rev66) → 0bc3a869 (rev67) → d777f090 (rev68). SA companion `ba4812be` (26 serves; 68 floor bounces of 162 rows — 94 are 'swing' racquet contacts).
- Reference video local: `ml_pipeline/test_videos/a798eff0_sa_video.mp4` (md5-identical to Tomo's OneDrive match.mp4).
- Backlog: bench_silver stale baseline; swing v2.1 retrain (4th class); corpus video-retention gap (labels point at deleted videos — blocks pixel-based models).

## Memory entries this arc
`feedback_perf_levers_need_accuracy_probe` (SAHI lesson). Calibration-amputation + compensation-debt + deploy-chain-tag patterns recorded in commit messages `b08a858`/`e9ae36f`/`328d3b8`/`b696c26` and this file.
---
**END OF PICKUP**
