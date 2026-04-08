#!/bin/bash
# lambda/deploy.sh — Deploy the ML trigger Lambda function
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - Lambda execution role already created (see below)
#   - ml_analysis schema already exists in PostgreSQL
#
# Usage:
#   cd lambda && bash deploy.sh

set -euo pipefail

FUNCTION_NAME="ten-fifty5-ml-trigger"
REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:-nextpoint-prod-uploads}"
BATCH_JOB_QUEUE="ten-fifty5-ml-queue"
BATCH_JOB_DEF="ten-fifty5-ml-pipeline"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/ten-fifty5-ml-trigger-role"

echo "=== Packaging Lambda ==="

# Create a temp directory for the deployment package
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# Install psycopg (binary) into the package
pip install psycopg[binary]==3.1.19 -t "$TMPDIR" --quiet

# Copy the Lambda handler
cp ml_trigger.py "$TMPDIR/"

# Create ZIP
cd "$TMPDIR"
zip -r9 /tmp/ml_trigger.zip . > /dev/null
cd -

echo "=== Creating/Updating Lambda Function ==="

# Check if function exists
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" 2>/dev/null; then
    echo "Updating existing function..."
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb:///tmp/ml_trigger.zip \
        --region "$REGION"

    aws lambda update-function-configuration \
        --function-name "$FUNCTION_NAME" \
        --environment "Variables={DATABASE_URL=${DATABASE_URL},S3_BUCKET=${S3_BUCKET},BATCH_JOB_QUEUE=${BATCH_JOB_QUEUE},BATCH_JOB_DEF=${BATCH_JOB_DEF},AWS_REGION=${REGION}}" \
        --timeout 30 \
        --memory-size 256 \
        --region "$REGION"
else
    echo "Creating new function..."
    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.11 \
        --handler ml_trigger.handler \
        --role "$ROLE_ARN" \
        --zip-file fileb:///tmp/ml_trigger.zip \
        --environment "Variables={DATABASE_URL=${DATABASE_URL},S3_BUCKET=${S3_BUCKET},BATCH_JOB_QUEUE=${BATCH_JOB_QUEUE},BATCH_JOB_DEF=${BATCH_JOB_DEF},AWS_REGION=${REGION}}" \
        --timeout 30 \
        --memory-size 256 \
        --tags "Project=TEN-FIFTY5,Environment=production" \
        --region "$REGION"
fi

echo "=== Configuring S3 Trigger ==="

# Grant S3 permission to invoke the Lambda
aws lambda add-permission \
    --function-name "$FUNCTION_NAME" \
    --statement-id s3-trigger \
    --action lambda:InvokeFunction \
    --principal s3.amazonaws.com \
    --source-arn "arn:aws:s3:::${S3_BUCKET}" \
    --region "$REGION" 2>/dev/null || true

# Create S3 event notification for videos/ prefix
cat > /tmp/s3_notification.json << 'NOTIF'
{
    "LambdaFunctionConfigurations": [
        {
            "LambdaFunctionArn": "LAMBDA_ARN_PLACEHOLDER",
            "Events": ["s3:ObjectCreated:*"],
            "Filter": {
                "Key": {
                    "FilterRules": [
                        {"Name": "prefix", "Value": "videos/"}
                    ]
                }
            }
        }
    ]
}
NOTIF

LAMBDA_ARN=$(aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" --query 'Configuration.FunctionArn' --output text)
sed -i "s|LAMBDA_ARN_PLACEHOLDER|${LAMBDA_ARN}|g" /tmp/s3_notification.json

aws s3api put-bucket-notification-configuration \
    --bucket "$S3_BUCKET" \
    --notification-configuration file:///tmp/s3_notification.json \
    --region "$REGION"

echo "=== Creating DLQ ==="

# Create SQS dead-letter queue
DLQ_ARN=$(aws sqs create-queue \
    --queue-name "${FUNCTION_NAME}-dlq" \
    --tags "Project=TEN-FIFTY5,Environment=production" \
    --attributes '{"MessageRetentionPeriod":"1209600"}' \
    --region "$REGION" \
    --query 'QueueUrl' --output text 2>/dev/null || true)

if [ -n "$DLQ_ARN" ]; then
    DLQ_QUEUE_ARN=$(aws sqs get-queue-attributes \
        --queue-url "$DLQ_ARN" \
        --attribute-names QueueArn \
        --query 'Attributes.QueueArn' --output text \
        --region "$REGION")

    aws lambda update-function-configuration \
        --function-name "$FUNCTION_NAME" \
        --dead-letter-config "TargetArn=${DLQ_QUEUE_ARN}" \
        --region "$REGION"
    echo "DLQ configured: ${DLQ_QUEUE_ARN}"
fi

echo "=== Done ==="
echo "Lambda: $FUNCTION_NAME"
echo "Trigger: s3://${S3_BUCKET}/videos/ → $FUNCTION_NAME"
