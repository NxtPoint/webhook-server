#!/bin/bash
# ml_pipeline/deploy_aws.sh — Create ECR repo, build/push Docker image, set up AWS Batch
#
# Prerequisites:
#   - AWS CLI v2 configured with appropriate credentials
#   - Docker installed and running
#   - Run from repo root: bash ml_pipeline/deploy_aws.sh
#
# All resources tagged: Project=TEN-FIFTY5, Environment=production

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="ten-fifty5-ml-pipeline"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}"
IMAGE_TAG="latest"

echo "Account: ${ACCOUNT_ID}"
echo "Region:  ${REGION}"
echo "ECR:     ${ECR_URI}"
echo ""

# ============================================================================
# TASK 4: ECR
# ============================================================================

echo "=== Task 4: ECR Repository ==="

# Create ECR repository
aws ecr create-repository \
    --repository-name "${ECR_REPO}" \
    --image-scanning-configuration scanOnPush=true \
    --tags "Key=Project,Value=TEN-FIFTY5" "Key=Environment,Value=production" \
    --region "${REGION}" 2>/dev/null || echo "ECR repo already exists"

# Set lifecycle policy: keep 5 most recent tagged, delete untagged after 1 day
aws ecr put-lifecycle-policy \
    --repository-name "${ECR_REPO}" \
    --lifecycle-policy-text '{
        "rules": [
            {
                "rulePriority": 1,
                "description": "Delete untagged images after 1 day",
                "selection": {
                    "tagStatus": "untagged",
                    "countType": "sinceImagePushed",
                    "countUnit": "days",
                    "countNumber": 1
                },
                "action": {
                    "type": "expire"
                }
            },
            {
                "rulePriority": 2,
                "description": "Keep only 5 most recent tagged images",
                "selection": {
                    "tagStatus": "tagged",
                    "tagPrefixList": ["latest", "v"],
                    "countType": "imageCountMoreThan",
                    "countNumber": 5
                },
                "action": {
                    "type": "expire"
                }
            }
        ]
    }' \
    --region "${REGION}"

echo "ECR lifecycle policy set"

# Docker login to ECR
aws ecr get-login-password --region "${REGION}" \
    | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# Build Docker image (from repo root, using ml_pipeline/Dockerfile)
echo "Building Docker image..."
docker build -f ml_pipeline/Dockerfile -t "${ECR_REPO}:${IMAGE_TAG}" .

# Tag and push
docker tag "${ECR_REPO}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"
docker push "${ECR_URI}:${IMAGE_TAG}"

echo "Pushed: ${ECR_URI}:${IMAGE_TAG}"

# ============================================================================
# TASK 5: AWS Batch
# ============================================================================

echo ""
echo "=== Task 5: AWS Batch Setup ==="

COMPUTE_ENV="ten-fifty5-ml-compute"
JOB_QUEUE="ten-fifty5-ml-queue"
JOB_DEF="ten-fifty5-ml-pipeline"
LOG_GROUP="/aws/batch/ten-fifty5-ml-pipeline"

# --- IAM Roles ---
# These must exist before creating Batch resources.
# If they don't exist, create them (idempotent via || true).

BATCH_SERVICE_ROLE="arn:aws:iam::${ACCOUNT_ID}:role/aws-service-role/batch.amazonaws.com/AWSServiceRoleForBatch"
ECS_INSTANCE_ROLE_NAME="ten-fifty5-ml-instance-role"
ECS_INSTANCE_PROFILE_NAME="ten-fifty5-ml-instance-profile"
JOB_ROLE_NAME="ten-fifty5-ml-job-role"

# Instance role (for EC2 instances in the compute environment)
aws iam create-role \
    --role-name "${ECS_INSTANCE_ROLE_NAME}" \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }' \
    --tags "Key=Project,Value=TEN-FIFTY5" "Key=Environment,Value=production" \
    2>/dev/null || echo "Instance role already exists"

aws iam attach-role-policy \
    --role-name "${ECS_INSTANCE_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role 2>/dev/null || true

aws iam create-instance-profile \
    --instance-profile-name "${ECS_INSTANCE_PROFILE_NAME}" 2>/dev/null || true

aws iam add-role-to-instance-profile \
    --instance-profile-name "${ECS_INSTANCE_PROFILE_NAME}" \
    --role-name "${ECS_INSTANCE_ROLE_NAME}" 2>/dev/null || true

# Job execution role (for the container — S3, DB, CloudWatch access)
aws iam create-role \
    --role-name "${JOB_ROLE_NAME}" \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }' \
    --tags "Key=Project,Value=TEN-FIFTY5" "Key=Environment,Value=production" \
    2>/dev/null || echo "Job role already exists"

# Attach S3 and CloudWatch policies
aws iam attach-role-policy \
    --role-name "${JOB_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess 2>/dev/null || true

aws iam attach-role-policy \
    --role-name "${JOB_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/CloudWatchLogsFullAccess 2>/dev/null || true

JOB_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${JOB_ROLE_NAME}"
INSTANCE_PROFILE_ARN="arn:aws:iam::${ACCOUNT_ID}:instance-profile/${ECS_INSTANCE_PROFILE_NAME}"

echo "IAM roles configured"

# --- CloudWatch Log Group ---
aws logs create-log-group \
    --log-group-name "${LOG_GROUP}" \
    --tags "Project=TEN-FIFTY5,Environment=production" \
    --region "${REGION}" 2>/dev/null || echo "Log group already exists"

aws logs put-retention-policy \
    --log-group-name "${LOG_GROUP}" \
    --retention-in-days 30 \
    --region "${REGION}"

echo "CloudWatch log group: ${LOG_GROUP}"

# --- Get default VPC and subnets ---
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=is-default,Values=true" --query "Vpcs[0].VpcId" --output text --region "${REGION}")
SUBNET_IDS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --query "Subnets[*].SubnetId" --output text --region "${REGION}" | tr '\t' ',')
SG_ID=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=default" --query "SecurityGroups[0].GroupId" --output text --region "${REGION}")

echo "VPC: ${VPC_ID}, Subnets: ${SUBNET_IDS}, SG: ${SG_ID}"

# --- Compute Environment (Spot, G4dn.xlarge) ---
aws batch create-compute-environment \
    --compute-environment-name "${COMPUTE_ENV}" \
    --type MANAGED \
    --compute-resources '{
        "type": "SPOT",
        "allocationStrategy": "SPOT_CAPACITY_OPTIMIZED",
        "minvCpus": 0,
        "maxvCpus": 4,
        "desiredvCpus": 0,
        "instanceTypes": ["g4dn.xlarge"],
        "subnets": ["'"$(echo ${SUBNET_IDS} | sed 's/,/","/g')"'"],
        "securityGroupIds": ["'"${SG_ID}"'"],
        "instanceRole": "'"${INSTANCE_PROFILE_ARN}"'",
        "bidPercentage": 60,
        "spotIamFleetRole": "arn:aws:iam::'"${ACCOUNT_ID}"':role/aws-ec2-spot-fleet-tagging-role",
        "tags": {
            "Project": "TEN-FIFTY5",
            "Environment": "production"
        }
    }' \
    --tags "Project=TEN-FIFTY5,Environment=production" \
    --region "${REGION}" 2>/dev/null || echo "Compute environment already exists"

echo "Waiting for compute environment to be VALID..."
aws batch describe-compute-environments \
    --compute-environments "${COMPUTE_ENV}" \
    --region "${REGION}" \
    --query "computeEnvironments[0].status" --output text

# --- Job Queue ---
aws batch create-job-queue \
    --job-queue-name "${JOB_QUEUE}" \
    --priority 1 \
    --compute-environment-order "order=1,computeEnvironment=${COMPUTE_ENV}" \
    --tags "Project=TEN-FIFTY5,Environment=production" \
    --region "${REGION}" 2>/dev/null || echo "Job queue already exists"

echo "Job queue: ${JOB_QUEUE}"

# --- Job Definition (4 vCPUs, 15GB RAM, 1 GPU) ---
aws batch register-job-definition \
    --job-definition-name "${JOB_DEF}" \
    --type container \
    --container-properties '{
        "image": "'"${ECR_URI}:${IMAGE_TAG}"'",
        "command": ["python", "-m", "ml_pipeline", "--job-id", "Ref::job_id", "--s3-key", "Ref::s3_key"],
        "resourceRequirements": [
            {"type": "VCPU", "value": "4"},
            {"type": "MEMORY", "value": "15360"},
            {"type": "GPU", "value": "1"}
        ],
        "environment": [
            {"name": "DATABASE_URL", "value": "'"${DATABASE_URL}"'"},
            {"name": "S3_BUCKET", "value": "'"${S3_BUCKET:-nextpoint-prod-uploads}"'"},
            {"name": "AWS_REGION", "value": "'"${REGION}"'"}
        ],
        "jobRoleArn": "'"${JOB_ROLE_ARN}"'",
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "'"${LOG_GROUP}"'",
                "awslogs-region": "'"${REGION}"'",
                "awslogs-stream-prefix": "ml-pipeline"
            }
        }
    }' \
    --parameters '{"job_id": "", "s3_key": ""}' \
    --tags "Project=TEN-FIFTY5,Environment=production" \
    --timeout '{"attemptDurationSeconds": 7200}' \
    --retry-strategy '{"attempts": 1}' \
    --region "${REGION}"

echo "Job definition: ${JOB_DEF}"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "ECR Repository:     ${ECR_URI}"
echo "Compute Environment: ${COMPUTE_ENV}"
echo "Job Queue:           ${JOB_QUEUE}"
echo "Job Definition:      ${JOB_DEF}"
echo "CloudWatch Logs:     ${LOG_GROUP}"
echo ""
echo "Test with:"
echo "  aws batch submit-job \\"
echo "    --job-name test-ml-pipeline \\"
echo "    --job-queue ${JOB_QUEUE} \\"
echo "    --job-definition ${JOB_DEF} \\"
echo "    --container-overrides '{\"command\":[\"python\",\"-m\",\"ml_pipeline\",\"--job-id\",\"test-123\",\"--s3-key\",\"videos/test/sample.mp4\"]}'"
