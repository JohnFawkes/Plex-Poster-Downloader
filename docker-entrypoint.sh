#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# gosu supports numeric uid:gid directly, so no usermod/groupmod needed.
# Just ensure the config dir exists and is owned by the target user.
mkdir -p /app/config /app/config/downloaded_posters
chown "${PUID}:${PGID}" /app/config /app/config/downloaded_posters

exec gosu "${PUID}:${PGID}" "$@"
