# Dockerfile for ml_pipeline
# Build from repo root: docker build -f ml_pipeline/Dockerfile -t ten-fifty5-ml-pipeline .

FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.11, FFmpeg, OpenCV system deps.
# NOTE: we install Ubuntu's apt ffmpeg as a baseline (libx264 fallback path
# in _transcode_to_mp4), then overlay a static nvenc-enabled build from
# BtbN/FFmpeg-Builds below at /usr/local/bin/ffmpeg. PATH puts /usr/local/bin
# first so the nvenc build wins for ffmpeg / ffprobe lookups.
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
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# L5 (Lever #5 from docs/_investigation/batch_optimisation_plan.md):
# overlay a static ffmpeg/ffprobe build with NVENC + CUDA support so
# _transcode_to_mp4 can use h264_nvenc on the T4's dedicated encoder
# silicon (~5-10× faster than libx264 ultrafast on a 44-min 1080p source).
# Ubuntu 22.04's apt ffmpeg is NOT built with --enable-nvenc, so without
# this overlay the Python code's NVENC primary call would fail and the
# in-Python auto-fallback to libx264 would kick in (correct but slower).
#
# Source: BtbN/FFmpeg-Builds — the canonical source of nvenc-enabled
# static linux64 ffmpeg builds. Uses the `latest` rolling release tag;
# the runtime `grep h264_nvenc` check at the bottom of this RUN block
# fails the Docker build immediately if the encoder ever drops out.
# Re-pin to a specific autobuild tag (BTBN_FFMPEG_REL) once we've
# observed a stable known-good build.
#
# Installs at /usr/local/bin which precedes /usr/bin in the default
# image PATH — `ffmpeg` resolves to the nvenc build for both Python's
# subprocess.run and any interactive shell session, while the apt-
# installed /usr/bin/ffmpeg stays as a last-resort fallback.
RUN set -eu; \
    curl -fSL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
        -o /tmp/ffmpeg.tar.xz; \
    mkdir -p /tmp/ffmpeg-extract; \
    tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg-extract --strip-components=1; \
    install -m 0755 /tmp/ffmpeg-extract/bin/ffmpeg  /usr/local/bin/ffmpeg; \
    install -m 0755 /tmp/ffmpeg-extract/bin/ffprobe /usr/local/bin/ffprobe; \
    rm -rf /tmp/ffmpeg.tar.xz /tmp/ffmpeg-extract; \
    /usr/local/bin/ffmpeg -hide_banner -encoders 2>&1 | grep -q h264_nvenc \
        || (echo "BUILD ABORT: /usr/local/bin/ffmpeg does not advertise h264_nvenc" && exit 1)

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

# WASB-SBDT ball detector — HRNet drop-in replacement for TrackNetV2.
# Pipeline picks between BallTracker and WASBBallTracker via the
# BALL_TRACKER env var (default tracknet_v2). Weights file is already in
# ml_pipeline/models/ (wasb_tennis_best.pth.tar) and gets copied via the
# models/ layer above; this just brings in the Python wrappers.
COPY ml_pipeline/wasb_ball_tracker.py /app/ml_pipeline/
COPY ml_pipeline/wasb_hrnet.py /app/ml_pipeline/
COPY ml_pipeline/player_tracker.py /app/ml_pipeline/
COPY ml_pipeline/heatmaps.py /app/ml_pipeline/
COPY ml_pipeline/db_schema.py /app/ml_pipeline/
COPY ml_pipeline/db_writer.py /app/ml_pipeline/
COPY ml_pipeline/bronze_export.py /app/ml_pipeline/

# Swing-type classifier (ADR-02 v2 — optical-flow R(2+1)D, both players).
# inference_v2.py runs in pipeline.py and writes bronze stroke_class. The v2
# weights (swing_classifier_v2.pt) ship via the wholesale `COPY models/` layer
# above — no separate weight COPY needed.
COPY ml_pipeline/stroke_classifier/ /app/ml_pipeline/stroke_classifier/

# Bounce detector — CNN bounce bronze model (ADR-01 v2). __main__ runs it
# post-pipeline (match flow only) and writes ml_analysis.ball_bounces; the
# weights (bounce_detector_v2_7match.pt) ship via the models/ layer above.
# Without this COPY the try/except around the stage in __main__ swallows
# the ImportError and the bounce stage silently never runs on Batch.
COPY ml_pipeline/bounce_detector/ /app/ml_pipeline/bounce_detector/

# ROI extractors — far-baseline ViTPose + (stubbed) WASB bounces. Called
# after TennisAnalysisPipeline in __main__._run_batch. See
# ml_pipeline/roi_extractors/pose.py for the pose extractor.
COPY ml_pipeline/roi_extractors/ /app/ml_pipeline/roi_extractors/

# Serve detector package — pose.py imports rally_state.RallyStateMachine to
# gate ROI extraction to between-rally moments. The package's __init__.py
# pulls in detector/schema/models/pose_signal/ball_toss; all transitive
# deps (sqlalchemy, stdlib) are already in the image. Without this COPY,
# the rally gate silently fails open and processes every frame.
COPY ml_pipeline/serve_detector/ /app/ml_pipeline/serve_detector/

# Serve model — trained far-serve candidate scorer (MLP, ~150KB weights via
# the models/ layer above). __main__ runs the serve-candidates stage after
# the ROI sweep (match flow only) and writes ml_analysis.serve_candidates;
# consumed Render-side by serve_detector behind SERVE_MODEL_ENABLED.
# Without this COPY the stage's try/except swallows the ImportError and
# candidates silently never land (rule-8 lesson, bounce_detector 2026-06-05).
COPY ml_pipeline/serve_model/ /app/ml_pipeline/serve_model/

# Copy package entry point
COPY ml_pipeline/__main__.py /app/ml_pipeline/__main__.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point: expects JOB_ID and S3_KEY as env vars
ENTRYPOINT ["python", "-m", "ml_pipeline"]
