#!/bin/sh
# Generates /shared/token on first run; idempotent on subsequent runs.
set -e

TOKEN_FILE="${TOKEN_FILE:-/shared/token}"

if [ -f "$TOKEN_FILE" ]; then
    echo "token-init: token already exists — skipping generation."
else
    (umask 077 && python3 -c "import secrets; print(secrets.token_hex(32), end='')" > "$TOKEN_FILE")
    echo "token-init: generated new ML API token at $TOKEN_FILE."
fi

chmod 600 "$TOKEN_FILE"
