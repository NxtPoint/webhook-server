# AWS Batch On-Demand Fallback Playbook

## Goal

Add an on-demand G4dn.xlarge Compute Environment as a second tier in
`ten-fifty5-ml-queue` so that:
- **Normal case**: Spot CE runs the job (cheapest)
- **Spot eviction**: AWS Batch retries the job; if Spot has no capacity,
  it lands on On-Demand automatically
- **Spot pool empty at submission time**: new jobs go straight to On-Demand

No code changes needed. Python-level region failover (eu-north-1 →
us-east-1) stays as the third tier for full-region outages.

## Region

All changes in **eu-north-1** (Stockholm).

## Prerequisites

- IAM role `AWSBatchServiceRole` already exists (used by the existing CE)
- IAM instance profile `ecsInstanceRole` already exists
- VPC / subnets / security group already wired for the existing Spot CE
  — reuse the same ones

## Step 1 — Create the On-Demand Compute Environment

AWS Console → **Batch → Compute environments → Create**

| Field | Value |
|---|---|
| Compute environment type | Managed |
| Name | `ten-fifty5-ml-ce-eu-ondemand` |
| Service role | `AWSBatchServiceRole` (existing) |
| Instance role | `ecsInstanceRole` (existing) |
| Provisioning model | **On-demand** |
| Instance types | `g4dn.xlarge` (ONLY — don't add optimal/other families) |
| Minimum vCPUs | 0 |
| Desired vCPUs | 0 |
| Maximum vCPUs | 16 (= 4 concurrent jobs max, each needs 4 vCPU) |
| Allocation strategy | `BEST_FIT_PROGRESSIVE` |
| VPC / subnets / SG | **Copy from existing Spot CE** |
| Tags | `Project=TEN-FIFTY5`, `Tier=OnDemand` |

Click **Create**. Wait for `VALID` + `ENABLED` state (~2 min).

## Step 2 — Attach On-Demand CE to the existing queue

AWS Console → **Batch → Job queues → `ten-fifty5-ml-queue` → Edit**

Under **Connected compute environments**:

| Order | Compute environment |
|---|---|
| 1 | `ten-fifty5-ml-ce-eu-spot` (existing — keep at order 1) |
| 2 | `ten-fifty5-ml-ce-eu-ondemand` (NEW — add at order 2) |

**Save**.

AWS Batch's scheduler places jobs on the lowest-order CE with available
capacity. Spot is tried first; if it has no capacity, On-Demand takes
over automatically.

## Step 3 — Update Job Definition retry strategy

AWS Console → **Batch → Job definitions → `ten-fifty5-ml-pipeline` →
Create new revision**

Under **Retry strategy**:

```json
{
  "attempts": 3,
  "evaluateOnExit": [
    {
      "action": "RETRY",
      "onStatusReason": "Host EC2*",
      "onReason": "*"
    },
    {
      "action": "RETRY",
      "onReason": "DockerTimeoutError*"
    },
    {
      "action": "EXIT",
      "onExitCode": "0"
    },
    {
      "action": "EXIT",
      "onReason": "*"
    }
  ]
}
```

This retries on:
- Spot eviction (`Host EC2 was terminated` pattern)
- Docker timeouts (occasional network hiccups pulling from ECR)

But exits immediately on successful completion (code 0) or real errors
(OOM, Python exception — retrying would just burn capacity).

**Register** the new revision. The existing env var `BATCH_JOB_DEF=ten-fifty5-ml-pipeline`
will automatically use the latest revision.

## Step 4 — Verify

Submit a test job via Media Room. Check:
1. Job lands on Spot CE first (check `jobName` in CE's running tasks)
2. `ml_analysis.video_analysis_jobs.status` progresses through stages
3. If Spot evicts, job retries and (if Spot still full) lands on On-Demand

To force a test: temporarily set Spot CE max vCPUs to 0 and submit a job.
It should immediately land on On-Demand. Revert max vCPUs afterwards.

## Cost context

On-Demand G4dn.xlarge (eu-north-1): ~$0.526/hr
Spot G4dn.xlarge (eu-north-1): ~$0.158/hr (70% discount)

Typical job: ~45 min = $0.40 (On-Demand) vs $0.12 (Spot). On-Demand
fallback only fires on reclamation, so marginal cost impact is minimal.

## Rollback

To disable the On-Demand tier: edit the queue, remove the On-Demand CE
from connected compute environments. Jobs fall back to Spot-only
behaviour. No code or env var changes needed.
