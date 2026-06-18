# video_pipeline

> Async video trim pipeline. Main API builds an EDL from silver, hands it off to a separate FFmpeg worker service, worker re-encodes and uploads `trimmed/{task_id}/review.mp4`, then callbacks back to update `bronze.submission_context.trim_status`.

## What this owns

- The EDL (Edit Decision List) builder that reads `silver.point_detail` and produces a list of keep-segments
- The trigger function `trigger_video_trim(task_id)` that ingest workers call
- The standalone Flask + FFmpeg worker service (separate Render Docker service)
- The completion callback contract (worker → main API)
- The `bronze.submission_context.trim_*` columns (set on boot via `_ensure_trim_columns`)

## What this is NOT

- **Not the `/video-trim-complete` callback handler.** That lives in `upload_app.py` and is the *receiver* for this module's outbound callback. It updates `trim_status`, `trim_output_s3_key`, and fires SES notify if not already sent.
- **Not the EDL business logic.** Padding, merge rules, and minimum-segment thresholds live in `build_video_timeline.py` constants. Python owns the logic; SQL only does the I/O.
- **Not the storage layer.** S3 write is by the worker subprocess; this module only orchestrates.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Package marker |
| `video_trim_api.py` | **Main API side.** `trigger_video_trim(task_id)` — loads silver, builds EDL via `build_video_timeline`, POSTs to worker, sets `trim_status='queued'`. |
| `build_video_timeline.py` | Pure-Python EDL builder. Reads silver, pads point boundaries, merges overlaps, drops too-short segments. No I/O. |
| `video_worker_app.py` | **Worker side (separate Render service).** Flask app. `POST /trim` validates auth + body, spawns detached subprocess, returns 202. |
| `ffmpeg_trim_worker.py` | Subprocess body. Downloads source from S3 → ffprobe → re-encodes per segment → concat → uploads `trimmed/{task_id}/review.mp4` → POSTs callback. |
| `video_worker_wsgi.py` | Gunicorn entry for the worker service. |

## Entry points

| Function | Where | Caller |
|---|---|---|
| `trigger_video_trim(task_id)` | `video_trim_api.py` | Ingest worker step 4 (`ingest_worker_app.py`); T5 ingest in-process (`upload_app.py::_do_ingest_t5`); technique pipeline (`upload_app.py::_technique_run_pipeline`) |
| `build_video_timeline_from_silver(task_id, conn)` | `build_video_timeline.py` | Called by `trigger_video_trim` |
| `timeline_to_edl(df)` | `build_video_timeline.py` | Called by `trigger_video_trim` to convert DataFrame → JSON segments |
| `POST /trim` | `video_worker_app.py:APP` | Main API → worker, async hand-off |
| `run_ffmpeg_trim(task_id, s3_bucket, s3_key, edl, callback_url, callback_headers)` | `ffmpeg_trim_worker.py` | Subprocess spawned by the worker `/trim` handler |

## Cross-service flow

```
─────────── MAIN API ("Sport AI - API call") ───────────────
ingest_worker_app step 4
        │
        ▼
trigger_video_trim(task_id)
        │
        ├─ skip if trim_status in {'completed','accepted','queued'}
        │
        ├─ build_video_timeline_from_silver(task_id, conn)
        │     ├─ SELECT ball_hit_s FROM silver.point_detail WHERE NOT exclude_d
        │     ├─ pad ±2s, merge overlaps, drop <2s segments
        │     └─ DataFrame of keep windows
        │
        ├─ timeline_to_edl(df) → {"segments": [{start, end}, ...]}
        │
        ├─ POST {VIDEO_WORKER_BASE_URL}/trim
        │     headers:  Authorization: Bearer {VIDEO_WORKER_OPS_KEY}
        │     body:     {task_id, s3_bucket, s3_key, edl, callback_url, callback_headers}
        │
        └─ UPDATE submission_context SET trim_status='queued', trim_requested_at=now()

─────────── VIDEO WORKER (Docker service) ──────────────────
POST /trim
        │
        ├─ auth: X-Ops-Key Bearer match against VIDEO_WORKER_OPS_KEY
        ├─ validate body
        ├─ spawn detached subprocess: run_ffmpeg_trim(...)
        └─ return 202 immediately (fire-and-forget)

   subprocess (run_ffmpeg_trim):
        │
        ├─ aws s3 cp s3://{bucket}/{key} /tmp/source.mp4
        ├─ ffprobe duration
        ├─ for each segment: ffmpeg -ss .. -to .. -c:v libx264 -crf 28 ... clip{n}.mp4
        ├─ ffmpeg -f concat -i list.txt -c copy review.mp4
        ├─ aws s3 cp review.mp4 s3://{bucket}/trimmed/{task_id}/review.mp4
        │
        └─ POST {callback_url} headers={callback_headers}
              body={task_id, status: 'completed'|'failed', output_s3_key,
                    source_duration_s, trim_duration_s, segment_count}

─────────── MAIN API ("Sport AI - API call") ───────────────
POST /video-trim-complete  (handler in upload_app.py, NOT this module)
        │
        ├─ auth: VIDEO_TRIM_CALLBACK_OPS_KEY (must equal main API's OPS_KEY)
        ├─ UPDATE submission_context SET trim_status='completed', trim_output_s3_key=...
        └─ if not ses_notified_at → fire video-complete email
```

## Status lifecycle

`bronze.submission_context.trim_status`:

| Status | Set by |
|---|---|
| (NULL) | Initial — no trim attempted |
| `queued` | Main API after successful `POST /trim` |
| `accepted` | Worker's optional pre-callback (rare; usually skipped) |
| `completed` | Worker callback on success — `trim_output_s3_key` set |
| `failed` | Worker callback on FFmpeg / S3 failure — `trim_error` set |

Trigger is idempotent: `queued`, `accepted`, and `completed` all skip re-submission.

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
