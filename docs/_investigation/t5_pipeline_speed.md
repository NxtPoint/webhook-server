# T5 Pipeline Speed — per-job runtime profile + ranked speed plan

**Tier:** REFERENCE / investigation
**Dated:** 2026-05-27
**Status:** PLAN TO ACTION LATER (training-stage lever — do NOT action during the build phase)

> **Framing (per `docs/north_star.md` §"★ THE OVERARCHING GOAL").** The project is in
> BUILD phase: get the 18 bronze base fields to ~70-80% with the standard models we
> already have. Training is LAST and self-funding (every production SportAI dual-submit
> is a free training pair). **Pipeline speed is a *training-stage* lever** — it makes the
> free training corpus accumulate faster; it is NOT a reason to pause building and NOT a
> bronze-accuracy fix. This doc is a ranked plan to action *after* "dev done" is signed
> off. Nothing here should be touched while bronze is still being built.

## The problem (as framed by the task)

- SportAI processes a ~40-min match in ~20 min.
- Our T5 pipeline takes ~4h on a ~40-min video (figure supplied by the orchestrator;
  see "DB-timing gap" below — I was unable to independently verify it from the DB this
  session).
- AWS Batch job timeout is 6h. A >1hr video would therefore exceed the timeout and fail.
  This is the corpus-throughput bottleneck for productionising dual-submit.

## DB-timing gap (Task 1 + Task 3 — NOT completed; honest blocker)

I could **not** retrieve the live run progress or historical job timings this session.
Every DB path was unavailable in this environment:

- The Bash and PowerShell tools were both **permission-denied**, so I could not run
  `.venv/Scripts/python -c "from db_init import engine ..."` as the task specified.
- There is no `.env` on disk and `DATABASE_URL` is not readable without a shell, so I
  could not construct a connection by any other means.
- `WebFetch` cannot POST or send auth headers, so it cannot hit the read-only
  `POST /ops/diag/sql` endpoint either.

**To finish Tasks 1 & 3, a session with shell access should run** (read-only):

```sql
-- Task 1: current run progress (the ~40-min match paired with SA ee12d918-...)
SELECT job_id, task_id, status, total_frames, video_duration_sec,
       processing_time_sec, ms_per_frame, batch_duration_sec, created_at, updated_at
FROM ml_analysis.video_analysis_jobs
ORDER BY created_at DESC
LIMIT 5;

-- how far along the in-flight job is (substitute its job_id)
SELECT COUNT(*) AS ball_rows, MAX(frame_idx) AS max_frame
FROM ml_analysis.ball_detections
WHERE job_id = '<job_id>';

-- Task 3: real per-frame cost from completed jobs
SELECT job_id, total_frames, video_duration_sec, processing_time_sec,
       ms_per_frame, batch_duration_sec,
       ROUND(batch_duration_sec - processing_time_sec) AS roi_plus_transcode_sec
FROM ml_analysis.video_analysis_jobs
WHERE status = 'complete' AND ms_per_frame IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;
```

**Critical interpretation note for whoever runs the above** (verified from
`ml_pipeline/db_writer.py` + `ml_pipeline/__main__.py`):

- `processing_time_sec` / `ms_per_frame` are recorded by `AnalysisResult` inside
  `pipeline.process()` — they cover **only the main per-frame loop** (court + ball +
  MOG2 + player). They do **NOT** include the two post-pipeline whole-video passes.
- `batch_duration_sec` is the **full** Batch wall-clock (download → main pipeline →
  ROI pose → ROI bounces → heatmaps → transcode → S3 upload).
- So `batch_duration_sec − processing_time_sec` ≈ the cost of the ROI ViTPose pass +
  ROI TrackNet pass + transcode. That delta is the cheapest, highest-confidence way to
  confirm how much of the ~4h lives in the second/third video decodes vs the main loop.

## Static profile — where per-frame time goes (verified from code)

The Batch job decodes the video **three times** end-to-end. This is the headline finding.

### Pass A — main per-frame loop (`ml_pipeline/pipeline.py::_process_frame`)

Runs at `FRAME_SAMPLE_FPS = 25` for match (`config.py:26`; practice = 10).
Four stages per sampled frame, in this order:

| Stage | What runs | Cadence | Cost character |
|---|---|---|---|
| `court` | ResNet50 court keypoints | CNN every 30 fr for first 300 fr, then **homography LOCKED** (`court_detector.py:162` returns cached) | ~0 after frame 300 — **not a bottleneck** |
| `ball` | TrackNet V2 **or** WASB (env `BALL_TRACKER`) | **every frame** | GPU fwd pass, **batch=1**, fp16 on cuda. TrackNet 640×360, WASB 512×288 |
| `motion_mask` | MOG2 background subtraction | every frame | CPU, cheap |
| `player` | YOLOv8x-pose full-frame + optional SAHI | **every `PLAYER_DETECTION_INTERVAL = 5` frames** (`config.py:190`); reuses last result on the other 4 | the **dominant** GPU cost when it runs |

**Player stage is the dominant per-frame cost.** Per detection frame
(`player_tracker.py`):
- Full-frame **YOLOv8x-pose at `YOLO_IMGSZ = 1280`** (`config.py:158`) — `~133MB` model,
  4× the pixels of the 640 default. One `model.predict()` per frame, **batch=1**.
- Then **SAHI tiled inference** (`SAHI_ENABLED = True`, `config.py:174`) unless the
  skip rule fires: SAHI slices the court-ROI into overlapping 640×640 tiles
  (`SAHI_SLICE_*`) and runs YOLOv8m **per tile**, then NMS-merges. When it runs this is
  ~hundreds of ms (the code comments cite "~300ms of tiled compute"). The skip rule
  (predicates A/B in `detect_frame`) is specifically there to avoid SAHI when full-frame
  pose already found both players — its hit rate is the single biggest swing in
  player-stage cost, and is already instrumented via `_sahi_run_count` /
  `_sahi_skip_count` and the `player_sub` log line.
- The pipeline already emits a `stage_timings` + `player_sub` breakdown every 100 frames
  (`pipeline.py::_log_stage_timings`) — **CloudWatch logs for any recent job already
  contain the real per-stage ms/frame and the `full_yolo` vs `sahi` vs `choose2` split.**
  Pulling one of those log lines is the fastest way to ground every estimate below.

### Pass B — far-player ROI pose (`roi_extractors/pose.py::extract_far_pose`)

Runs after the main pipeline, **match only** (`__main__.py:197`). **Re-opens and
re-decodes the whole video.** Samples `sample_every = 2` (12.5 fps effective). For each
sampled frame in the far-baseline ROI: YOLOv8m det (`imgsz=1280`) → **ViTPose-Base**
(`usyd-community/vitpose-plus-base`) on the expanded crop, **batch=1**. A rally-state
gate skips IN_RALLY frames, which cuts the ViTPose count materially, but the **video
decode + per-frame YOLO det still touches ~half of all frames**.

### Pass C — service-box ROI bounces (`roi_extractors/bounces.py::extract_far_bounces`)

Runs after Pass B, match only (`__main__.py:224`). Anchors on in-memory bounces, builds
±2.5s windows, and runs a fresh `BallTracker` (TrackNet) over the service-box crop for
each window. **Re-decodes the video per window** via `cap.set(CAP_PROP_POS_FRAMES, ...)`
seeks. The TrackNet model is loaded **once** and shared across windows (the "Bug 2" fix,
`bounces.py:457` — a prior version reloaded weights per window for a ~7× slowdown that
already timed out long matches). Cost scales with (#windows × window length).

### Frame I/O (`video_preprocessor.py::frames`)

`cap.read()` is called on **every source frame** even when downsampling. At 30fps source
→ 25fps target it still decodes all 30 and discards 5 (`frames()` yields on a sampling
accumulator but never seeks). Decode is a real, non-trivial CPU cost paid **three times**
(once per pass A/B/C). It is not GPU-bound, so it does not overlap the GPU work.

### Structural finding: batch=1 everywhere

Every detector does `...unsqueeze(0)...` — **one frame per GPU forward pass** across
TrackNet (`ball_tracker.py:304`), WASB (`wasb_ball_tracker.py:210`), YOLO
(`predict()` one frame), and ViTPose (`pose.py:375`). On a G4dn T4 the small ball models
(512×288 / 640×360) leave the GPU badly underutilized at batch=1 — this is the largest
*structural* speedup available and it costs **no accuracy** (identical math, just wider
batches).

## Ranked speed levers

Ranked by **(win × low accuracy-risk × low effort)**. "Batch vs Render" = which service
the change lands in (the per-frame detection all runs on **AWS Batch**; serve detection +
silver build run on Render and are not the bottleneck here).

| # | Lever | Expected speedup | Accuracy/quality RISK | Where | Effort |
|---|---|---|---|---|---|
| **1** | **Single-decode architecture: fuse Pass B (ROI pose) and Pass C (ROI bounces) into Pass A's frame loop** so the video is decoded **once**, not three times. ViTPose/ROI-TrackNet run on the same decoded frame that's already in memory. | **High** — removes 2 of 3 full/near-full video decodes + their redundant per-frame YOLO det. If the ROI passes are a large share of `batch_duration − processing_time` (confirm via DB), this is the biggest wall-clock cut with zero model change. | **None** — same models, same frames, same outputs; only the decode/scheduling changes. | Batch | Med-High (refactor of `__main__.py` orchestration + ROI extractors to accept in-memory frames) |
| **2** | **GPU batching: run TrackNet/WASB and YOLO with batch>1** (e.g. accumulate 8-16 frames, one forward pass). T4 is idle at batch=1 on 512×288/640×360 inputs. | **High** for ball (every-frame); **Med** for player (every-5th). | **None** — identical per-frame math; outputs bit-for-bit equivalent (modulo fp accumulation). | Batch | Med (buffer + reshape in `detect_frame`; YOLO `predict` already accepts a list) |
| **3** | **Tune/curtail SAHI** — raise the skip-rule hit rate, or gate SAHI to only frames where full-frame pose is missing the far player; or drop `SAHI_OVERLAP_RATIO` / widen `SAHI_SLICE_*` for fewer tiles. | **Med** — SAHI is hundreds of ms when it runs; skipping more of it directly cuts the dominant player-stage cost. | **Med** — SAHI exists to catch the ~30-40px far player that full-frame YOLOv8x-pose under-resolves. Cutting it risks far-player recall, which is already a weak field (far pose sparse — see north_star build table). **Measure recall on the bench fixtures before/after.** | Batch | Low-Med (config + skip-predicate tuning; already instrumented via `_sahi_*` counters) |
| **4** | **fp16 / smaller YOLO for the player pass** — YOLOv8x-pose at imgsz=1280 is the heaviest single op. Options: imgsz 960 instead of 1280, or fp16 inference, or YOLOv8m-pose for the full-frame pass. | **Med** | **Med-High** — `YOLO_IMGSZ=1280` and the 0.10 confidence floor were *deliberately* set to recover the far/near player on GPU (`config.py:146-158` documents a GPU-FP16-suppression episode). Lowering imgsz or model size directly attacks the same small-player detection these were tuned to fix. High regression risk on player coverage. | Batch | Low (config) but **needs reconcile vs SA before/after** |
| **5** | **Increase `PLAYER_DETECTION_INTERVAL` (5 → 7-8)** — run YOLO+SAHI on fewer frames, reuse the cached result between. | **Med** (player stage is dominant; fewer runs scale ~linearly). | **Med** — already at 5 (was 3); the config comment warns further increases "risk missing serve impact positions" — i.e. the player bbox at contact drifts, hurting hitter attribution + serve detection (both already weak fields). | Batch | Low (config) |
| **6** | **Lower `FRAME_SAMPLE_FPS` (25 → ~15-18) for the ball pass** — fewer frames overall. | **High** (touches the every-frame ball stage + everything else linearly). | **HIGH — flag honestly.** Lowering sample FPS worsens bounce timing (already ~0.5s loose per `bounce_accuracy.md`) and ball/event temporal resolution. Bounce x/y is the **single weakest base field** (recall 55% / precision 27% / 4.57m err per north_star). This trades directly against the field we most need to *improve*. **Do not pull during build; reconsider only post-train if bounce timing has headroom.** | Batch | Low (config) — but accuracy cost is real |
| **7** | **Frame I/O: seek/skip instead of decode-all when downsampling**, and/or hardware-accelerated decode (NVDEC on the T4). | **Low-Med** (decode is CPU, paid 3× today — but #1 already removes 2 of the 3). | **None** (same frames delivered). | Batch | Med (codec/seek handling is fiddly; `frames()` currently decodes-then-discards) |

## The single biggest win + its risk

**Lever #1 — decode the video once.** The pipeline currently decodes the full video
three times (main loop, then ROI ViTPose at ~12.5fps, then ROI TrackNet windows), each
with its own per-frame YOLO/det work. Fusing the two ROI passes into the main frame loop
removes two whole video traversals and their redundant detection, at **zero accuracy cost**
(identical models on identical frames). It is the largest wall-clock reduction that does
not touch a single model or threshold — so it cannot regress any of the 18 bronze fields.

The honest caveat: I could not measure the ROI passes' share of `batch_duration_sec` this
session (DB blocked). **Confirm with the `batch_duration_sec − processing_time_sec` query
above before investing the refactor** — if that delta is small, the win shifts to Lever #2
(GPU batching), which is also accuracy-neutral and the next-best structural lever.

The highest-*accuracy-risk* lever, to avoid during build, is **#6 (lower FRAME_SAMPLE_FPS)**:
it speeds everything up linearly but directly degrades bounce timing and temporal
resolution — and bounce x/y is the weakest field we are still trying to build up. Speed
must not be bought with the accuracy the build phase exists to create.
