# Required Environment Variables

## Main API ("Sport AI - API call" on Render; `name: webhook-server` in `render.yaml`)

**Required** (service boots but degraded without these):
| Env Var | Notes |
|---|---|
| `DATABASE_URL` | Postgres, falls back to `POSTGRES_URL` / `DB_URL`, normalized to `postgresql+psycopg://` |
| `OPS_KEY` | Ops auth, server-to-server |
| `CLIENT_API_KEY` | `/api/client/*` auth |
| `ANTHROPIC_API_KEY` | **LLM Coach** — Claude Sonnet 4.6 via Anthropic SDK |
| `S3_BUCKET` | Uploads, footage, ML bronze JSON, debug frames |
| `AWS_REGION` | Default `us-east-1`. Actual: `eu-north-1` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | implicit boto3 |
| `SPORT_AI_TOKEN` | SportAI API |
| `TECHNIQUE_API_BASE` | **Technique Analysis** — base URL, required when technique module used |
| `TECHNIQUE_API_TOKEN` | Optional bearer token for Technique API (if auth-protected) |
| `INGEST_WORKER_BASE_URL` + `INGEST_WORKER_OPS_KEY` | Worker calls |
| `VIDEO_WORKER_BASE_URL` + `VIDEO_WORKER_OPS_KEY` | Video trim |
| `VIDEO_TRIM_CALLBACK_URL` + `VIDEO_TRIM_CALLBACK_OPS_KEY` | Trim callback (must match main API `OPS_KEY`) |

**Optional** (sensible defaults):
- `SES_FROM_EMAIL` (`noreply@ten-fifty5.com`), `COACH_ACCEPT_BASE_URL`, `LOCKER_ROOM_BASE_URL`, `PLANS_PAGE_URL`
- `SPORT_AI_BASE`, `SPORT_AI_SUBMIT_PATH`, `SPORT_AI_STATUS_PATH`, `SPORT_AI_CANCEL_PATH`
- `AUTO_INGEST_ON_COMPLETE=1`, `INGEST_REPLACE_EXISTING=1`, `ENABLE_CORS=0`
- `MAX_CONTENT_MB=150`, `MULTIPART_PART_SIZE_MB=25`, `S3_PREFIX=incoming` (prod sets to `wix-uploads`), `S3_GET_EXPIRES=604800`
- `BATCH_REGION=eu-north-1` (single-region default), `BATCH_REGIONS_PRIORITY=eu-north-1,us-east-1` (T5 Batch region failover)
- `BATCH_JOB_QUEUE=ten-fifty5-ml-queue`, `BATCH_JOB_DEF=ten-fifty5-ml-pipeline`
- `BILLING_OPS_KEY` (falls back to `OPS_KEY`)
- `SERVE_CNN_BOUNCES=1` — T5 serve detector's bounce source. Default: consume the CNN bounce model (`ml_analysis.ball_bounces`, Batch rev 66+) when the task has rows, falling back to the legacy velocity-reversal `is_bounce` flags when it doesn't (old tasks, fixtures). Set `0` to force the legacy path everywhere (rollback knob, no code change). Validated 2026-06-05 on `60b11b09`: CNN beats legacy on every serve metric (P 53.8 vs 42.9, R 26.9 vs 23.1, ts-err 0.32s vs 1.05s).
- `SERVE_MODEL_ENABLED=1` — T5 serve detector consumes the Batch serve-model candidates (`ml_analysis.serve_candidates`, rev 73+) as ADDITIVE far events (`source='model_far'`; heuristic wins collisions, near path untouched). Set `0` to roll back to heuristic-only (no code change). Validated 2026-06-06 (p10, rev 73): far 3/12 → 7/12, total 20/26 at eval tolerance.
- `SERVE_MODEL_THRESHOLD` — optional override of the serve model's operating point (default: the train-time threshold stored on the candidate rows, currently 0.60). Tunable without a Batch rerun (candidates persist raw above a 0.2 floor).
- ~~`T5_SERVE_FROM_EVENTS`~~ — **DELETED 2026-06-07**: the serve-events overlay is now unconditional and the legacy geometric serve path it toggled against was removed from `build_silver_match_t5.py` (pure bronze import, Tomo directive). Setting the env var does nothing.
- `T5_SERVE_EVENTS_MIN_CONF=0.0` — min serve_event confidence silver inherits. `0.0` = literally everything verbatim (Tomo, 2026-06-06); raise only if a bronze-quality gate is ever needed again.
- `SERVE_MODEL_STAGE=1` — **Batch job-def env, not Render**: run the serve-candidates scoring stage after the ROI sweep (rev 73+). Set `0` on the job-def to skip the stage (rollback, no rebuild).
- `BOUNCE_CNN_THRESHOLD=0.70` — **Batch-side** (`ml_pipeline/__main__.py` bounce stage): CNN score cutoff for `ml_analysis.ball_bounces`. Tuned 2026-06-14 via the offline corpus sweep (`.claude/tmp/bounce_precision_sweep.py`, 5 labelled tasks): default raised 0.5→0.70 → precision 11%→23% (2.1×), over-emission 1.88×→0.78×, −2.5pp recall (recall is training-gated). Lower it to recover recall once the CNN is retrained on sharp-far footage. Env-tunable on the job-def, no Batch rebuild.

**Legacy (Wix payment transition — remove when own payment auth is built):**
`WIX_NOTIFY_UPLOAD_COMPLETE_URL`, `RENDER_TO_WIX_OPS_KEY`, `WIX_NOTIFY_TIMEOUT_S`, `WIX_NOTIFY_RETRIES`

## Other Services

- **Ingest Worker**: `INGEST_WORKER_OPS_KEY` (required — startup crash), `DATABASE_URL`, `VIDEO_WORKER_*` for trim trigger.
- **Video Trim Worker** (Docker): `VIDEO_WORKER_OPS_KEY`, `S3_BUCKET`, `AWS_REGION`, AWS credentials. FFmpeg tunables: `VIDEO_CRF=28`, `VIDEO_PRESET=veryfast`, `FFMPEG_TIMEOUT_S=1800`.
- **Locker Room**: `PORT=5050` only. No DB or S3.
- **Cron `cron_capacity_sweep.py`**: `OPS_KEY`, `DATABASE_URL`, `INGEST_STALE_S=1800`, `TRIM_STALE_S=1800`.
- **Cron `cron_monthly_refill.py`**: `BILLING_OPS_KEY` or `OPS_KEY`.
- **Lambda `lambda/ml_trigger.py`**: `BATCH_JOB_QUEUE`, `BATCH_JOB_DEF`, `DATABASE_URL`.
- **ML Pipeline Docker** (`ml_pipeline/__main__.py`): `S3_BUCKET`, `DATABASE_URL`, `AWS_REGION=us-east-1`.
  - **Batch perf levers** (all in `ml_pipeline/config.py`, set on the detection job-def; each is a `0`/`1`-default zero-risk rollback unless noted). Live rev-80 job-def state shown:
    - `PIPELINE_STAGE_OVERLAP` — code default `0`; **ADOPTED `=1`** on rev-80. Runs MOG2(frame N) on a bounded worker thread concurrently with the court+ball GPU stages of the same frame, joining before the player stage. Only the schedule changes — byte-identical motion_mask.
    - `MOG2_DOWNSCALE` — code default `1`; **ADOPTED `=4`** on rev-80. Runs MOG2 on a 1/N-scaled frame (=4 → ~16× cheaper apply); the motion ratio it feeds is downscale-invariant. Keep the OFF (`=1`) branch as rollback.
    - `SAHI_SKIP_A_FAR_YMAX` — default `5.0` (= unchanged). Widens the far-pose acceptance upper bound that lets a frame skip SAHI when full-frame YOLO already resolved the far player; recommended `8.0` after a far-coverage reconcile.
    - `BALL_BATCH_SIZE` — default `1` (per-frame). `>1` accumulates that many WASB sliding-window inputs into one forward pass (TrackNet ignores it); `8` is a good T4 start.
    - `ROI_BOUNCE_BATCH` — default `1` (eager per-frame). `>1` switches `roi_extractors/bounces.py` to a deferred batched TrackNet forward (V2 only), replaying an identical per-frame postprocess; `8-16` is a good T4 start.
