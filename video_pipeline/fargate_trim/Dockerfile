# Fargate video-trim job image.
#
# Deliberately tiny: ffmpeg + boto3 + requests and the two modules the trim
# actually needs. It does NOT install requirements.txt — the trim touches only
# S3 and one HTTPS callback, never the database, so pulling in the web stack
# would add minutes to every cold start for nothing.
#
# Build + push (from the repo root):
#   aws ecr get-login-password --region eu-north-1 \
#     | docker login --username AWS --password-stdin 696793787014.dkr.ecr.eu-north-1.amazonaws.com
#   docker build -f video_pipeline/fargate_trim/Dockerfile \
#     -t 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-video-trim:latest .
#   docker push 696793787014.dkr.ecr.eu-north-1.amazonaws.com/ten-fifty5-video-trim:latest
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AWS_DEFAULT_REGION=eu-north-1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3 requests

WORKDIR /app
COPY video_pipeline/__init__.py            /app/video_pipeline/__init__.py
COPY video_pipeline/ffmpeg_trim_worker.py  /app/video_pipeline/ffmpeg_trim_worker.py
COPY video_pipeline/fargate_trim/          /app/video_pipeline/fargate_trim/

ENTRYPOINT ["python", "-m", "video_pipeline.fargate_trim.runner"]
