# SPDX-License-Identifier: GPL-3.0-or-later
"""Preludarr 1.11 — Anime TV / Anime Movies / Shows / Movies root-folder types, Shows & Movies sources (Plex TV Themes, ThemerrDB by
TMDB or IMDb), quota-safe YouTube search (preview only, cookies.txt mandatory), cookie test, Library tabs, Collections entry,
dashboard stats. Run in a Preludarr image, --network none. Every outside request is faked; nothing is downloaded."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

work = Path(tempfile.mkdtemp())
for d in ('anime/Naruto (2002)', 'movies', 'shows/Breaking Bad (2008)', 'shows/Friends', 'films/The Matrix (1999)', 'films/Amélie (2001)',
          'films/Nothing Here (2033)', 'films/Up (2009)'):
    (work / d).mkdir(parents=True, exist_ok=True)
(work / 'films/Up (2009)/theme.mp3').write_bytes(b'x' * 4096)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0',
                  POSTER_BACKGROUND_ENABLED='0', THEMEARR_LIBRARY_MOUNTS=f"{work}/anime,{work}/movies,{work}/shows,{work}/films")
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import kinds as kinds  # noqa: E402
import theme_queue as queue  # noqa: E402
import shows_movies as regular  # noqa: E402
import root_folders as rf  # noqa: E402
import sources as src  # noqa: E402
import youtube_search as ys  # noqa: E402
import youtube_settings as cks  # noqa: E402
import youtube_ui as ytui  # noqa: E402
sys.modules['app'] = app
rf.load(app)
__import__('auto_themes').init(app)
src.save_settings(app.CONFIG, {'plex': True, 'themerrdb': True, 'tmdb_search': False})  # off by default; turned on like a user would
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


class H:
    def __init__(self, path='/', body=''):
        b = body.encode()
        self.headers = {'Content-Length': str(len(b)), 'Content-Type': 'application/x-www-form-urlencoded'}
        self.path = path; self.rfile = io.BytesIO(b)


redirected = []
redirect = lambda h, loc: redirected.append(urllib.parse.unquote_plus(loc)) or loc  # noqa: E731

# ------------------------------------------------------------------ types ----
check('Four types; stored values unchanged for existing installs', kinds.TYPES == ('Anime TV', 'Anime Movies', 'Shows', 'Movies')
      and [kinds.label(k) for k in kinds.TYPES] == ['Anime TV', 'Anime Movies', 'Shows', 'Movies'])
check('Series/movie and anime/regular come from the type', [kinds.is_movie(k) for k in kinds.TYPES] == [False, True, False, True]
      and [kinds.is_anime(k) for k in kinds.TYPES] == [True, True, False, False] and [kinds.is_regular(k) for k in kinds.TYPES] == [False, False, True, True])
rf.add(app, str(work / 'shows'), 'TV', 'Shows')
rf.add(app, str(work / 'films'), 'Films', 'Movies')
try:
    rf.add(app, str(work / 'films'), 'x', 'Cartoons'); bad = False
except ValueError as e:
    bad = 'Anime, Anime Movies, Shows or Movies' in str(e)
check('Root folders accept the 4 types and refuse others', bad and [k for k, _ in app.ROOTS] == ['Anime TV', 'Anime Movies', 'Shows', 'Movies'])
saved = json.loads((work / 'config' / rf.FILE).read_text())
# 1.11.1: the same disk folder reached through a second volume (bind mounts in production; a symlink stands in for one here)
os.symlink(work / 'shows', work / 'alias')
check('Same folder through another path is refused (no duplicate titles)',
      'already a root folder' in rf._same_place(work / 'alias', work / 'shows') and 'is inside the root folder' in rf._same_place(work / 'alias' / 'Friends', work / 'shows')
      and 'contains the root folder' in rf._same_place(work, work / 'shows') and rf._same_place(work / 'films', work / 'shows') == '')
check('Saved root folders keep their type + name', [(r['type'], r['name']) for r in saved['roots']][-2:] == [('Shows', 'TV'), ('Movies', 'Films')])
app.discover()
with app.db() as c:
    lib = {Path(r['path']).name: (r['library'], r['state']) for r in c.execute('SELECT path,library,state FROM shows')}
check('Scan tags each title with its root folder type', lib['Breaking Bad (2008)'] == ('Shows', 'missing') and lib['The Matrix (1999)'] == ('Movies', 'missing')
      and lib['Naruto (2002)'] == ('Anime TV', 'missing') and lib['Up (2009)'] == ('Movies', 'installed'), lib)

# ------------------------------------------------------------------ sources per type (faked) ----
calls = []


def fake_get(url, limit, timeout=25):
    calls.append(url)
    host = urllib.parse.urlsplit(url).hostname
    if host == src.PLEX_HOST:
        return 200, 'audio/mpeg', urllib.parse.urlsplit(url), b'ID3' + b'\0' * 100
    if url.endswith('/movies/imdb/tt0211915.json') or url.endswith('/tv_shows/themoviedb/1396.json') or url.endswith('/movies/themoviedb/603.json'):
        return 200, 'application/json', urllib.parse.urlsplit(url), json.dumps({'youtube_theme_url': 'https://www.youtube.com/watch?v=AAAAAAAAAAA'}).encode()
    raise urllib.error.HTTPError(url, 404, 'nf', {}, None)


import urllib.error  # noqa: E402
src._get = fake_get
P = lambda rel: str(work / rel)  # noqa: E731
with app.db() as c:
    c.executemany('INSERT OR REPLACE INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES(?,?,?,?,?)', [
        (P('shows/Breaking Bad (2008)'), 81189, 1396, None, 'sonarr'), (P('films/The Matrix (1999)'), None, 603, 'tt0133093', 'radarr'),
        (P('films/Amélie (2001)'), None, None, 'tt0211915', 'radarr'), (P('anime/Naruto (2002)'), None, None, 'tt0409591', 'sonarr')])
found, notes = src.find(app, P('shows/Breaking Bad (2008)'), 'Breaking Bad (2008)', 'Shows')
check('Shows: Plex TV Themes first, then ThemerrDB (TV)', [f['source'] for f in found] == ['plex', 'themerrdb'] and 'tv_shows/themoviedb/1396' in calls[-1], (found, calls))
calls.clear(); found, _ = src.find(app, P('films/The Matrix (1999)'), 'The Matrix (1999)', 'Movies')
check('Movies: ThemerrDB by TMDB id, no Plex', [f['source'] for f in found] == ['themerrdb'] and all('plexapp' not in u for u in calls) and 'movies/themoviedb/603' in calls[0], calls)
calls.clear(); found, _ = src.find(app, P('films/Amélie (2001)'), 'Amélie (2001)', 'Movies')
check('Movies without a TMDB id: ThemerrDB by IMDb id', [f['source'] for f in found] == ['themerrdb'] and calls == ['https://app.lizardbyte.dev/ThemerrDB/movies/imdb/tt0211915.json']
      and 'IMDb tt0211915' in found[0]['note'], calls)
calls.clear(); found, notes = src.find(app, P('anime/Naruto (2002)'), 'Naruto (2002)', 'Anime TV')
check('Anime unchanged: an IMDb-only id is not used (no lookups)', found == [] and calls == [], (found, calls))
found, notes = src.find(app, P('shows/Friends'), 'Friends', 'Shows')
check('No exact id → nothing, with a clear note (no guessing)', found == [] and any('No exact TVDB/TMDB/IMDb id' in n for n in notes), notes)

# ------------------------------------------------------------------ YouTube search: settings + gate ----
check('Budget clamped to 1..95 (default 80)', (ys.clamp_budget(500), ys.clamp_budget(0), ys.clamp_budget('x')) == (95, 1, 80))
ys.save_settings({'enabled': True, 'api_key': 'AIza' + 'k' * 35, 'budget': 80, 'background': False})
check('Settings file is private (0600)', oct((work / 'config' / ys.FILE).stat().st_mode & 0o777) == '0o600')
ok, why = ys.can_search()
check('cookies.txt is mandatory: no search without it', not ok and 'cookies.txt' in why and not ys.ready(), why)
r = ys.post(H('/download-clients/youtube-search', 'enabled=1&budget=80'), redirect)
check('…and YouTube search cannot be switched on without it', 'cookies.txt first' in redirected[-1], redirected[-1])
cookie = ('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t%d\tSID\tsecretvalue\n.youtube.com\tTRUE\t/\tTRUE\t%d\tPREF\tf1\n'
          % (int(time.time()) + 86400 * 200, int(time.time()) + 86400 * 200)).encode()
cks.install(str(work / 'config'), cookie)
check('With cookies.txt + key: ready', ys.ready() and ys.can_search() == (True, ''))

# ------------------------------------------------------------------ YouTube search: calls, quota, cache ----
api = []


def fake_call(endpoint, params, key):
    api.append((endpoint, dict(params)))
    if endpoint == 'search':
        return {'items': [{'id': {'videoId': v}, 'snippet': {'title': t, 'channelTitle': 'Ch'}} for v, t in
                          (('BBBBBBBBBBB', 'Friends Theme Song (Official)'), ('CCCCCCCCCCC', 'Friends theme LIVE cover 10 hours'), ('DDDDDDDDDDD', 'Friends 1999 trailer'))]}
    return {'items': [{'id': 'BBBBBBBBBBB', 'contentDetails': {'duration': 'PT1M2S'}, 'status': {'embeddable': True}, 'snippet': {'title': 'Friends Theme Song (Official)', 'channelTitle': 'Ch'}},
                      {'id': 'CCCCCCCCCCC', 'contentDetails': {'duration': 'PT10H'}, 'status': {'embeddable': True}, 'snippet': {'title': 'Friends theme LIVE cover 10 hours', 'channelTitle': 'Ch'}},
                      {'id': 'DDDDDDDDDDD', 'contentDetails': {'duration': 'PT2M'}, 'status': {'embeddable': False}, 'snippet': {'title': 'Friends 1999 trailer', 'channelTitle': 'Ch'}}]}


real_call = ys._call
ys._call = fake_call
res, note = ys.suggest(app, P('shows/Friends'), 'Friends', 'Shows')
SEARCH = next(x for x in api if x[0] == 'search')  # 1.12: the free MainThemeSoundtracks list read may come first
check('One search.list + one videos.list per title; query says "theme song"', [e for e, _ in api if e != 'playlistItems'] == ['search', 'videos'] and SEARCH[1]['q'] == 'Friends theme song'
      and SEARCH[1]['maxResults'] == 5 and SEARCH[1]['videoEmbeddable'] == 'true', api)
check('Not-embeddable results dropped; preview-only results', [x['video_id'] for x in res] == ['BBBBBBBBBBB', 'CCCCCCCCCCC'] and all(x['source'] == 'youtube_search' for x in res), res)
check('Warning flags: cover / live / extended / very long', set(res[1]['flags']) >= {'cover', 'live', 'extended'} and any(f.startswith('very long') for f in res[1]['flags'])
      and res[0]['flags'] == [], res[1]['flags'])
check('Usage counted (1 search)', ys.usage()['used'] == 1)
api.clear(); res2, note2 = ys.suggest(app, P('shows/Friends'), 'Friends', 'Shows')
check('Cached: the same title costs nothing the second time', api == [] and [x['video_id'] for x in res2] == ['BBBBBBBBBBB', 'CCCCCCCCCCC'] and '30 days' in note2, note2)
res3, note3 = ys.suggest(app, P('films/Nothing Here (2033)'), 'Nothing Here (2033)', 'Movies')
check('Searches are spaced (title-page searches ≥10 s apart)', res3 == [] and 'spacing' in note3 and not [x for x in api if x[0] == 'search'], note3)  # 1.12: the MainThemeSoundtracks list read is not a search
u = ys.usage(); u['last'] = 0; ys._write(ys.USAGE, u)
res3, _ = ys.suggest(app, P('films/Nothing Here (2033)'), 'Nothing Here (2033)', 'Movies')
check('Movies query says "main theme" with the year', next(x[1] for x in api if x[0] == 'search')['q'] == 'Nothing Here 2033 main theme', api)


def quota(endpoint, params, key):
    raise ys.ApiError(403, 'quotaExceeded')


ys._call = quota
ys.forget(app, P('films/Nothing Here (2033)')); u = ys.usage(); u['last'] = 0; ys._write(ys.USAGE, u); used = ys.usage()['used']
res4, note4 = ys.suggest(app, P('films/Nothing Here (2033)'), 'Nothing Here (2033)', 'Movies')
check('quotaExceeded → pause until the daily reset, no retry', res4 == [] and ys.usage()['paused'] and 'stops until' in note4 and ys.usage()['used'] == used, note4)
ok, why = ys.can_search()
check('…and nothing more is searched today', not ok and 'paused until' in why, why)
u = ys.usage(); u.update(paused=False, used=80, last=0); ys._write(ys.USAGE, u)
ok, why = ys.can_search()
check('Daily budget enforced', not ok and 'budget is used up' in why, why)
u = ys.usage(); u.update(used=0); ys._write(ys.USAGE, u)
ys._call = fake_call

captured = []


class FakeResp(io.BytesIO):
    status = 200

    def geturl(self):
        return 'https://www.googleapis.com/youtube/v3/search'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = lambda req, timeout=0: captured.append(req) or FakeResp(b'{"items":[]}')
real_call('search', {'q': 'x'}, 'AIzaSECRETKEY' + 'z' * 26)
urllib.request.urlopen = real_urlopen
check('API key sent in a header, never in the URL', 'AIzaSECRETKEY' not in captured[0].full_url and captured[0].get_header('X-goog-api-key', '').startswith('AIzaSECRET'))

# ------------------------------------------------------------------ cached_find / queue / auto / process ----
src.SUGGEST_DAYS = 7
found, notes = src.cached_find(app, P('shows/Breaking Bad (2008)'), 'Breaking Bad (2008)', 'Shows')
with app.db() as c:
    detail = c.execute('SELECT state,detail FROM shows WHERE path=?', (P('shows/Breaking Bad (2008)'),)).fetchone()
check('Suggestions remembered; title stays "missing" with a summary', detail['state'] == 'missing' and detail['detail'].startswith('Suggestions: 2'), dict(detail))
s_auto = __import__('auto_themes').settings(); s_auto.update(mode='all', per_scan=50); __import__('auto_themes')._save(s_auto)
picked = [Path(p).name for p in __import__('auto_themes').to_install(app)]
check('…even with "all missing" automatic mode', 'Naruto (2002)' in picked and not any(n in picked for n in ('Breaking Bad (2008)', 'Friends', 'The Matrix (1999)')), picked)
out = app.process(P('shows/Breaking Bad (2008)'))
check('Download / Retry on a Show only refreshes suggestions (no download)', out.startswith('Suggestions:') and not (work / 'shows/Breaking Bad (2008)/theme.mp3').exists(), out)
row = {'path': P('shows/Breaking Bad (2008)'), 'name': 'Breaking Bad (2008)', 'library': 'Shows'}
state, source, prop, note = queue.propose(app, row)
check('Theme Queue: Shows skip AnimeThemes and propose the exact-ID Plex theme', state == 'proposed' and source == 'plex' and prop['kind'] == 'fallback', (state, source))
state, source, prop, note = queue.propose(app, {'path': P('shows/Friends'), 'name': 'Friends', 'library': 'Shows'})
check('Theme Queue never proposes a YouTube search result', state == 'no_match' and 'YouTube search result' in note and prop is None, (state, note))

# ------------------------------------------------------------------ install validation ----
redirected.clear()
regular.post(app, H('/regular/install', urllib.parse.urlencode({'path': P('shows/Friends'), 'idx': '0', 'confirm': '1'})), redirect)
check('A YouTube search result can never be installed', 'Not done' in redirected[-1] and not (work / 'shows/Friends/theme.mp3').exists(), redirected[-1])
regular.post(app, H('/regular/install', urllib.parse.urlencode({'path': P('shows/Breaking Bad (2008)'), 'idx': '9', 'confirm': '1'})), redirect)
check('Only a saved suggestion (by index) can be installed', 'changed meanwhile' in redirected[-1], redirected[-1])
regular.post(app, H('/regular/install', urllib.parse.urlencode({'path': P('shows/Breaking Bad (2008)'), 'idx': '0'})), redirect)
check('Install needs the confirmation tick', 'Tick the confirmation box' in redirected[-1], redirected[-1])
regular.post(app, H('/regular/install', urllib.parse.urlencode({'path': P('anime/Naruto (2002)'), 'idx': '0', 'confirm': '1'})), redirect)
check('Anime titles are refused here', 'not in a Shows or Movies root folder' in redirected[-1], redirected[-1])

# ---- 1.11.8: streamed install progress; read-only folders stop before any download ----
class SH:
    def __init__(self, body):
        b = body.encode(); self.headers = {'Content-Length': str(len(b)), 'Content-Type': 'application/x-www-form-urlencoded', 'X-Preludarr-Progress': 'v1'}
        self.path = '/regular/install'; self.rfile = io.BytesIO(b); self.wfile = io.BytesIO(); self.close_connection = False

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


import library_health as lh  # noqa: E402
real_pre = lh.preflight
lh.preflight = lambda folder: (_ for _ in ()).throw(ValueError('The library is mounted read-only in Docker — make the mapping read/write to install themes.'))
calls.clear()
h = SH(urllib.parse.urlencode({'path': P('shows/Breaking Bad (2008)'), 'idx': '0', 'confirm': '1'}))
regular.post(app, h, redirect)
lines = [json.loads(x) for x in h.wfile.getvalue().decode().splitlines() if x.strip()]
check('Progress: read-only folder → clear error, nothing downloaded', [x['phase'] for x in lines] == ['checking', 'error'] and 'read-only' in lines[-1]['message'] and calls == [], lines)
lh.preflight = real_pre

# ------------------------------------------------------------------ cookie test (local part) ----
cfgdir = str(work / 'config')
check('Cookie test: sign-in cookies found', ytui.local_check(cfgdir)['ok'])
cks.install(cfgdir, b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\told\n')
check('Cookie test: expired sign-in cookies reported', 'expired' in ytui.local_check(cfgdir)['text'])
cks.install(cfgdir, b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t9999999999\tPREF\tf1\n')
check('Cookie test: missing sign-in cookies reported', 'No YouTube sign-in cookies' in ytui.local_check(cfgdir)['text'])
cks.install(cfgdir, b'# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t9999999999\tSID\tx\n')
t = ytui.run_test(cfgdir)
check('Cookie test: no youtube.com cookies → fails without any network request', not t['ok'] and 'no youtube.com cookies' in t['text'] and 'Live test skipped' in t['text'], t)
check('Cookie test result is saved privately and shown', oct(os.stat(os.path.join(cfgdir, ytui.TEST_FILE)).st_mode & 0o777) == '0o600' and 'Last test' in ytui._last_test_html(cfgdir))
check('Cookie values never appear in the test result', 'secretvalue' not in json.dumps(t) and 'secretvalue' not in ytui.panel(cfgdir))
check('Guide links to the official yt-dlp instructions', 'yt-dlp/wiki/Extractors#exporting-youtube-cookies' in ytui.COOKIE_GUIDE and 'how-do-i-pass-cookies-to-yt-dlp' in ytui.COOKIE_GUIDE
      and 'spare Google account' in ytui.COOKIE_GUIDE)

# ------------------------------------------------------------------ 1.11.1 cookie upload: any .txt name, clear errors, auto-test ----
class UH:
    class Conn:
        def settimeout(self, t):
            pass

    class Hdr(dict):
        def get_all(self, k, d=None):
            return [self[k]] if k in self else d

    def __init__(self, filename, content):
        b = (b'--XyZ\r\nContent-Disposition: form-data; name="cookies"; filename="' + filename.encode() + b'"\r\nContent-Type: text/plain\r\n\r\n'
             + content + b'\r\n--XyZ--\r\n')
        self.headers = UH.Hdr({'Content-Length': str(len(b)), 'Content-Type': 'multipart/form-data; boundary=XyZ'})
        self.rfile = io.BytesIO(b); self.connection = UH.Conn(); self.loc = ''

    def send_response(self, c):
        pass

    def send_header(self, k, v):
        if k == 'Location':
            self.loc = urllib.parse.unquote_plus(v)

    def end_headers(self):
        pass


ytui.live_check = lambda d: (True, 'YouTube accepted the cookies as signed in.')  # no network in tests
good = ('# Netscape HTTP Cookie File\r\n.youtube.com\tTRUE\t/\tTRUE\t%d\tSID\tabc\r\n' % (int(time.time()) + 864000)).encode()
h = UH('www.youtube.com_cookies (2).txt', good); ytui.handle(h, '/youtube-cookies-upload', cfgdir)
check('Upload accepts the extension’s file name (saved under Preludarr’s own name) and tests it at once',
      'Cookies saved' in h.loc or ('YouTube cookies saved' in h.loc and 'Test passed' in h.loc), h.loc)
check('…saved file is the fixed internal name, private', cks.status(cfgdir).get('configured') and cks.status(cfgdir).get('private'))
h = UH('notes.txt', b'hello'); ytui.handle(h, '/youtube-cookies-upload', cfgdir)
check('A wrong file gets a clear reason', 'Cookies not saved: Expected Netscape cookies.txt format' in h.loc, h.loc)
h = UH('cookies.json', good); ytui.handle(h, '/youtube-cookies-upload', cfgdir)
check('Non-.txt files are refused with a reason', 'exported .txt cookies file' in h.loc, h.loc)
check('Test button warns when a chosen file was not uploaded yet', 'have not uploaded it yet' in ytui.panel(cfgdir))
check('API key guide: Google console steps + official links', all(x in ys.API_KEY_GUIDE for x in ('apis/library/youtube.googleapis.com', 'apis/credentials',
      'Restrict key', 'developers.google.com/youtube/v3/getting-started')) and 'youtube-api-key-guide' in ys.card_html())

# ------------------------------------------------------------------ 1.11.1 Activity titles link to the Theme Browser ----
app.event('The Matrix (1999)', 'installed', 'test'); app.event('System', 'scan', 'Discovered 9 titles')
act = __import__('system')._activity_page(app, {'tab': ['activity']})
mx = urllib.parse.quote_plus(P('films/The Matrix (1999)'))
check('Activity: a title links to its Theme Browser page', ('href="/themes?path=' + mx + '"') in act and '>The Matrix (1999)</a>' in act)
check('Activity: non-title events (System) stay plain text', '>System</td>' in act)

m = json.loads(__import__('login').manifest_json())
check('Installed app: Edge/Chrome get back + reload (minimal-ui); Safari keeps standalone; stable app id',
      m.get('display_override') == ['minimal-ui'] and m.get('display') == 'standalone' and m.get('id') == '/' and m.get('start_url') == '/')

bt = __import__('login').icon_tags()
check('Installed app: own Back button, only in standalone mode and only with somewhere to go back to',
      'display-mode: standalone' in bt and 'history.back()' in bt and 'history.length>1||same()' in bt and 'aria-label="Go back' not in bt and 'Go back to the previous page' in bt)

# ------------------------------------------------------------------ pages ----
app.auth.get = lambda handler: True
server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


op = urllib.request.build_opener(NoRedirect)


def get(path):
    try:
        r = op.open(f'http://127.0.0.1:{port}{path}', timeout=60)
        return r.status, r.headers.get('Location', ''), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Location', ''), e.read().decode()


# 1.11.9: a Library tab per ROOT FOLDER, named by its display name (user: "whatever display name i give it … on the library tab")
(work / 'kids' / 'Bluey (2018)').mkdir(parents=True)
os.environ['THEMEARR_LIBRARY_MOUNTS'] += f',{work}/kids'
rf.add(app, str(work / 'kids'), 'Kid Shows', 'Shows'); app.discover()
films = 'root:' + str(work / 'films')
st, loc, _ = get('/?view=library&library=Movies&q=the')
check('Older type links open that type\'s first root-folder tab', st in (302, 303) and urllib.parse.unquote_plus(loc).endswith('library=' + films + '&q=the') or ('library=' + urllib.parse.quote_plus(films)) in loc, (st, loc))
st, _, page = get('/?' + urllib.parse.urlencode({'view': 'library', 'library': films, 'q': 'the'}))
nav = page.split('<nav class="tm-nav">')[1].split('</nav>')[0]
check('Sidebar: one Library entry + Collections, no per-type entries', '📚 Library' in nav and '🗂️ Collections' in nav and 'Anime Series' not in nav and nav.count('view=library&library=') == 1, nav[:600])
tabs = page.split('id="library-tabs">')[1].split('</nav>')[0]
labels = [x.split('>')[-1] for x in tabs.split('</a>')[:-1]]
check('One tab per root folder, named by its display name; Films selected', labels == ['Anime TV', 'Anime Movies', 'TV', 'Films', 'Kid Shows'] and 'aria-current="page" title="Movies' in tabs, labels)
check('Switching tabs keeps filters but resets the page', 'page=' not in tabs and 'q=the' in tabs)
check('No Library dropdown on a tab; the tab scopes search + bulk actions', 'id="ls-library"' not in page and page.count('name="library" value="' + films + '"') >= 3)
check('Clear keeps the current tab (and resets the remembered filters to All, 1.12.2)', ('href="/?view=library&amp;library=' + urllib.parse.quote_plus(films) + '&amp;state=all&amp;poster=all&amp;f=1" class="ls-btn ls-quiet">Clear') in page)
res_html = page.split('id="library-results"')[1].split('id="recent-activity"')[0]
check('Results only from the selected tab', 'The Matrix (1999)' in res_html and 'Breaking Bad' not in res_html and 'Naruto' not in res_html)
kid = 'root:' + str(work / 'kids')
st, _, kp = get('/?' + urllib.parse.urlencode({'view': 'library', 'library': kid}))
kres = kp.split('id="library-results"')[1].split('id="recent-activity"')[0]
check('Two Shows folders stay apart: Kid Shows tab lists only its own titles', 'Bluey (2018)' in kres and 'Breaking Bad' not in kres and ' in Kid Shows' in kp)
check('Bulk queue count is scoped to the tab', __import__('theme_queue').candidates(app, {'q': [''], 'library': [kid], 'state': ['all']}) == [str(work / 'kids' / 'Bluey (2018)')])
check('Poster bulk filter accepts the tab and scopes to it', __import__('poster_filters').selection(library=kid)['library'] == kid)
try:
    __import__('poster_filters').selection(library='root:/etc'); bad_root = False
except ValueError:
    bad_root = True
check('A tab value for an unregistered folder is refused', bad_root)
st, loc, _ = get('/?q=matrix&library=Movies')
check('Old dashboard search bookmarks open the Library', st in (302, 303) and loc.startswith('/?view=library') and 'q=matrix' in loc, (st, loc))
st, _, dash = get('/')
check('Dashboard: a stats card per root folder, linked to its tab, + Theme Queue', all(('Kid Shows' in dash, 'Films' in dash, urllib.parse.quote_plus(kid) in dash, '🧾 Theme Queue' in dash))
      and 'body.dashboard-view #library-search' in dash)
st, _, col = get('/collections')
check('Collections page opens (1.12: real page; no media server connected → explains where to connect)', st == 200 and 'Collections - Preludarr' in col and 'Settings → Connect' in col)
q = urllib.parse.urlencode({'path': P('shows/Breaking Bad (2008)'), 'fragment': 'match'})
st, _, frag = get('/themes?' + q)
check('Show title page: suggestions with exact-ID evidence, install needs a tick', 'Theme suggestions' in frag and 'TVDB 81189' in frag and 'name="confirm" value="1" required' in frag
      and 'Search AnimeThemes' not in frag)
q = urllib.parse.urlencode({'path': P('shows/Friends'), 'fragment': 'match'})
st, _, frag = get('/themes?' + q)
check('YouTube results: preview + copy link only (no install button)', 'YouTube search results' in frag and 'data-rg-copy' in frag and 'youtube-nocookie' in frag
      and '/regular/install' not in frag.split('YouTube search results')[1].split('Look again')[0])
os.chmod(work / 'shows' / 'Breaking Bad (2008)', 0o555)
st, _, frag = get('/themes?' + urllib.parse.urlencode({'path': P('shows/Breaking Bad (2008)'), 'fragment': 'match'}))
os.chmod(work / 'shows' / 'Breaking Bad (2008)', 0o755)
check('Read-only folder: explained up front and Install switched off', ('read-only' in frag and 'disabled title="The folder is read-only in Docker"' in frag) or os.geteuid() == 0, os.geteuid())
server.shutdown()

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
