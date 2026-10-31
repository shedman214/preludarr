#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Start the image as a brand-new install and check it answers like one. Usage: tools/smoke_test.sh IMAGE
set -eu
IMAGE=${1:?usage: tools/smoke_test.sh IMAGE}
NAME=preludarr-smoke-$$
trap 'docker rm -f -v "$NAME" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$NAME" --network none "$IMAGE" >/dev/null
i=0; until docker logs "$NAME" 2>&1 | grep -q 'listening on'; do i=$((i + 1)); [ $i -gt 60 ] && { docker logs "$NAME"; exit 1; }; sleep 1; done
docker logs "$NAME" 2>&1 | grep -q 'first-run setup' || { echo 'FAIL: no setup code in the log'; exit 1; }
docker exec -i "$NAME" python3 - <<'PY'
import urllib.error, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
opener = urllib.request.build_opener(NoRedirect)
def get(path):
    try:
        r = opener.open('http://127.0.0.1:8787' + path, timeout=10); return r.status, ''
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Location', '')
import time
for _ in range(60):  # the log line can appear just before the port opens
    try:
        get('/health'); break
    except urllib.error.URLError:
        time.sleep(1)
checks = [('/health', get('/health')[0] == 200), ('/ -> /setup', get('/') == (303, '/setup')), ('/setup', get('/setup')[0] == 200)]
for name, ok in checks: print(('PASS ' if ok else 'FAIL ') + name)
raise SystemExit(0 if all(ok for _, ok in checks) else 1)
PY
docker exec "$NAME" sh -c 'command -v ffmpeg >/dev/null && command -v deno >/dev/null && yt-dlp --version >/dev/null' && echo 'PASS ffmpeg, deno, yt-dlp present'
echo 'SMOKE TEST PASSED'
