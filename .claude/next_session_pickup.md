# Next-session pickup — 2026-05-31 — ROI fixes DEPLOYED + PROVEN (rev 59); next = main-loop cycle for sub-1h

## ⚡ Executive summary (read first — 30 seconds)
**FIRST ACTION:** read `docs/north_star.md` §"★ RULES OF THE GAME".
**Bench:** serve `a798eff0 20/24, 880dff02 23/24` GREEN. `bench_calib` 4/4. bounce fps-fix validated (M4 0%→26.7%) — baseline NOT locked.
**Deploy state:** **job-defs eu rev 59 / us rev 41** LIVE (image `7f1998a5` / amd64 `sha256:0976c304…`). All ROI fixes shipped + proven on match 4 (job `624e0b36`).
**What landed (rev 59, proven in prod):**
- ✅ **Far-pose alignment bug FIXED** — far-pose `player_detections_roi.frame_idx` now `[24..71914]` (25-fps, aligned with bronze) vs `[56..172594]` (60-fps, MISALIGNED) before. This was silently wrong on every 50/60-fps match (the bronze_export player merge). **Correctness win.**
- ✅ **pose fp16 crash fixed** (heatmap→float32 cast) — far-pose runs at fp16, no crash (14,153 rows).
- ✅ **bounce OOM-safe** (size-guard + eager fallback) — 16,204 rows / 952 bounces, no OOM.
- ✅ **ROI sweep 25-fps decode-skip** — `decoded 71915 sampled frames` (not 172,596). Sweep **91→52 min**.
- **Total 157→118 min** (7054 s), main loop 48.0 ms/fr / 0 errors, court 88%.
**NOT sub-1h yet.** Main loop ~58 min + ROI compute (far-pose 39 + bounce 45, overlapped→52) are the rocks.
**Next session's job:** the **main-loop optimization cycle** — work `docs/_investigation/t5_runtime_backlog.md`, **start with B1** (the main `VideoPreprocessor.frames()` over-decodes exactly like the sweep did — safe, output-identical `grab()/retrieve()` decode-skip, the top lever). Then B2 (`MOG2=4`) + D1 (memory) with coverage reconciles.

If that's enough, go. Depth below.

---

## 🗒️ THE master list: `docs/_investigation/t5_runtime_backlog.md`
Every known runtime lever with cost/impact/risk/validation/status (10 shipped, ~12 todo). This is the source of truth for the optimization cycle — read it first. Sequence + honest sub-1h math at the bottom.

**Top of the queue (next session):**
- **B1 (DO FIRST, 🟢 safe):** `video_preprocessor.py:84` `cap.read()`s every 60-fps frame, yields only ~41%. Apply `grab()/retrieve()` decode-skip (same as the sweep fix, `unified.py`). Output-identical, big main-loop win. **Batch-side → rebuild.**
- B2 (🟡): `MOG2_DOWNSCALE=4` (env only) — reconcile player coverage.
- D1 (🟡): GPU memory audit (`empty_cache` between phases) — unlocks ball/ROI batching (the rev-58 OOM theme).
- **NEW C4 (🟡): far-pose density dropped 47,974→14,153** because it's now `every-2` of 25-fps = 12.5 fps. Now that it's aligned + fp16-cheap, consider `pose_sample_every=1` (25-fps, ~matches bronze density). Validate far-player coverage.

## How to run the cycle (mechanics that worked this session)
- Rebuild: `docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .` → tag+push both ECR regions → extract amd64 digest → `.claude/tmp/calib_audit/register_jobdef_opt.py <digest> <region>` (clones latest active, adds opt env, fixes eu command). Edit its `OPT_ENV` for new flags.
- Run: re-upload source (`aws s3 cp <local> s3://nextpoint-prod-uploads/wix-uploads/1779964630_Tomo_vs_Jimbo.mp4`; deleted on each success), then `.claude/tmp/submit_v59.py`-style (fresh job_id + create `video_analysis_jobs` row + submit on the new rev, g5 queue `ten-fifty5-ml-queue`). **Use a fresh job_id** — reusing one DOUBLES detection rows (no delete/ON CONFLICT). Don't touch corpus `ca475740`.
- Monitor: a persistent Monitor on `aws batch describe-jobs` status + 15-min heartbeat + terminal. Logs: group `/aws/batch/ten-fifty5-ml-pipeline` (NOT `/aws/batch/job`); use `MSYS_NO_PATHCONV=1 PYTHONIOENCODING=utf-8` for the AWS CLI on this box.
- Validate: court LOCKED %, `ms_per_frame`, `roi_unified: decoded N sampled`, far-pose/bounce `frame_idx` max ~71.9k (alignment), 0 errors, reconcile vs SA.

## Open items
| # | Item | Notes |
|---|---|---|
| 1 | Main-loop optimization cycle (sub-1h) | backlog B1→B2→D1; fresh session |
| 2 | Lambda **function** code deploy (`update-function-code`) | rev-59 job-def fixed the stored-command half; the Lambda path still needs deploy to restore DIRECT S3 uploads. ⚠️ **BLOCKED from this box** — IAM user `nextpoint-uploader` has NO Lambda perms (`lambda:ListFunctions`/`UpdateFunctionCode` denied). Needs a Lambda-capable credential (Tomo) or an IAM grant. Code fix already committed (`lambda/ml_trigger.py`, args-only). |
| 3 | far-pose density (`pose_sample_every=1`) | backlog C4; validate coverage |
| 4 | Bounce pass is CPU/postprocess-bound | backlog C1 |
| 5 | Optional: refresh corpus match-4 entry from a clean run's superior bronze | backlog/corpus; don't double-count `ca475740` |

## Session note
This was a very long session (init → calibration deploy → fps fix → overnight → 4 rebuilds → ROI fixes). Recommend the main-loop cycle start FRESH with this pickup + the backlog.

## Jobs (manual submits — NOT auto-ingested to silver)
- rev-59 proof: `624e0b36-…` (bronze in `s3://…/analysis/624e0b36-…/bronze.json.gz`; ROI rows in `ml_analysis.player_detections_roi` + `ball_detections` roi_prod).

---
**END OF PICKUP**
