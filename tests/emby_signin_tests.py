# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign in to Emby (1.12): username + password sent only to Emby; token saved; password never stored. Run in a Preludarr image, --network none."""
import io
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
import emby_signin as es  # noqa: E402
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


PORT = 18998
LOG = work / 'fake.log'; LOG.write_text('')
srv = subprocess.Popen([sys.executable, '/src/tests/fake_media_server.py', str(PORT), str(LOG)])
time.sleep(1.0)
real_base = ms._base_url
ms._base_url = lambda raw: str(raw).rstrip('/') if str(raw).startswith('http://127.0.0.1:') else real_base(raw)  # fake Emby on loopback
URL = f'http://127.0.0.1:{PORT}'


class H:
    def __init__(self, form):
        body = urllib.parse.urlencode(form).encode()
        self.path = '/media-servers/emby-signin'; self.headers = {'Content-Length': str(len(body))}; self.rfile = io.BytesIO(body); self.loc = None


def redirect(h, location):
    h.loc = urllib.parse.unquote_plus(location)


def post(form):
    h = H(form); es.post(h, app.CONFIG, redirect); return h.loc


ms._write(app.CONFIG, 'servers', {'emby': {'kind': 'emby', 'url': URL, 'api_key': 'old', 'enabled': False, 'mappings': {'/x': 'e1'}}})
loc = post({'url': URL, 'username': 'embyadmin', 'password': 'wrong-pass'})
check('Wrong password: refused with a plain message; nothing saved', 'refused the username or password' in loc and ms._read(app.CONFIG, 'servers')['emby']['api_key'] == 'old', loc)
loc = post({'url': URL, 'username': 'embyadmin', 'password': 'correct-pass'})
rec = ms._read(app.CONFIG, 'servers')['emby']
check('Right username + password: signed in, connection tested and saved with Emby\'s token', 'Emby connected: signed in as embyadmin.' in loc
      and rec['api_key'] == 'embytoken' and rec['url'] == URL and rec['enabled'] is True and rec['admin'] is True, loc)
check('Same server as before: per-title links are kept', rec.get('mappings') == {'/x': 'e1'})
cfg_text = ''.join(p.read_text(errors='ignore') for p in (work / 'config').rglob('*') if p.is_file() and p.suffix in ('.json', '.log', ''))
check('The password is never stored (config files and logs)', 'correct-pass' not in cfg_text and 'wrong-pass' not in cfg_text)
check('…and never shown in the message', 'correct-pass' not in loc)
loc = post({'url': URL, 'username': 'embyuser', 'password': 'user-pass'})
check('Non-administrator: connected, with a warning about refresh', 'not an administrator' in loc, loc)
loc = post({'url': 'http://8.8.8.8:8096', 'username': 'a', 'password': 'b'})
check('A public plain-HTTP address is refused (same rules as a manual connection)', 'private LAN' in loc, loc)
loc = post({'url': URL, 'username': '', 'password': 'x'})
check('Empty username refused', 'Enter your Emby username' in loc, loc)
es._FAILS.clear()
for _ in range(5):
    post({'url': URL, 'username': 'embyadmin', 'password': 'nope'})
loc = post({'url': URL, 'username': 'embyadmin', 'password': 'correct-pass'})
check('Guessing is throttled: after 5 failures in 5 minutes even the right password waits', 'Too many failed sign-ins' in loc, loc)
src = Path('/src/preludarr/app.py').read_text()
check('Route wired', 'emby_signin.post(self,CONFIG,auth.redirect)' in src)


class P:
    path = '/media-servers?tab=media&configure=emby'; sent = None

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)


pg = P(); ms.settings_page(pg, app.CONFIG, app.ROOTS, app.db)
check('The Emby dialog offers "Sign in to Emby", address pre-filled, password not autofilled', 'Sign in to Emby' in pg.sent[1] and 'value="' + URL + '"' in pg.sent[1]
      and 'autocomplete="new-password"' in pg.sent[1])
srv.terminate()
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
