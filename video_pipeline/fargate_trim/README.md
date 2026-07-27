# fargate_trim — the video trim as a per-use AWS Batch job

> **Status: LIVE, proven end-to-end in prod on `df594aea` 2026-07-27.**
> **8.6 min wall, 2.19× realtime, ~$0.14**, `trim_status=completed`, 18.9-min reel
> from a 74-min / 8.0 GB source (55 min of dead time cut). Full chain confirmed:
> main API → Batch submit → Fargate encode → S3 → callback → DB.
> (A prior run with the callback disabled measured 7.9 min / 2.40× — same work,
> the spread is normal variance plus container start.)
> The same match on the Render video-worker ran **121 min and never finished**.

## Why this exists

The trim used to run on `nextpoint-video-worker`, an always-on Render Docker
service costing **$25/month whether or not anything was trimming**. Its plan
(starter = 0.5 CPU / 512 MB) could not re-encode a long match's highlight reel
at all — measured 0.17–0.23× realtime, so ~24 min of reel needed ~2 hours.

Encoding is bursty: minutes of heavy CPU, then nothing for days. That is the
worst possible fit for a fixed monthly instance and a natural fit for per-second
billing. Fargate bills per CPU-second, so **16 vCPU for 8 minutes costs about
the same as 0.5 vCPU for 4 hours** — you simply get the answer sooner, and pay
nothing between trims.

It also deletes three failure modes we hit on Render: the 2 GB `/tmp` ceiling,
OOM when two trims overlapped, and the stale-trim sweeps killing healthy long
trims (a Batch job reports its own terminal state).

The trim touches **only S3 and one HTTPS callback — never the database**, which
is why it can run outside the Render VPC without the Postgres IP-allowlist
problem that constrains the ML Batch jobs.

## Shape

```
main API  video_trim_api.trigger_video_trim
            └─ TRIM_BACKEND=batch → fargate_trim.submit.submit_trim_job
                 ├─ PUT  s3://<bucket>/trim-jobs/<task_id>.json   (work order + EDL)
                 └─ batch:SubmitJob  ten-fifty5-trim-queue

Fargate    fargate_trim.runner
            ├─ GET the work order
            ├─ ffmpeg_trim_worker.run_ffmpeg_trim  (unchanged, shared with the HTTP path)
            ├─ PUT trimmed/<task_id>/review.mp4
            ├─ POST the completion callback → main API  /video-trim-complete
            └─ DELETE the work order
```

The EDL goes via S3, not argv/env, so a long match with hundreds of segments has
no size limit. The callback URL + key arrive as container env from the submitter.

## Live AWS resources (eu-north-1, account 696793787014)

| Resource | Name |
|---|---|
| ECR repo | `ten-fifty5-video-trim` |
| Job definition | `ten-fifty5-video-trim` (rev 1, FARGATE, 16 vCPU / 32 GB / 60 GiB ephemeral) |
| Job queue | `ten-fifty5-trim-queue` |
| Compute env | `ten-fifty5-trim-fargate` (FARGATE, maxvCpus 32) |
| Task role | `ten-fifty5-ml-job-role` (existing — S3 + Logs) |
| Execution role | `ten-fifty5-trim-execution-role` (created for ECR pull + logs) |

Networking reuses the ML VPC's three **public** subnets with
`assignPublicIp=ENABLED`, so no NAT gateway is needed (and none is billed).

## Env vars (main API + ingest worker)

| Var | Default | Meaning |
|---|---|---|
| `TRIM_BACKEND` | `batch` | `batch` = Fargate job; `http` = legacy Render worker (rollback) |
| `VIDEO_TRIM_ENABLED` | `1` | `0` = no trims at all (leaves `trim_status` NULL) |
| `TRIM_BATCH_VCPU` | `16` | Fargate allows 0.25/0.5/1/2/4/8/16 |
| `TRIM_BATCH_MEMORY_MB` | `32768` | must be compatible with the vCPU choice |
| `TRIM_BATCH_TIMEOUT_S` | `3600` | Batch-enforced ceiling, independent of `TRIM_ENCODE_TIMEOUT_S` |
| `TRIM_BATCH_ATTEMPTS` | `2` | Batch-level retry; the trim is idempotent (same output key) |
| `TRIM_BATCH_QUEUE` / `TRIM_BATCH_JOB_DEF` / `TRIM_BATCH_REGION` | as above | overrides |

Encode-side knobs (`TRIM_SEEK_INPUTS_PER_PASS`, `TRIM_MAX_HEIGHT`,
`VIDEO_PRESET`, `VIDEO_CRF`, …) live on the **job definition**, not on Render —
change them with a new job-def revision. `TRIM_SEEK_INPUTS_PER_PASS` is 24 here
versus 4 on Render, because 32 GB has room for many concurrent inputs where
512 MB did not.

## Rebuild + redeploy the image

```bash
aws ecr get-login-password --region eu-north-1 \
  | docker login --username AWS --password-stdin 696793787014.dkr.ecr.eu-north-1.amazonaws.com
docker build -f video_pipeline/fargate_trim/Dockerfile \
  -t 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-video-trim:latest .
docker push 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-video-trim:latest
```

The job definition points at `:latest`, so a push is enough — no new revision
needed unless you change resources or env. **Any change to
`ffmpeg_trim_worker.py` needs an image rebuild**; it is baked in, and a Render
deploy alone will NOT update it.

> On PowerShell the `get-login-password | docker login --password-stdin` pipe
> fails with a 400; run that line from Git Bash.

## Diagnostics

```bash
aws batch list-jobs --region eu-north-1 --job-queue ten-fifty5-trim-queue --job-status RUNNING
aws batch describe-jobs --region eu-north-1 --jobs <job_id>
# logs: group /aws/batch/job, stream from describe-jobs .container.logStreamName
```

Every pass logs its rate and a projection
(`pass 2/4 done … (2.92x) | cumulative … projected 11min`), so a too-slow job is
obvious within the first pass rather than after an hour.
