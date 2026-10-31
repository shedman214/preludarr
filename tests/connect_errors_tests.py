# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.2 — clear connection errors for Sonarr/Radarr (and media servers). Run in a Themearr image, --network none:
python3 /src/tests/connect_errors_tests.py
"""
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

work = Path(tempfile.mkdtemp())
lib = work / 'lib'
for n in ('anime/Romelia War Chronicle', 'anime/Koupen-chan (2025)', 'movies/Bubble (2022)'):
    (lib / n).mkdir(parents=True)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(lib / 'anime'), MOVIE_ROOT=str(lib / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
srv = subprocess.Popen([sys.executable, '/src/tests/fake_arr.py', '18991', '17880'])  # odd port = Sonarr, even = Radarr


class Misc(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        if port == 18100:  # a plain web page, not an API
            raw = b'<html><body>Welcome to some router</body></html>'
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        elif port == 18101:
            self.send_response(404); self.end_headers()
        elif port == 18102:
            self.send_response(302); self.send_header('Location', 'https://example.invalid/login'); self.end_headers()
        else:
            self.send_response(500); self.end_headers()


class Fast(ThreadingHTTPServer):
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = 'localhost', self.server_address[1]


for port in (18100, 18101, 18102, 18103):
    threading.Thread(target=Fast(('127.0.0.1', port), Misc).serve_forever, daemon=True).start()


def garbage_server(port):  # answers with bytes that are not HTTP
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', port)); s.listen(5)
    def run():
        while True:
            c, _ = s.accept()
            try:
                c.recv(1024); c.sendall(b'\x00\x01garbage\r\n\r\n')
            finally:
                c.close()
    threading.Thread(target=run, daemon=True).start()


def silent_server(port):  # accepts, never answers
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', port)); s.listen(5)
    keep = []
    threading.Thread(target=lambda: [keep.append(s.accept()) for _ in iter(int, 1)], daemon=True).start()


garbage_server(18104)
silent_server(18105)
for _ in range(100):
    try:
        urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:17880/api/v3/system/status', headers={'X-Api-Key': 'testkey123'}), timeout=1); break
    except Exception:
        time.sleep(0.2)

import app  # noqa: E402
import arr as arr  # noqa: E402
import neterrors as ne  # noqa: E402
import permissions as perm  # noqa: E402
import media_servers as v4  # noqa: E402
real_base = v4._base_url
v4._base_url = lambda raw: raw if raw.startswith(('http://127.0.0.1:', 'https://127.0.0.1:')) else real_base(raw)  # tests only

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


class H:
    def __init__(self, body): b = body.encode(); self.headers = {'Content-Length': str(len(b))}; self.rfile = io.BytesIO(b)


redirect = lambda h, url: urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('arr_notice', [''])[0]  # noqa: E731


def post(fields):
    return arr.post(app, H(urllib.parse.urlencode(fields)), redirect)


def card(iid):
    html = arr.connect_html(app.CONFIG)
    i = html.find('name="id" value="' + iid + '"')
    start = html.rfind('<section class="arr-card">', 0, i)
    return html[start:html.find('</section>', i)]


def raw_error(url, timeout=3):
    try:
        urllib.request.urlopen(url, timeout=timeout)
    except Exception as error:  # noqa: BLE001
        return error
    return None


app.discover(); app.auto_themes.init(app)
KEYS = ('testkey123', 'wrongkey999')

# ---------------------------------------------------------------- explain() on real failures ----
cases = [
    ('refused port', raw_error('http://127.0.0.1:18199/'), 'Nothing is answering'),
    ('timeout', raw_error('http://127.0.0.1:18105/', timeout=1), 'timed out'),
    ('HTTP 404', raw_error('http://127.0.0.1:18101/'), 'not the Sonarr API'),
    ('HTTP 500', raw_error('http://127.0.0.1:18103/'), 'server error'),
    ('not HTTP at all', raw_error('http://127.0.0.1:18104/'), 'closed without a proper answer'),
    ('https to an http port', raw_error('https://127.0.0.1:18100/'), 'does not seem to speak HTTPS'),
    ('unknown host', urllib.error.URLError(socket.gaierror(-2, 'Name or service not known')), 'could not be found'),
    ('untrusted certificate', urllib.error.URLError(ssl.SSLCertVerificationError(1, 'self-signed certificate')), 'not trusted'),
    ('host unreachable', urllib.error.URLError(OSError(113, 'No route to host')), 'cannot be reached'),
    ('not JSON', json.JSONDecodeError('Expecting value', '<html>', 0), 'not the Sonarr API'),
]
for label, error, want in cases:
    reason, todo = ne.explain(error, 'Sonarr', 'http://192.168.1.30:8989')
    check('explain: ' + label, want in reason and bool(todo), reason + ' | ' + todo)
reason, todo = ne.explain(urllib.error.HTTPError('u', 401, 'x', {}, None), 'Radarr')
check('explain: 401 says where to find the key', 'rejected the API key' in reason and 'Radarr → Settings → General → Security → API Key' in todo, todo)
reason, todo = ne.explain(urllib.error.HTTPError('u', 401, 'x', {}, None), 'Plex')
check('explain: Plex 401 mentions the Plex token', 'token' in todo.lower())
check('explain: unknown error still names the type + a next step', 'ZeroDivisionError' in ne.text(ZeroDivisionError(), 'Sonarr'))

# ------------------------------------------------------------------- whole flow via the Connect page ----
r = post({'action': 'save', 'kind': 'sonarr', 'name': 'Anime', 'url': 'http://127.0.0.1:18199', 'api_key': 'testkey123'})
iid = arr.load(app.CONFIG)['instances'][0]['id']
check('Save tests right away: refused port → red banner with reason', r.startswith('✗') and 'Nothing is answering' in r, r)
c = card(iid)
check('Card keeps the problem visible: "Last check failed" tag (V1.9.14) + Can’t connect box', 'Last check failed' in c and 'Can’t connect' in c and 'Nothing is answering' in c, c[:300])
check('Card no longer says “not tested yet” after a failed test', 'not tested yet' not in c and 'unknown — not connected' in c)

r = post({'action': 'save', 'id': iid, 'name': 'Anime', 'url': 'http://127.0.0.1:18991', 'api_key': 'wrongkey999'})
check('Wrong key → says the key was rejected + where to find it', 'rejected the API key' in r and 'Settings → General → Security' in r, r)
r = post({'action': 'sync', 'id': iid})
check('Sync with wrong key → red banner, reason shown', r.startswith('✗') and 'rejected the API key' in r, r)
check('API key never shown in banners or card', all(k not in r + card(iid) for k in KEYS))

r = post({'action': 'save', 'id': iid, 'name': 'Anime', 'url': 'http://127.0.0.1:17880', 'api_key': 'testkey123'})
check('Sonarr pointed at Radarr → “This address is Radarr, not Sonarr”', 'This address is Radarr, not Sonarr' in r, r)
r = post({'action': 'save', 'id': iid, 'name': 'Anime', 'url': 'http://127.0.0.1:18100', 'api_key': 'testkey123'})
check('A plain web page → “not the Sonarr API”', 'not the Sonarr API' in r, r)
r = post({'action': 'save', 'id': iid, 'name': 'Anime', 'url': 'http://127.0.0.1:18102', 'api_key': 'testkey123'})
check('Redirect → explained', 'redirects' in r, r)

r = post({'action': 'save', 'id': iid, 'name': 'Anime', 'url': 'http://127.0.0.1:18991', 'api_key': 'testkey123'})
c = card(iid)
check('Right address + key → green banner “connected”', r.startswith('✓') and 'connected to Sonarr 4.0.99.1' in r, r)
check('Card: "Last check succeeded" tag (V1.9.14 wording), no old error left', 'Last check succeeded' in c and 'Can’t connect' not in c and 'Sync failed' not in c and 'rejected' not in c, c[:400])
check('Card shows the suggested mapping with a Preview button + how suggestions work (V1.9.10)', 'Suggested:' in c and '/data/tv/anime' in c
      and 'Preview mapping' in c and 'Use this mapping' not in c and 'checked up to 400' in c, c[:600])

r = post({'action': 'sync', 'id': iid})
c = card(iid)
check('Sync without a mapping → amber "needs attention" (not a red error, V1.9.14) and explains', r.startswith('⚠') and 'needs attention' in r and 'none of its' in r and 'path mapping' in r, r)
check('Card warns “Connected, but nothing lines up”', 'nothing lines up' in c)

n = next(i for i, x in enumerate(arr.load(app.CONFIG)['instances'][0]['suggested']) if x['remote'] == '/data/tv/anime')


def post_full(fields):  # returns the whole redirect query (notice + preview token)
    return arr.post(app, H(urllib.parse.urlencode(fields)), lambda h, url: {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()})


def snapshot():
    with app.db() as cx:
        ids = cx.execute('SELECT COUNT(*) FROM media_ids').fetchone()[0]
        added = cx.execute('SELECT COUNT(*) FROM show_added').fetchone()[0]
    return ids, added, sorted(p.name for p in (lib / 'anime').iterdir()), dict(arr._STATE.get(iid) or {}).get('at')


before = snapshot()
q = post_full({'action': 'preview_suggestion', 'id': iid, 'n': str(n)})
inst = arr.load(app.CONFIG)['instances'][0]
check('Preview (V1.9.10): nothing saved, says so', inst['mappings'] == [] and 'nothing has been saved' in q.get('arr_notice', ''), q)
check('Preview: no sync, no IDs/dates written, no folders created', snapshot() == before, (snapshot(), before))
tok = q.get('arr_preview', '')
html = arr.connect_html(app.CONFIG, '', tok)
check('Preview panel: mapping, counts, examples, what Save does, Save + Cancel', all(x in html for x in ('Preview: <code>/data/tv/anime</code>', 'under this path',
      'folders found in', 'Examples that line up', 'Nothing is synced, scanned, created or moved', 'value="save_suggestion"', '>Cancel</a>')), html[html.find('arr-preview'):][:900])
check('Preview hidden without its token', 'arr-preview"' not in arr.connect_html(app.CONFIG) and 'arr-preview"' not in arr.connect_html(app.CONFIG, '', 'bogus'))
q2 = post_full({'action': 'use_suggestion', 'id': iid, 'n': str(n)})
check('Old “Use this mapping” form now only previews (never saves)', arr.load(app.CONFIG)['instances'][0]['mappings'] == [] and q2.get('arr_preview'), q2)
r = post({'action': 'save_suggestion', 'id': iid, 'token': 'expired-or-made-up'})
check('Save with an unknown/expired preview → refused, nothing saved', r.startswith('✗') and 'expired' in r and arr.load(app.CONFIG)['instances'][0]['mappings'] == [], r)
r = post({'action': 'save_suggestion', 'id': iid, 'token': tok})
inst = arr.load(app.CONFIG)['instances'][0]
c = card(iid)
check('Save mapping → saved, says nothing was synced or created', r.startswith('✓') and 'mapping saved' in r and 'Nothing was synced or created' in r, r)
check('Mapping stored', inst['mappings'] == [{'remote': '/data/tv/anime', 'local': str(lib / 'anime')}], inst['mappings'])
check('Save did not sync or create anything', snapshot() == before, (snapshot(), before))
check('Card: mapping listed, suggestion gone', '/data/tv/anime</code> → ' in c and 'Preview mapping' not in c, c[:600])
r = post({'action': 'save_suggestion', 'id': iid, 'token': tok})
check('A preview can be used only once', r.startswith('✗') and 'expired' in r, r)
r = post({'action': 'preview_suggestion', 'id': iid, 'n': '99'})
check('Stale suggestion number → clear message', r.startswith('✗') and 'Test again' in r, r)
r = post({'action': 'sync', 'id': iid})
check('Sync now (explicit) → green banner with counts', r.startswith('✓') and 'synced' in r and ' of ' in r, r)

r = post({'action': 'toggle', 'id': iid}); r = post({'action': 'sync', 'id': iid})
check('Sync while turned off → says so', 'turned off' in r, r)
post({'action': 'toggle', 'id': iid})

# status survives a restart (stored in the settings file, not only in memory)
post({'action': 'save', 'id': iid, 'name': 'Anime', 'url': 'http://127.0.0.1:18199', 'api_key': ''})
arr._STATE.clear()
check('Failure is remembered after a restart', 'Can’t connect' in card(iid))

# password managers
form = arr._form(None, None)
pm = perm.settings_html(app)
check('Password managers told to ignore Sonarr/Radarr fields', form.count('data-bwignore="true"') == 3 and 'data-1p-ignore' in form)
check('Password managers told to ignore chown User/Group', pm.count('data-bwignore="true"') == 3 and 'id="pm-user" maxlength="32" autocomplete="off"' in pm)

srv.terminate()
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
