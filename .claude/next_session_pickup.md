# Next-session pickup — 2026-06-06 (overnight session) — SERVE FEED REGRESSION FOUND+FIXED; next = morning validation + serve model v1 training

## ⚡ Executive summary (read first — 30 seconds)
**Today's date:** 2026-06-06 (written ~01:30 after an overnight autonomous session)
**Phase active:** bronze-first roadmap step 2 (serve) — build phase ~DONE pending live validation; training next.
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN (verified 3×). **CI bench GREEN again** (was silently red since May 28 — missing numpy in requirements-bench.txt, fixed `f24f4f5`).
**What shipped overnight:** (1) bounce stage live (rev 66/47) + validated on a real run; (2) serve detector consumes CNN `ball_bounces` w/ legacy fallback (`05fe85d`, env `SERVE_CNN_BOUNCES`); (3) **the serve-feed regression root-caused to `SAHI_BATCHED=1` and fixed in job-def eu rev 67 / us rev 48**; (4) CI repaired.
**What's blocked:** nothing.
**Next session's job:** Tomo uploads fresh match → confirm ~15-17/26 serves live on rev 67 → then serve model v1 training (kickoff draft ready).

## 🎯 THE OVERNIGHT FINDING (read this before touching serve or Batch perf knobs)
Fresh runs scored 7/26 serves while the bench said 20-23/24. NOT a detector regression — the **upstream near-pose
feed lost 26%** (10150→7535 rows; serve-window coverage 101→~64 ragged) somewhere in the May runtime campaign.
Probe ladder (6 Batch reruns of the reference video, one env knob at a time, full table in the session log below):
- Exonerated: YOLO_FP16 (keep =1, saves 9min, bit-identical), PLAYER_BATCH_SIZE, MOG2_DOWNSCALE,
  PIPELINE_STAGE_OVERLAP, PLAYER_DETECTION_INTERVAL (predates), hardware (all g4dn/T4).
- **Culprit: `SAHI_BATCHED=1`** (May-29 tile-fan prototype). With =0: pose_near 10333, near recall 12-13/14
  (fixture level), 17/26 total matched, F1 66.7 vs 35.9. Runtime price ~+13min/10-min video — accepted.
- **Fixed: job-def eu rev 67 / us rev 48** (env-only, image unchanged `a60c3909`). p6 probe = the validation run.
- Memory: `feedback_perf_levers_need_accuracy_probe`. SAHI_BATCHED code path still exists (env-gated off) —
  consider daylight investigation of WHY batched tile-fan drops near detections, or delete the prototype.

## Serve status after the fix (reference video, vs SA 26 = 14 near / 12 far)
- Near: 13/14 (heuristic ceiling — the 1 miss is the known pose-amplitude case)
- Far: 4/12 ← the remaining gap to Tomo's 20/26 target. Heuristic-unfixable (receiver-FP + faults outside
  service box are structurally invisible to bounce-first far path — bench-proven). **= TRAINING territory.**
- Serve ts accuracy on matches: mean 0.32s (CNN bounce path). Far xy capped by far-calibration (separate item).

## 🎓 SERVE MODEL v1 — ready to start (the morning's main job)
Kickoff draft: `.claude/tmp/serve_model_v1_kickoff_draft.md` (promote to .claude/ after Tomo review). Key facts:
- Corpus VERIFIED: 404 serve labels / 8 matches (200 FAR / 204 NEAR) with hit_frame/ts, server court xy, role,
  ball_speed — `training/labels/*_serves.json`. ⚠️ corpus video_s3_keys are all DELETED (post-trim) → v1 must be
  FEATURE-based (pose/ball/bounce series from ml_analysis), not pixel-based. ⚠️ frame-space: convert via ts only.
- Recipe = bounce-CNN port: high-recall candidates (relaxed far gates) → small scorer → gate per-serve vs SA
  ≥ heuristic baseline + serve bench green → env-gated `SERVE_MODEL_ENABLED=0` default.
- Sequencing gate: measure candidate recall on corpus FIRST (target ≥90%) before training anything.
- Where it runs: prefer Batch-side post-pipeline (one-model-per-fact; Render has no torch/weights).

## Canonical state
- Batch job-def: **eu rev 68 / us rev 49** (digest `830cf1f5`, `SAHI_BATCHED=0`, `SWING_CLASSIFIER_ENABLED=0`).
  Image = main @ `b08a858`: **far-calibration fix** (far-player bias +11.0m→+0.52m vs SA — keypoint-amputation
  root cause, see commit msg; benches green incl. degeneracy traps) + bounce CNN stage + progress-pct fix +
  serve_detector sync. **Awaiting first live validation run** (expect: far bounce/hit xy accurate ~±1m,
  bounce NULL-coord rate collapsing from 40%, far serve-geometry gate judging true coords).
- No Batch-side diffs pending vs deployed image (all synced at b08a858).
- Render (main): serve detector prefers CNN ball_bounces, falls back to legacy is_bounce (old tasks/fixtures);
  rollback knob `SERVE_CNN_BOUNCES=0` (docs/env_vars.md).
- Reference video: local copies `ml_pipeline/test_videos/a798eff0_sa_video.mp4` ≡ Tomo's
  `C:\Users\tomos\OneDrive\ten-fifty5\videos\match.mp4` (md5 089207968c…). SA companion ba4812be (26 serves,
  68 floor bounces + 94 swing rows — "bounce" target = the 68 floor).
- Probe harness (reusable): `.claude/tmp/probe_{submit,measure}.py` — direct Batch submit + local bronze ingest
  + eval, zero prod side effects. Probe ml_analysis rows cleaned after use (this session: all 6 probes deleted).
- Probe ledger: `.claude/tmp/probe_results.md` (gitignored — key numbers replicated above).
- CI: green (`f24f4f5`). Latest main: serve-consumer migration + CI fix + progress fix.

## 🗂️ Backlog (in order, after serve v1)
- **Stroke = ball-hit** (roadmap step 3, keystone): stroke_events has NO swing_type / ball-hit-xy columns —
  SA carries both at 100%. Then flip silver hit-driven; T5 silver 183 rows vs SA 94 dies from correctness.
- **Far-court calibration** (2.4-7m overshoot, 40% NULL bounce coords) — caps far xy for serve+bounce+stroke.
- Swing v2.1 retrain (4th "other" class) → re-gate → `SWING_CLASSIFIER_ENABLED=1`.
- Per-run scorecard automation (eval-serve + bounce eval auto-run on reference-video ingests) — kills the
  "regression invisible for a week" class permanently. Cheap, high value.
- SAHI_BATCHED prototype: root-cause or delete. bench_silver stale baseline re-base. Corpus video retention gap
  (labels point at deleted videos — blocks pixel-based training).
---
**END OF PICKUP**
