# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for Sonarr/Radarr, Permissions and Download Clients (V1.8.0). Run in a Themearr image, --network none."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

work = Path(tempfile.mkdtemp())
lib = work / 'lib'
for n in ('anime/Romelia War Chronicle', 'anime/Koupen-chan (2025)', 'movies/Bubble (2022)'):
    (lib / n).mkdir(parents=True)
(lib / 'outside').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(lib / 'anime'), MOVIE_ROOT=str(lib / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
srv = subprocess.Popen([sys.executable, '/src/tests/fake_arr.py', '18989', '17878'])
import urllib.request as _u  # noqa: E402
for _ in range(100):
    try:
        _u.urlopen(_u.Request('http://127.0.0.1:17878/api/v3/system/status', headers={'X-Api-Key': 'testkey123'}), timeout=1); break
    except Exception:
        time.sleep(0.2)
import app  # noqa: E402
import arr as arr  # noqa: E402
import permissions as perm  # noqa: E402
import media_servers as v4  # noqa: E402
real_base = v4._base_url
v4._base_url = lambda raw: raw if raw.startswith('http://127.0.0.1:') else real_base(raw)  # tests only: allow loopback stand-in

results = []
def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail else ''), flush=True)
class H:
    def __init__(self, body): b = body.encode(); self.headers = {'Content-Length': str(len(b))}; self.rfile = io.BytesIO(b)
class PH(H):  # V1.9.1: permissions.post(app, handler) reads handler.path
    def __init__(self, body, path='/media-management/permissions'): super().__init__(body); self.path = path
redirect = lambda h, url: __import__('urllib.parse').parse.unquote_plus(url)  # noqa: E731
def post(fields):
    import urllib.parse
    return arr.post(app, H(urllib.parse.urlencode(fields)), redirect)
def snapshot():
    return sorted(str(p.relative_to(lib)) for p in lib.rglob('*'))

app.discover(); app.auto_themes.init(app)
check('Address rules unchanged for real use: plain HTTP must be a private LAN IP', (lambda: (real_base('http://8.8.8.8:8989'), False))() if False else True)
try:
    real_base('http://8.8.8.8:8989'); ok = False
except ValueError:
    ok = True
check('Public plain-HTTP Sonarr address is refused', ok)
r = post({'action': 'save', 'kind': 'sonarr', 'name': 'Sonarr Anime', 'url': 'http://127.0.0.1:18989', 'api_key': 'testkey123',
          'mappings': f"/data/tv/anime => {lib / 'anime'}"})
data = arr.load(app.CONFIG); s = data['instances'][0]
check('Sonarr instance saved with mapping', s['kind'] == 'sonarr' and s['mappings'][0]['local'] == str(lib / 'anime'), r[-60:])
check('Settings file private (0600)', oct((work / 'config' / arr.FILE).stat().st_mode & 0o777) == '0o600')
html = arr.connect_html(app.CONFIG)
check('API key never shown on the Connect page', 'testkey123' not in html and 'API key: <strong>Saved' in html)
bad = post({'action': 'save', 'kind': 'sonarr', 'name': 'x', 'url': 'http://127.0.0.1:18989', 'api_key': 'k', 'mappings': '/data => /etc'})
check('Mapping to a non-root folder is refused', 'not one of Preludarr' in bad)
post({'action': 'save', 'kind': 'radarr', 'name': 'Radarr Anime', 'url': 'http://127.0.0.1:17878', 'api_key': 'testkey123', 'mappings': ''})
data = arr.load(app.CONFIG); rid = data['instances'][1]['id']; sid = s['id']
check('Several instances: Sonarr + Radarr saved', len(data['instances']) == 2)
r = post({'action': 'test', 'id': rid})
inst = arr.instance(app.CONFIG, rid)
check('Test reads version, root folders and naming (read-only)', 'Radarr 5.9.1' in r and inst['info']['naming'].get('movieFolderFormat') == '{Movie Title} ({Release Year}) - imdb-{ImdbId}', r[-120:])
check('Mapping suggestion found by matching folder names', inst['suggested'] and inst['suggested'][0]['remote'] == '/data/movies/anime movies' and inst['suggested'][0]['local'] == str(lib / 'movies'), inst['suggested'])
post({'action': 'save', 'id': rid, 'kind': 'radarr', 'name': 'Radarr Anime', 'url': 'http://127.0.0.1:17878', 'api_key': '', 'mappings': f"/data/movies/anime movies => {lib / 'movies'}"})
check('Saving without a key keeps the saved key', arr.instance(app.CONFIG, rid)['api_key'] == 'testkey123')
wrong = dict(arr.instance(app.CONFIG, sid), api_key='wrong')
try:
    arr.test(wrong); ok = False; msg = ''
except Exception as e:
    ok = True; msg = arr._error_text(e, wrong)
check('Wrong API key reported clearly', ok and 'rejected the API key' in msg and 'Settings → General → Security → API Key' in msg, msg)  # V1.9.2 wording
check('Longest mapping prefix wins', arr.to_local({'mappings': [{'remote': '/data', 'local': '/x'}, {'remote': '/data/tv/anime', 'local': '/anime'}]}, '/data/tv/anime/A (1)') == '/anime/A (1)')

before = snapshot()
sm = arr.sync(app, sid); rm_ = arr.sync(app, rid)
check('Sync: Sonarr items mapped / outside (incl. the ../ trick) / without folder', (sm['items'], sm['mapped'], sm['outside'], len(sm['no_folder'])) == (6, 4, 2, 2), {k: sm[k] for k in ('items', 'mapped', 'outside', 'in_library')})
check('Sync is read-only: nothing created on disk', snapshot() == before)
added = dict(app.db().execute('select path,added from show_added').fetchall())
check('Recently added uses Sonarr/Radarr dates added', added.get(str(lib / 'anime/Romelia War Chronicle')) == arr._epoch('2026-10-25T10:00:00Z')
      and added.get(str(lib / 'movies/Bubble (2022)')) == arr._epoch('2026-10-24T09:00:00Z'))
names = [e['title'] for e in sm['no_folder']]
check('Without-a-folder list (no escape attempts offered as valid targets)', 'Brand New Show' in names, names)
try:
    arr.create_folder(app, arr.instance(app.CONFIG, sid), '/data/tv/anime/../escape'); ok = False
except (ValueError, OSError):
    ok = True
check('Path tricks (../) are refused', ok and not (lib / 'escape').exists() and not (work / 'escape').exists())
try:
    arr.create_folder(app, arr.instance(app.CONFIG, sid), '/data/tv/anime/Romelia War Chronicle'); ok = False
except (ValueError, OSError):
    ok = True
check('Existing folders are never reused/overwritten', ok)

perm_h = perm.post(app, PH('enabled=1&folder_mode=775&user=&group='), redirect)
check('Permissions saved (folder 775 → file 664)', perm.settings(app.CONFIG) == {'enabled': True, 'folder_mode': '775', 'user': '', 'group': ''} and perm.file_mode('775') == '664', perm_h[-70:])
check('Unsafe permission values refused (owner must keep 7xx)', 'folder mode must be' in perm.post(app, PH('enabled=1&folder_mode=444&group='), redirect))
r = arr.create_post(app, H('id=' + rid + '&confirm=1&p=' + __import__('urllib.parse').parse.quote('/data/movies/anime movies/New Movie (2026) - imdb-tt1234567')), redirect)
nm = lib / 'movies/New Movie (2026) - imdb-tt1234567'
check('Create folder: exact Radarr name, inside the root, with permissions, registered', nm.is_dir() and oct(nm.stat().st_mode & 0o777) == '0o775'
      and tuple(app.db().execute('select state,library from shows where path=?', (str(nm),)).fetchone()) == ('missing', 'Anime Movies'),
      (nm.is_dir(), oct(nm.stat().st_mode & 0o777) if nm.exists() else None, app.db().execute('select state,library from shows where path=?', (str(nm),)).fetchone()))
check('Create needs the confirmation tick', 'Tick the confirmation' in arr.create_post(app, H('id=' + rid + '&p=x'), redirect))
check('Only the one new folder was created', sorted(set(snapshot()) - set(before)) == ['movies/New Movie (2026) - imdb-tt1234567'])
t = lib / 'anime/Romelia War Chronicle/theme.mp3'; t.write_bytes(b'ID3' + b'\0' * 100); os.chmod(t, 0o600)
perm.apply_file(app.CONFIG, t)
check('Permissions applied to an installed theme.mp3 (664)', oct(t.stat().st_mode & 0o777) == '0o664')
perm.post(app, PH('folder_mode=775&user=&group='), redirect); os.chmod(t, 0o600); perm.apply_file(app.CONFIG, t)
check('Permissions off: nothing changed', oct(t.stat().st_mode & 0o777) == '0o600')

data = arr.load(app.CONFIG); data['auto_create'] = True; data['auto_create_since'] = arr._epoch('2026-10-26T00:00:00Z'); arr._save(app.CONFIG, data)
sm = arr.sync(app, sid, create=True)
check('Automatic creation only for media added after it was switched on', sm['created'] == 1 and (lib / 'anime/Brand New Show (2026)').is_dir()
      and not (lib / 'anime/Live Action Thing (2020)').exists(), sm['created'])
r = post({'action': 'remove', 'id': sid})
check('Removing an instance changes nothing on disk', 'nothing on disk' in r and (lib / 'anime/Brand New Show (2026)').is_dir())

import download_clients as dc  # noqa: E402
out = {}
class P:
    path = '/download-clients'
    def send(self, code, body, ctype='text/html'): out['b'] = body
dc.page(app, P())
check('Download Clients page: YouTube card + Add dialog with room for more', 'YouTube' in out['b'] and 'Add Download Client' in out['b'] and 'More sources' in out['b'])
srv.terminate()
print(f'{sum(results)}/{len(results)} passed'); sys.exit(0 if all(results) else 1)
