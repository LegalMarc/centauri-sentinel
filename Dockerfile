# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — builder: install dependencies with uv
# ---------------------------------------------------------------------------
FROM python:3.12.8-slim AS builder

RUN pip install --no-cache-dir uv==0.7.0

WORKDIR /build
COPY pyproject.toml uv.lock ./

# Install runtime deps only (no dev extras), frozen from lockfile
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage 2 — runtime: copy venv + source, run as non-root
# ---------------------------------------------------------------------------
FROM python:3.12.8-slim AS runtime

# Non-root user + su-exec for privilege drop in entrypoint
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends su-exec \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /bin/sh sentinel

# Copy the virtualenv from builder
COPY --from=builder /build/.venv /app/.venv

# Copy application source
WORKDIR /app
COPY sentinel/ ./sentinel/

# Set PATH so the venv's binaries are used
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Data directory — pre-create so permissions are correct when mount is empty.
# The entrypoint.sh script re-chowns on every startup to handle first-mount
# race where Docker creates the volume as root.
RUN mkdir -p /data/snapshots && chown -R sentinel:sentinel /data /app

# Entrypoint: fixes /data ownership then drops to sentinel user
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# USER sentinel directive removed to allow entrypoint.sh to run as root and fix permissions.
# entrypoint.sh will drop privileges to sentinel via su-exec.

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -m sentinel.healthcheck || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "sentinel", "run"]
