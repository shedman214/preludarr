# SPDX-License-Identifier: GPL-3.0-or-later
"""Preludarr 1.11.2 — root folders can move without losing saved data, and never stall on a running scan.
Replays the 2026-10-29 production case: anime roots moved from /anime to /media/tv/anime, titles re-found, every per-path
record left behind. Run in a Preludarr image, --network none."""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
old = work / 'anime'
titles = ('Bleach (2004)', 'Naruto (2002)', 'Gintama (2006)', 'Mushishi (2005)', 'Monster (2004)', 'Haikyu!! (2014)')
for t in titles:
    (old / t).mkdir(parents=True)
(old / 'Bleach (2004)' / 'theme.mp3').write_bytes(b'x' * 5000)
(work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(old), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0',
                  THEMEARR_LIBRARY_MOUNTS=f'{old},{work}/movies,{work}/media')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import path_moves as tp  # noqa: E402
import root_folders as rf  # noqa: E402
sys.modules['app'] = app
rf.load(app)
__import__('auto_themes').init(app)
app.discover()
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


cfg = work / 'config'
O = lambda t: str(old / t)  # noqa: E731
# ---- saved data under the OLD path (as production had) ----
with app.db() as c:
    for i, t in enumerate(titles):
        c.execute('INSERT OR REPLACE INTO posters(path,status,checked,provider) VALUES(?,?,?,?)', (O(t), 'ok', 1, 'TMDB'))
        c.execute('INSERT OR REPLACE INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES(?,?,?,?,?)', (O(t), 1000 + i, 2000 + i, None, 'sonarr'))
        c.execute('UPDATE show_added SET added=?, baseline=1 WHERE path=?', (1700000000 + i, O(t)))
    c.execute("INSERT INTO theme_queue(path,name,library,state,source,proposal,note,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
              (O('Naruto (2002)'), 'Naruto (2002)', 'Anime TV', 'no_match', '', '', '', 1, 1))
app.POSTER_DIR.mkdir(parents=True, exist_ok=True)
for t in titles:
    app.poster_file(O(t)).write_bytes(b'JPEG' + t.encode())
(cfg / 'themearr_v4_matches.json').write_text(json.dumps({O('Gintama (2006)'): {'slug': 'gintama', 'name': 'Gintama'}}))
(cfg / 'themearr_media_servers_v1.json').write_text(json.dumps({'servers': {'plex': {'mappings': {O(t): str(100 + i) for i, t in enumerate(titles)}}}}))
(cfg / 'themearr_arr.json').write_text(json.dumps({'instances': [{'kind': 'sonarr', 'name': 'Anime', 'mappings': [{'remote': '/data/tv/anime', 'local': str(old)}]}]}))
(cfg / 'themearr_library_health.json').write_text(json.dumps({'issues': [{'path': O('Monster (2004)'), 'kind': 'two themes'}]}))
(cfg / 'theme-trash' / 'x1').mkdir(parents=True)
(cfg / 'theme-trash' / 'x1' / 'manifest.json').write_text(json.dumps({'folder': O('Bleach (2004)'), 'name': 'Bleach (2004)'}))

# ---- the user moves the folder and points the root at the new location (like editing the JSON / remove + add) ----
new = work / 'media' / 'tv' / 'anime'
new.parent.mkdir(parents=True)
shutil.move(str(old), str(new))
N = lambda t: str(new / t)  # noqa: E731
entries = [{'path': str(new), 'type': 'Anime TV', 'name': 'Anime TV'}, {'path': str(work / 'movies'), 'type': 'Anime Movies', 'name': 'Anime Movies'}]
rf._save(app, entries); rf.load(app)
app.discover()
with app.db() as c:
    st = {Path(r['path']).name: r['state'] for r in c.execute('SELECT path,state FROM shows')}
    stale = c.execute('SELECT COUNT(*) FROM shows WHERE path LIKE ?', (str(old) + '/%',)).fetchone()[0]
check('After the move, titles are re-found under the new path (states from disk)', st.get('Bleach (2004)') == 'installed' and len(st) == 6 and stale == 0, (st, stale))
offers = tp.orphans(app)
check('Preludarr notices saved data left under the old location and matches it to the moved root',
      len(offers) == 1 and offers[0]['old'] == str(old) and offers[0]['root'] == str(new) and offers[0]['titles'] == 6 and offers[0]['matches'] == 6, offers)
dry = tp.remap(app, str(old), str(new), apply=False)
check('Dry run counts without changing anything', dry['posters'] == 6 and dry['media_ids'] == 6 and dry['themearr_media_servers_v1.json'] == 6
      and app.poster_file(O('Naruto (2002)')).is_file(), dry)


class H:
    def __init__(self, path, body):
        b = body.encode(); self.headers = {'Content-Length': str(len(b))}; self.path = path; self.rfile = io.BytesIO(b)


locs = []
rf.post(app, H('/media-management/reconnect', urllib.parse.urlencode({'old': str(old), 'root': str(new)})), lambda h, l: locs.append(urllib.parse.unquote_plus(l)))
check('Reconnect needs the confirmation tick', 'Tick the confirmation box' in locs[-1], locs[-1])
rf.post(app, H('/media-management/reconnect', urllib.parse.urlencode({'old': str(old), 'root': str(new), 'confirm': '1'})), lambda h, l: locs.append(urllib.parse.unquote_plus(l)))
check('Reconnect reports what moved', 'reconnected' in locs[-1] and '6 posters' in locs[-1] and '6 Sonarr/Radarr ids' in locs[-1], locs[-1])
with app.db() as c:
    ids = {Path(r['path']).name: r['tvdb'] for r in c.execute('SELECT path,tvdb FROM media_ids')}
    added = {Path(r['path']).name: r['added'] for r in c.execute('SELECT path,added FROM show_added')}
    q = c.execute('SELECT path FROM theme_queue').fetchone()[0]
    left = sum(c.execute(f'SELECT COUNT(*) FROM {t} WHERE path LIKE ?', (str(old) + '/%',)).fetchone()[0] for t in ('posters', 'media_ids', 'show_added', 'shows'))
check('Sonarr/Radarr ids reconnected', ids.get('Naruto (2002)') == 1001 and all(Path(p).parent == new for p in [N(t) for t in titles]), ids)
check('Original "added" dates restored (not all "recently added")', added.get('Gintama (2006)') == 1702 * 10 ** 6 + 2 or added.get('Gintama (2006)') == 1700000002, added)
check('Queue item points at the new folder', q == N('Naruto (2002)'), q)
check('Poster images renamed to the new paths', all(app.poster_file(N(t)).read_bytes() == b'JPEG' + t.encode() for t in titles) and not app.poster_file(O('Bleach (2004)')).exists())
check('Nothing left under the old path', left == 0, left)
j = lambda n: json.loads((cfg / n).read_text())  # noqa: E731
check('Remembered AnimeThemes match moved', N('Gintama (2006)') in j('themearr_v4_matches.json'))
check('Media-server mappings moved (all 6)', set(j('themearr_media_servers_v1.json')['servers']['plex']['mappings']) == {N(t) for t in titles})
check('Sonarr path mapping follows the root', j('themearr_arr.json')['instances'][0]['mappings'][0]['local'] == str(new))
check('Library Health note moved', j('themearr_library_health.json')['issues'][0]['path'] == N('Monster (2004)'))
check('Recycle-bin Undo restores into the new folder', json.loads((cfg / 'theme-trash/x1/manifest.json').read_text())['folder'] == N('Bleach (2004)'))
check('Rewritten settings files stay private (0600)', oct((cfg / 'themearr_media_servers_v1.json').stat().st_mode & 0o777) == '0o600')
check('The offer disappears once reconnected', tp.orphans(app) == [])

# ---- Edit → Folder: move a root and keep everything in one step ----
moved = work / 'media' / 'anime2'
shutil.move(str(new), str(moved))
counts = rf.edit(app, str(new), 'Anime TV', 'Anime TV', str(moved))
with app.db() as c:
    under = c.execute('SELECT COUNT(*) FROM media_ids WHERE path LIKE ?', (str(moved) + '/%',)).fetchone()[0]
check('Edit → Folder moves the root and its saved data together', [str(r) for _, r in app.ROOTS][0] == str(moved) and under == 6 and counts['posters'] == 6
      and j('themearr_arr.json')['instances'][0]['mappings'][0]['local'] == str(moved), counts)
try:
    rf.edit(app, str(moved), 'x', 'Anime TV', str(work / 'movies')); bad = ''
except ValueError as e:
    bad = str(e)
check('Edit refuses to move onto another root folder', 'overlaps' in bad, bad)

# ---- never stalls on a running scan ----
app.STATUS['running'] = True
(work / 'media' / 'extra').mkdir()
p = rf.add(app, str(work / 'media' / 'extra'), 'Extra', 'Shows')
n = rf.remove(app, str(work / 'media' / 'extra'))
check('Add and remove work while a scan is running', str(p).endswith('extra') and all(str(r) != str(p) for _, r in app.ROOTS))
app.STATUS['running'] = False
app.LOCK.acquire()
started = app.job(False, root=moved)
app.LOCK.release()
check('A scan asked for during another job is queued, not lost', started is False and moved in app.PENDING_SCANS)
app.PENDING_SCANS.clear()
(work / 'media' / 'gone').mkdir(); (work / 'media' / 'gone' / 'Some Title (2020)').mkdir()
rf.add(app, str(work / 'media' / 'gone'), 'Gone', 'Shows')
real_roots = list(app.ROOTS)
app.ROOTS[:] = [r for r in app.ROOTS if not str(r[1]).endswith('gone')]
with app.db() as c:
    c.execute("INSERT OR REPLACE INTO shows(path,library,name,state,detail,updated) VALUES(?,?,?,?,?,?)", (str(work / 'media/gone/Some Title (2020)'), 'Shows', 'Some Title (2020)', 'missing', '', ''))
app.discover()
with app.db() as c:
    ghost = c.execute('SELECT COUNT(*) FROM shows WHERE path LIKE ?', (str(work / 'media/gone') + '/%',)).fetchone()[0]
check('Titles of a root removed mid-scan are cleaned up at the end of the scan', ghost == 0, ghost)

# ---- 1.11.3: merge rules work in BOTH directions (the original data may be at either path) ----
with app.db() as c:
    c.execute("INSERT OR REPLACE INTO show_added(path,added,baseline) VALUES('/a/T1',1600000000,1),('/b/T1',1790000000,0),('/a/T2',1790000000,0),('/b/T2',1600000000,0)")
    c.execute("INSERT OR REPLACE INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES('/a/T1',NULL,555,NULL,'tmdb-search'),('/b/T1',111,NULL,'tt1','sonarr')")
tp.remap(app, '/a', '/b')
with app.db() as c:
    sa = {r['path']: (r['added'], r['baseline']) for r in c.execute("SELECT * FROM show_added WHERE path LIKE '/b/%'")}
    mi = c.execute("SELECT tvdb,tmdb,imdb FROM media_ids WHERE path='/b/T1'").fetchone()
check('Either direction: the original (baseline / earliest) "added" date survives', sa['/b/T1'] == (1600000000, 1) and sa['/b/T2'] == (1600000000, 0), sa)
check('Either direction: Sonarr/Radarr ids from both records are merged', tuple(mi) == (111, 555, 'tt1'), tuple(mi))

# ---- 1.11.3: API key test ----
import youtube_search as ys  # noqa: E402
def fake_ok(endpoint, params, key):
    assert endpoint == 'videos' and params['id'] == ys.KEY_TEST_VIDEO
    return {'items': [{'id': ys.KEY_TEST_VIDEO}]}
ys._call = fake_ok
ok, text = ys.test_key('AIza' + 'q' * 35)
check('Test API key: a working key passes (1 unit, not a search)', ok and 'works' in text and ys.usage()['used'] == 0, text)
for detail, word in (('API_KEY_INVALID', 'not valid'), ('SERVICE_DISABLED', 'not enabled'), ('API_KEY_SERVICE_BLOCKED', 'restricted to other APIs'),
                     ('API_KEY_HTTP_REFERRER_BLOCKED', 'Application restrictions')):
    def fake_err(endpoint, params, key, d=detail):
        raise ys.ApiError(403, 'forbidden', d)
    ys._call = fake_err
    ok, text = ys.test_key('AIza' + 'q' * 35)
    check(f'Test API key explains {detail}', not ok and word in text, text)
ok, text = ys.test_key('hello')
check('Test API key: obviously wrong text is caught without a request', not ok and 'does not look like' in text)
ys._call = fake_ok
locs = []
ys.post_test(H('/download-clients/youtube-search-test', 'api_key=AIza' + 'q' * 35 + '&budget=80'), lambda h, l: locs.append(urllib.parse.unquote_plus(l)))
check('Test API key never saves the key; shows the result', 'test passed' in locs[-1] and ys.settings()['api_key'] == '' and ys.last_key_test()['ok']
      and 'AIza' not in json.dumps(ys.last_key_test()), locs[-1])
check('Card has a Test API key button next to Save', 'formaction="/download-clients/youtube-search-test"' in ys.card_html() and 'Last key test' in ys.card_html())

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
