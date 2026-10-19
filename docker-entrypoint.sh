#!/bin/sh
# Fly mounts the volume root-owned at runtime, after the image was built, so the
# build-time chown doesn't apply. Fix ownership as root, then drop to `app`.
set -e
mkdir -p "$(dirname "${ASX_SHORTS_DB:-/data/asx_shorts.sqlite}")"
chown -R app:app /data
exec setpriv --reuid=app --regid=app --init-groups "$@"
