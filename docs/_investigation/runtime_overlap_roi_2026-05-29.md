# Runtime levers: CPU/GPU stage overlap + ROI bounce-window batching

**Tier:** REFERENCE / investigation
**Dated:** 2026-05-29
**Status:** PROTOTYPE — env-gated, default OFF, on branch `opt/runtime-overlap-roi`. NOT merged, NOT deployed. Daylight deploy + validation plan below.
**Companion:** `docs/_investigation/batch_optimisation_plan.md` (the ranked roadmap; these two are TASK 1 = the L10 CPU/GPU overlap lever, and TASK 2 = the ROI bounce sub-lever of L4).
**Goal:** drive the 45-min match from ~2 h (after the already-shipped L1/L3/L4/L5/L2c levers, steady-state ~70 ms/frame) toward sub-1 h.

Both prototypes are **env-gated default-OFF** → byte-identical to current behaviour until the flag is flipped on the Batch job-def. Neither touches the serve detector, so both are **bench-neutral by construction** (the CI bench replays `serve_detector` against a frozen fixture — it never executes `pipeline.py` or `roi_extractors/`).

---

## Live profile this work targets (match-4 clean run, today)

```
per-frame main loop (steady state, ~70 ms/fr):
  motion_mask 58%  (37.6 ms/fr)  CPU  MOG2, every frame
  player      23%  (15.0 ms/fr)  GPU  YOLOv8x-pose + SAHI (cached 4/5 frames)
  ball        18%  (11.4 ms/fr)  GPU  TrackNet/WASB, every frame
  court       ~1%  (locked after calibration)
ROI bounce stage (post-loop):
  ~194 windows processed SEQUENTIALLY, ~4-21 s each, ~25 min total
  TrackNet on a service-box crop, batch=1 per frame within each window
```

The motion_mask 58% share is the single biggest line in the per-frame loop, and it is **CPU** while the other three are **GPU** — but they run **serially**, so the CPU idles during GPU work and vice-versa. That serialisation is the opportunity for TASK 1. The ~25 min sequential ROI bounce stage is the opportunity for TASK 2.

---

## TASK 1 — CPU/GPU stage overlap (`PIPELINE_STAGE_OVERLAP`)

### Files
- `ml_pipeline/config.py` — `PIPELINE_STAGE_OVERLAP` flag (default OFF).
- `ml_pipeline/pipeline.py` — `_make_motion_mask()` helper, a bounded 1-worker `ThreadPoolExecutor`, overlap dispatch in `_process_frame`, executor teardown in `process()` + re-arm in `reset()`, and observability in `_log_stage_timings`.

### Design
`_process_frame(frame N)` runs four stages. Today they are strictly sequential:

```
court(N) → ball(N) → motion_mask(N) → player(N)
   GPU       GPU         CPU            GPU
```

With overlap ON, the schedule becomes:

```
submit MOG2(N) to worker thread ─┐ (CPU, runs concurrently)
  court(N)  ── GPU ──────────────┤
  ball(N)   ── GPU ──────────────┘
  join MOG2(N).result()  ←──────── (residual wait only)
  player(N) ── GPU (consumes motion_mask(N))
```

MOG2 is `cv2.createBackgroundSubtractorMOG2().apply()` — OpenCV C++ that **releases the GIL** for its duration, so the worker thread genuinely runs on a second core while the main thread issues the court + ball CUDA work. Per-frame wall time goes from `court + ball + mog2 + player` to `max(mog2, court+ball) + player`.

### Data-dependency map (why this is safe)
I traced every consumer of each stage's output:

| Stage output | Consumed by | Reads motion_mask? |
|---|---|---|
| `court` (homography, bbox, corners, projection callables) | `player_tracker.detect_frame` (court_bbox, court_corners, to_court_coords) | n/a |
| `ball` (`ball_tracker.detect_frame`) | post-loop `_postprocess` only | **no** |
| `motion_mask` (MOG2) | `player_tracker.detect_frame` → `_choose_two_players` → `_compute_motion_ratio` (`player_tracker.py:1138/1422`) | — |
| `player` | post-loop `_postprocess` | — |

The **only** consumer of `motion_mask` is the player stage. Court and ball never read it, and they mutate only their own internal state (court detector cache, ball sliding-window buffer). So MOG2(N) is independent of court(N) and ball(N) and can run concurrently with them. The join happens **before** player(N), so the player stage receives the exact `motion_mask(N)` it does today.

### Correctness argument (outputs identical; only the schedule changes)
1. **Same input.** MOG2(N) is applied to `frame N` — the same array the sequential path applies it to. (The frame is not mutated by court or ball; both treat it read-only — confirmed: detectors operate on the raw frame and write to their own state.)
2. **Same state evolution.** MOG2 is stateful (an adaptive background model). The worker is `max_workers=1` and we **join every frame** before issuing the next `submit`, so at most one `apply()` is ever in flight and they execute in strict frame order `0,1,2,…` — the identical sequence to the sequential path. `learningRate=MOG2_LEARNING_RATE` is unchanged. Therefore the background model after frame N is bit-for-bit what it is today, and `motion_mask(N)` is byte-identical.
3. **Same consumer input.** `player_tracker.detect_frame(... motion_mask=motion_mask ...)` receives that identical mask. Everything downstream of the player stage is unchanged.
4. **No torch/CUDA threading hazard.** The worker thread does **no** torch/CUDA work — it only calls OpenCV. All GPU calls (court, ball, player) stay on the main thread, so there is no multi-thread CUDA-context or stream issue. (This is deliberately *not* the "two CUDA streams" version of L10, which would carry real risk; this is the safe CPU-overlap-only subset.)

**One documented edge-case divergence (error frames only):** the sequential path computes MOG2 at *step 3*, after court(1) and ball(2); if court or ball **raises**, MOG2 never runs for that frame. The overlap path submits MOG2 *first*, so on an errored frame MOG2 has already advanced the background model by one `apply()`. The code drains the future on the exception path (so the single-worker queue can't deadlock) but the state has advanced. Impact is bounded and negligible: frame errors are exceptional (capped logging at 5; zero on a clean run), the MOG2 model is adaptive at `learningRate=0.005` (~200-frame memory) so a single extra `apply()` self-heals within a second of video, and the motion_mask only contributes a *bonus* term to far-candidate scoring in `_choose_two_players` (never a hard gate). On a clean run (no frame errors) the outputs are byte-identical. A human validating should confirm `frame_errors == 0` on the validation re-run (it is logged at end of frame processing).

### Expected speedup
Per-frame, the CPU MOG2 (37.6 ms) overlaps with court+ball (≈11.4 + ~0 ball/court GPU ms — court is locked, so effectively ball ≈ 11.4 ms plus the player stage's own GPU which is *after* the join). The overlap hides `min(mog2, court+ball_GPU)` of wall time per frame. With MOG2 = 37.6 ms and the concurrent GPU window (court+ball) ≈ 11-15 ms, the realistic hidden time is **~11-15 ms/frame** (we can only hide as much MOG2 as there is concurrent GPU work to hide it behind; MOG2 is *longer* than court+ball, so court+ball is fully hidden and ~22-26 ms of MOG2 remains as residual).

- Today: ~70 ms/fr.
- Overlap: ~70 − (court+ball hidden) ≈ 70 − 12 ≈ **~58 ms/fr → ~17% main-loop reduction.**

This is **below** the optimistic "approaches max(CPU,GPU)" ceiling because MOG2 (37.6 ms) is *larger* than the concurrent GPU window (court+ball ≈ 12 ms) — you can only overlap the smaller of the two. The headline 25-40% in the plan assumed CPU≈GPU; here CPU dominates, so the realistic win is ~15-20%. The `stage_overlap` log line (added) reports `overlapped_hidden` ms/fr directly so the real number is measured, not guessed, on the first validation run.

> If the measured hidden time is disappointing, the natural follow-up (separate lever, not in this prototype) is to also overlap MOG2(N) with **player(N−1)** — a deeper pipeline that hides MOG2 behind the *player* GPU stage (15 ms) too, getting closer to full `max(CPU,GPU)`. That needs a 1-frame-deep player-input pipeline and is riskier; defer until this simple version is measured.

### Risks
- **Thread overhead per frame.** One `submit`/`result` round-trip per detect frame. Negligible (microseconds) vs the 37 ms MOG2.
- **GIL contention if a future cv2/numpy build stops releasing the GIL on `apply()`.** MOG2 has released the GIL for many OpenCV versions; if a base-image bump regressed this, the overlap would degrade to ~0 gain (not a correctness issue — the `overlapped_hidden` log would show ~0 and we'd revert the flag).
- **Executor lifetime.** Torn down at end of `process()`, re-armed in `reset()` — covered. Batch runs one `process()` per process so this is effectively a no-op there.

---

## TASK 2 — ROI bounce-window TrackNet batching (`ROI_BOUNCE_BATCH`)

### Files
- `ml_pipeline/config.py` — `ROI_BOUNCE_BATCH` flag (default 1 = sequential).
- `ml_pipeline/roi_extractors/bounces.py` — `_ReplayModel` shim, batched-mode crop collection in `feed()`, `_run_window_batched()` (the batched forward + per-frame replay), `_build_v2_input()`, V2/V3 detection in `prepare()`, `finalize()` flush of trailing crops.

### Design
Today `RoiBounceProcessor.feed()` constructs a fresh `BallTracker(model=shared)` per window and calls `detect_frame(crop, idx)` eagerly per frame — TrackNet forward at batch=1. With `ROI_BOUNCE_BATCH>1`:

1. **Collect** (`feed`): store the window's crops (`crop.copy()` — the decoder reuses its frame buffer) instead of inferring eagerly.
2. **Batched forward** (`_run_window_batched`, at window close):
   - *Phase 1* — replicate `BallTracker._detect_frame_v2`'s resize (`640×360`, BGR-kept) + 3-frame sliding buffer to enumerate the exact per-frame model-input tensors the eager path would build, in frame order. (A model call happens once the buffer is full, i.e. for `crops[n-1:]`.)
   - *Phase 2* — concatenate up to `ROI_BOUNCE_BATCH` of those `(1,9,H,W)` tensors into one `(B,9,H,W)` batch, run **one** `model(batched, testing=True)` forward, and slice the `(B,C,H*W)` output back into per-call `(1,C,H*W)` slices.
   - *Phase 3* — replay `detect_frame` on a fresh `BallTracker` whose `.model` is swapped for a `_ReplayModel` that serves those precomputed slices in call order. The **real** `_detect_frame_v2` postprocess (`_postprocess_heatmap`: Hough → connected-component → argmax tiers) and the sequential frame-delta Hough fallback run unchanged.
3. The window then runs `interpolate_gaps` / `detect_bounces` / projection / zone-filter exactly as before.

### Correctness argument (per-window outputs identical)
- **Same forward inputs.** Phase 1 reuses the identical resize + buffer construction as `_detect_frame_v2` (verified line-by-line: `cv2.resize` to `TRACKNET_INPUT_WIDTH×HEIGHT`, `TRACKNET_BGR2RGB` honoured, `/255`, `permute(2,0,1).unsqueeze(0)`, `.half()` on cuda). The batched tensor is just `torch.cat` of those same per-frame tensors.
- **Batching is element-independent.** `BallTrackerNet.forward` is conv + maxpool + upsample + a per-element `Softmax(dim=1)` — every op is batch-element-independent, BatchNorm runs in eval/running-stats (model is `.eval()`), so row `i` of the `(B,…)` output equals the single-frame forward of input `i`. Identical on CPU; within fp-noise on GPU — the **same equivalence already proven and accepted for L1 (`PLAYER_BATCH_SIZE`) and L4 (`ROI_BATCH_SIZE`)**.
- **Postprocess is replayed, not reimplemented.** Phase 3 calls the real `tracker.detect_frame`, so heatmap→(x,y), the buffer warmup (first `n−1` frames return None → no detection, no model call), tier diagnostics, scaling by `scale_x/scale_y`, and the frame-delta fallback all execute exactly as in the eager path. The `_ReplayModel` only substitutes the GPU forward — and `argmax` (used by `_detect_frame_v2`) is invariant to the softmax, so serving the post-softmax slice is exactly correct.
- **Call-count alignment.** The number of model calls = frames with a full buffer = `len(crops) − (n−1)`, and Phase 1 enumerates exactly that many tensors in the same order the replay consumes them. The fallback fires on precisely the same frames in both paths (it is keyed off the heatmap postprocess result, which is identical).
- **No cross-window state leak.** Each window still gets a fresh `BallTracker`; only the shared *model weights* are reused (read-only at inference) — same as today's Bug-2 fix.

### Scope note (V2 only)
The batched forward is implemented for **TrackNet V2** (the production ROI ball model — `tracknet_v3.pt` is absent in prod, confirmed in `config.py`/CLAUDE.md). If the shared model is V3 (8-frame + background, 27-channel, U-Net), `prepare()` logs once and falls back to the **eager per-frame path** for correctness (still no behaviour change vs today). V3 batching would need the background-median + 8-frame-window tensor reconstruction and is deferred until/if V3 weights ship.

### Expected speedup
The ROI bounce stage is ~25 min sequential (~194 windows × ~4-21 s). The TrackNet forward at batch=1 underutilises the T4 on a single `640×360` input. Batching the forward into groups of 8-16 turns N device round-trips into N/B, with the same kernels. Conservatively this matches L4's ROI ViTPose batching profile (~3-5× on the batched-forward portion). The forward is the dominant cost of each window (resize + Hough postprocess are cheap CPU ops, and the frame-delta fallback is CPU but only on no-signal frames). Net: ROI bounce stage **~25 min → ~6-10 min**, i.e. **~15-19 min off the long match**. The per-window timing log already prints `(%.1fs)` per window so the before/after is directly comparable.

### Risks
- **Memory.** A window is ~125 frames (±2.5 s at 25 fps); the collected crops are small service-box crops, and the batched tensors are `B×9×360×640` fp16/fp32 — at B=16 that's well within T4 memory and the recent OOM-hardening headroom. Cap is `ROI_BOUNCE_BATCH`; start at 8.
- **Replay drift if `_detect_frame_v2` changes upstream.** Phase 1 duplicates the tensor-build; if `ball_tracker.py`'s V2 preprocessing ever changes, this duplicate must track it. Mitigated by: (a) the duplication is tiny and commented as a mirror, (b) `ball_tracker.py` is explicitly out of this task's editable set so it won't drift in this change, (c) a divergence would show as different bounce counts in the per-window log on validation.
- **fp-noise on GPU.** Same accepted class as L1/L4 — validated by comparing `roi_prod` row counts (below).

---

## Combined effect on the ~2 h → sub-1 h target

Starting from ~2 h (≈70 ms/fr main loop on ~67k frames ≈ 78 min main loop + ~25 min ROI bounce + pose/transcode):

| Lever | Saving | Notes |
|---|---|---|
| TASK 1 overlap | ~17% of main loop ≈ **~13 min** | measured-not-guessed via `stage_overlap` log; CPU-dominant so below the 25-40% ceiling |
| TASK 2 ROI bounce batch | **~15-19 min** off the ROI bounce stage | stacks with the already-shipped L4 ROI pose batching |

Together ≈ **28-32 min** off the ~2 h, landing around ~1 h 30 m. **Not sufficient alone** to hit sub-1 h — these two close roughly half the remaining gap. The decisive remaining lever is the plan's **L7 (G5.xlarge / A10G)** hardware swap (~1.8-2.2× on top of all software levers, no code change) and/or the **L2 SAHI tile-fan batching** already prototyped behind `SAHI_BATCHED`. The honest framing: TASK 1 + TASK 2 are the last two *software-in-the-loop* levers; after them the path to sub-1 h is hardware (G5) + flipping the already-shipped batched-SAHI flag.

---

## Daylight deploy + validation plan

Both changes are Batch-side (`pipeline.py`, `roi_extractors/bounces.py`, `config.py` are all in the rule #8 glob) → full BATCH-SIDE CHECKLIST. **Do not** merge or deploy overnight (memory `feedback_overnight_branch_only.md`). One lever per commit on `main` once validated.

### Pre-merge (this branch)
1. **Bench is neutral by construction** but still run it: `python -m ml_pipeline.diag.bench` must stay 20/24 (a798eff0) / 23/24 (880dff02). It does not touch `pipeline.py`/`roi_extractors/`, so green is expected — confirms no import-time breakage.
2. `python -m ml_pipeline.diag.bench_ball` — confirms the ROI/ball code still imports and the V2 path is intact (local-only).

### Step A — TASK 1 (`PIPELINE_STAGE_OVERLAP`)
1. Commit `pipeline.py` + `config.py` flag to `main` (one commit), `git push` (rule #7).
2. Docker rebuild → dual-region ECR push (eu-north-1 + us-east-1) → new job-def revisions in **both** regions (rule #8).
3. Re-ingest one production long match (e.g. re-fire a `9378f2dd`-class 67k-frame match) **with `PIPELINE_STAGE_OVERLAP=1`** on the job-def.
4. Read back from `ml_analysis.video_analysis_jobs`: `ms_per_frame` vs the ~70 ms baseline. Grep CloudWatch for the new `stage_overlap … overlapped_hidden=… (… ms/fr saved)` line — that is the direct measured win.
5. **Correctness gate:** `python -m ml_pipeline.harness reconcile <SA_tid> <T5_tid>` on the SA pair — player coverage / hitter attribution must be unchanged (motion_mask feeds `_choose_two_players`, so a regression would show here). Confirm `frame_errors == 0` in the job log (the documented error-frame edge case).
6. If `overlapped_hidden` ≈ 0 (GIL not released / no concurrency), flip the flag back OFF (env-only rollback, no rebuild) and stop — the lever didn't land on this base image.

### Step B — TASK 2 (`ROI_BOUNCE_BATCH`)
1. Commit `roi_extractors/bounces.py` + `config.py` flag to `main` (separate commit), `git push`.
2. Docker rebuild → dual-region ECR push → new job-def revisions (rule #8).
3. Re-ingest the **same** long match with `ROI_BOUNCE_BATCH=8` (and keep `PIPELINE_STAGE_OVERLAP` at whatever Step A concluded).
4. **Correctness gate (the load-bearing check):** compare `roi_prod` bounce rows before/after:
   ```sql
   SELECT source, count(*), count(*) FILTER (WHERE is_bounce)
   FROM ml_analysis.ball_detections
   WHERE job_id = '<tid>' GROUP BY source;
   ```
   The `source='roi_prod'` row count and bounce count must match the eager run within a tiny fp-noise margin (ideally identical; allow ±1-2 rows for GPU fp ordering). A larger delta = the replay diverged → revert and debug Phase 1 tensor reconstruction.
5. Read the per-window `roi_bounces: [k/N] … (%.1fs)` log lines — per-window seconds should drop ~3-5×; total ROI bounce wall time (`batch_duration_sec − processing_time_sec`, minus the pose ROI portion) should drop ~15-19 min.
6. Env-only rollback: set `ROI_BOUNCE_BATCH=1` (no rebuild) if anything looks off.

### Stop-check
After both: re-read `ms_per_frame` + `batch_duration_sec − processing_time_sec`. If still > 1 h on the 67k-frame match (expected), the next move is **L7 (G5.xlarge)** and/or flipping `SAHI_BATCHED=1` — both already prepared, neither in this branch.

---

## Rollback summary
| Flag | OFF value (default) | Rollback cost |
|---|---|---|
| `PIPELINE_STAGE_OVERLAP` | `0` | env-only (no rebuild) |
| `ROI_BOUNCE_BATCH` | `1` | env-only (no rebuild) |

Both default-OFF → the merged code is byte-identical to today until a job-def env flip turns it on.
