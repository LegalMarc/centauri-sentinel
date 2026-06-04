"""Health-check probe: exits 0 if /readyz responds 200, 1 otherwise.

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
