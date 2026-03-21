#!/bin/sh
set -e

# Ensure the config directory exists and is writable by appuser before
# dropping privileges. This is needed when /app/config is a bind-mount
# created by root on the host.
mkdir -p /app/config
chown -R appuser:appgroup /app/config

exec gosu appuser "$@"
