"""Health-check probe: exits 0 if /healthz responds 200, 1 otherwise.

Probes the liveness endpoint (/healthz), not the readiness endpoint (/readyz).
This ensures that a powered-off printer or disconnected camera (which are normal
operational states) does not mark the container unhealthy.  The container is only
flagged unhealthy when the process itself is broken (e.g. watcher task dead).

Usage (from Dockerfile / compose healthcheck):
    python -m sentinel.healthcheck
"""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> None:
    try:
        port = os.getenv("BIND_PORT", "8000")
        with urllib.request.urlopen(f"http://localhost:{port}/healthz", timeout=5) as resp:
            sys.exit(0 if resp.status == 200 else 1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
