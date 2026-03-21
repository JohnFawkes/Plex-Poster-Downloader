#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# gosu supports numeric uid:gid directly, so no usermod/groupmod needed.
# Just ensure the config dir exists and is owned by the target user.
mkdir -p /app/config
chown "${PUID}:${PGID}" /app/config

exec gosu "${PUID}:${PGID}" "$@"
