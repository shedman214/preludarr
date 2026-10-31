# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end checks against a running Preludarr container (started by tools/e2e_test.sh with disposable data).

Runs inside the app container's network namespace (--network container:<app>), so the app itself needs no network.
Env: MODE=fresh|upgrade, SETUP_CODE (fresh), PASSWORD, USERNAME. Secrets are never printed.
"""
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.parse

HOST, PORT = '127.0.0.1', 8787
ORIGIN = f'http://{HOST}:{PORT}'
MODE = os.environ.get('MODE', 'fresh')
USER, PW = os.environ.get('USERNAME', 'e2e-owner'), os.environ['PASSWORD']
results = []
cookie = ''


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


def req(method, path, body=None, headers=None, auth=True, origin=ORIGIN, timeout=30):
    global cookie
    h = {'Host': f'{HOST}:{PORT}'}
    if origin:
        h['Origin'] = origin
    if auth and cookie:
        h['Cookie'] = cookie
    if isinstance(body, dict):
        body = urllib.parse.urlencode(body).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    h.update(headers or {})
    c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    for k, v in r.getheaders():
        if k.lower() == 'set-cookie' and v.startswith('themearr_session=') and 'Max-Age=0' not in v:
            cookie = v.split(';', 1)[0]
    c.close()
    return r.status, dict((k.lower(), v) for k, v in r.getheaders()), data


def csrf(page, action):
    m = re.search(r'action="' + re.escape(action) + r'"[^>]*>.*?name="csrf" value="([^"]+)"', page, re.S)
    return m.group(1) if m else ''


for _ in range(60):
    try:
        if req('GET', '/health', auth=False)[0] == 200:
            break
    except OSError:
        time.sleep(1)

if MODE == 'fresh':
    st, h, _ = req('GET', '/', auth=False)
    check('Fresh install: every page leads to first-run setup', st == 303 and h.get('location') == '/setup')
    st, _, _ = req('GET', '/api/v1/status', auth=False)
    check('Fresh install: the API is closed until setup is finished', st == 503)
    form = {'code': 'AAAA-BBBB', 'username': USER, 'password': PW, 'confirm': PW}
    st, _, _ = req('POST', '/setup', form, auth=False)
    check('Setup with a wrong code is refused', st == 403)
    form['code'] = os.environ['SETUP_CODE']
    st, h, _ = req('POST', '/setup', form, auth=False)
    check('Setup with the code from the log creates the account and signs in', st == 303 and bool(cookie))
else:
    st, h, _ = req('GET', '/', auth=False)
    check('Upgrade: no setup page for an existing install (sign-in instead)', st == 303 and h.get('location', '').startswith('/login'))
    st, h, _ = req('POST', '/login', {'username': USER, 'password': PW}, auth=False)
    check('Upgrade: the old password still signs in', st == 303 and bool(cookie))

st, _, body = req('GET', '/')
check('Signed in: the Library page opens', st == 200 and b'Preludarr' in body)

# ---- authenticated writes, CSRF / origin, sign-in required
page = req('GET', '/system?tab=backup')[2].decode()
st, h, _ = req('POST', '/system/backup/create', {'csrf': csrf(page, '/system/backup/create')})
check('Authenticated write: Create backup works', st == 303 and 'Backup created' in urllib.parse.unquote(h.get('location', '')))
st, h, _ = req('POST', '/system/backup/create', {'csrf': 'x'}, auth=False)
check('The same write without sign-in is refused (sent to sign-in)', st == 303 and h.get('location', '').startswith('/login'))
st, _, _ = req('POST', '/system/backup/create', {'csrf': csrf(req('GET', '/system?tab=backup')[2].decode(), '/system/backup/create')}, origin='http://evil.example')
check('The same write from another site (Origin) is refused', st == 403)

# ---- download (password step) + upload (multipart)
page = req('GET', '/system?tab=backup')[2].decode()
name = re.search(r'(tsbackup-\d{8}T\d{6}Z-[a-f0-9]{8}\.zip)', page).group(1)
dl = req('GET', '/system/backup/download?name=' + name)[2].decode()
st, h, zipdata = req('POST', '/system/backup/download', {'csrf': csrf(dl, '/system/backup/download'), 'name': name, 'password': PW})
check('Backup download needs the password and returns the ZIP', st == 200 and zipdata[:2] == b'PK')
page = req('GET', '/system?tab=backup')[2].decode()
tok = csrf(page, '/system/backup/upload')
boundary = 'e2eboundary7d1'
mp = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{tok}\r\n'
      f'--{boundary}\r\nContent-Disposition: form-data; name="archive"; filename="backup.zip"\r\nContent-Type: application/zip\r\n\r\n').encode() + zipdata + f'\r\n--{boundary}--\r\n'.encode()
st, h, _ = req('POST', '/system/backup/upload', mp, {'Content-Type': 'multipart/form-data; boundary=' + boundary})
check('Upload: the backup ZIP is accepted and verified', st == 303 and '/system/backup/verify' in h.get('location', ''))

# ---- API key permissions
settings = req('GET', '/settings')[2].decode()
key = re.search(r'id="api-key"[^>]*value="([0-9a-f]{32})"', settings) or re.search(r'value="([0-9a-f]{32})"[^>]*id="api-key"', settings)
key = key.group(1) if key else ''
st, _, _ = req('GET', '/api/v1/status', auth=False, origin=None, headers={'X-Api-Key': key})
check('API key (header) can read status', st == 200)
st, _, body = req('POST', '/api/v1/command/scan', auth=False, origin=None, headers={'X-Api-Key': key})
if MODE == 'fresh':
    check('Fresh install: API key is read-only — starting a scan is refused (403)', st == 403)
else:
    check('Upgrade: an existing install keeps "API key may start scans" (202 started or 409 busy)', st in (202, 409), (st, body[:80]))

# ---- streaming install progress (title seeded by the script; no network → the download fails, but progress streams)
if MODE == 'fresh':
    st, h, body = req('POST', '/regular/install', {'path': '/media/shows/E2E Show (2020)', 'idx': '0', 'confirm': '1'}, headers={'X-Preludarr-Progress': 'v1'}, timeout=120)
    lines = [json.loads(x) for x in body.decode().splitlines() if x.strip()]
    check('Install progress streams as JSON lines through the hardened server', st == 200 and 'ndjson' in h.get('content-type', '')
          and len(lines) >= 2 and all('phase' in x for x in lines), (st, h.get('content-type'), body[:200]))
    check('…and reports the failed download plainly (network is off in this test)', lines and lines[-1]['phase'] in ('error', 'failed'), lines[-1:] if lines else '')

# ---- slow client
s = socket.create_connection((HOST, PORT), timeout=40)
t = time.time()
try:
    closed = s.recv(1) == b''
except OSError:
    closed = True
check('A connection that sends nothing is closed by the server (header deadline)', closed and time.time() - t < 35, round(time.time() - t, 1))
s.close()

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
