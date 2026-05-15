# lambda

> AWS Lambda function that fires when a new video lands in S3 `videos/`, creates an `ml_analysis.video_analysis_jobs` row, and submits an AWS Batch job to kick off the T5 ML pipeline.

## What this owns

- The Lambda handler that bridges S3 → Postgres → AWS Batch.
- The deploy script (`deploy.sh`) that packages the function, creates / updates the Lambda, wires up the S3 trigger, and provisions a dead-letter SQS queue.

## What this is NOT

- The ML pipeline itself — that runs in the Batch container (see `ml_pipeline/`, out of scope here).
- A general task router for the platform. Match uploads from the portal go through `POST /api/submit_s3_task` → ingest worker, *not* this Lambda. This Lambda is **only** for the T5 path triggered by raw S3 drops on the `videos/` prefix.

## Files

| File | Purpose |
|---|---|
| `ml_trigger.py` | Lambda handler. S3 ObjectCreated event → insert `video_analysis_jobs` row → submit Batch job → write `batch_job_id` back to the row. |
| `deploy.sh` | Packages handler + `psycopg[binary]` into a zip, creates / updates the Lambda function, configures the S3 notification, creates a DLQ. |

## Entry point

`ml_trigger.handler(event, context)` at `ml_trigger.py:35`.

Triggered by: `s3:ObjectCreated:*` on bucket `nextpoint-prod-uploads`, prefix `videos/`.

## Flow

```
S3 object created at videos/<task_id>/...
        │
        ▼
Lambda handler (ml_trigger.handler)
        │
        ├─ skip if key not under videos/
        ├─ skip if size == 0
        ├─ extract task_id from key path (parts[1] when path is videos/<task_id>/file.mp4)
        ├─ generate job_id (uuid4)
        │
        ├─ INSERT INTO ml_analysis.video_analysis_jobs (job_id, s3_key, task_id, status='queued')
        │     ON CONFLICT (job_id) DO NOTHING
        │
        ├─ batch_client.submit_job(...)  → returns batch_job_id
        │
        └─ UPDATE video_analysis_jobs SET batch_job_id = ..., updated_at = now()
```

On any failure to submit the Batch job, the row is marked `status='failed'` with the error message before re-raising (so the Lambda retries via DLQ if configured).

## Environment variables

Set on the Lambda function itself, not in `render.yaml`. The deploy script writes them.

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection — same DB as Render |
| `S3_BUCKET` | Source bucket name (default: `nextpoint-prod-uploads`) |
| `BATCH_JOB_QUEUE` | AWS Batch job queue name |
| `BATCH_JOB_DEF` | AWS Batch job definition name or ARN |
| `AWS_REGION` | Defaults to `us-east-1` |

## Deploy

```bash
cd lambda
DATABASE_URL=postgresql://… \
  bash deploy.sh
```

What it does (`deploy.sh`):
1. Packages `ml_trigger.py` + `psycopg[binary]==3.1.19` into `/tmp/ml_trigger.zip`
2. Creates or updates the Lambda function `ten-fifty5-ml-trigger`
3. Grants S3 invoke permission
4. Configures S3 event notification on `videos/` prefix
5. Creates SQS dead-letter queue and wires it to the Lambda

Prerequisites:
- AWS CLI configured with credentials that can manage Lambda + S3 notifications + SQS
- IAM role `ten-fifty5-ml-trigger-role` already exists with permissions for: CloudWatch Logs, RDS Postgres connect, AWS Batch submit, SQS write
- `ml_analysis` schema already provisioned in Postgres

## Gotchas

- **Lambda timeout 30s, memory 256 MB.** The handler must be quick — no waiting for the Batch job. It only submits and returns.
- **Database connection per invocation.** No connection pooling — `psycopg.connect()` is called inside each handler. Acceptable because invocations are sparse (one per video upload).
- **Idempotency.** `INSERT … ON CONFLICT (job_id) DO NOTHING` protects against Lambda retries. `job_id` is generated fresh per record — collisions are vanishingly unlikely.
- **`task_id` extraction is path-based.** Assumes keys look like `videos/<task_id>/<filename>.mp4`. Three-or-more-segment paths only; otherwise `task_id` is `NULL`.
- **DLQ retention is 14 days.** `MessageRetentionPeriod=1209600`. If the Lambda fails repeatedly without anyone draining the DLQ, messages expire silently.

## See also

- [`../CLAUDE.md`](../CLAUDE.md) §T5 ML Pipeline — high-level pipeline overview
- `.claude/handover_t5.md` — canonical doc for the T5 pipeline (what runs in the Batch container)
- [`../docs/env_vars.md`](../docs/env_vars.md) — full env-var matrix including the Lambda
