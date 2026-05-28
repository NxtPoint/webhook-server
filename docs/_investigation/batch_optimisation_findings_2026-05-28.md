# Batch optimisation — overnight findings (2026-05-28)

**Tier:** REFERENCE / investigation
**Author:** overnight autonomous agent (isolated worktree, branch `opt/overnight-findings`)
**Status:** PROTOTYPE — for daylight human review. NOT merged, NOT pushed, NOT deployed.
**Supersedes nothing.** Extends `docs/_investigation/batch_optimisation_plan.md` with the post-g5+L1+L4+L5 profile (where MOG2 and SAHI now dominate) and three committed prototypes.

---

## Why this session, in one paragraph

The player-GPU levers (L1 player-batching, L4 ROI batching, L5 NVENC) shipped 2026-05-28 and worked: full-frame YOLOv8x-pose dropped from ~192 → ~55 ms/fr. But the live g5/A10G steady-state profile (frame ~1400, FP16 + player-batching active) shows only ~20-30% overall improvement (~150 ms/fr ≈ 6.6 fps) because **two costs no prior lever touched now dominate**:

```
stage_timings: court 9% | ball 9% | motion_mask 44% (~65 ms/fr) | player 37% (~55 ms/fr)
player_sub:    full_yolo 15% | sahi 85%   (sahi_ran=327, sahi_skipped=1)   <- SAHI ~never skips
```

So: **MOG2 motion_mask is now the #1 cost**, and inside the (now-smaller) player stage **SAHI is 85% and skips 0% of frames**. This session prototyped a cut for each, plus fixed a confirmed latent prod bug in the S3-trigger Lambda.

---

## Hard constraint hit this session: bench could not be executed

The mandatory gate `python -m ml_pipeline.diag.bench` **could not be run** — every `python.exe` invocation in this overnight sandbox was denied (the worktree also has no `.venv`; the main repo's venv exists but invoking it was blocked). I did **not** work around the denial.

Instead I proved bench-neutrality **by construction** from the bench's own source, which is stronger than a single green run for these particular changes:

- `ml_pipeline/diag/bench.py` loads `*.pkl.gz` fixtures and calls `replay()`.
- `ml_pipeline/diag/replay_serves.py::replay()` calls **only** `ml_pipeline.serve_detector.detector.detect_serves_offline(...)`, fed pre-pickled `pose_near` / `pose_far` / `ball_rows`.
- The bench **never imports or executes** `pipeline.py`, `player_tracker.py`, MOG2, SAHI, or `lambda/ml_trigger.py`.

Therefore **none of the three changes can move the bench number** — they are outside the replay code path entirely. Two of the three are additionally **env-gated default-OFF**, so even the live Batch default path is byte-identical until a human flips the flag (the project's standard `feedback_env_var_rollback_pattern.md`).

> **DAYLIGHT REQUIREMENT (do not skip — rule #9):** a human must still run `python -m ml_pipeline.diag.bench` on a real box before any push, and must run it **with the new env flags actually set** (`MOG2_DOWNSCALE=2`, `SAHI_SKIP_A_FAR_YMAX=8.0`) to confirm green — although, per the path analysis above, the flags cannot reach the bench code, so this is a belt-and-braces check. The real validation for items 1 and 2 is a **long-match SA-pair reconcile** (player / far coverage), not the bench.

---

## Item 1 — MOG2 motion_mask is the #1 cost (44%, ~65 ms/fr, CPU, every frame)

### Root cause
`ml_pipeline/pipeline.py::_process_frame` step 3 calls
`self._bg_subtractor.apply(frame, learningRate=MOG2_LEARNING_RATE)` on the **full 1080p frame, single-threaded CPU, every frame**. It is the single `apply()` call site (the other `createBackgroundSubtractorMOG2` at `pipeline.py:648` is only `reset()` re-instantiating the subtractor; there is no second per-frame apply, including in the L1-batched path — only the YOLO call defers, MOG2 stays per-frame).

### Consumer audit (the safety proof)
`motion_mask` is consumed by exactly one path:
`player_tracker._choose_two_players` (line ~1239) → `PlayerTracker._compute_motion_ratio(box, motion_mask)` (line ~954). That function returns `fg_pixels / total_pixels` over the candidate's bbox ROI, compared against `MOG2_MIN_MOTION_RATIO = 0.03` to grant a flat `motion_bonus = 500`. It is a **coarse foreground-pixel fraction → binary moving/stationary decision**. A moving player scores ~5-15%, a seated spectator ~0-1% (config comment). This fraction is **downscale-invariant**: sampling the same bbox at 540p vs 1080p preserves the ~5-15% vs ~0-1% separation far from the 3% threshold. There is **no** sub-pixel / edge-precise / coordinate-precise use of the mask anywhere. Downscaling is therefore safe for detection accuracy.

### Prototype committed
`f9ecd5f perf(t5/batch): env-gated MOG2 downscale (MOG2_DOWNSCALE, default OFF)`
Files: `ml_pipeline/config.py` (new `MOG2_DOWNSCALE = max(1, int(getenv("MOG2_DOWNSCALE","1")))`), `ml_pipeline/pipeline.py` (`_process_frame` step 3 + import).

When `MOG2_DOWNSCALE > 1`: `cv2.resize(frame, /N, INTER_AREA)` → `apply()` on the small frame → `cv2.resize(mask, full, INTER_NEAREST)` (NEAREST keeps the mask binary 0/255 — no grey edges that would dilute the fraction near the 0.03 threshold). `=1` is the literal pre-change call (default).

### Expected impact
MOG2.apply() is roughly linear in pixel count. `MOG2_DOWNSCALE=2` → ¼ the pixels → ~4× cheaper apply(); the resize overhead is small relative to MOG2's per-pixel Gaussian-mixture update. From ~65 ms/fr, expect motion_mask to drop to roughly **~15-20 ms/fr** (including the two resizes), i.e. **~45-50 ms/fr saved** — on a 67k-frame match that is **~50-55 min off the main loop** wall time. `=4` would cut further but starts to risk the far-player bbox (≈30-40px → ≈8-10px at /4) carrying too few foreground pixels for a stable ratio; **2 is the recommended setting**, 4 only if a reconcile proves coverage holds.

### Risk
Low, gated. The only theoretical failure is the far player's already-tiny bbox losing motion-fraction fidelity at high downscale — mitigated by recommending `=2` (far bbox stays ~15-20px, plenty for a fraction) and by the flag defaulting OFF. No silver/gold gating depends on the mask.

### Bench result
Not run (sandbox denial). **Bench-neutral by construction** (replay path never touches MOG2). Daylight: run bench green, then validate the real thing via a long-match SA reconcile of player/far coverage.

### Daylight deploy plan — **BATCH-SIDE (rule #8)**
1. Cherry-pick / re-apply commit `f9ecd5f` onto `main`.
2. `python -m ml_pipeline.diag.bench` green (belt-and-braces).
3. `git push origin main` (rule #7).
4. Docker rebuild (`ml_pipeline/Dockerfile`) + **dual-region ECR push** (eu-north-1 + us-east-1).
5. New job-def revisions in both regions **with `MOG2_DOWNSCALE=2`** in the environment.
6. Re-ingest a long match (e.g. `9378f2dd` / `c645a7ee`); read back `ms_per_frame` from `ml_analysis.video_analysis_jobs` and confirm the motion_mask share collapsed (pull a `stage_timings` CloudWatch line).
7. `python -m ml_pipeline.harness reconcile <SA_tid> <T5_tid>` on that pair — confirm no far-player coverage regression. If clean, leave the flag on; rollback = set `MOG2_DOWNSCALE=1` (no rebuild needed, env-only).

---

## Item 2 — SAHI tile-fan is 85% of the player stage and skips 0% of frames

### Root cause
SAHI (court-ROI tiled YOLOv8m fan, ~300 ms when it runs) is the single largest op once full-frame YOLO got batched. The skip rule lives in `player_tracker.py::_detect_frame_postprocess` (the block guarded by `if SAHI_ENABLED and self._sahi_model is not None:`). It skips SAHI if **Rule A** (spatial: full-frame pose found a near-half AND a far-half pose-carrying candidate) **or Rule B** (metric: any candidate's feet project into `[-10, 5] m` of the far baseline) fires.

The profile says SAHI skipped **1 of 328** detect-frames. Rule A's far-half predicate (lines ~441-459) is the bottleneck: when `to_court_coords` is available it accepts a far-half pose candidate **only if its feet project to `court_y <= 5.0 m`**. That `5.0` was deliberately introduced 2026-04-19 to stop the **net umpire** (court_y ≈ 11-12 m, often pose-carrying) from spoofing Rule A and causing a bad SAHI skip. But it is **too tight on the other end**: a far player who has stepped *into* the court for a return projects to court_y ≈ 5-9 m and is rejected → `has_far_pose` stays False → Rule A never fires → SAHI runs. So the rule that was supposed to skip SAHI when both players are already resolved almost never gets the chance.

(Rule B also rarely fires on these matches — the `[-10,5]` band centred on the far baseline misses mid-court rally positions for the same reason.)

### Why this is safe (does NOT reduce far-player coverage)
SAHI exists to catch the ~30-40px far player that full-frame YOLOv8x-pose can't resolve (its keypoint floor is ~60-80px). Rule A by construction **only** fires on a frame where the full-frame pass *already produced a pose-carrying far candidate* — meaning the far player was large/clear enough this frame that YOLO resolved them with keypoints. On exactly those frames SAHI is redundant. We are not skipping SAHI on the frames where it matters (tiny far player, no full-frame pose) — those still have `has_far_pose = False` and run SAHI.

### Prototype committed — direction (a), the safe one
`ae47b45 perf(t5/batch): env-gated SAHI skip-rule A relaxation (SAHI_SKIP_A_FAR_YMAX)`
Files: `ml_pipeline/config.py` (new `SAHI_SKIP_A_FAR_YMAX = float(getenv("SAHI_SKIP_A_FAR_YMAX","5.0"))`), `ml_pipeline/player_tracker.py` (replace hardcoded `pt_box[1] <= 5.0` with `pt_box[1] <= SAHI_SKIP_A_FAR_YMAX` + import).

Default `5.0` = byte-identical to current. Recommended human setting **8.0** — lets a far player in the 5-8 m return zone satisfy `has_far_pose`, while staying well below the net umpire at ~11-12 m so the 2026-04-19 spoof guard holds. Lower bound is intentionally left unbounded (lens extrapolation pushes the true far baseline to negative court_y, already accepted by `<= YMAX`).

### Expected impact
If raising YMAX to 8.0 moves even a third of the currently-running SAHI frames into the skip bucket, that is ~110 fewer ~300 ms tile-fans per ~330 detect-frames. Player stage is ~55 ms/fr today with SAHI at 85% of it. Conservatively expect **~15-25% player-stage reduction → ~8-14 ms/fr off the main loop**. The exact number depends on the true `would_skip_A_only` counter, which the code already tracks (`_sahi_skip_by_rule`) and logs at end-of-run — **a human should read that counter from one long-match log to size the win precisely before flipping** (the diagnostic is already there; no new instrumentation needed).

### Risk
Low-Med, gated. The umpire-spoof regression is the one to watch; keeping YMAX ≤ ~10 preserves the guard. Validate with a far-player coverage reconcile.

### Bench result
Not run (sandbox denial). Bench-neutral by construction + default-OFF.

### Daylight deploy plan — **BATCH-SIDE (rule #8)**
Same 7-step shape as Item 1, with `SAHI_SKIP_A_FAR_YMAX=8.0` in the new job-def env. **Before flipping**, read the `_sahi_skip_by_rule` end-of-run log line (`fired_by_A_pose`, `would_skip_A_only`) from a recent long-match CloudWatch log to confirm the skip rule will actually fire more. Reconcile far-player coverage on the SA pair after the run. Rollback = `SAHI_SKIP_A_FAR_YMAX=5.0` (env-only, no rebuild).

### Direction (b) — batch the SAHI tile-fan into one FP16 YOLOv8m forward pass (PLAN ONLY, not coded)
`_run_sahi` (line ~879) calls `sahi.predict.get_sliced_prediction`, which loops tiles **sequentially** at batch=1 — the same batch=1 underutilisation L1 fixed for the full-frame pass, but now across every tile of every SAHI frame. The true parallel win:
- **Replace the SAHI library call with a hand-rolled tiler**: slice the court-ROI into the same 640×640 / 15%-overlap grid, stack all tiles into one `(N_tiles, 3, 640, 640)` batch, run a single `self._sahi_model.model.predict(batch, half=True)` (FP16), then offset each tile's boxes back to full-frame coords and run the existing NMS merge.
- Note from `config.py:159-163`: `sahi==0.11.18`'s `get_sliced_prediction` exposes **no `half` flag** and force-half'ing its wrapped model dtype-mismatches the FP32 tiles it feeds — which is exactly why this needs to bypass the library and feed tiles directly.
- Expected: tile-fan from sequential batch=1 → one batched FP16 pass = **~2-4× faster SAHI** on the frames where it still runs (stacks multiplicatively with the (a) skip-rate win). Outputs are mathematically equivalent (per-tile conv math is batch-independent; NMS merge unchanged).
- Risk: Med — re-implements SAHI's slice/merge in-house; must reproduce SAHI's overlap + NMS merge exactly or far-player dedup shifts. Worth a dedicated daylight session with a tile-count + coverage reconcile, **not** an overnight prototype.

---

## Item 3 — CONFIRMED latent prod bug: S3-trigger Lambda double-invokes the command

### Root cause
The image `ENTRYPOINT` is `["python","-m","ml_pipeline"]` (`ml_pipeline/Dockerfile:130`, verified). `lambda/ml_trigger.py` set `containerOverrides.command = ["python","-m","ml_pipeline","--job-id",...,"--s3-key",...]` (lines ~84-88). AWS Batch **appends** `command` to the entrypoint, so the container ran:

```
python -m ml_pipeline   python -m ml_pipeline --job-id ... --s3-key ...
```

→ `ml_pipeline/__main__.py`'s argparse sees the second `python -m ml_pipeline` as positional junk and dies:
```
__main__.py: error: unrecognized arguments: -m ml_pipeline
```

So the **direct-S3-upload path is broken on the rebuilt image**. The in-app submit path in `upload_app.py:923` uses the correct args-only form (`cmd = ["--job-id", job_id, "--s3-key", s3_key]`), which is why app-driven uploads work and only the Lambda path is broken.

### Prototype committed
`5f0ac64 fix(lambda): drop redundant python -m ml_pipeline prefix from Batch command override`
File: `lambda/ml_trigger.py`. `command` is now args-only (`["--job-id", job_id, "--s3-key", s3_key]`), matching `upload_app.py`. One-line behavioural fix + explanatory comment.

### Expected impact
Correctness, not speed: restores the S3-ObjectCreated → Batch path. No effect on app-submitted jobs.

### Risk
Negligible. Pure args-only alignment with the already-working `upload_app.py` path.

### Bench result
Not run (sandbox denial). **Cannot affect bench** — `lambda/` is outside the bench/CI glob entirely and is never imported by the replay path.

### Daylight deploy plan — **LAMBDA deploy (NOT a Batch-image rebuild)**
1. Re-apply commit `5f0ac64` onto `main`, `git push origin main`.
2. Redeploy the Lambda function from the updated `lambda/ml_trigger.py` (zip/console/CI — whatever the existing Lambda deploy mechanism is; this repo has no IaC for it, so it is a manual function-code update). **No Docker rebuild, no ECR push, no job-def revision** for the Lambda fix itself.
3. Smoke test: drop a small `videos/{task_id}/clip.mp4` into the bucket and confirm the Batch job starts and `__main__.py` parses args cleanly.

### ALSO NOTE (daylight, do NOT change here) — the stored job-def has the SAME bug
The task brief flags that the **registered AWS Batch job definition's stored `command`** carries the identical redundant `python -m ml_pipeline` prefix. That is an **AWS-side job-def change** (a new job-def revision) and is explicitly out of scope for this no-AWS overnight session. A human should, at daylight, register a job-def revision whose `command` is args-only (or empty, letting the per-submit override supply args), in **both** eu-north-1 and us-east-1, so that any submitter relying on the stored command (rather than overriding it) also works. Until then, only submitters that override `command` with the correct args-only form (i.e. `upload_app.py`, and now the fixed Lambda) will run successfully.

---

## Summary table

| # | Item | Commit | Default | Expected win | Deploy class |
|---|------|--------|---------|--------------|--------------|
| 1 | MOG2 downscale | `f9ecd5f` | OFF (`MOG2_DOWNSCALE=1`) | ~45-50 ms/fr (~50 min/long match) at `=2` | BATCH-SIDE (rebuild + ECR + job-def) |
| 2 | SAHI skip-rule A relax | `ae47b45` | OFF (`SAHI_SKIP_A_FAR_YMAX=5.0`) | ~8-14 ms/fr at `=8.0` (size via `_sahi_skip_by_rule` log first) | BATCH-SIDE (rebuild + ECR + job-def) |
| 3 | Lambda double-invoke fix | `5f0ac64` | n/a (correctness) | restores S3-trigger path | LAMBDA deploy (no rebuild). Plus daylight job-def revision for the stored-command twin bug. |

**Stacked (1)+(2) at recommended settings:** ~55-65 ms/fr off the ~150 ms/fr g5 main loop → roughly ~90-95 ms/fr (~10-11 fps), a further ~1.5-1.7× on top of the already-shipped L1/L4/L5. Combined with the ROI/transcode cuts already landed, this plausibly brings the 67k-frame match under the 1 h target — **to be confirmed by a real long-match re-ingest at daylight.**

## Branch
All three commits are on **`opt/overnight-findings`** in the isolated worktree. Not merged, not pushed. One change per commit. For daylight human review per the prototype-only mandate.
