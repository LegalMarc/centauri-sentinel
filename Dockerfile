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
# Stage 2 — runtime: copy venv + source, run as non-root via gosu
# ---------------------------------------------------------------------------
FROM python:3.12.8-slim AS runtime

# Non-root user + gosu for privilege drop in entrypoint.
# The container starts as root so the entrypoint can fix /data ownership on
# bind-mounted volumes (Docker creates them root:root on first use), then
# drops privileges to sentinel via `exec gosu sentinel "$@"`.
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends gosu \
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

# Data directory — pre-create so ownership is set correctly in the image layer.
# The entrypoint.sh script re-chowns on every startup to handle the first-mount
# race where Docker creates a bind-mounted directory as root before the
# container starts.
RUN mkdir -p /data/snapshots && chown -R sentinel:sentinel /data /app

# Entrypoint: runs as root, re-chowns /data if needed, then drops to sentinel
# via gosu.  The container intentionally has no USER directive; the entrypoint
# must start as root so it can call chown before exec-ing gosu to drop
# privileges.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -m sentinel.healthcheck || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "sentinel", "run"]
