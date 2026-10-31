# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign in with Plex (1.12): PIN, popup URL, server-side polling, server choice, tokens never sent to the browser. Run in a Preludarr image, --network none."""
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
import plex_signin as ps  # noqa: E402
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


PORT = 18996
LOG = work / 'fake.log'; LOG.write_text('')
srv = subprocess.Popen([sys.executable, '/src/tests/fake_media_server.py', str(PORT), str(LOG)])
time.sleep(1.0)
real_base = ms._base_url
ms._base_url = lambda raw: str(raw).rstrip('/') if str(raw).startswith('http://127.0.0.1:') else real_base(raw)  # fake Plex on loopback
ps.PLEX_TV = ps.CLIENTS_TV = f'http://127.0.0.1:{PORT}'
real_call = ps._call


class H:
    def __init__(self, path, form=None):
        body = urllib.parse.urlencode(form or {}).encode()
        self.path = path; self.headers = {'Content-Length': str(len(body))}; self.rfile = io.BytesIO(body); self.sent = None; self.loc = None

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)


def redirect(h, location):
    h.loc = urllib.parse.unquote_plus(location)


h = H('/media-servers/plex-signin/start'); ps.post(h, app.CONFIG, redirect)
start = json.loads(h.sent[1])
q = urllib.parse.parse_qs(start['auth_url'].split('#?', 1)[1])
check('Start: a PIN is created and the popup goes to app.plex.tv with this install\'s client id', h.sent[0] == 200 and start['auth_url'].startswith('https://app.plex.tv/auth#?')
      and q['code'] == ['abcd1234wxyz'] and q['context[device][product]'] == ['Preludarr'] and q['clientID'] == [(work / 'config' / ps.CLIENT_FILE).read_text()])
check('The client id is kept privately (0600) and reused', oct((work / 'config' / ps.CLIENT_FILE).stat().st_mode & 0o777) == '0o600' and ps._client_id(app.CONFIG) == q['clientID'][0])
h = H('/media-servers/plex-signin/status?pin=' + start['pin']); ps.get(h, app.CONFIG)
check('Before approval: "waiting"', json.loads(h.sent[1]) == {'state': 'waiting'})
h = H('/media-servers/plex-signin/status?pin=' + start['pin']); ps.get(h, app.CONFIG)
st = json.loads(h.sent[1])
check('After approval: every Plex server of the account is listed (players and servers without access skipped)', st['state'] == 'ready'
      and [x['name'] for x in st['servers']] == ['Living Room Plex', 'Second Plex'])
check('Local address offered first', st['servers'][0]['connections'][0]['uri'] == f'http://127.0.0.1:{PORT}' and st['servers'][0]['connections'][0]['label'].startswith('Local network'))
check('No token ever reaches the browser', 'token' not in h.sent[1].lower() and 'plextoken' not in h.sent[1] and 'accounttoken' not in h.sent[1])
ms._write(app.CONFIG, 'servers', {'plex': {'kind': 'plex', 'url': 'http://192.168.9.9:32400', 'api_key': 'old', 'enabled': False, 'machine': 'machine-main', 'mappings': {'/x': '1'}}})
h = H('/media-servers/plex-signin/choose', {'pin': start['pin'], 'use_7': '0', 'enabled': '1'}); ps.post(h, app.CONFIG, redirect)
check('An invalid choice is refused and nothing is saved', 'Plex not connected' in h.loc and ms._server_map(app.CONFIG)['plex']['api_key'] == 'old')
h = H('/media-servers/plex-signin/choose', {'pin': start['pin'], 'use_0': '0', 'use_1': '0', 'main': '0', 'enabled': '1'}); ps.post(h, app.CONFIG, redirect)
doc = ms._read(app.CONFIG, 'servers')
check('Two servers chosen: the main one is saved as the Plex connection (address + its own token, tested live)', 'connected to Living Room Plex, Second Plex (main: Living Room Plex)' in h.loc
      and doc['plex']['url'] == f'http://127.0.0.1:{PORT}' and doc['plex']['api_key'] == 'plextoken' and doc['plex']['enabled'] is True, h.loc)
check('…the main server is the same Plex server as before (server id), so its title links are kept', doc['plex'].get('mappings') == {'/x': '1'} and 'Title links kept: 1' in h.loc)
check('…the second server is stored as an extra Plex server with its own token', doc.get('plex-2', {}).get('api_key') == 'plextoken2' and doc['plex-2']['name'] == 'Second Plex')
check('The account token is never stored', 'accounttoken' not in (work / 'config' / 'themearr_media_servers_v1.json').read_text())
mc = __import__('movie_collections')
srvs = mc._servers(app)
check('Collections gets a tab per Plex server', list(srvs) == ['plex', 'plex-2'] and mc._label('plex-2') == 'Plex — Second Plex')
ms_page = __import__('io')
class P:
    path = '/media-servers?tab=media'; sent = None
    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)
pg = P(); ms.settings_page(pg, app.CONFIG, app.ROOTS, app.db); html = pg.sent[1]
check('Connect lists the other Plex server with a Remove button', 'Other Plex servers' in html and 'Second Plex' in html and 'plex-extra/remove' in html)
h = H('/media-servers/plex-extra/remove', {'key': 'plex-2'}); ps.post(h, app.CONFIG, redirect)
check('Removing needs the confirmation box', 'Tick the box' in h.loc and 'plex-2' in ms._read(app.CONFIG, 'servers'))
h = H('/media-servers/plex-extra/remove', {'key': 'plex-2', 'confirm': '1'}); ps.post(h, app.CONFIG, redirect)
check('Remove takes the extra server out; the main one stays', 'plex-2' not in ms._read(app.CONFIG, 'servers') and ms._read(app.CONFIG, 'servers')['plex']['api_key'] == 'plextoken')
h = H('/media-servers/plex-extra/remove', {'key': 'plex', 'confirm': '1'}); ps.post(h, app.CONFIG, redirect)
check('The main Plex connection cannot be removed through this button', 'plex' in ms._read(app.CONFIG, 'servers') and 'Only an extra' in h.loc)
# a second sign-in where the user makes the OTHER server the main one: old title links belong to another server and are cleared
ps.PLEX_TV = ps.CLIENTS_TV = f'http://127.0.0.1:{PORT}'
h = H('/media-servers/plex-signin/start'); ps.post(h, app.CONFIG, redirect); pin2 = json.loads(h.sent[1])['pin']
for _ in range(2):
    h = H('/media-servers/plex-signin/status?pin=' + pin2); ps.get(h, app.CONFIG)
h = H('/media-servers/plex-signin/choose', {'pin': pin2, 'use_1': '0', 'main': '1', 'enabled': '1'}); ps.post(h, app.CONFIG, redirect)
doc = ms._read(app.CONFIG, 'servers')
check('Switching the main server to a different Plex server clears the old title links and says so', doc['plex']['api_key'] == 'plextoken2'
      and not doc['plex'].get('mappings') and 'old title links were cleared' in h.loc, h.loc)
h = H('/media-servers/plex-signin/status?pin=' + pin2); ps.get(h, app.CONFIG)
check('A used sign-in cannot be reused', json.loads(h.sent[1])['state'] == 'expired')
h = H('/media-servers/plex-signin/status?pin=99999'); ps.get(h, app.CONFIG)
check('Unknown sign-in id → expired', json.loads(h.sent[1])['state'] == 'expired')
ps.PLEX_TV = 'http://127.0.0.1:9'   # nothing listens there
h = H('/media-servers/plex-signin/start'); ps.post(h, app.CONFIG, redirect)
check('Plex unreachable: a plain error, no traceback', h.sent[0] == 502 and 'error' in json.loads(h.sent[1]))
check('Only plex.tv and clients.plex.tv over HTTPS are used in the shipped code', "PLEX_TV = 'https://plex.tv'" in Path('/src/preludarr/plex_signin.py').read_text()
      and "CLIENTS_TV = 'https://clients.plex.tv'" in Path('/src/preludarr/plex_signin.py').read_text())
src = Path('/src/preludarr/app.py').read_text()
check('Routes wired', "plex_signin.post(self,CONFIG,auth.redirect)" in src and "plex_signin.get(self,CONFIG)" in src)
srv.terminate()
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
