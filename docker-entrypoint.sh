#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Remap appuser/appgroup to match the host user's UID/GID so that the
# bind-mounted /app/config directory is writable without changing its
# ownership on the host.
groupmod -o -g "$PGID" appgroup
usermod  -o -u "$PUID" appuser

mkdir -p /app/config

exec gosu appuser "$@"
