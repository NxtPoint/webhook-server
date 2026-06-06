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
- `T5_SERVE_FROM_EVENTS=1` — T5 silver inherits `ml_analysis.serve_events` verbatim (north_star RULE #1). Default ON since 2026-06-06 — it had shipped default-OFF on 2026-05-27 and the Render env flip never landed, so prod silver silently ran the legacy geometric serve path for 10 days. Set `0` to roll back.
- `T5_SERVE_EVENTS_MIN_CONF=0.0` — min serve_event confidence silver inherits. `0.0` = literally everything verbatim (Tomo, 2026-06-06); raise only if a bronze-quality gate is ever needed again.
- `SERVE_MODEL_STAGE=1` — **Batch job-def env, not Render**: run the serve-candidates scoring stage after the ROI sweep (rev 73+). Set `0` on the job-def to skip the stage (rollback, no rebuild).

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
