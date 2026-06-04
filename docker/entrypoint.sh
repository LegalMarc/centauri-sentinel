#!/bin/sh
# docker-entrypoint.sh
#
# Ensures the /data/snapshots directory exists and is owned by the sentinel
# user regardless of who created the Docker named volume.
# Named volumes created by Docker default to root:root ownership, which would
# cause a PermissionError at runtime for the non-root sentinel user (UID 1000).
#
# This script is run as root (before USER sentinel), fixes ownership, then
# drops privileges via exec.

set -eu

# Fix ownership of the data and shared token volumes on every startup.
# chown is idempotent — safe to run even when ownership is already correct.
chown -R sentinel:sentinel /data 2>/dev/null || true
chown -R sentinel:sentinel /shared 2>/dev/null || true

exec gosu sentinel "$@"
