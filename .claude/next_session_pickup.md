# Next-session pickup â€” 2026-06-06 PM â€” FAR-COURT FOUNDATION FIXED (calibration+identity+ROI); next = compensation cleanup â†’ fresh scorecard â†’ serve model retrain

## âš¡ Executive summary (read first â€” 30 seconds)
**Phase:** bronze-first; the far-court structural defects are FIXED and probe-validated. Deployed: **rev 70/51**.
**Bench:** serve 20/24, 23/24 GREEN (every push gated). CI green.
**The day's chain (each measured vs SA on the reference video, probe ledger below):**
1. **Calibration** (`b08a858`): far keypoints were amputated by every fitting stage â†’ far court extrapolated.
   Far-player bias **+11.0m â†’ -1.05m**; bounce xy **4.9m â†’ 0.90m**; in-flight phantom bounces now auto-killed by honest projection.
2. **Identity** (`e9ae36f`, env `PLAYER_SPLIT_BY_NET`): near/far split by court NET line, not frame midline.
   pid-1 pollution **46% â†’ 0%** (probe p8). Near pose rows 7535 â†’ 11767.
3. **ROI gate** (`328d3b8`): bounce-CNN stage reordered BEFORE ROI sweep; rally gate consumes CNN events.
   Far-ROI usable poses **391 â†’ 1019** (det 598â†’2008, p7).
4. Serve consumer on CNN bounces (`05fe85d`, env `SERVE_CNN_BOUNCES`); SAHI_BATCHED=0 feed fix (rev 67); CI repaired (`f24f4f5`).
**Serve now:** near 13/14 (ceiling), far 3/12 â€” far heuristic is signal-saturated (more coverage â†’ FPs not recall, P 53â†’39 with ROI lift): **TRAINING territory, now with clean data.**
**Next jobs (order):** (1) compensation cleanup, (2) fresh 18-field scorecard, (3) serve model retrain on clean corpus.

## ðŸ§¹ JOB 1 â€” Warp-compensation cleanup (verified in code, each its own probe)
The codebase grew compensations for the old +11m far bias; they're now wrong in the OPPOSITE direction (over-wide gates admitting noise):
- `build_silver_match_t5.py:191-192` â€” `HITTER_FAR_MAX = COURT_LENGTH_M + 6.0` / `HITTER_NEAR_MIN = -6.0` â†’ honest â‰ˆ Â±4m.
- `player_tracker.py:489` â€” SAHI skip predicate `-10.0 <= pt[1] <= 5.0` (A0 widening) and tier-2 `-10m` behind-baseline zone (~line 1479) â†’ honest â‰ˆ -4m. Also the line-1472 TODO ("investigate far extrapolation bias") is RESOLVED â€” delete it.
- `roi_extractors/pose.py:41` â€” `FAR_ROI_Y_LO = -8.0` â†’ can tighten (~-5) = smaller crop, faster, less noise.
- âš ï¸ `serve_detector/detector.py` `_baseline_zone` far range (-3.5..4.5) â€” **CI-SENSITIVE and BLOCKED on fixture regeneration**: the locked bench fixtures carry WARP-ERA court coords; tightening zones breaks the bench falsely. Correct path: regenerate fixtures from a rev-70 run (snapshot_task) + re-baseline in the same commit (rule #9-compliant, justified), THEN tighten.
- Also stale serve eval data: 41 emissions / P 39% on rev 70 â€” partly the far-pose path over-firing on the new ROI coverage. Expected to be fixed by the serve model, not heuristics.

## ðŸ“Š JOB 2 â€” Fresh 18-field scorecard
All historical accuracy numbers were measured against the warped map. Re-run the field-by-field scorecard on a rev-70 run (probe p8 data exists under job 52e072a7, in ml_analysis now â€” or cleaner: next real upload). Tool: `.claude/tmp/scorecard.py <job_id>` + `harness eval-serve`. Output: per-field build-bar status â†’ the dev-ceiling sign-off list. Promote scorecard.py into ml_pipeline/diag/ as the automated per-run scorecard (backlog item, high value).

## ðŸŽ“ JOB 3 â€” Serve model retrain (after 1+2)
Scaffold shipped `61b677b` (ml_pipeline/serve_model/). v1 weights trained on WARPED far coords â†’ held-out parity only (gate not met, not wired). Retrain needs clean-coordinate corpus: every new SportAI dual-submit now generates clean labels+features. Candidate recall 98.5% already. Far ROI coverage 2.6Ã— + identity clean = better features. Target: far 3/12 â†’ 8+/12 â‡’ â‰¥20/26 total.

## Canonical state
- Batch: **eu rev 72 / us rev 53** (digest `1d41e8ff`, identity fix + tracker decomp; SAHI_BATCHED=0, SWING_CLASSIFIER_ENABLED=0 carried).
  main @ `e9ae36f` fully synced with image. Env knobs: `PLAYER_SPLIT_BY_NET`(1), `SERVE_CNN_BOUNCES`(1), `BOUNCE_CANDIDATE_MODE`, `SWING_CLASSIFIER_ENABLED`(0).
- Probe harness: `.claude/tmp/probe_{submit,measure}.py` + `scorecard.py`. Probe ledger `.claude/tmp/probe_results.md`.
  Probe ml_analysis rows for p7 (c2d33296) + p8 (52e072a7) still in DB â€” p9b (ea1e500c) KEPT as scorecard source; p7/p8/stale-p9 deleted.
- Real-task runs of reference video: 60b11b09 (rev66) â†’ 0bc3a869 (rev67) â†’ d777f090 (rev68). SA companion ba4812be.
- Reference video local: `ml_pipeline/test_videos/a798eff0_sa_video.mp4` (â‰¡ Tomo's OneDrive match.mp4).
- Serve corpus: 404 labels / 8 matches â€” features must be REGENERATED from clean-coordinate runs for retraining (old task coords are warped; only new runs/dual-submits carry honest coords).
- bench_silver stale baseline (pre-existing) + swing v2.1 retrain (4th class) still in backlog.

## Memory entries this arc
`feedback_perf_levers_need_accuracy_probe` (SAHI lesson). Calibration-amputation + compensation-debt pattern recorded in commit messages b08a858/e9ae36f/328d3b8.
---
**END OF PICKUP**
