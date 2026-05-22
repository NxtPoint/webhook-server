# Phase 5c.4 — Finetuned Ball-Tracker Weights Promotion Playbook

## When to use this playbook

You ran `python -m ml_pipeline.diag.bench_finetuned --weights-path <candidate.pt>`
and it printed `verdict: PROMOTE`. You have a candidate `.pt` file that
materially improves `post_filter_sa_recall` on at least one fixture without
regressing the guardrail metrics on any. Now you ship it to AWS Batch.

If the bench verdict is `NEUTRAL` or `REJECT`: **do not promote**. Iterate on
the training side instead.

## Why this trips guardrail #8

The candidate weights file lives in the Docker image at `ml_pipeline/models/`.
The ball tracker module (`ml_pipeline/ball_tracker.py`, on the Batch-side
file list) reads from `TRACKNET_WEIGHTS` defined in `ml_pipeline/config.py`
(also Batch-side). Both changing the file AND wiring an env-gate require a
Docker rebuild + dual-region ECR push + new job-def revisions. See
`.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST" for the canonical
deploy script.

## The shape of the change — recommended (env-gated)

Use the same pattern as the 2026-05-21 WASB swap (memory:
`feedback_env_var_rollback_pattern.md`). The candidate weights ship into
the image alongside `tracknet_v2.pt`, and `TRACKNET_WEIGHTS` becomes
env-var-overridable. Rollback then doesn't need a Docker rebuild — just
unset the env var on the job-def and re-register.

### Step 1 — Add the env-gate (one-time)

Edit `ml_pipeline/config.py`:

```python
TRACKNET_WEIGHTS = os.getenv(
    "TRACKNET_WEIGHTS",
    os.path.join(MODELS_DIR, "tracknet_v2.pt"),
)
```

This is a no-op for any environment that doesn't set `TRACKNET_WEIGHTS`. It
trips guardrail #8 because `config.py` is on the Batch-side file list — Step 4
deploy is required.

### Step 2 — Ship the candidate weights into the image

Copy `<candidate.pt>` to `ml_pipeline/models/tracknet_v2_ft_<YYYYMMDD>.pt`.

`ml_pipeline/models/` is gitignored (~270 MB of weights live there), so the
file is NOT committed. Instead, the Dockerfile `COPY ml_pipeline/models/ ...`
pulls it into the image at build time. **Check `ml_pipeline/Dockerfile`
includes a COPY of `models/`** — at the time of writing, it does. If you
ship the file via a different mechanism (e.g. pulled from S3 at boot), the
playbook in `.claude/handover_t5.md` §"Models loaded at boot" covers that
path.

### Step 3 — Local bench against the IMAGE-mounted weights (optional but cheap)

Same `bench_finetuned` run as before, but with the candidate path that
matches what Batch will see (`ml_pipeline/models/tracknet_v2_ft_<date>.pt`).
This catches "I moved the file" goofs before they cost a Docker build:

```bash
.venv/Scripts/python -m ml_pipeline.diag.bench_finetuned \
    --weights-path ml_pipeline/models/tracknet_v2_ft_<date>.pt
```

Expect identical PROMOTE verdict to the earlier run.

### Step 4 — Docker rebuild + dual-region ECR push

Standard `BATCH-SIDE CHANGE CHECKLIST` deploy. From repo root, in this order:

```bash
# 1. Auth (run from a shell with AWS creds)
aws ecr get-login-password --region eu-north-1 | docker login --username AWS --password-stdin 696793787014.dkr.ecr.eu-north-1.amazonaws.com
aws ecr get-login-password --region us-east-1  | docker login --username AWS --password-stdin 696793787014.dkr.ecr.us-east-1.amazonaws.com

# 2. Build
docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline:latest .

# 3. Tag + push to BOTH regions (in parallel)
docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker tag ten-fifty5-ml-pipeline:latest 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest
docker push 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-ml-pipeline:latest &
docker push 696793787014.dkr.ecr.us-east-1.amazonaws.com/ten-fifty5-ml-pipeline:latest &
wait

# 4. Extract amd64 sub-manifest digest
MSYS_NO_PATHCONV=1 aws ecr batch-get-image --region eu-north-1 --repository-name ten-fifty5-ml-pipeline \
  --image-ids imageTag=latest \
  --accepted-media-types application/vnd.oci.image.index.v1+json application/vnd.docker.distribution.manifest.list.v2+json \
  --query 'images[0].imageManifest' --output text \
  | python -c "import json,sys; m=json.loads(sys.stdin.read()); [print(x['digest']) for x in m['manifests'] if x['platform']['architecture']=='amd64']"

# 5. Register new job-def revisions in both regions, digest-pinned to the new amd64 sub-manifest.
#    The new job-def revisions inherit env-vars from the previous active revision.
#    AT THIS POINT TRACKNET_WEIGHTS IS NOT YET SET — Batch jobs still use tracknet_v2.pt.
```

Verify the new revisions came up ACTIVE in both regions before proceeding:

```bash
aws batch describe-job-definitions --region eu-north-1 --job-definition-name ten-fifty5-ml-pipeline --status ACTIVE --query 'jobDefinitions[0].revision'
aws batch describe-job-definitions --region us-east-1  --job-definition-name ten-fifty5-ml-pipeline --status ACTIVE --query 'jobDefinitions[0].revision'
```

### Step 5 — Flip the env var on the new job-def

Two paths — the second one is cleaner.

**5a (in-place env-var add):** modify the new job-def revisions in both
regions to set `TRACKNET_WEIGHTS=/app/ml_pipeline/models/tracknet_v2_ft_<date>.pt`.
This requires re-registering the job-def revisions a second time (you can't
edit an active revision in place).

**5b (set env-var in the original register call):** when you run the
register script in step 5 above, set `TRACKNET_WEIGHTS` in the containerProperties
environment block. Result: the SAME revision that ships the new file also
points to it. One register call, not two.

Prefer 5b — it's atomic and leaves a cleaner audit trail.

### Step 6 — End-to-end verification on a real upload

Take a fresh `tennis_singles` upload (or an existing task that you can
rerun-ingest). Watch CloudWatch logs for the line:

```
BallTracker: loaded BallTrackerNet (V2) from /app/ml_pipeline/models/tracknet_v2_ft_<date>.pt
```

vs the previous baseline:

```
BallTracker: loaded BallTrackerNet (V2) from /app/ml_pipeline/models/tracknet_v2.pt
```

If you see the new path, the env-gate is wired and the finetune is live.

Then re-run `python -m ml_pipeline.diag.bench_silver <task_id>` (or check
silver row counts directly) to confirm production bronze density matches
the bench prediction.

## Rollback path

If post-deploy verification shows a regression you didn't see in the bench
(legitimately possible — bench fixtures don't cover every match shape):

1. **First 5 minutes:** unset `TRACKNET_WEIGHTS` on the active job-def, register
   a new revision. New jobs fall back to the default `tracknet_v2.pt`. No
   Docker rebuild needed.
2. **If anything weird:** point the job-def back at the PREVIOUS amd64 digest
   from before this deploy. The old revision's image still exists in ECR
   (we never delete revisions).
3. **Don't `git revert` the config.py env-gate change** — it's a no-op when
   `TRACKNET_WEIGHTS` is unset, and rolling it back would trigger another
   Docker rebuild for no behavioural gain.

## When NOT to use the env-gate path

If the candidate is a strict drop-in replacement (same architecture, same
input shape, same purpose, just better weights) AND you've already shipped
the env-gate before, you can skip steps 1-3 and just replace
`ml_pipeline/models/tracknet_v2.pt` outright. Then Step 4 deploys the new
image. Rollback in this case = revert the job-def to the previous revision
(which is digest-pinned to the old image).

This path skips the env-gate but loses the "switch back without rebuild"
property. Only use it if the rollback risk is genuinely low.

## Pre-flight checklist

Before touching Docker:

- [ ] `bench_finetuned` verdict was PROMOTE, not NEUTRAL or REJECT
- [ ] Candidate `.pt` is actually finetuned weights, not the training-time
      optimiser state file (which is larger and won't load)
- [ ] Tomo has bandwidth to watch the deploy — Batch deploys overnight are
      explicitly discouraged (`.claude/sop.md` §SOP for Batch deploys)
- [ ] No active Batch jobs running (`aws batch list-jobs ... --job-status RUNNING`)
      — deploying mid-job means the in-flight job uses the old image and
      the next one uses the new image, which is fine but worth being
      explicit about

## Related docs

- `.claude/handover_t5.md` §"BATCH-SIDE CHANGE CHECKLIST" — canonical deploy
  script (this playbook borrows the exact commands)
- `.claude/playbook_aws_batch_ondemand_fallback.md` — on-demand capacity
  fallback (relevant if Spot is tight during the deploy)
- `MEMORY.md` → `feedback_env_var_rollback_pattern.md` — why env-gating is
  the preferred pattern for model swaps
- `ml_pipeline/diag/bench_finetuned.py` — the upstream bench that gates entry
  to this playbook
