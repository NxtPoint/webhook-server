# video_pipeline

> Async video trim pipeline. Main API builds an EDL from silver, hands it to an
> encoder, which re-encodes and uploads `trimmed/{task_id}/review.mp4`, then
> calls back to update `bronze.submission_context.trim_status`.

> **★ Since 2026-07-27 the encoder is a per-use AWS Batch (Fargate) job**
> (`fargate_trim/`, `TRIM_BACKEND=batch` = default). The always-on Render
> video-worker service is **SUSPENDED** and kept only as the `TRIM_BACKEND=http`
> rollback. Prod: 8.6 min / 2.19x realtime / ~$0.14 for a 74-min match, versus
> 121 min and never finishing on the Render worker.
> **`fargate_trim/README.md` is the trim runbook — read it first.**

## What this owns

- The EDL (Edit Decision List) builder that reads `silver.point_detail` and produces a list of keep-segments
- The trigger function `trigger_video_trim(task_id)` that ingest workers call
- The two encoder backends: `fargate_trim/` (live, AWS Batch) and the standalone Flask worker service (suspended, rollback only)
- The completion callback contract (worker → main API)
- The `bronze.submission_context.trim_*` columns (set on boot via `_ensure_trim_columns`)

## What this is NOT

- **Not the `/video-trim-complete` callback handler.** That lives in `upload_app.py` and is the *receiver* for this module's outbound callback. It updates `trim_status`, `trim_output_s3_key`, and fires SES notify if not already sent.
- **Not the EDL business logic.** Padding, merge rules, and minimum-segment thresholds live in `build_video_timeline.py` constants. Python owns the logic; SQL only does the I/O.
- **Not the storage layer.** S3 write is done by the encoder; this module only orchestrates.
- **Not deployed by `git push` (the encode half).** `ffmpeg_trim_worker.py` is baked into the ECR image the Fargate job runs — changing it needs a Docker rebuild + ECR push. See `fargate_trim/README.md`.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Package marker |
| `video_trim_api.py` | **Main API side.** `trigger_video_trim(task_id)` — loads silver, builds the EDL, dispatches to the chosen backend, marks `trim_status='accepted'`. Also `pad_single_shot_points()`, which stops single-shot points (ACES) being dropped. |
| `fargate_trim/` | **The live encoder backend.** `submit.py` (main-API side: work order → S3, `batch:SubmitJob`), `runner.py` (container entrypoint), `Dockerfile`, and **`README.md` — the runbook**. |
| `build_video_timeline.py` | Pure-Python EDL builder. Reads silver, pads point boundaries, merges overlaps, drops too-short segments. No I/O. |
| `video_worker_app.py` | **Legacy worker (Render service, SUSPENDED).** Flask app; `POST /trim` spawns a detached subprocess, returns 202. Holds the PID-checked duplicate-trim lock. Rollback path only. |
| `ffmpeg_trim_worker.py` | Subprocess body. Streams (or downloads, if small) the S3 source → ffprobe → encodes with **one `-ss/-t` seek input per kept segment** + `concat` → uploads `trimmed/{task_id}/review.mp4` → POSTs callback. Runtime scales with the *highlight* length, not the match length — see the header comment for the two designs this replaced. |
| `tests/test_trim_cmd.py` | Arg/segment math, no deps: `python -m video_pipeline.tests.test_trim_cmd`. |
| `tests/test_trim_lock.py` | In-flight duplicate-trim lock (acquire / refuse / stale takeover). |
| `tests/test_timeline_aces.py` | Proves single-shot points (aces) reach the reel. Needs pandas → run in a container. |
| `tests/e2e_trim_docker.py` | Real-ffmpeg end-to-end over a synthetic clip, run in Docker (the dev box has no ffmpeg). Covers seek accuracy + the multi-pass concat join. |
| `video_worker_wsgi.py` | Gunicorn entry for the worker service. |

## Entry points

| Function | Where | Caller |
|---|---|---|
| `trigger_video_trim(task_id)` | `video_trim_api.py` | Ingest worker step 4 (`ingest_worker_app.py`); T5 ingest in-process (`upload_app.py::_do_ingest_t5`); technique pipeline (`upload_app.py::_technique_run_pipeline`) |
| `build_video_timeline_from_silver(task_id, conn)` | `build_video_timeline.py` | Called by `trigger_video_trim` |
| `timeline_to_edl(df)` | `build_video_timeline.py` | Called by `trigger_video_trim` to convert DataFrame → JSON segments |
| `submit_trim_job(...)` | `fargate_trim/submit.py` | **Live path** — called by `trigger_video_trim` when `TRIM_BACKEND=batch` |
| `POST /trim` | `video_worker_app.py:APP` | Legacy path (`TRIM_BACKEND=http`), service suspended |
| `run_ffmpeg_trim(task_id, s3_bucket, s3_key, edl, callback_url, callback_headers)` | `ffmpeg_trim_worker.py` | Subprocess spawned by the worker `/trim` handler |

## Cross-service flow (live = Batch backend)

```
─────────── MAIN API / INGEST WORKER ───────────────────────
trigger_video_trim(task_id)
        │
        ├─ VIDEO_TRIM_ENABLED=0 ? → return 'disabled', write NOTHING (status stays NULL)
        ├─ skip if trim_status in {'queued','accepted','processing'} (or 'completed')
        │
        ├─ load silver points  →  pad_single_shot_points()   ← keeps ACES in the reel
        ├─ build_video_timeline_from_silver()  → pad ±2s, merge overlaps, drop <2s
        ├─ timeline_to_edl(df) → {"segments": [{start_s, end_s}, ...]}
        │
        ├─ TRIM_BACKEND=batch (default):
        │     ├─ PUT s3://{bucket}/trim-jobs/{task_id}.json   (work order incl. EDL)
        │     └─ batch:SubmitJob  queue=ten-fifty5-trim-queue  def=ten-fifty5-video-trim
        │           env: TRIM_JOB_S3, TRIM_CALLBACK_URL, TRIM_CALLBACK_OPS_KEY
        │
        ├─ TRIM_BACKEND=http (rollback, service suspended):
        │     └─ POST {VIDEO_WORKER_BASE_URL}/trim  → 202, detached subprocess
        │
        └─ UPDATE submission_context SET trim_status='accepted', trim_requested_at=now()
              (only AFTER the hand-off succeeds — no phantom rows)

─────────── AWS FARGATE (per-use job, ~8 min) ──────────────
fargate_trim.runner
        │
        ├─ GET the work order from S3
        ├─ run_ffmpeg_trim(...)   ← identical code on both backends
        │     ├─ source: presigned-URL stream, or download if < TRIM_LOCAL_COPY_MAX_MB
        │     ├─ ffprobe duration / has_audio / fps / height (metadata only)
        │     ├─ per pass (<= TRIM_SEEK_INPUTS_PER_PASS segments):
        │     │     ffmpeg -ss s1 -t d1 -i SRC  -ss s2 -t d2 -i SRC ... concat
        │     │     → ONE seek input per segment: ffmpeg SEEKS (HTTP range) rather
        │     │       than decoding forward, so only the kept ~30% is decoded
        │     ├─ if >1 pass: ffmpeg -f concat -i parts.txt -c copy   (no re-encode)
        │     └─ PUT trimmed/{task_id}/review.mp4
        ├─ POST {callback_url}  {task_id, status, output_s3_key, durations, counts}
        └─ DELETE the work order   (kept on failure, as the record of the attempt)

─────────── MAIN API ───────────────────────────────────────
POST /video-trim-complete   (handler in upload_app.py, NOT this module)
        ├─ auth: VIDEO_TRIM_CALLBACK_OPS_KEY (must equal main API's OPS_KEY)
        ├─ UPDATE trim_status='completed', trim_output_s3_key=…, durations
        └─ if not ses_notified_at → fire video-complete email
```

## Status lifecycle

`bronze.submission_context.trim_status`:

| Status | Set by |
|---|---|
| (NULL) | No trim attempted — **also what `VIDEO_TRIM_ENABLED=0` leaves.** The SPAs treat NULL as "no reel, show the original video", so it degrades cleanly |
| `queued` | Legacy/transitional; the current path goes straight to `accepted` |
| `accepted` | Main API, after the Batch job is submitted (or the worker returns 202) |
| `completed` | Completion callback — `trim_output_s3_key` + durations set |
| `failed` | Completion callback on encode/S3 failure (`trim_error`), **or** a staleness sweep giving up. A sweep `failed` does NOT prove the encode died — check the job/worker log |

Trigger is idempotent: `queued`, `accepted`, `processing` and `completed` all skip
re-submission. `/ops/retrim` clears the status to force a re-fire (it does not
reset `trim_attempts`).

## Tunable EDL constants

In `build_video_timeline.py`:

| Constant | Default | Purpose |
|---|---|---|
| `PAD_BEFORE_S` | 2 | Seconds prepended to each point start |
| `PAD_AFTER_S` | 2 | Seconds appended to each point end |
| `MERGE_GAP_S` | 0 | 0 = merge overlaps only; >0 would also merge close-but-disjoint segments |
| `MIN_SEGMENT_S` | 2 | Segments shorter than this are dropped |

In `ffmpeg_trim_worker.py` (env-var overridable):

| Var | Default | Purpose |
|---|---|---|
| `VIDEO_CRF` | `28` | H.264 quality (lower = better quality, larger file) |
| `VIDEO_PRESET` | `veryfast` | Encoding speed/efficiency tradeoff |
| `AUDIO_BITRATE` | `96k` | AAC audio bitrate |
| `MIN_KEEP_SEGMENT_S` | `0.25` | Hard floor; below this FFmpeg gets unstable |
| `FFMPEG_TIMEOUT_S` | `1800` | Per-segment encode ceiling (30 min) |
| `FFPROBE_TIMEOUT_S` | `60` | Source duration probe ceiling |
| `TRIM_MIN_DISK_FREE_MB` | `500` | Pre-flight free-disk check |

## Gotchas

- **Two services, two keys.** `VIDEO_WORKER_OPS_KEY` authenticates main → worker `POST /trim`. `VIDEO_TRIM_CALLBACK_OPS_KEY` authenticates worker → main `POST /video-trim-complete` and **must equal** the main API's `OPS_KEY`. They are separate env vars and changing one without the other breaks the loop.
- **Fire-and-forget.** Worker returns 202 the instant the subprocess is spawned. There is no "still working" status — the only signals are the eventual completion callback or `trim_status` staying `queued`.
- **Callback retry with exponential backoff.** Worker tries 3 times with `2s, 4s, 8s` waits (`CALLBACK_MAX_RETRIES`, `CALLBACK_RETRY_BASE_S`). If all fail, the trim is silently lost — `trim_status` stays `queued` until manually retried.
- **EDL ignores excluded points.** `WHERE exclude_d = false`. Points marked excluded in silver (e.g. timeouts, replays) don't appear in the trimmed video.
- **Trim source is per-pipeline.** SportAI/T5 trim from the original upload (`s3_key`). Technique pipeline trims from `trim_output_s3_key` (the API-produced practice MP4). Practice for T5 is the practice MP4, not the deleted original.
- **Codec re-encode is mandatory.** `-c copy` doesn't honor `-ss`/`-to` precisely on non-keyframe boundaries. We re-encode H.264 with `-crf 28 -preset veryfast` to get frame-accurate cuts. Costs CPU.
- **Concat uses FFmpeg concat demuxer.** Per-segment files are listed in `list.txt` and concatenated with `-f concat -c copy` (segments already match codec params, so this part doesn't re-encode).
- **Subprocess logs persist in `/tmp/trim_logs`.** Useful for debugging failed trims; rotation is not implemented (rely on container ephemeral disk to clear).

## Required environment variables

Main API side:

| Var | Purpose |
|---|---|
| `VIDEO_WORKER_BASE_URL` | Worker service base URL (no trailing slash) |
| `VIDEO_WORKER_OPS_KEY` | Bearer auth for outbound `/trim` |
| `VIDEO_TRIM_CALLBACK_URL` | Where the worker calls back (typically main API `/video-trim-complete`) |
| `VIDEO_TRIM_CALLBACK_OPS_KEY` | Auth for inbound callback (must equal main API's `OPS_KEY`) |
| `VIDEO_WORKER_REQUEST_TIMEOUT_S` | Outbound request timeout (default 10s — must not block ingest) |
| `S3_BUCKET` | Fallback when `submission_context.s3_bucket` is null |

Worker side:

| Var | Purpose |
|---|---|
| `VIDEO_WORKER_OPS_KEY` | Auth for inbound `/trim` |
| `VIDEO_TRIM_CALLBACK_TIMEOUT_S` | Callback POST timeout (default 20s) |
| `VIDEO_TRIM_CALLBACK_MAX_RETRIES` | Default 3 |
| `VIDEO_TRIM_CALLBACK_RETRY_BASE_S` | Default 2.0 |
| `FFMPEG_BIN`, `FFPROBE_BIN` | Defaults `ffmpeg`, `ffprobe` (in container PATH) |
| AWS keys | For S3 download/upload |

## See also

- [`../CLAUDE.md`](../CLAUDE.md) §Video Trim Pipeline
- [`../docs/business/env-vars.md`](../docs/business/env-vars.md) — full env-var matrix including the worker
- `upload_app.py::/video-trim-complete` — the main-API callback receiver
- `ingest_worker_app.py` step 4 — primary caller
- `Dockerfile.worker` — container build for the video worker service
