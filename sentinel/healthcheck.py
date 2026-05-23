"""Health-check probe: exits 0 if /healthz responds 200, 1 otherwise.

Usage (from Dockerfile / compose healthcheck):
    python -m sentinel.healthcheck
"""

from __future__ import annotations

import sys
import urllib.request


def main() -> None:
    try:
        with urllib.request.urlopen("http://localhost:8000/healthz", timeout=5) as resp:
            sys.exit(0 if resp.status == 200 else 1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
