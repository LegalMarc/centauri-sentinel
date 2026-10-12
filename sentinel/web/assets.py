"""Static UI assets: location and a content version for cache busting."""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

# Page CSS/JS live here; vendor/ is built by web/build.mjs (Dockerfile web stage)
# and is absent in a bare checkout.
STATIC_DIR = Path(__file__).parent / "static"


@cache
def asset_version() -> str:
    """Short content hash of the static assets, appended to their URLs as ?v=.

    A deploy that changes any asset changes the hash, so browsers never keep a
    stale copy. Computed once per process; assets do not change at runtime.
    """
    digest = hashlib.sha256()
    if STATIC_DIR.is_dir():
        for path in sorted(p for p in STATIC_DIR.rglob("*") if p.is_file()):
            digest.update(path.relative_to(STATIC_DIR).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
