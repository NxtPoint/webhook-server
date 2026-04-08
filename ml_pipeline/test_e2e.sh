#!/bin/bash
# ml_pipeline/test_e2e.sh — End-to-end test for the ML pipeline
#
# Prerequisites:
#   - AWS CLI configured
#   - All infrastructure deployed (run deploy_aws.sh and lambda/deploy.sh first)
#   - DATABASE_URL, S3_BUCKET, OPS_KEY env vars set
#
# This script:
#   1. Uploads a test video to S3 videos/ prefix
#   2. Waits for Lambda trigger → Batch job submission
#   3. Monitors Batch job until complete
#   4. Verifies database rows exist
#   5. Verifies heatmaps in S3
#   6. Hits API endpoints to confirm data

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:-nextpoint-prod-uploads}"
API_BASE="${API_BASE:-https://api.nextpointtennis.com}"
OPS_KEY="${OPS_KEY}"
TEST_VIDEO="${1:-ml_pipeline/test_videos/sample.mp4}"

if [ ! -f "$TEST_VIDEO" ]; then
    echo "ERROR: Test video not found at $TEST_VIDEO"
    echo "Usage: bash ml_pipeline/test_e2e.sh <path_to_video>"
    exit 1
fi

TEST_ID="e2e-test-$(date +%s)"
S3_KEY="videos/${TEST_ID}/test.mp4"

echo "=== Step 1: Upload test video ==="
echo "Uploading ${TEST_VIDEO} → s3://${S3_BUCKET}/${S3_KEY}"
aws s3 cp "$TEST_VIDEO" "s3://${S3_BUCKET}/${S3_KEY}" --region "${REGION}"
echo "Upload complete."

echo ""
echo "=== Step 2: Wait for Lambda to create job row ==="
echo "Waiting 15 seconds for S3 event → Lambda trigger..."
sleep 15

# Check for job row in database
JOB_ID=$(psql "${DATABASE_URL}" -t -c "
    SELECT job_id FROM ml_analysis.video_analysis_jobs
    WHERE s3_key = '${S3_KEY}'
    ORDER BY created_at DESC LIMIT 1;
" | tr -d '[:space:]')

if [ -z "$JOB_ID" ]; then
    echo "ERROR: No job row found for s3_key=${S3_KEY}"
    echo "Check Lambda logs: aws logs tail /aws/lambda/ten-fifty5-ml-trigger --region ${REGION}"
    exit 1
fi
echo "Job created: ${JOB_ID}"

echo ""
echo "=== Step 3: Monitor Batch job ==="
BATCH_JOB_ID=$(psql "${DATABASE_URL}" -t -c "
    SELECT batch_job_id FROM ml_analysis.video_analysis_jobs
    WHERE job_id = '${JOB_ID}';
" | tr -d '[:space:]')

echo "Batch job: ${BATCH_JOB_ID}"
echo "Monitoring (this may take 10-30 minutes for GPU startup + processing)..."

while true; do
    STATUS=$(aws batch describe-jobs --jobs "${BATCH_JOB_ID}" --region "${REGION}" \
        --query "jobs[0].status" --output text 2>/dev/null || echo "UNKNOWN")

    DB_STATUS=$(psql "${DATABASE_URL}" -t -c "
        SELECT status || ' (' || COALESCE(current_stage,'?') || ' ' || COALESCE(progress_pct::text,'0') || '%)'
        FROM ml_analysis.video_analysis_jobs WHERE job_id = '${JOB_ID}';
    " | tr -d '[:space:]')

    echo "  Batch: ${STATUS} | DB: ${DB_STATUS}"

    if [ "$STATUS" = "SUCCEEDED" ] || [ "$STATUS" = "FAILED" ]; then
        break
    fi
    sleep 30
done

if [ "$STATUS" = "FAILED" ]; then
    echo "ERROR: Batch job failed!"
    echo "Check logs: aws logs tail /aws/batch/ten-fifty5-ml-pipeline --region ${REGION}"
    ERROR=$(psql "${DATABASE_URL}" -t -c "
        SELECT error_message FROM ml_analysis.video_analysis_jobs WHERE job_id = '${JOB_ID}';
    ")
    echo "Error: ${ERROR}"
    exit 1
fi

echo ""
echo "=== Step 4: Verify database rows ==="

echo "Job row:"
psql "${DATABASE_URL}" -c "
    SELECT job_id, status, current_stage, progress_pct, processing_time_sec,
           estimated_cost_usd, ball_heatmap_s3_key
    FROM ml_analysis.video_analysis_jobs WHERE job_id = '${JOB_ID}';
"

echo "Ball detections:"
psql "${DATABASE_URL}" -c "
    SELECT COUNT(*) AS total_detections,
           COUNT(*) FILTER (WHERE is_bounce) AS bounces
    FROM ml_analysis.ball_detections WHERE job_id = '${JOB_ID}';
"

echo "Player detections:"
psql "${DATABASE_URL}" -c "
    SELECT player_id, COUNT(*) AS detections
    FROM ml_analysis.player_detections WHERE job_id = '${JOB_ID}'
    GROUP BY player_id;
"

echo "Match analytics:"
psql "${DATABASE_URL}" -c "
    SELECT ball_detection_rate, bounce_count, rally_count, player_count,
           processing_time_sec
    FROM ml_analysis.match_analytics WHERE job_id = '${JOB_ID}';
"

echo ""
echo "=== Step 5: Verify heatmaps in S3 ==="
aws s3 ls "s3://${S3_BUCKET}/analysis/${JOB_ID}/" --region "${REGION}"

echo ""
echo "=== Step 6: Test API endpoints ==="

echo "GET /api/analysis/jobs/${JOB_ID}:"
curl -s -H "X-Ops-Key: ${OPS_KEY}" "${API_BASE}/api/analysis/jobs/${JOB_ID}" | python -m json.tool | head -20

echo ""
echo "GET /api/analysis/heatmap/${JOB_ID}/ball:"
curl -s -H "X-Ops-Key: ${OPS_KEY}" "${API_BASE}/api/analysis/heatmap/${JOB_ID}/ball" | python -m json.tool

echo ""
echo "=== Step 7: Cleanup ==="
echo "Test video left at: s3://${S3_BUCKET}/${S3_KEY}"
echo "To clean up: aws s3 rm s3://${S3_BUCKET}/${S3_KEY} && aws s3 rm s3://${S3_BUCKET}/analysis/${JOB_ID}/ --recursive"

echo ""
echo "=== E2E TEST PASSED ==="
echo "Job ID: ${JOB_ID}"
echo "Batch Job: ${BATCH_JOB_ID}"
