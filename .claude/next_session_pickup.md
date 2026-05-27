# Next-session pickup — 2026-05-27 (ROI Bug 2 fixed + validated, ON A BRANCH; needs daylight Batch deploy)

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** 2026-05-27
**Phase active:** Phase 7 reframed — bounce *precision + coverage*, NOT calibration (calibration is a faithful homography). See `docs/_investigation/bounce_accuracy.md`.
**Bench:** serve `a798eff0=20/24, 880dff02=23/24` green; `bench_ball` green (no regressions). **Note: this box is CPU-only → `bench_ball` takes ~3 HOURS.**
**What shipped (to main):** bounce-proximity precision guard (`aa6c522`); SportAI confirmed a usable bounce yardstick (95/90) so manual ground-truth is built-but-parked; full bounce diagnosis in `bounce_accuracy.md`.
**ROI Bug 2 — DEPLOYED 2026-05-27** (supervised). On main (`2c461e9`); image built + pushed both ECR regions (amd64 `sha256:255efa59`); job-def revisions eu-north-1 **rev 50** / us-east-1 **rev 32** pin the new image (retry attempts=3). Branch deleted. Lambda resolves by name → next job runs the fix.
**What's blocked:** nothing actively. **VALIDATION PENDING:** Tomo uploads the 40-min match that previously timed out → confirms the speed fix (should finish well under 6h) + adds corpus #2. Tomo self-serves the upload (gated to tomo.stojakovic@gmail.com) and reports the task_id.
**Next session's job:** confirm the 40-min validation run finished fast; if long videos still slow, profile the next bottleneck (roi_bounces re-opens the video per window; full-frame TrackNet/pose passes). Then B1 (ball interpolation heuristic). B2/training gated on corpus volume.

---

## ⚠️ FIRST ACTION (daylight, with Tomo) — deploy ROI Bug 2

**Branch:** `roi-bug2-balltracker-hoist` (`2cdb68c`), pushed to origin. **Not** on main.
**What it fixes:** `roi_extractors/bounces.py` built a fresh `BallTracker()` per window → reloaded TrackNet weights every window → ~7× slowdown → **timed out Match 2 at the 6h Batch limit.** Now the model loads once and is shared; a fresh `BallTracker` per window keeps all per-window state clean. (`ball_tracker.py` gained an optional `model=` arg + `_use_fp16` moved to `__init__`.)
**Validated (CPU box):** `bench_ball` no regressions (default path); injected-vs-load equivalence **IDENTICAL** (64=64 detections); serve bench green.

**It's a BATCH-SIDE change** (`ball_tracker.py` + `roi_extractors/`) → full deploy required. Steps (full detail in `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST" + the deploy playbook ~line 285):
1. `git checkout roi-bug2-balltracker-hoist` (or merge to main first — see note).
2. ECR login both regions; `docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .` (code-only change → cached pip layers, ~3-5 min). Run build in background.
3. Tag + push to eu-north-1 AND us-east-1 (~5-10 min each).
4. Get the amd64 sub-manifest digest (`aws ecr batch-get-image`, Gotcha #1).
5. Register new job-def revisions in **both** regions pinned to that digest, retryStrategy preserved.
6. **Tomo uploads a fresh match** (Singles T5, gated to tomo.stojakovic@gmail.com) → gives the new task_id.
7. Monitor the Batch job; confirm the per-window time is flat (no "BallTracker: loaded" every window) and a long match completes < 6h.
8. **Merge `roi-bug2-balltracker-hoist` → main** once the run looks good. (Per `feedback_always_main_branch`, main is the home; the branch was only to avoid an unsupervised overnight Batch change per `feedback_overnight_branch_only`.)

Docker 29.3.1 + aws-cli 2.34.26 are installed here; boto3 finds creds via default chain (region eu-north-1).

---

## Where the bounce thread stands (the arc this session)

1. **Diagnosis (done):** bounce error is precision + coverage, NOT calibration. The homography is faithful (reproduces stored bounce coords to 0.11 m). The "177 dropped bounces" are ~84% airborne false-positives, correctly clamped. `docs/_investigation/bounce_accuracy.md` §1-7.
2. **Proximity guard SHIPPED (main, `aa6c522`):** drops bounces within 1.5 m of a player (racquet contacts). M1: serve precision vs SA 45→67%, bench green.
3. **SportAI is the working yardstick (Tomo: 95% bounce-ID / 90% coords).** So bounce precision IS measurable vs SA now; **manual ground-truth is built but PARKED** for the fine-tuning stage. Tooling ready: `ml_pipeline/training/label_bounces_manual.py` + `bounce_xy_accuracy.py --ground-truth` + Match 1 video at `ml_pipeline/test_videos/78c32f53_practice.mp4` (720p; labels need ×1.5 to ml_analysis 1080 space — see `ml_pipeline/ground_truth/README.md`).
4. **Current T5-vs-SA-floor (the live measurement):** recall 55%, precision 27%, median 4.57 m → T5 bounce well behind SortAI → **use SortAI bounces in-product for now.** Improving T5 bounce = B1/B2 below.
5. **ROI Bug 2 (this session):** fixed + validated, on branch (see above). Unblocks Match 2 → a SECOND validation match (everything so far is single-match-calibrated).

## Task board (open)
- **#16 ROI Bug 2** — code done + validated on branch; **deploy is the open step** (above).
- **#12 B1** — short-gap ball interpolation (+7% coverage, tightens timing). Batch-side. Measurable vs SA.
- **#13 B2** — ball-detector fine-tune for the 29% sustained-gap loss (the big coverage lever). Batch, multi-day; uses existing `training/` pipeline + Phase 5c corpus.
- **#9 Stage-2 bounce precision** — ON HOLD (cheap silver filters underdeliver; needs ground-truth/coverage first).
- **Q2-B player identity — PARKED** (deleted from board). End-anchoring not viable: T5 detects only 3 noisy games for a ~12-game match; side-based tracking can't see changeovers. Use SortAI identity or relabel "Near/Far". Bronze-first #11.

## Things NOT to do (load-bearing)
- **Don't put the ROI Bug 2 change on main without a daylight deploy + test match.** It's Batch-side; main = old image until deploy, so merging without deploying creates the silent-stale hazard (CLAUDE.md #8).
- **Don't chase T5 bounce coords vs SA as "calibration"** — calibration is faithful; the gap is precision + coverage + far-court resolution limit.
- **Don't flip `T5_STROKE_DRIVEN_SILVER`** (unchanged; still gated OFF).
- **CPU box:** `bench_ball` ~3h here; prefer running it in the background and continuing.

## Read in this order
1. This file.
2. `docs/_investigation/bounce_accuracy.md` (§7 conclusion + §8 scope + the 2026-05-26 SA-yardstick update).
3. `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST" + deploy playbook (for the ROI Bug 2 deploy).
4. `docs/north_star.md` Phase 7 (reframed).
