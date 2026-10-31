# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for Bulk Theme Queue + secondary sources (V1.9.0). Run in a Themearr image with --network none."""
import io
import json
import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
lib = work / 'lib'
for n in ('anime/Good Match (2014)', 'anime/Remembered (2015)', 'anime/Nothing Anywhere (2020)', 'anime/Plex Only (2018)', 'anime/Has Theme (2001)',
          'movies/Themerr Movie (2001)'):
    (lib / n).mkdir(parents=True)
(lib / 'anime/Has Theme (2001)/theme.mp3').write_bytes(b'ID3' + b'\0' * 999)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(lib / 'anime'), MOVIE_ROOT=str(lib / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import theme_queue as q  # noqa: E402
import sources as src  # noqa: E402
import media_servers as v4  # noqa: E402

results = []
def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail else ''), flush=True)
class H:
    def __init__(self, fields): b = urllib.parse.urlencode(fields).encode(); self.headers = {'Content-Length': str(len(b))}; self.rfile = io.BytesIO(b); self.path = ''
redirect = lambda h, url: urllib.parse.unquote_plus(url)  # noqa: E731

def show(slug, year, ops=True):
    return {'slug': slug, 'name': slug.replace('_', ' ').title(), 'year': year, 'animethemes': [
        {'type': 'OP', 'slug': 'OP1', 'song': {'title': 'Song ' + slug, 'artists': [{'name': 'Band'}]}, 'animethemeentries': [
            {'id': 1, 'videos': [{'id': 77, 'link': 'https://v.animethemes.moe/x.webm', 'audio': {'link': 'https://a.animethemes.moe/x.ogg'}}]}]}] if ops else []}
SHOWS = {'Good Match (2014)': show('good_match', 2014), 'Remembered (2015)': show('remembered_entry', 2015)}
import urllib.error  # noqa: E402
def _anilist(name):
    if 'Plex Only' in name:  # AniList answers "not found" with HTTP 404 — must count as no match, not as an outage
        raise urllib.error.HTTPError('https://graphql.anilist.co', 404, 'Not Found', {}, None)
    return {'seasonYear': app.year(name), 'title': {}, 'synonyms': []} if 'Good' in name else None
app.anilist = _anilist
app.anime_theme = lambda name, media: SHOWS.get(name)
def fake_request_json(url, data=None, fresh=False):
    if '/anime/remembered_entry' in url:
        return {'anime': SHOWS['Remembered (2015)']}
    if 'search/movie' in url:
        return {'results': [{'id': 129, 'title': 'Themerr Movie', 'release_date': '2001-07-20'}]}
    if 'search/tv' in url:
        return {'results': []}
    raise OSError('offline')
app.request_json = fake_request_json
app.poster_connect.effective_values = lambda cfg: ('tmdbkey', '', '', 'tmdb')

app.discover(); app.auto_themes.init(app)
with v4._LOCK:
    v4._write(app.CONFIG, 'matches', {str(lib / 'anime/Remembered (2015)'): {'slug': 'remembered_entry', 'name': 'Remembered', 'year': '2015'}})
with app.db() as c:
    c.execute("INSERT INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES(?,?,?,?,?)", (str(lib / 'anime/Plex Only (2018)'), 81189, None, None, 'sonarr'))

q.wake = lambda app: None  # tests drive the queue step by step (no background worker)
q.wake_real = None
paths = [r[0] for r in app.db().execute('select path from shows order by name')]
added, skipped = q.add(app, paths + paths)
check('A: add skips titles that already have a theme (duplicates collapsed)', (added, skipped) == (5, 1), (added, skipped))
check('Nothing is installed by adding', not any((lib / p).joinpath('theme.mp3').exists() for p in ('anime/Good Match (2014)',)))

src.save_settings(app.CONFIG, {'plex': False, 'themerrdb': False, 'tmdb_search': True})
calls = []
src._get = lambda url, limit, timeout=25: (calls.append(url), (200, 'audio/mpeg', urllib.parse.urlsplit(url), b'ID3' + b'\0' * 4000) if 'plexapp' in url
                                          else (200, 'application/json', urllib.parse.urlsplit(url), json.dumps({'youtube_theme_url': 'https://www.youtube.com/watch?v=sC2NriNNX_A'}).encode()))[1]
q._STATE['paused'] = False
for _ in range(10):
    q.step(app)
rows = {r['name']: r for r in app.db().execute('select * from theme_queue')}
p = json.loads(rows['Good Match (2014)']['proposal'])
check('B: Original Matching proposal with the opening track', rows['Good Match (2014)']['state'] == 'proposed' and p['anime_slug'] == 'good_match' and p['track'].startswith('OP1 — Song good_match'), p.get('track'))
p = json.loads(rows['Remembered (2015)']['proposal'])
check('B: remembered match is used (no title search)', rows['Remembered (2015)']['state'] == 'proposed' and p['how'] == 'Your remembered match')
check('B: sources off → no match, with a clear note', rows['Plex Only (2018)']['state'] == 'no_match' and 'Plex TV Themes and ThemerrDB are off' in rows['Plex Only (2018)']['note'])  # 1.11 wording
check('B: dry run installed nothing and contacted no fallback', not calls and not (lib / 'anime/Good Match (2014)/theme.mp3').exists())

src.save_settings(app.CONFIG, {'plex': True, 'themerrdb': True, 'tmdb_search': True})
with app.db() as c:
    c.execute("UPDATE theme_queue SET state='queued' WHERE state='no_match'")
for _ in range(10):
    q.step(app)
rows = {r['name']: r for r in app.db().execute('select * from theme_queue')}
pp = json.loads(rows['Plex Only (2018)']['proposal'] or '{}')
check('Sources: Plex theme proposed from the Sonarr TVDB id, flagged for review', rows['Plex Only (2018)']['state'] == 'proposed' and pp.get('source') == 'plex'
      and pp['url'] == 'https://tvthemes.plexapp.com/81189.mp3' and 'check before approving' in pp['how'], pp)
pm = json.loads(rows['Themerr Movie (2001)']['proposal'] or '{}')
check('Sources: ThemerrDB YouTube theme via an exact TMDB title+year match', rows['Themerr Movie (2001)']['state'] == 'proposed' and pm.get('source') == 'themerrdb'
      and 'TMDB 129 (id from exact TMDB title + year match)' in pm.get('note', ''), pm)
check('Sources: no id → no guess (Nothing Anywhere stays no match)', rows['Nothing Anywhere (2020)']['state'] == 'no_match')
check('Sources: TMDB search needs a single exact title+year match', src.ids(app, str(lib / 'anime/Nothing Anywhere (2020)'), 'Nothing Anywhere (2020)', 'Anime TV') == {})

installs = []
app.resolve_animethemes_track = lambda slug, vid: {'slug': slug, 'video_id': vid}
class Stage:
    def __init__(self, *a): pass
    def __enter__(self): return Path('/tmp/fake.ogg')
    def __exit__(self, *a): return False
app.stage_verified_theme_audio = Stage
def fake_install(folder, audio, replace=False, **k):
    installs.append((Path(folder).name, replace, k.get('anime_slug')))
    if 'Remembered' in str(folder):
        raise ValueError('Existing theme requires explicit replacement')
    return True
app.manual_install_mp3 = fake_install
res = q.post(app, H({'action': 'approve_all'}), redirect)
check('C: Approve all needs the confirmation tick', 'Tick the confirmation' in res)
gm = rows['Good Match (2014)']['id']; rm = rows['Remembered (2015)']['id']
q.post(app, H({'action': 'approve', 'id': gm}), redirect); q.post(app, H({'action': 'approve', 'id': rm}), redirect)
q._STATE['paused'] = True
check('C: paused queue does nothing', q.step(app) is False and not installs)
q._STATE['paused'] = False
q.step(app); q.step(app)
st = {r['name']: r['state'] for r in app.db().execute('select name,state from theme_queue')}
check('C: approved install uses the safe installer with replace=False', ('Good Match (2014)', False, 'good_match') in installs and st['Good Match (2014)'] == 'installed', installs)
check('C: a theme that appeared meanwhile is left untouched (skipped)', st['Remembered (2015)'] == 'skipped', st)
check('C: unapproved proposals are never installed', not any(n.startswith(('Plex', 'Themerr')) for n, _, _ in installs))
q.post(app, H({'action': 'approve', 'id': rows['Plex Only (2018)']['id']}), redirect)
app.manual_install_mp3 = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Another manual installation is running'))
q.step(app)
check('C: another install running → stays approved (retried later)', app.db().execute('select state from theme_queue where name=?', ('Plex Only (2018)',)).fetchone()[0] == 'approved')
try:
    src.install(app, str(lib / 'anime/Plex Only (2018)'), {'source': 'plex', 'url': 'https://evil.example.com/81189.mp3'}); ok = False
except ValueError:
    ok = True
check('Sources: install refuses any host other than Plex', ok)
q.post(app, H({'action': 'cancel', 'id': rows['Plex Only (2018)']['id']}), redirect)
check('D: cancel an approved item → nothing installed', app.db().execute('select state from theme_queue where name=?', ('Plex Only (2018)',)).fetchone()[0] == 'cancelled')
q.post(app, H({'action': 'retry', 'id': rows['Plex Only (2018)']['id']}), redirect)
check('D: retry puts it back for a fresh dry run', app.db().execute('select state from theme_queue where name=?', ('Plex Only (2018)',)).fetchone()[0] == 'queued')
with app.db() as c:
    c.execute("UPDATE theme_queue SET state='searching' WHERE name='Plex Only (2018)'"); c.execute("UPDATE theme_queue SET state='installing' WHERE name='Themerr Movie (2001)'")
q.recover(app)
st = {r['name']: r['state'] for r in app.db().execute('select name,state from theme_queue')}
check('D: after a restart, interrupted steps resume safely', st['Plex Only (2018)'] == 'queued' and st['Themerr Movie (2001)'] == 'approved')
cand = q.candidates(app, {'library': ['Anime Movies']})
check('A: filter → candidates (missing/review in Anime Movies)', cand == [str(lib / 'movies/Themerr Movie (2001)')], cand)
print(f'{sum(results)}/{len(results)} passed'); sys.exit(0 if all(results) else 1)

