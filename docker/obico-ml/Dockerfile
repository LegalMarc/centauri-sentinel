# Builds the Obico ML API using a CPU-only PyTorch/ONNX stack.
#
# This replaces the previous 5 GB CUDA base image with a ~600 MB
# python:3.10-slim image that runs onnxruntime on CPU.  Suitable for
# homelab deployments without an NVIDIA GPU.  Inference speed is
# adequate for the 10-second polling interval used by centauri-sentinel.
#
# The GPU/CUDA variant is no longer maintained; the ONNX model is
# accurate enough and far easier to deploy.
#
# Build:
#   docker build -t ghcr.io/legalmarc/centauri-sentinel/obico-ml:1.4 .

# syntax=docker/dockerfile:1
FROM python:3.10.16-slim

# System deps
#   libgl1 + libglib2.0-0 — required by opencv-python-headless
#   wget                  — fetches model weights at build time
#   git                   — clones obico-server ml_api source
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends \
       libgl1 \
       libglib2.0-0 \
       wget \
       git \
    && rm -rf /var/lib/apt/lists/*

# Fetch the ml_api source from obico-server (shallow clone, copy to /app, discard)
# ml_api expects to live at /app (model.meta hardcodes /app/model/names)
RUN git clone --depth 1 \
       https://github.com/TheSpaghettiDetective/obico-server.git /tmp/obico \
    && cp -rT /tmp/obico/ml_api /app/ \
    && rm -rf /tmp/obico

WORKDIR /app

# Install Python dependencies.
# We skip newrelic (APM) and ipdb (debug REPL) — not needed in production.
# onnxruntime (CPU) replaces the CUDA-compiled darknet/PyTorch stack.
RUN pip install --no-cache-dir \
    flask==2.3.3 \
    gunicorn==21.2.0 \
    requests==2.31.0 \
    "sentry-sdk[flask]==1.40.3" \
    Flask-Compress==1.15 \
    backoff \
    onnxruntime \
    numpy \
    opencv-python-headless

# Download the ONNX model weights.
# The darknet weights are skipped; the detection_model loader falls back to
# ONNX automatically when the darknet library is absent.
RUN mkdir -p /model_cache/ml_api/onnx \
    && wget -q -O /model_cache/ml_api/onnx/model-weights.onnx \
       "$(cat model/model-weights.onnx.url | tr -d '\r\n')"

EXPOSE 3333

CMD ["python3", "server.py"]
