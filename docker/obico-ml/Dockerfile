# Builds the Obico ML API from the obico-server ml_api/ source.
#
# The base image provides PyTorch, torchvision, and the trained model weights.
# We fetch only the ml_api/ directory from the obico-server repo on top of it.
#
# Build args:
#   IMAGE_TAG_SUFFIX  — empty for amd64, "-linux-arm64" for arm64.
#
# Example (arm64):
#   docker build --build-arg IMAGE_TAG_SUFFIX=-linux-arm64 .

# syntax=docker/dockerfile:1
ARG IMAGE_TAG_SUFFIX=
FROM thespaghettidetective/ml_api_base:1.4${IMAGE_TAG_SUFFIX}

# Fetch the ml_api source from obico-server (shallow clone, copy to /app, discard)
# ml_api expects to live at /app (model.meta hardcodes /app/model/names)
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends git \
    && git clone --depth 1 \
       https://github.com/TheSpaghettiDetective/obico-server.git /tmp/obico \
    && cp -rT /tmp/obico/ml_api /app/ \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/* /tmp/obico

WORKDIR /app

RUN if [ -f requirements.txt ]; then \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# Download model weights from the URLs specified in the ml_api source
RUN mkdir -p /model_cache/ml_api/darknet /model_cache/ml_api/onnx \
    && wget -q -O /model_cache/ml_api/darknet/model-weights.darknet \
       "$(cat model/model-weights.darknet.url | tr -d '\r\n')" \
    && wget -q -O /model_cache/ml_api/onnx/model-weights.onnx \
       "$(cat model/model-weights.onnx.url | tr -d '\r\n')"

EXPOSE 3333

CMD ["python3", "server.py"]
