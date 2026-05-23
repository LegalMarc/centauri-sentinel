# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — builder: install dependencies with uv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv==0.7.0

WORKDIR /build
COPY pyproject.toml uv.lock ./

# Install runtime deps only (no dev extras), frozen from lockfile
RUN uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage 2 — runtime: copy venv + source, run as non-root
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root user
RUN useradd --uid 1000 --create-home --shell /bin/sh sentinel

# Copy the virtualenv from builder
COPY --from=builder /build/.venv /app/.venv

# Copy application source
WORKDIR /app
COPY sentinel/ ./sentinel/

# Set PATH so the venv's binaries are used
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Data directory (mounted as named volume in compose)
RUN mkdir -p /data/snapshots && chown -R sentinel:sentinel /data /app

USER sentinel

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["python", "-m", "sentinel", "run"]
