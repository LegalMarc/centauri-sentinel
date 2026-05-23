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

# Fetch the ml_api directory from obico-server (shallow clone, copy, then discard)
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends git \
    && git clone --depth 1 \
       https://github.com/TheSpaghettiDetective/obico-server.git /tmp/obico \
    && cp -r /tmp/obico/ml_api /app/ \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/* /tmp/obico

WORKDIR /app/ml_api

RUN if [ -f requirements.txt ]; then \
        pip install --no-cache-dir -r requirements.txt; \
    fi

EXPOSE 3333

# start.sh is the standard entrypoint in obico-server ml_api/
CMD ["bash", "start.sh"]
