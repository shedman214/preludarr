#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# End-to-end test of a Preludarr image with disposable data (network off).
#   fresh install : non-root start, setup code, sign-in, authenticated writes, CSRF/origin, download + upload, API read-only,
#                   streaming install progress, slow-client cut-off
#   upgrade       : (only when OLD_IMAGE is given) an install made by OLD_IMAGE as root is started with IMAGE as PUID 1000:
#                   /config handed over, old password works, API scan permission kept, data intact
# Usage: tools/e2e_test.sh IMAGE [OLD_IMAGE]
set -eu
IMAGE=${1:?usage: tools/e2e_test.sh IMAGE [OLD_IMAGE]}
OLD=${2:-}
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
APP=preludarr-e2e-$$
PW=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 24)
failed=0
cleanup() { docker rm -f -v "$APP" >/dev/null 2>&1 || true; docker run --rm -v "$WORK":/w alpine:3.24 rm -rf /w/fresh /w/up >/dev/null 2>&1 || true; rm -rf "$WORK" 2>/dev/null || true; }
trap cleanup EXIT

wait_up() { i=0; until docker logs "$APP" 2>&1 | grep -q 'listening on'; do i=$((i + 1)); [ $i -gt 90 ] && { docker logs "$APP" | tail -20; return 1; }; sleep 1; done; }
client() { docker run --rm --network "container:$APP" -v "$REPO":/src:ro -e PASSWORD="$PW" "$@" --entrypoint python3 "$IMAGE" -B /src/tests/e2e/client.py || failed=$((failed + 1)); }
owner_ok() { # every file in the config folder belongs to 1000:1000
  bad=$(docker exec "$APP" sh -c 'find /config -xdev \( ! -user 1000 -o ! -group 1000 \) | head -3')
  [ -z "$bad" ] && echo "PASS /config belongs to PUID:PGID 1000:1000" || { echo "FAIL files not owned by 1000:1000: $bad"; failed=$((failed + 1)); }
}
runs_as() { u=$(docker exec "$APP" sh -c "ps -o user,comm | awk '/python3/{print \$1; exit}'"); [ "$u" = "$1" ] && echo "PASS Preludarr runs as $1" || { echo "FAIL Preludarr runs as '$u', expected $1"; failed=$((failed + 1)); }; }

echo "== fresh install ($IMAGE)"
mkdir -p "$WORK/fresh"
docker run -d --name "$APP" --network none -e PUID=1000 -e PGID=1000 -e PRELUDARR_UPDATE_CHECK=0 -v "$WORK/fresh":/config "$IMAGE" >/dev/null
wait_up
runs_as 1000
owner_ok
docker exec -u 1000:1000 "$APP" python3 -c "
import sqlite3, json, time
c = sqlite3.connect('/config/library.db')
c.execute(\"INSERT OR REPLACE INTO shows VALUES ('/media/shows/E2E Show (2020)','Shows','E2E Show (2020)','missing','',datetime('now'))\")
c.execute('INSERT OR REPLACE INTO theme_suggestions VALUES (?,?,?,?)', ('/media/shows/E2E Show (2020)',
          json.dumps([{'source': 'plex', 'url': 'https://tvthemes.plexapp.com/12345.mp3', 'title': 'E2E Show theme'}]), '', int(time.time())))
c.commit()"
CODE=$(docker logs "$APP" 2>&1 | sed -n 's/.*enter this setup code: \([A-Z2-9]\{4\}-[A-Z2-9]\{4\}\).*/\1/p' | tail -1)
[ -n "$CODE" ] && echo "PASS setup code found in the container log" || { echo "FAIL no setup code in the log"; failed=$((failed + 1)); }
client -e MODE=fresh -e SETUP_CODE="$CODE"
docker rm -f -v "$APP" >/dev/null

if [ -n "$OLD" ]; then
  echo "== upgrade: $OLD (as root) -> $IMAGE (PUID 1000)"
  mkdir -p "$WORK/up"
  AUTH=$(python3 -c "import hashlib,os,sys;s=os.urandom(16);print(s.hex()+':'+hashlib.pbkdf2_hmac('sha256',sys.argv[1].encode(),s,240000).hex())" "$PW")
  docker run -d --name "$APP" --network none -e THEMEARR_WRITE_AUTH="$AUTH" -v "$WORK/up":/config "$OLD" >/dev/null
  wait_up
  old_owner=$(docker exec "$APP" sh -c 'stat -c %u /config/library.db')
  before=$(docker exec "$APP" python3 -c "import sqlite3;print(sorted(r[0] for r in sqlite3.connect('/config/library.db').execute(\"SELECT name FROM sqlite_master WHERE type='table'\")))")
  docker rm -f -v "$APP" >/dev/null
  docker run -d --name "$APP" --network none -e PUID=1000 -e PGID=1000 -e PRELUDARR_UPDATE_CHECK=0 -e THEMEARR_WRITE_AUTH="$AUTH" -v "$WORK/up":/config "$IMAGE" >/dev/null
  wait_up
  runs_as 1000
  owner_ok
  if [ "$old_owner" = "1000" ]; then
    echo "PASS the old version already ran as 1000 (no handover needed)"
  else
    docker logs "$APP" 2>&1 | grep -q "giving /config to 1000:1000" && echo "PASS the one-time /config handover is logged" || { echo "FAIL no handover log line"; failed=$((failed + 1)); }
  fi
  after=$(docker exec "$APP" python3 -c "import sqlite3;print(sorted(r[0] for r in sqlite3.connect('/config/library.db').execute(\"SELECT name FROM sqlite_master WHERE type='table'\")))")
  python3 -c "import ast,sys;b=set(ast.literal_eval(sys.argv[1]));a=set(ast.literal_eval(sys.argv[2]));sys.exit(0 if b<=a else 1)" "$before" "$after" \
    && echo "PASS every database table from the old version is still there" || { echo "FAIL tables lost in the upgrade"; failed=$((failed + 1)); }
  client -e MODE=upgrade -e USERNAME=admin
  docker rm -f -v "$APP" >/dev/null
else
  echo "SKIP upgrade test (no OLD_IMAGE given)"
fi

[ $failed -eq 0 ] && echo 'E2E PASSED' || echo "E2E: $failed check group(s) FAILED"
exit $failed
