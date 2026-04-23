# Dockerfile for ml_pipeline
# Build from repo root: docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline .

FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.11, FFmpeg, OpenCV system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Make python3.11 the default
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

RUN python -m pip install --upgrade pip setuptools wheel

WORKDIR /app

# Install Python dependencies (cache-friendly layer)
COPY ml_pipeline/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download ViTPose-Base (~300 MB) into the HuggingFace cache so the
# Batch job doesn't pay the download + network-unreliability cost on
# every cold start. Cache lives at /root/.cache/huggingface; consumed
# by ml_pipeline/roi_extractors/pose.py via VitPoseForPoseEstimation.
# from_pretrained("usyd-community/vitpose-plus-base").
ENV HF_HOME=/root/.cache/huggingface
ENV TRANSFORMERS_OFFLINE=0
RUN python -c "from transformers import VitPoseForPoseEstimation, VitPoseImageProcessor; \
    VitPoseForPoseEstimation.from_pretrained('usyd-community/vitpose-plus-base'); \
    VitPoseImageProcessor.from_pretrained('usyd-community/vitpose-plus-base')"

# Copy model weights (~135MB, git-ignored but present on disk)
COPY ml_pipeline/models/ /app/ml_pipeline/models/

# Copy pipeline package
COPY ml_pipeline/__init__.py /app/ml_pipeline/
COPY ml_pipeline/config.py /app/ml_pipeline/
COPY ml_pipeline/pipeline.py /app/ml_pipeline/
COPY ml_pipeline/video_preprocessor.py /app/ml_pipeline/
COPY ml_pipeline/court_detector.py /app/ml_pipeline/
COPY ml_pipeline/camera_calibration.py /app/ml_pipeline/
COPY ml_pipeline/ball_tracker.py /app/ml_pipeline/
COPY ml_pipeline/tracknet_v3.py /app/ml_pipeline/
COPY ml_pipeline/player_tracker.py /app/ml_pipeline/
COPY ml_pipeline/heatmaps.py /app/ml_pipeline/
COPY ml_pipeline/db_schema.py /app/ml_pipeline/
COPY ml_pipeline/db_writer.py /app/ml_pipeline/
COPY ml_pipeline/bronze_export.py /app/ml_pipeline/

# Stroke classifier (optical flow for far player)
COPY ml_pipeline/stroke_classifier/ /app/ml_pipeline/stroke_classifier/

# ROI extractors — far-baseline ViTPose + (stubbed) WASB bounces. Called
# after TennisAnalysisPipeline in __main__._run_batch. See
# ml_pipeline/roi_extractors/pose.py for the pose extractor.
COPY ml_pipeline/roi_extractors/ /app/ml_pipeline/roi_extractors/

# Copy package entry point
COPY ml_pipeline/__main__.py /app/ml_pipeline/__main__.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point: expects JOB_ID and S3_KEY as env vars
ENTRYPOINT ["python", "-m", "ml_pipeline"]
