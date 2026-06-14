"""Submit a per-fact GPU training job to AWS Batch — the seamless path.

ONE command trains a fact on GPU without a box to start/stop or an SSH:

    python -m ml_pipeline.training.submit_train_job --fact swing
    python -m ml_pipeline.training.submit_train_job --fact bounce --epochs 60
    python -m ml_pipeline.training.submit_train_job --fact serve

It submits to the existing GPU compute environments (Spot/on-demand g4dn/g5
behind ten-fifty5-ml-queue) using the training job-definition
`ten-fifty5-ml-train`. The job runs ml_pipeline.training.batch_train inside
the training image, trains on the T4/A10G, and uploads the new weights to
s3://nextpoint-prod-uploads/training/weights/<fact>/_latest/.

Then pull them down for the next detection rebuild:

    python -m ml_pipeline.training.submit_train_job --fact swing --download

This script does NOT register the job-def or push the image — that is the
one-time setup (see register-jobdef / build below and
.claude/training_environment.md). It assumes ten-fifty5-ml-train:<rev> exists.

Sub-commands:
    --fact <f>            submit a training job for fact f and print the jobId
    --download --fact <f> download the latest trained weights into models/
    --register-jobdef    (one-time) register/update ten-fifty5-ml-train pinned
                         to the current training image digest (requires the
                         image already pushed to ECR; see .claude doc)
    --status <jobId>     print one Batch job's status

Monitor a submitted job with the run dashboard (Tomo self-serves) or:
    aws batch describe-jobs --region eu-north-1 --jobs <jobId> \\
        --query 'jobs[0].[status,statusReason]'
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("submit_train_job")

REGION = "eu-north-1"
JOB_QUEUE = "ten-fifty5-ml-queue"
JOB_DEF = "ten-fifty5-ml-train"
S3_BUCKET = "nextpoint-prod-uploads"
S3_WEIGHTS_PREFIX = "training/weights"
ECR_REPO = "ten-fifty5-ml-train"
ACCOUNT = "696793787014"

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
WEIGHT_FILES = {
    "serve": "serve_model_v1.pt",
    "hit": "hit_model_v1.pt",
    "bounce": "bounce_detector_v2_7match.pt",
    "swing": "swing_classifier_v2.pt",
}

# Resource asks per fact. swing (R(2+1)D) wants the GPU + headroom; the coord
# MLPs barely need it but run on the same GPU node for parity.
FACT_RESOURCES = {
    "serve": {"vcpus": 4, "memory": 15360, "gpu": 1},
    "hit": {"vcpus": 4, "memory": 15360, "gpu": 1},
    "bounce": {"vcpus": 4, "memory": 15360, "gpu": 1},
    "swing": {"vcpus": 4, "memory": 15360, "gpu": 1},
}


def _batch():
    import boto3
    return boto3.client("batch", region_name=REGION)


def _build_command(args) -> list[str]:
    cmd = ["--fact", args.fact, "--epochs", str(args.epochs)]
    if args.fact == "swing":
        if args.skip_dataset:
            cmd.append("--skip-dataset")
        if args.focal:
            cmd.append("--focal")
    if args.fact == "bounce" and args.candidate_mode:
        cmd += ["--candidate-mode", args.candidate_mode]
    if args.no_upload:
        cmd.append("--no-upload")
    return cmd


def submit(args) -> str:
    batch = _batch()
    res = FACT_RESOURCES[args.fact]
    command = _build_command(args)
    logger.info("submitting fact=%s -> %s queue=%s cmd=%s",
                args.fact, JOB_DEF, JOB_QUEUE, command)
    resp = batch.submit_job(
        jobName=f"train-{args.fact}-{args.epochs}ep",
        jobQueue=JOB_QUEUE,
        jobDefinition=JOB_DEF,
        containerOverrides={
            "command": command,
            "resourceRequirements": [
                {"type": "VCPU", "value": str(res["vcpus"])},
                {"type": "MEMORY", "value": str(res["memory"])},
                {"type": "GPU", "value": str(res["gpu"])},
            ],
        },
    )
    job_id = resp["jobId"]
    logger.info("submitted jobId=%s — monitor with --status %s", job_id, job_id)
    return job_id


def status(job_id: str) -> None:
    batch = _batch()
    jobs = batch.describe_jobs(jobs=[job_id])["jobs"]
    if not jobs:
        logger.error("no such job %s", job_id)
        return
    j = jobs[0]
    print(json.dumps({
        "jobId": j["jobId"], "jobName": j["jobName"],
        "status": j["status"], "statusReason": j.get("statusReason"),
        "logStreamName": j.get("container", {}).get("logStreamName"),
    }, indent=2))


def download(fact: str) -> None:
    import boto3
    s3 = boto3.client("s3")
    filename = WEIGHT_FILES[fact]
    key = f"{S3_WEIGHTS_PREFIX}/{fact}/_latest/{filename}"
    dest = MODELS_DIR / filename
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("downloading s3://%s/%s -> %s", S3_BUCKET, key, dest)
    s3.download_file(S3_BUCKET, key, str(dest))
    meta_key = f"{S3_WEIGHTS_PREFIX}/{fact}/_latest/meta.json"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=meta_key)
        logger.info("meta: %s", obj["Body"].read().decode())
    except Exception:
        logger.info("(no meta sidecar)")
    logger.info("done — rebuild the DETECTION image to ship these weights "
                "(models/ COPY layer), then redeploy the job-def (rule #8).")


def register_jobdef(args) -> None:
    """One-time: register ten-fifty5-ml-train pinned to the training image
    amd64 digest. Clones the detection job-def's role/log/retry/timeout.

    Requires: the training image pushed to ECR as
    <acct>.dkr.ecr.<region>.amazonaws.com/ten-fifty5-ml-train:latest, and
    --digest <amd64-sub-manifest-sha> (extract per .claude/handover_t5.md
    step 6) OR --image <full image ref>.
    """
    batch = _batch()
    if args.image:
        image = args.image
    elif args.digest:
        image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}@{args.digest}"
    else:
        raise SystemExit("--register-jobdef needs --digest <sha256:...> or --image <ref>")

    # Clone the detection job-def's container shell (role/log), swap image+entrypoint.
    detect = batch.describe_job_definitions(
        jobDefinitionName="ten-fifty5-ml-pipeline", status="ACTIVE",
    )["jobDefinitions"]
    detect = sorted(detect, key=lambda d: d["revision"])[-1]
    cp = detect["containerProperties"]

    container = {
        "image": image,
        "jobRoleArn": cp.get("jobRoleArn"),
        "logConfiguration": cp.get("logConfiguration"),
        "environment": [e for e in cp.get("environment", [])
                        if e["name"] in ("DATABASE_URL", "S3_BUCKET", "AWS_REGION")],
        "resourceRequirements": [
            {"type": "VCPU", "value": "4"},
            {"type": "MEMORY", "value": "15360"},
            {"type": "GPU", "value": "1"},
        ],
    }
    if cp.get("executionRoleArn"):
        container["executionRoleArn"] = cp["executionRoleArn"]

    resp = batch.register_job_definition(
        jobDefinitionName=JOB_DEF,
        type="container",
        platformCapabilities=detect.get("platformCapabilities", ["EC2"]),
        containerProperties=container,
        retryStrategy=detect.get("retryStrategy"),
        timeout=detect.get("timeout", {"attemptDurationSeconds": 21600}),
    )
    logger.info("registered %s:%d image=%s",
                resp["jobDefinitionName"], resp["revision"], image)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fact", choices=sorted(WEIGHT_FILES))
    ap.add_argument("--epochs", type=int, default=None)
    ap.add_argument("--candidate-mode", choices=("is_bounce", "gravity_residual"),
                    default="gravity_residual")
    ap.add_argument("--skip-dataset", action="store_true")
    ap.add_argument("--focal", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--download", action="store_true",
                    help="Download latest trained weights for --fact into models/.")
    ap.add_argument("--status", default=None, help="Print one Batch jobId status.")
    ap.add_argument("--register-jobdef", action="store_true")
    ap.add_argument("--digest", default=None, help="amd64 sub-manifest sha for register.")
    ap.add_argument("--image", default=None, help="full image ref for register.")
    args = ap.parse_args(argv)

    if args.status:
        status(args.status)
        return 0
    if args.register_jobdef:
        register_jobdef(args)
        return 0
    if args.download:
        if not args.fact:
            raise SystemExit("--download needs --fact")
        download(args.fact)
        return 0
    if not args.fact:
        ap.error("--fact is required to submit a training job")

    if args.epochs is None:
        args.epochs = {"serve": 200, "hit": 200, "bounce": 50, "swing": 50}[args.fact]
    job_id = submit(args)
    print(job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
