# Technique Analysis (`technique/`)

Biomechanics stroke analysis via the external SportAI Technique API. Dev-only — gated to `tomo.stojakovic@gmail.com` in `media_room.html`. Sport type: `technique_analysis`.

## Flow

Unlike SportAI (async + URL polling) and T5 (AWS Batch + sentinel URL), the Technique API is **synchronous streaming**. A single background thread in `upload_app.py::_technique_run_pipeline()` does everything end-to-end:

```
Media Room → /api/submit_s3_task {gameType: 'technique'}
  → _technique_submit() creates task_id, spawns daemon thread:
    1. Download video from S3 (in memory, no intermediate storage)
    2. POST multipart/form-data to TECHNIQUE_API_BASE/process
    3. Read streaming JSON lines until status=done
    4. Bronze ingest → bronze.technique_* tables
    5. Silver build → silver.technique_* tables
    6. Copy video → trimmed/{task_id}/technique.mp4
    7. Mark complete (session_id + ingest_finished_at on submission_context)
    8. SES notify via existing _notify_ses_completion
```

Status tracked via standard `bronze.submission_context` columns (same as SportAI/T5). No in-memory tracker, no sentinel URL, no auto-ingest routing — `_technique_status()` just reads the DB.

## Tables

**Bronze** (`bronze.technique_*`, created by `technique/db_schema.py::technique_bronze_init()`):
- `technique_analysis_metadata` (1 row per task: uid, status, sport, swing_type, dominant_hand, height, warnings, errors)
- `technique_features` (1 row per feature: name, level, score, value, observation, suggestion, ranges, highlight_joints/limbs)
- `technique_feature_categories` (category → score, feature_names)
- `technique_kinetic_chain` (per body segment: peak_speed, peak_timestamp, plot_values)
- `technique_wrist_speed` (raw wrist_speed JSON, 1 row per task)
- `technique_pose_2d` / `technique_pose_3d` (full pose JSON blob, 1 row per task)

**Silver** (`silver.technique_*`, built by `technique/silver_technique.py::build_silver_technique()`):
- `technique_summary` — per-analysis: overall_score, level, top_strength, top_improvement
- `technique_features_enriched` — features joined with category scores + score_vs_category delta
- `technique_kinetic_chain_analysis` — peak ordering/sequencing, speed/time deltas between segments, is_sequential flag
- `technique_pose_timeline` — per-frame 2D+3D consolidated with confidence extraction
- `technique_trends` — cross-session (email-scoped): feature score history per (email, swing_type, feature_name, task_id)

**Gold** (`gold.technique_*`, created by `technique/gold_technique.py::init_technique_gold_views()` — DROP+CREATE pattern like `gold_init.py`):
- `technique_report` — per-analysis complete report (overall_score, category_scores, top_strengths/improvements, all_features as JSON arrays)
- `technique_comparison` — per-feature benchmarks (beginner/intermediate/advanced/professional ranges)
- `technique_kinetic_chain_summary` — simplified: chain_sequence, fastest/slowest segment, duration, is_sequential
- `technique_progression` — cross-session improvement (rolling_avg_5, delta_vs_prev, trend: improving/declining/stable)

## Key files

| File | Purpose |
|---|---|
| `technique/api_client.py` | `call_technique_api(video_bytes, metadata)` — streaming POST, reads JSON lines until status=done/failed |
| `technique/db_schema.py` | Bronze table DDL, idempotent |
| `technique/bronze_ingest_technique.py` | `ingest_technique_bronze(conn, payload, task_id, replace=True)` — extracts JSON into bronze tables |
| `technique/silver_technique.py` | Silver builder — same pattern as `build_silver_v2.py` |
| `technique/gold_technique.py` | Gold view DDL + `init_technique_gold_views()` |
| `technique/coach_data_fetcher.py` | Assembles technique data for LLM Coach (reads gold views) |

## Frontend

Media Room Step 3 `renderTechniqueForm()` collects: sport (currently tennis-only), swing type (12 dropdown options: forehand/backhand drive/topspin/slice, 3 serve types, 2 volleys, overhead), dominant hand toggle, height in cm (converted to mm on submit), date, location.

## Notes

- Unlike SportAI, **no intermediate S3 storage of the JSON result** — the payload stays in memory and goes straight into bronze ingest.
- Swing type list in the form is currently hardcoded; spec says to fetch dynamically from API when available.
- Pickleball sport is recognised by the API but out of scope for this build.
- Video trim is a simple `s3.copy_object` to `trimmed/{task_id}/technique.mp4` — no EDL, no FFmpeg (technique videos are 3-10s).
