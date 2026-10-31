#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Start Preludarr as PUID:PGID (default 1000:1000) instead of root.
#   PUID=99 PGID=100 on Unraid (nobody:users) · PUID=0 keeps root (not recommended) · UMASK (default 002) for new files.
# Only the config folder is ever given to PUID:PGID here; media folders are never changed.
set -eu
umask "${UMASK:-002}"

if [ "$(id -u)" != "0" ]; then        # started with --user: nothing to switch
  exec "$@"
fi

PUID=${PUID:-1000}
PGID=${PGID:-1000}
for v in "$PUID" "$PGID"; do
  case "$v" in ''|*[!0-9]*) echo "Preludarr: PUID and PGID must be numbers (got '$PUID' / '$PGID')." >&2; exit 64;; esac
  [ "$v" -le 65535 ] || { echo "Preludarr: PUID/PGID must be 0-65535." >&2; exit 64; }
done

if [ "$PUID" = "0" ]; then
  echo "Preludarr: PUID=0, running as root. Set PUID/PGID to the owner of your media to run as a normal user."
  exec "$@"
fi

CONFIG=${CONFIG_DIR:-/config}
mkdir -p "$CONFIG"
# Give the config folder to PUID:PGID once (first start, or after running as root before 1.12). Symlinks are never followed.
if [ -n "$(find "$CONFIG" -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) ! -type l -print 2>/dev/null | head -n 1)" ]; then
  echo "Preludarr: giving $CONFIG to $PUID:$PGID (one-time)."
  find "$CONFIG" -xdev ! -type l \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} +
fi

export HOME=/tmp/preludarr-home        # yt-dlp / Deno caches; not kept
mkdir -p "$HOME" && chown "$PUID:$PGID" "$HOME" && chmod 700 "$HOME"
export PUID PGID
echo "Preludarr: running as $PUID:$PGID."
exec su-exec "$PUID:$PGID" "$@"
