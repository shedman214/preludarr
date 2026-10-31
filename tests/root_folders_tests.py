# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for Root Folders (V1.7.2). Run in a Themearr image with --network none: python3 root_folders_tests.py"""
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

work = Path(tempfile.mkdtemp())
lib = work / 'lib'
for p in ('anime/series/Show A (2015)', 'anime/series/Show B (2016)', 'anime/kids/Kid Show (2020)', 'anime/kids/.hidden', 'anime/.secret',
          'movies/Movie A (2001)', 'outside/Other'):
    (lib / p).mkdir(parents=True)
(lib / 'anime/series/Show A (2015)/theme.mp3').write_bytes(b'ID3' + b'\0' * 2000)
os.symlink(lib / 'outside', lib / 'anime/link-to-outside')
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(lib / 'anime/series'), MOVIE_ROOT=str(lib / 'movies'),
                  THEMEARR_LIBRARY_MOUNTS=f"{lib / 'anime'},{lib / 'movies'}", AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import root_folders as rf  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail else ''), flush=True)


def snapshot():
    out = {}
    for root, dirs, files in os.walk(lib, followlinks=False):
        for n in dirs + files:
            p = Path(root) / n
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ('link' if p.is_symlink() else 'dir')
    return out


def wait_job():
    for _ in range(100):
        if not app.STATUS['running']:
            return
        time.sleep(0.1)


before = snapshot()
saved = json.loads((work / 'config' / rf.FILE).read_text())['roots']
check('First start keeps today\'s two roots (seeded from TV_ROOT/MOVIE_ROOT)',
      [(e['type'], e['path']) for e in saved] == [('Anime TV', str(lib / 'anime/series')), ('Anime Movies', str(lib / 'movies'))])
b = rf.browse(str(lib / 'anime'))
names = [d['name'] for d in b['dirs']]
check('Picker lists sub-folders, hides hidden folders and symlinks', names == ['kids', 'series'], names)
for bad, why in ((str(lib / 'outside'), 'outside mounts'), (str(lib / 'anime/link-to-outside'), 'symlink'), (str(lib / 'anime/../outside'), 'traversal'),
                 (str(lib / 'anime/.secret'), 'hidden'), ('relative/path', 'relative'), ('/etc', 'system folder')):
    try:
        rf.browse(bad); ok = False
    except ValueError:
        ok = True
    check(f'Picker refuses {why}', ok)
try:
    rf.add(app, str(lib / 'anime'), 'All', 'Anime TV'); ok = False
except ValueError as e:
    ok = 'inside each other' in str(e)
check('Nested roots refused (parent of an existing root)', ok)
try:
    rf.add(app, str(lib / 'anime/series'), 'Dup', 'Anime TV'); ok = False
except ValueError as e:
    ok = 'already' in str(e)
check('Duplicate root refused', ok)
app.discover()
check('Full scan unchanged: 3 titles from the 2 original roots', sorted(r['name'] for r in app.db().execute('select name from shows')) == ['Movie A (2001)', 'Show A (2015)', 'Show B (2016)'])
st = rf.stats(app, lib / 'anime/kids')
p = rf.add(app, str(lib / 'anime/kids'), 'Kids', 'Anime TV')
check('Add a new root: saved and live in app.ROOTS', ('Anime TV', p) in app.ROOTS and any(e['path'] == str(p) for e in json.loads((work / 'config' / rf.FILE).read_text())['roots']))
check('Before scanning, its folder counts as unmapped', rf.stats(app, p)['unmapped'] == ['Kid Show (2020)'])
app.job(False, root=p); wait_job()
rows = {r['name']: r['library'] for r in app.db().execute('select name,library from shows')}
check('Per-root scan registers only that root\'s folders', rows.get('Kid Show (2020)') == 'Anime TV' and len(rows) == 4 and rf.stats(app, p)['unmapped'] == [], rows)
st = rf.stats(app, lib / 'anime/series')
check('Stats: folders / in library / themes / free space', (st['folders'], st['in_library'], st['themes']) == (2, 2, 1) and st['free'] and st['writable'], st)
rf.edit(app, str(p), 'Kids anime', 'Anime Movies')
check('Edit name + type updates the root and its titles\' library label', ('Anime Movies', p) in app.ROOTS and rf._NAMES[str(p)] == 'Kids anime'
      and app.db().execute("select library from shows where name='Kid Show (2020)'").fetchone()[0] == 'Anime Movies')
n = rf.remove(app, str(p))
check('Remove: registration gone, its titles leave the library list', n == 1 and all(r != p for _, r in app.ROOTS)
      and app.db().execute("select count(*) from shows where name='Kid Show (2020)'").fetchone()[0] == 0)
app.process(str(lib / 'anime/series/Show A (2015)'))
check('Existing theme.mp3 is never replaced (process says already installed)', app.db().execute("select detail from shows where name='Show A (2015)'").fetchone()[0] == 'Already exists')
check('NOTHING on disk changed (every file and folder identical)', snapshot() == before)
app.ROOTS[:] = []
rf.load(app)
check('Restart: saved roots are reloaded', [str(r) for _, r in app.ROOTS] == [str(lib / 'anime/series'), str(lib / 'movies')])
print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
