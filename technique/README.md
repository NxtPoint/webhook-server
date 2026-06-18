# technique

> Biomechanics stroke analysis via the external SportAI Technique API. Bronze → silver → gold pipeline for swing-by-swing technique scores.

**Canonical reference:** [`../docs/business/features.md`](../docs/business/features.md) (Technique Analysis section). This README is the file-level orientation and entry-point map; the business doc covers full flow, tables, and frontend.

## What this owns

- The `bronze.technique_*` table family (created idempotently on boot via `technique_bronze_init()`)
- The `silver.technique_*` analytical layer (built by Python, not SQL views — see `silver_technique.py`)
- The `gold.technique_*` presentation views
- The SportAI Technique API client (streaming JSON over HTTP)
- The data fetcher used by AI Coach when the user opens a technique analysis

## What this is NOT

- **Not the upload route.** That's `POST /api/submit_s3_task` in `upload_app.py` with `sport_type='technique_analysis'`.
- **Not the orchestration thread.** End-to-end pipeline runs in `upload_app.py::_technique_run_pipeline()` (single background thread), which calls into this module for each step.
- **Not a T5 equivalent.** No in-house ML — the actual biomechanics computation happens in the SportAI Technique API. We just ingest the result.
- **Not user-facing yet.** Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Package marker |
| `api_client.py` | `call_technique_api(video_bytes, …)` — streaming POST to `TECHNIQUE_API_BASE/process`, reads JSON lines until status=done |
| `db_schema.py` | `technique_bronze_init()` — idempotent bronze table creation |
| `bronze_ingest_technique.py` | `ingest_technique_bronze(conn, payload, task_id, replace=True)` — extract API JSON into 7 bronze tables |
| `silver_technique.py` | `build_silver_technique()` — Python-driven silver builder (mirrors `build_silver_v2.py` pattern); 5 silver tables including `technique_trends` for cross-session progression |
| `gold_technique.py` | `init_technique_gold_views()` — DROP+CREATE views in a single transaction; 4 gold views |
| `coach_data_fetcher.py` | `fetch_technique_data(task_id)` — assembles compact dict for AI Coach (reads `gold.technique_report` + `gold.technique_kinetic_chain_summary`) |

## Entry points

| Function | Caller |
|---|---|
| `api_client.call_technique_api(video_bytes, filename, sport, swing_type, dominant_hand, player_height_mm, …)` | `upload_app._technique_run_pipeline` |
| `db_schema.technique_bronze_init(engine)` | `upload_app` on boot |
| `bronze_ingest_technique.ingest_technique_bronze(conn, payload, task_id)` | `upload_app._technique_run_pipeline` |
| `silver_technique.build_silver_technique(task_id)` | `upload_app._technique_run_pipeline` |
| `gold_technique.init_technique_gold_views()` | `upload_app` on boot |
| `coach_data_fetcher.fetch_technique_data(task_id)` | `tennis_coach.coach_api._fetch_data_for_task` (when sport_type='technique_analysis') |

## Flow (excerpted from `docs/business/features.md`)

```
Media Room → POST /api/submit_s3_task {gameType: 'technique'}
        │
        ▼
upload_app._technique_run_pipeline (single daemon thread, end-to-end)
        │
        ├─ 1. Download video from S3 (in memory)
        ├─ 2. api_client.call_technique_api(...)  — streaming JSON
        ├─ 3. bronze_ingest_technique.ingest_technique_bronze(...)
        ├─ 4. silver_technique.build_silver_technique(...)
        ├─ 5. s3.copy_object → trimmed/{task_id}/technique.mp4
        ├─ 6. Mark submission_context.ingest_finished_at
        └─ 7. SES notify via _notify_ses_completion
```

No async polling, no AWS Batch, no sentinel URL — synchronous streaming.

## Gotchas

- **Single thread, end-to-end.** If the Render dyno restarts mid-pipeline, the analysis is lost. Acceptable because technique videos are 3–10s and the whole pipeline takes ~30–120s.
- **Payload stays in memory.** No intermediate S3 storage of the API response. Goes straight from streaming response → bronze.
- **Trim is `s3.copy_object`, not FFmpeg.** Source video is already short; just rename to `trimmed/{task_id}/technique.mp4`.
- **Billing routes via `sport_type`.** `billing_import_from_bronze.py` checks `submission_context.sport_type == 'technique_analysis'` and consumes from the *technique* credit pool, not the match pool. Pools never swap.
- **Dev-only gate is in the frontend.** `media_room.html` shows the technique form only for `tomo.stojakovic@gmail.com`. There's no server-side gate beyond the standard entitlement check.
- **Swing type is hardcoded in the form.** 12-option dropdown. Spec says to fetch dynamically from the API when an endpoint exists; today it's a static list in `media_room.html`.

## Required environment variables

| Var | Purpose |
|---|---|
| `TECHNIQUE_API_BASE` | Base URL of SportAI Technique API (no default — required) |
| `TECHNIQUE_API_TOKEN` | Optional bearer token for authenticated endpoints |
| `TECHNIQUE_API_TIMEOUT_S` | Streaming POST timeout (default 300s) |

## See also

- [`../docs/business/features.md`](../docs/business/features.md) (Technique Analysis section) — **canonical reference** (full flow, table schemas, frontend form)
- [`../docs/business/README.md`](../docs/business/README.md) §3 + §5 — technique credit pool rules
- `upload_app.py::_technique_run_pipeline` — orchestration thread
- `tennis_coach/` — uses `coach_data_fetcher.fetch_technique_data` for technique-analysis AI Coach calls
- [`../CLAUDE.md`](../CLAUDE.md) §Technique Analysis
