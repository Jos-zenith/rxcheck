#!/bin/sh
# The persistent disk is mounted over RXCHECK_HOME at run time and is usually owned by root. Hand it to the
# unprivileged app user, then drop root for good before starting the app.
set -e
mkdir -p "$RXCHECK_HOME"
if [ "$(id -u)" = "0" ]; then
    chown -R 10001:10001 "$RXCHECK_HOME"
    exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
fi
exec "$@"
