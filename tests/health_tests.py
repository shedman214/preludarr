# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.3 Library Health / Path Validator tests. Run in a Themearr image, --network none, with a read-only fixture:
docker run --rm --network none --tmpfs /tmp -v SRC:/src:ro -v RO_FIXTURE:/rolib:ro ... python3 /src/tests/health_tests.py
(RO_FIXTURE must contain one folder "Read Only Show (2020)".)
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

work = Path(tempfile.mkdtemp())
lib = work / 'lib'
anime, movies = lib / 'anime', lib / 'movies'
for d in (anime, movies):
    d.mkdir(parents=True)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(anime), MOVIE_ROOT=str(movies), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0',
                  THEMEARR_LIBRARY_MOUNTS=f'{anime},{movies},/rolib')
sys.path.insert(0, '/src/preludarr')

SONG = b'ID3' + b'\x01' * 60000


def title(root, name, theme=None, extra=()):
    d = root / name
    d.mkdir()
    if theme is not None:
        (d / 'theme.mp3').write_bytes(theme)
    for rel, data in extra:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


good = title(anime, 'Good Show (2010)', SONG)
title(anime, 'Second Good (2011)', SONG)
title(anime, 'Third Good (2012)', SONG)
symdir_target = work / 'elsewhere'; symdir_target.mkdir()
(anime / 'Linked Show (2013)').symlink_to(symdir_target)
d = title(anime, 'Theme Link (2014)'); (d / 'theme.mp3').symlink_to(work / 'outside.mp3'); (work / 'outside.mp3').write_bytes(SONG)
empty = title(anime, 'Empty Theme (2015)', b'')
dup = title(anime, 'Two Themes (2016)', SONG, [('theme.flac', b'fLaC' + b'0' * 5000), ('Theme.MP3', SONG)])
nested = title(anime, 'Nested (2017)', None, [('Season 01/theme.mp3', SONG), ('Season 01/ep1.mkv', b'video')])
temp = title(anime, 'Temp Left (2018)', SONG, [('.themearr-theme.tmp', b'x')])
mismatch = title(anime, 'Says Installed (2019)')
mov = title(movies, 'A Movie (2020)', SONG)
title(movies, 'B Movie (2021)', SONG)

import app  # noqa: E402
import library_health as lh  # noqa: E402
import media_servers as v4  # noqa: E402
import arr as arr  # noqa: E402

real_base = v4._base_url
v4._base_url = lambda raw: raw if raw.startswith('http://127.0.0.1:') else real_base(raw)  # tests only: loopback stand-in
lh.SPACING = 0

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


app.discover(); app.auto_themes.init(app)
with app.db() as c:
    c.execute("INSERT OR REPLACE INTO shows VALUES(?,?,?,?,?,?)", (str(anime / 'Gone Show (2009)'), 'Anime TV', 'Gone Show (2009)', 'missing', '', ''))
    c.execute("UPDATE shows SET state='installed' WHERE path=?", (str(mismatch),))
    c.execute("INSERT OR REPLACE INTO shows VALUES(?,?,?,?,?,?)", (str(anime / 'Linked Show (2013)'), 'Anime TV', 'Linked Show (2013)', 'missing', '', ''))

# ------------------------------------------------------------------ fake Plex ----
PLEX_ROOT = {str(anime): '/data/media/tv/anime', str(movies): '/data/media/movies/anime movies'}
IDS = {}
n = 100
for root, kind in ((anime, 'show'), (movies, 'movie')):
    for p in sorted(root.iterdir()):
        n += 1; IDS[str(n)] = (p, kind)


class Plex(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get('X-Plex-Token') != 'plextoken123':
            self.send_response(401); self.end_headers(); return
        if self.path.startswith('/library/sections'):
            body = {'MediaContainer': {'Directory': [{'Location': [{'path': '/data/media/tv/anime'}]}, {'Location': [{'path': '/data/media/movies/anime movies'}]}]}}
        elif self.path.startswith('/library/metadata/'):
            iid = self.path.rsplit('/', 1)[1]
            if iid not in IDS:
                self.send_response(404); self.end_headers(); return
            p, kind = IDS[iid]
            if kind == 'show':
                body = {'MediaContainer': {'Metadata': [{'Location': [{'path': PLEX_ROOT[str(p.parent)] + '/' + p.name}]}]}}
            else:  # movies: Plex knows them under a DIFFERENT folder name → must not be "verified"
                body = {'MediaContainer': {'Metadata': [{'Media': [{'Part': [{'file': PLEX_ROOT[str(p.parent)] + '/Renamed ' + p.name + '/movie.mkv'}]}]}]}}
        else:
            self.send_response(404); self.end_headers(); return
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)


class Fast(ThreadingHTTPServer):
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = 'localhost', self.server_address[1]


threading.Thread(target=Fast(('127.0.0.1', 18400), Plex).serve_forever, daemon=True).start()
v4._write(app.CONFIG, 'servers', {
    'plex': {'kind': 'plex', 'enabled': True, 'url': 'http://127.0.0.1:18400', 'api_key': 'plextoken123',
             'mappings': {str(p): iid for iid, (p, _) in IDS.items()}},
    'jellyfin': {'kind': 'jellyfin', 'enabled': False, 'url': 'http://192.168.1.30:8096', 'api_key': 'jf'}})
d_arr = arr.load(app.CONFIG)
d_arr['instances'] = [{'id': 'abc', 'kind': 'sonarr', 'enabled': True, 'name': 'Anime', 'url': 'http://192.168.1.30:8993', 'api_key': 'k' * 32,
                       'mappings': [{'remote': '/data/tv/anime', 'local': str(anime)}], 'status': {'ok': True, 'at': 1, 'text': 'Connected.'}}]
arr._save(app.CONFIG, d_arr)


def snapshot():
    out = {}
    for p in sorted(lib.rglob('*')) + [work / 'elsewhere', work / 'outside.mp3']:
        st = os.lstat(p)
        out[str(p)] = (st.st_mode, st.st_uid, st.st_gid, st.st_ino, st.st_mtime_ns, st.st_size)
    return out


# ------------------------------------------------------------------ scan ----
before = snapshot()
rep = lh.scan(app)
check('Scan changed nothing on disk (modes, owners, inodes, times, sizes)', snapshot() == before)
kinds = {}
for f in rep['findings']:
    kinds.setdefault(f['kind'], set()).add(Path(f['path']).name)
expect = {
    'missing_folder': {'Gone Show (2009)'}, 'symlink_folder': {'Linked Show (2013)'}, 'theme_symlink': {'Theme Link (2014)'},
    'theme_tiny': {'Empty Theme (2015)'}, 'duplicate_theme': {'Two Themes (2016)'}, 'nested_theme': {'Nested (2017)'},
    'stray_temp': {'Temp Left (2018)'}}
for kind, names in expect.items():
    check('finds ' + kind, kinds.get(kind) == names, kinds.get(kind))
dups = sorted(f['detail'] for f in rep['findings'] if f['kind'] == 'duplicate_theme')
check('duplicate lists theme.flac AND Theme.MP3', dups == ['Theme.MP3', 'theme.flac'], dups)
check('state mismatch: list says installed, no theme.mp3', 'Says Installed (2019)' in kinds.get('state_mismatch', set()), kinds.get('state_mismatch'))
check('healthy titles produce no findings', not any(n in s for s in kinds.values() for n in ('Good Show (2010)', 'A Movie (2020)')))
check('no wrong_owner findings while Permissions is off', 'wrong_owner' not in kinds)
check('severity order: problems before notes', [lh.SEV_ORDER[lh.FINDINGS[f['kind']][0]] for f in rep['findings']] == sorted(lh.SEV_ORDER[lh.FINDINGS[f['kind']][0]] for f in rep['findings']))
check('results saved privately (0600)', oct((work / 'config' / lh.FILE).stat().st_mode & 0o777) == '0o600')

plex = rep['media']['plex']
a_root, m_root = plex['roots'][str(anime)], plex['roots'][str(movies)]
check('Plex: anime root verified → /data/media/tv/anime', a_root['state'] == 'verified' and a_root['server_root'] == '/data/media/tv/anime', a_root)
check('Plex: sample is limited (≤ 8 requests per root)', a_root['checked'] <= lh.SAMPLE and m_root['checked'] <= lh.SAMPLE)
check('Plex: movies with different folder names are NOT verified', m_root['state'] != 'verified' and 'None of' in m_root['detail'], m_root)
check('Jellyfin turned off → not checked, said so', rep['media']['jellyfin']['state'] == 'off')
check('Emby not set up → shown as not set up', rep['media']['emby']['state'] == 'not_set_up')
check('Sonarr mapping reported', rep['arr'][0]['mappings'][0]['remote'] == '/data/tv/anime')

# Plex unreachable → plain reason, scan still completes
v4._write(app.CONFIG, 'servers', dict(v4._read(app.CONFIG, 'servers'), plex=dict(v4._read(app.CONFIG, 'servers')['plex'], url='http://127.0.0.1:18499')))
rep2 = lh.scan(app)
check('Plex unreachable → “Nothing is answering”, scan finished anyway', rep2['media']['plex']['state'] == 'error' and 'Nothing is answering' in rep2['media']['plex']['detail']
      and rep2['checked'] == rep['checked'], rep2['media']['plex'])

# Permissions on → wrong owner findings
import permissions as pm  # noqa: E402
(work / 'config' / pm.FILE).write_text(json.dumps({'enabled': True, 'folder_mode': '775', 'user': '99', 'group': '100'}))
rep3 = lh.scan(app, check_servers=False)
check('Permissions on → theme.mp3 with other owner reported', any(f['kind'] == 'wrong_owner' for f in rep3['findings']))
(work / 'config' / pm.FILE).write_text(json.dumps({'enabled': True, 'folder_mode': '777', 'user': '', 'group': ''}))
rep3 = lh.scan(app, check_servers=False)
check('Mode-only difference (644 vs 666) is NOT reported', not any(f['kind'] == 'wrong_owner' for f in rep3['findings']))
(work / 'config' / pm.FILE).unlink()
rep = lh.scan(app, check_servers=False)

# ------------------------------------------------------------------ roots ----
roots = {r['path']: r for r in rep['roots']}
check('Root report: exists/readable/writable + free space + counts', roots[str(anime)]['exists'] and roots[str(anime)]['readable'] and roots[str(anime)]['writable']
      and roots[str(anime)]['free'] and roots[str(anime)]['folders'] >= 10)
check('Mount info: root is on a mount with a source', all(r['mount_point'] for r in roots.values()))

# ------------------------------------------------------------------ preflight ----
def pre(folder):
    try:
        lh.preflight(folder); return 'ok'
    except ValueError as error:
        return str(error)


ro = Path('/rolib/Read Only Show (2020)')
check('preflight ok for a normal folder', pre(good) == 'ok')
check('preflight: symlink folder refused', 'symlink' in pre(anime / 'Linked Show (2013)'))
check('preflight: missing folder explained', 'no longer exists' in pre(anime / 'Gone Show (2009)'))
check('preflight: read-only Docker mount explained', 'read-only in Docker' in pre(ro), pre(ro))
check('mount_for sees /rolib as read-only', lh.mount_for(ro)['opts'][0] == 'ro' or 'ro' in lh.mount_for(ro)['opts'])

# read-only Docker mount → ONE root-level finding, no per-folder "not writable" flood
app.ROOTS.append(('Anime TV', Path('/rolib')))
app.discover()
rep4 = lh.scan(app, check_servers=False)
ro_f = [f for f in rep4['findings'] if f['path'].startswith('/rolib')]
check('Read-only mount → one “Library mounted read-only” finding, no per-folder flood', [f['kind'] for f in ro_f] == ['readonly_root'], ro_f)
app.ROOTS.pop()

# automatic install path stops before any network on a read-only folder
app.ROOTS.append(('Anime TV', Path('/rolib')))
app.discover()
r = app.process(ro)
with app.db() as c:
    row = c.execute('SELECT state,detail FROM shows WHERE path=?', (str(ro),)).fetchone()
check('Automatic install: read-only folder → Needs review with the reason (no download tried)', r.startswith('Needs review') and row['state'] == 'review'
      and 'read-only' in row['detail'], (r, dict(row) if row else None))
app.ROOTS.pop()

# manual install path
src = work / 'src.mp3'; src.write_bytes(SONG)
try:
    app.manual_install_mp3(anime / 'Linked Show (2013)', src); err = ''
except Exception as e:
    err = str(e)
check('Manual install into a symlink folder refused with a plain reason', 'symlink' in err, err)
with app.db() as c:
    ev = c.execute("SELECT status,detail FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
check('…and logged to Activity as install failed', ev and ev['status'] == 'install failed' and 'symlink' in ev['detail'], dict(ev) if ev else None)

# ------------------------------------------------------------------ pages ----
class H:
    def __init__(self, path):
        self.path = path; self.headers = {'Content-Length': '0'}; self.rfile = io.BytesIO(b''); self.sent = None; self.code = None; self.hdrs = {}

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass


import system as sysv  # noqa: E402
sys.modules['app'] = app
body = sysv.page(app.CONFIG, {'tab': ['health']})
check('System → Library Health tab renders findings, roots, mapping, read-only note', 'Root folders' in body and 'Path mapping' in body
      and '<h2>Findings</h2>' in body and 'Read-only — nothing is changed' in body and 'aria-current="page">Library Health' in body)
check('V1.9.8: findings + last scan come BEFORE the root/path diagnostics', body.index('<h2>Findings</h2>') < body.index('Folder &amp; path diagnostics') < body.index('Root folders'))
check('V1.9.8: last scan time shown with timezone and labelled Cached', 'Last scan <strong>' in body and 'tm-label cached' in body)
check('System tabs: Library Health just before Advanced Settings (last)', body.index('>Library Health</a>') < body.index('>Advanced Settings</a>'))
check('V1.9.8: one group per category, explanation shown once per group', body.count('class="lh-group ') == len(lh._load(app.CONFIG)['counts']) and body.count('Keep one theme per folder') == 1, (body.count('class="lh-group '), lh._load(app.CONFIG)['counts']))
h = H('/library-health?kind=nested_theme&page=2'); lh.page(app, h)
check('Old /library-health address forwards to the System tab (keeps filters)', h.code == 303 and h.hdrs['Location'].startswith('/system?tab=health&kind=nested_theme&page=2'), h.hdrs)
check('Page shows Plex column + Sonarr mapping', '/data/tv/anime' in body)
check('No secrets on the page', 'plextoken123' not in body and 'k' * 32 not in body)
fb = lh.body(app, {'kind': ['duplicate_theme']})
grp = fb.split('id="kind-duplicate_theme"')[1].split('</details>')[0]
check('Opening one category shows it expanded with its rows', grp.startswith(' open') and 'Two Themes (2016)' in grp and 'Nested (2017)' not in grp)
check('Other categories stay collapsed when one is opened', 'id="kind-nested_theme" open' not in fb)
box = lh.title_box(app.CONFIG, str(dup))
check('Theme Browser note for a title with an extra theme file', 'Library Health' in box and 'Theme.MP3' in box)
check('No Theme Browser note for a healthy title', lh.title_box(app.CONFIG, str(good)) == '')

# background scan + status
lh.start_scan(app)
for _ in range(200):
    if not lh.status()['running']:
        break
    time.sleep(0.05)
check('Background scan finishes and records Activity', not lh.status()['running'] and lh.status().get('error') in ('', None))
try:
    lh._RUN['running'] = True; lh.start_scan(app); twice = False
except ValueError:
    twice = True
finally:
    lh._RUN['running'] = False
check('Only one scan at a time', twice)

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
