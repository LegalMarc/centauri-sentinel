#!/bin/sh
# Generates /shared/token on first run; idempotent on subsequent runs.
# token-init runs as root; sentinel runs as UID 1000 (keep in sync with
# Dockerfile's useradd UID 1000).  The token volume is private (not
# world-accessible), so 0644 is safe: only containers that mount the volume
# can read it.  We use chown to make sentinel the owner as well, so a
# read-only volume mount works without requiring group tricks.
set -e

TOKEN_FILE="${TOKEN_FILE:-/shared/token}"

if [ -f "$TOKEN_FILE" ]; then
    echo "token-init: token already exists — skipping generation."
else
    (umask 077 && python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$TOKEN_FILE")
    echo "token-init: generated new ML API token at $TOKEN_FILE."
fi

# 0644: readable by sentinel (UID 1000) on the private ml-token volume.
# chown to UID 1000 so the file is owned by sentinel even inside a ro mount.
chown 1000:1000 "$TOKEN_FILE" 2>/dev/null || true
chmod 644 "$TOKEN_FILE"
