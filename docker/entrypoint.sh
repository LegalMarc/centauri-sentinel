#!/bin/sh
# docker-entrypoint.sh
#
# Runs as root on container startup.  Ensures /data/snapshots exists and is
# owned by the sentinel user (UID 1000), then drops privileges via gosu and
# exec-s the requested command as sentinel.
#
# This handles the bind-mount case: Docker creates bind-mounted host
# directories as root:root on first use, which would cause a PermissionError
# at runtime for the non-root sentinel user.  Named volumes inherit the image
# layer ownership set by the Dockerfile RUN chown and do not need fixing, but
# the chown is idempotent so running it every startup is safe and cheap.
#
# NOTE: USER sentinel must NOT be set in the Dockerfile before this entrypoint;
# the script must start as root so it can call chown.

set -eu

# Ensure data directories exist and are owned by sentinel (UID 1000).
mkdir -p /data/snapshots
chown -R sentinel:sentinel /data

# Drop from root to sentinel and exec the requested command.
exec gosu sentinel "$@"
