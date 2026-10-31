# SPDX-License-Identifier: GPL-3.0-or-later
"""Jellyfin Quick Connect (1.12): code, server-side polling, token saved (never sent to the browser). Run in a Preludarr image, --network none."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import media_servers as ms  # noqa: E402
import jellyfin_quickconnect as jq  # noqa: E402
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


PORT = 18997
LOG = work / 'fake.log'; LOG.write_text('')
srv = subprocess.Popen([sys.executable, '/src/tests/fake_media_server.py', str(PORT), str(LOG)])
time.sleep(1.0)
real_base = ms._base_url
ms._base_url = lambda raw: str(raw).rstrip('/') if str(raw).startswith('http://127.0.0.1:') else real_base(raw)  # fake Jellyfin on loopback
URL = f'http://127.0.0.1:{PORT}'


class H:
    def __init__(self, path, form=None):
        body = urllib.parse.urlencode(form or {}).encode()
        self.path = path; self.headers = {'Content-Length': str(len(body))}; self.rfile = io.BytesIO(body); self.sent = None

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)


h = H('/media-servers/jellyfin-qc/start', {'url': 'http://8.8.8.8:8096'}); jq.post(h, app.CONFIG)
check('A public plain-HTTP address is refused (same rules as a manual connection)', h.sent[0] == 400 and 'private LAN' in json.loads(h.sent[1])['error'])
ms._write(app.CONFIG, 'servers', {'jellyfin': {'kind': 'jellyfin', 'url': URL, 'api_key': 'old', 'enabled': False, 'mappings': {'/x': 'abc'}}})
h = H('/media-servers/jellyfin-qc/start', {'url': URL}); jq.post(h, app.CONFIG)
st = json.loads(h.sent[1])
check('Start: Jellyfin returns a code to show the user', h.sent[0] == 200 and st['code'] == '482913' and 'id' in st and 'qcsecret' not in h.sent[1])
h = H('/media-servers/jellyfin-qc/status?id=' + st['id']); jq.get(h, app.CONFIG)
check('Before approval: "waiting"', json.loads(h.sent[1])['state'] == 'waiting')
h = H('/media-servers/jellyfin-qc/status?id=' + st['id']); jq.get(h, app.CONFIG)
done = json.loads(h.sent[1])
rec = ms._read(app.CONFIG, 'servers')['jellyfin']
check('After approval: connected as the approving user, connection tested and saved', done == {'state': 'done', 'user': 'jfadmin', 'admin': True}
      and rec['api_key'] == 'jftoken' and rec['url'] == URL and rec['enabled'] is True, (done, rec.get('url')))
check('No token or secret reaches the browser', 'jftoken' not in h.sent[1] and 'qcsecret' not in h.sent[1])
check('Same server as before: per-title links are kept', rec.get('mappings') == {'/x': 'abc'})
h = H('/media-servers/jellyfin-qc/status?id=' + st['id']); jq.get(h, app.CONFIG)
check('A finished Quick Connect cannot be reused', json.loads(h.sent[1])['state'] == 'expired')
subprocess.run([sys.executable, '-c', 'pass'])
# Quick Connect switched off on the server
import urllib.request  # noqa: E402
jq_call = jq._call
jq._call = lambda base, method, path, body=None, limit=1_000_000: False if path == '/QuickConnect/Enabled' else jq_call(base, method, path, body, limit)
h = H('/media-servers/jellyfin-qc/start', {'url': URL}); jq.post(h, app.CONFIG)
check('Quick Connect turned off in Jellyfin: a clear message on where to turn it on', h.sent[0] == 400 and 'Dashboard → General' in json.loads(h.sent[1])['error'])
jq._call = jq_call
h = H('/media-servers/jellyfin-qc/start', {'url': 'http://192.168.250.250:1'}); jq.post(h, app.CONFIG)
check('Unreachable address: a plain error, no traceback', h.sent[0] in (400, 502) and 'error' in json.loads(h.sent[1]))
src = Path('/src/preludarr/app.py').read_text()
check('Routes wired', 'jellyfin_quickconnect.post(self,CONFIG)' in src and 'jellyfin_quickconnect.get(self,CONFIG)' in src)
class P:
    path = '/media-servers?tab=media&configure=jellyfin'; sent = None
    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)
pg = P(); ms.settings_page(pg, app.CONFIG, app.ROOTS, app.db)
check('The Jellyfin dialog offers Quick Connect, pre-filled with the saved address', 'Connect with Quick Connect' in pg.sent[1] and 'value="' + URL + '"' in pg.sent[1])
srv.terminate()
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
