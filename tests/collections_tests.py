# SPDX-License-Identifier: GPL-3.0-or-later
"""Collections (1.12): theme songs for movie collections — Plex upload, Jellyfin/Emby collection folder. Run in a Preludarr image, --network none."""
import hashlib
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
movies = work / 'movies'
jfcol = work / 'jellyfin-collections'
for d in ('Bleach - Memories of Nobody (2006)', 'Bleach - The DiamondDust Rebellion (2007)', 'Bleach - Fade to Black (2008)',
          'The Matrix (1999)', 'The Matrix Reloaded (2003)'):
    (movies / d).mkdir(parents=True)
(jfcol / 'Bleach Collection [boxset]').mkdir(parents=True)
(work / 'anime').mkdir()


def mp3(tag):
    return b'ID3\x03\x00\x00\x00\x00\x00\x00' + tag.encode() * 300


(movies / 'Bleach - Memories of Nobody (2006)' / 'theme.mp3').write_bytes(mp3('memories'))
(movies / 'Bleach - The DiamondDust Rebellion (2007)' / 'theme.mp3').write_bytes(mp3('diamond'))
(movies / 'The Matrix Reloaded (2003)' / 'theme.mp3').write_bytes(mp3('reloaded'))

os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(movies), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import media_servers as ms  # noqa: E402
import movie_collections as mc  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


for d in sorted(movies.iterdir()):
    app.save(str(d), 'Anime Movies', d.name, 'missing')

PORT = 18995
LOG = work / 'fake.log'
LOG.write_text('')
srv = subprocess.Popen([sys.executable, '/src/tests/fake_media_server.py', str(PORT), str(LOG), str(movies)])
time.sleep(1.0)
ms._base_url = lambda raw: str(raw).rstrip('/')  # the fake server is on 127.0.0.1 (real rule: private LAN IPs or HTTPS only)
url = f'http://127.0.0.1:{PORT}'
ms._write(app.CONFIG, 'servers', {'plex': {'kind': 'plex', 'url': url, 'api_key': 'plextoken', 'enabled': True},
                                  'jellyfin': {'kind': 'jellyfin', 'url': url, 'api_key': 'jftoken', 'enabled': True},
                                  'emby': {'kind': 'emby', 'url': url, 'api_key': 'embytoken', 'enabled': True}})


class H:
    def __init__(self, path, form=None):
        body = urllib.parse.urlencode(form or {}).encode()
        self.path = path; self.headers = {'Content-Length': str(len(body))}; self.rfile = io.BytesIO(body); self.sent = None; self.loc = None

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)


def redirect(h, location):
    h.loc = urllib.parse.unquote_plus(location)


def page_q(server, library):
    h = H('/collections?' + urllib.parse.urlencode({'server': server, 'library': library})); mc.page(app, h); return h.sent[1]


def page_q_root(server, root):
    h = H('/collections?' + urllib.parse.urlencode({'server': server, 'root': root})); mc.page(app, h); return h.sent[1]


def page(server=''):
    h = H('/collections' + ('?server=' + server if server else '')); mc.page(app, h); return h.sent[1]


def writes():
    return [json.loads(x) for x in LOG.read_text().splitlines() if x.strip()]


def refresh(kind):
    assert mc.refresh(app, kind)
    for _ in range(100):
        if not mc._RUN[kind]['running']:
            return
        time.sleep(0.1)


html = page()
check('Before a refresh the page says "not read yet" and contacts no server', 'Not read yet' in html and not writes() and 'Plex' in html and 'experimental' in html)
refresh('plex')
state = mc.load(app)['servers']['plex']
check('Refresh reads every Plex movie collection in every movie library (TV sections skipped)', not state['error'] and [(i['title'], i['library']) for i in state['items']] ==
      [('Bleach Collection', 'Movies'), ('The Matrix Collection', 'Movies'), ('Unknown Collection', 'Movies'), ('The Matrix Collection', '4K Movies')], state)
items = {i['title']: i for i in state['items'] if i['library'] == 'Movies'}
reads = lambda: len([w for w in writes() if w['op'] == 'children'])  # noqa: E731
first = reads()
refresh('plex')
check('A second Refresh only re-reads collections that changed in Plex (none here)', first == 4 and reads() == first
      and [len(i['members']) for i in mc.load(app)['servers']['plex']['items']] == [len(i['members']) for i in state['items']], (first, reads()))
cands = mc.candidates(app, items['Bleach Collection'])
check('Movies are matched to library titles by folder name and ordered earliest first', [c['year'] for c in cands] == [2006, 2007, 2008]
      and all(c['path'] for c in cands) and [c['theme'] for c in cands] == [True, True, False])
html = page('plex')
bleach = html[html.index('<h3>Bleach Collection'):html.index('<h3>The Matrix Collection')]
check('Default song = the first movie\'s theme (Memories of Nobody, 2006)', 'selected>Bleach the Movie: Memories of Nobody (2006)' in bleach)
check('A tab per movie root folder, like the Library: this root has its 3 collections; 4K Movies goes to "Other libraries"',
      'id="collection-tabs"' in html and html.count('<h3>The Matrix Collection') == 1 and '4K Movies' not in html and '<h3>Unknown Collection' in html
      and 'Other libraries <span class="col-count">1</span>' in html and '3 collections in' in html)
other = page_q_root('plex', 'other')
check('"Other libraries" tab lists collections from libraries that are not root folders (with their Plex library)', other.count('<h3>') == 1
      and '<span class="tag">4K Movies</span>' in other and 'not Preludarr root folders' in other)
matrix = html[html.index('<h3>The Matrix Collection'):html.index('<h3>Unknown Collection')]
check('First movie without a theme: explained, next movie with one is used', 'has no theme yet' in matrix and 'selected>The Matrix Reloaded (2003)' in matrix)
check('A collection Plex already themes is labelled', 'Has a theme in Plex' in matrix and 'Replace theme' in matrix)
unknown = html[html.index('<h3>Unknown Collection'):]
check('Collection with no movies in the library says so (no Install button)', 'were found in your Preludarr root folders' in unknown and 'Install theme' not in unknown)



class R:
    def __init__(self, path):
        self.path = path; self.code = None; self.hdr = {}; self.wfile = io.BytesIO()

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        self.hdr[k] = v

    def end_headers(self):
        pass


def poster(server, ident):
    r = R('/collections/poster?' + urllib.parse.urlencode({'server': server, 'id': ident})); mc.poster(app, r); return r


r = poster('plex', '501')
cache = list((Path(app.CONFIG) / mc.POSTERS).glob('plex-*.img'))
check('Poster: the collection artwork comes from Plex through Preludarr (JPEG, cached privately)', r.code == 200 and r.hdr['Content-Type'] == 'image/jpeg'
      and r.wfile.getvalue()[:3] == b'\xff\xd8\xff' and len(cache) == 1 and oct(cache[0].stat().st_mode & 0o777) == '0o600', (r.code, r.hdr))
n = len([w for w in writes() if w['op'] == 'poster'])
r = poster('plex', '501')
check('…the second view is served from the cache (Plex is not asked again)', r.code == 200 and len([w for w in writes() if w['op'] == 'poster']) == n)
check('A collection without artwork: 404, and the page shows a placeholder', poster('plex', '502').code == 404 and 'col-noart' in page('plex')
      and 'src="/collections/poster?server=plex&amp;id=501"' in page('plex'))
check('Unknown server or collection: 404', poster('nope', '501').code == 404 and poster('plex', '999').code == 404)
def search(term):
    h = H('/collections?' + urllib.parse.urlencode({'server': 'plex', 'q': term})); mc.page(app, h); return h.sent[1]


res = search('matrix')
check('Search by name looks in every tab (Movies and Other libraries)', res.count('<h3>The Matrix Collection') == 2 and '2 collections match “matrix” in all folders' in res
      and 'Other libraries</span>' in res and 'Clear</a>' in res)
res = search('Memories of Nobody')
check('Search also finds a collection by one of its movies\' names', res.count('<h3>') == 1 and '<h3>Bleach Collection' in res)
check('No match: a plain message', 'No collection or movie matches “zzzz”' in search('zzzz'))
pg = page('plex')
check('The search bar is on the page, and each row carries its searchable names for filtering as you type', 'id="col-q"' in pg
      and 'data-search="bleach collection bleach the movie: fade to black' in pg and 'press Enter to search all folders' in pg)
memories = str(movies / 'Bleach - Memories of Nobody (2006)')
h = H('/collections/install', {'server': 'plex', 'id': '501', 'source': memories}); mc.post(app, h, redirect)
w = writes()
expect = hashlib.sha256(mp3('memories')).hexdigest()
check('Install (Plex): the movie\'s theme.mp3 is uploaded to the collection unchanged', h.loc and 'Theme installed for Bleach Collection' in h.loc
      and w and w[-1]['op'] == 'upload' and w[-1]['path'] == '/library/metadata/501/themes' and w[-1]['sha256'] == expect, (h.loc, w[-1:] if w else w))
check('…recorded and shown on the page', mc.load(app)['installed']['plex']['501']['source_name'] == 'Bleach - Memories of Nobody (2006)'
      and 'Theme installed by Preludarr' in page('plex'))
for src, why in ((str(movies / 'The Matrix Reloaded (2003)'), 'a movie from another collection'), (str(movies / 'Bleach - Fade to Black (2008)'), 'a member without a theme'),
                 ('/etc', 'an arbitrary folder')):
    h = H('/collections/install', {'server': 'plex', 'id': '501', 'source': src}); n = len(writes()); mc.post(app, h, redirect)
    check('Refused: ' + why, h.loc and h.loc.startswith('/collections?server=plex&notice=Not done') and len(writes()) == n, h.loc)
link = movies / 'Bleach - Fade to Black (2008)' / 'theme.mp3'
link.symlink_to(movies / 'The Matrix Reloaded (2003)' / 'theme.mp3')
check('A symbolic-link theme.mp3 is never offered or read', not [c for c in mc.candidates(app, items['Bleach Collection']) if c['year'] == 2008][0]['theme'])
link.unlink()
h = H('/collections/remove', {'server': 'plex', 'id': '501'}); mc.post(app, h, redirect)
check('Remove needs the confirmation box', 'Tick the box' in h.loc)
h = H('/collections/remove', {'server': 'plex', 'id': '501', 'confirm': '1'}); mc.post(app, h, redirect)
check('Remove (Plex): deletes the uploaded theme and forgets the record', writes()[-1] == {'server': 'plex', 'op': 'delete', 'path': '/library/metadata/501/theme'}
      and '501' not in mc.load(app)['installed'].get('plex', {}), h.loc)

# ---- other sources: your own audio, a YouTube link, smart YouTube search
def multipart(fields, filename, data):
    b = 'colboundary42'
    out = b''.join(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode() for k, v in fields.items())
    out += ('--%s\r\nContent-Disposition: form-data; name="audio"; filename="%s"\r\nContent-Type: audio/wav\r\n\r\n' % (b, filename)).encode() + data + ('\r\n--%s--\r\n' % b).encode()
    return out, 'multipart/form-data; boundary=' + b


class U:
    def __init__(self, body, ctype):
        self.path = '/collections/upload'; self.headers = {'Content-Length': str(len(body)), 'Content-Type': ctype}; self.rfile = io.BytesIO(body); self.loc = None


wav = work / 'tone.wav'
subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3', str(wav)], check=True)
body, ctype = multipart({'server': 'plex', 'id': '501'}, 'my theme.wav', wav.read_bytes())
os.environ.pop('THEMEARR_MANUAL_INSTALL_ENABLED', None)
h = U(body, ctype); mc.post_upload(app, h, redirect)
check('Own audio is refused while manual installs are off', 'Uploads are off' in h.loc, h.loc)
os.environ['THEMEARR_MANUAL_INSTALL_ENABLED'] = '1'
n = len(writes())
h = U(body, ctype); mc.post_upload(app, h, redirect)
up = writes()[-1]
check('Own audio: converted to MP3 and uploaded to the Plex collection', 'Theme installed for Bleach Collection (from Upload: my theme.wav)' in h.loc
      and len(writes()) == n + 1 and up['op'] == 'upload' and up['path'] == '/library/metadata/501/themes' and up['bytes'] > 1024, (h.loc, up))
check('…no temporary files left in /config', not list(Path(app.CONFIG).glob('.preludarr-upload-*')) and not list(Path(app.CONFIG).glob('.preludarr-collection-*')))
body2, ctype2 = multipart({'server': 'plex', 'id': '501'}, 'notes.wav', b'this is not audio' * 100)
h = U(body2, ctype2); mc.post_upload(app, h, redirect)
check('A file that is not audio is refused', 'no usable audio' in h.loc, h.loc)
body3, ctype3 = multipart({'server': 'plex', 'id': '501'}, 'virus.exe', wav.read_bytes())
h = U(body3, ctype3); mc.post_upload(app, h, redirect)
check('Only audio file types are accepted', 'Choose an audio file' in h.loc, h.loc)

yd = __import__('youtube_download')
stages = []
def fake_download(url, config):
    st = Path(tempfile.mkdtemp(prefix='themearr-youtube-', dir=config)); a = st / 'abcdefghijk.webm'; a.write_bytes(wav.read_bytes()); stages.append(st)
    return {'stage': st, 'audio': a, 'video_id': 'abcdefghijk', 'title': 'Bleach OST - Number One'}
real_dl = yd.download_to_stage
yd.download_to_stage = fake_download
h = H('/collections/youtube', {'server': 'plex', 'id': '501', 'url': 'https://www.youtube.com/watch?v=abcdefghijk'}); mc.post(app, h, redirect)
check('YouTube link needs the confirmation box', 'Tick the box' in h.loc and not stages)
h = H('/collections/youtube', {'server': 'plex', 'id': '501', 'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'confirm': '1'}); mc.post(app, h, redirect)
check('YouTube link: the audio is downloaded, converted and uploaded; the download folder is removed', 'from YouTube: Bleach OST - Number One' in h.loc
      and writes()[-1]['op'] == 'upload' and stages and not stages[-1].exists(), h.loc)


class S:
    """A streaming request (X-Preludarr-Progress: v1), as the page's script sends it."""
    def __init__(self, path, form):
        body = urllib.parse.urlencode(form).encode()
        self.path = path; self.headers = {'Content-Length': str(len(body)), 'X-Preludarr-Progress': 'v1'}; self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO(); self.code = None; self.hdr = {}

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        self.hdr[k] = v

    def end_headers(self):
        pass

    def lines(self):
        return [json.loads(x) for x in self.wfile.getvalue().decode().splitlines() if x.strip()]


s = S('/collections/youtube', {'server': 'plex', 'id': '501', 'root': str(movies), 'url': 'https://www.youtube.com/watch?v=abcdefghijk', 'confirm': '1'})
mc.post(app, s, redirect)
ln = s.lines()
check('YouTube install streams its steps: checking → downloading → converting → uploading → done', s.hdr.get('Content-Type', '').startswith('application/x-ndjson')
      and [x['phase'] for x in ln] == ['checking', 'downloading', 'converting', 'installing', 'done'], [x['phase'] for x in ln])
nxt = urllib.parse.unquote_plus(ln[-1].get('next', ''))
check('…and then returns to the same tab and the same collection', ('root=' + str(movies)) in nxt and 'at=501' in nxt and nxt.endswith('#col-501'), nxt)
s = S('/collections/youtube', {'server': 'plex', 'id': '501', 'url': 'https://evil.example/watch?v=abcdefghijk', 'confirm': '1'})
mc.post(app, s, redirect)
check('A wrong link: the error is streamed into the row (nothing downloaded)', s.lines()[-1]['phase'] == 'error' and 'YouTube' in s.lines()[-1]['message'], s.lines())
yd.download_to_stage = real_dl
h = H('/collections/install', {'server': 'plex', 'id': '501', 'root': str(movies), 'source': '/etc'}); mc.post(app, h, redirect)
check('Without the script (plain form): back to the same tab, scrolled to the collection, message in that row', ('root=' + str(movies)) in h.loc and h.loc.endswith('#col-501'))
q = h.loc.split('?', 1)[1].split('#')[0]
hh = H('/collections?' + urllib.parse.urlencode(dict(urllib.parse.parse_qsl(q)))); mc.page(app, hh); pg = hh.sent[1]
row = pg[pg.index('id="col-501"'):pg.index('<h3>The Matrix Collection')]
check('…the result appears inside that collection\'s row, not at the top', 'Not done' in row and pg.index('Not done') > pg.index('id="col-501"'))
r = R('/collections/theme?server=plex&id=501'); mc.current_theme(app, r)
check('"Current theme ▶" plays the theme the collection has in Plex (through Preludarr)', r.code == 200 and r.hdr['Content-Type'] == 'audio/mpeg' and r.wfile.getvalue()[:3] == b'ID3')
check('Rows with a theme show the Current theme player', 'Current theme' in row and '/collections/theme?server=plex&amp;id=501' in row)

ys = __import__('youtube_search')
fake_results = [{'source': 'youtube_search', 'video_id': 'zyxwvutsrqp', 'url': 'https://www.youtube.com/watch?v=zyxwvutsrqp', 'track': 'Bleach Movie Theme',
                 'channel': 'Some Channel', 'seconds': 185, 'flags': ['cover']}]
real_settings, real_suggest, real_cached = ys.settings, ys.suggest, ys._cached
ys.settings = lambda: {'enabled': True, 'api_key': 'k' * 39}
calls = []
ys.suggest = lambda app_, path, name, library, interactive=True, everywhere=False: (calls.append((path, name, library)) or (fake_results, ''))
h = H('/collections/search', {'server': 'plex', 'id': '501'}); mc.post(app, h, redirect)
check('Smart search: searches YouTube for the collection name (without the word "Collection")', calls == [('collection:plex:501', 'Bleach', 'Movies')] and '1 YouTube result' in h.loc, (calls, h.loc))
ys._cached = lambda app_, path, query: (fake_results, 0) if path == 'collection:plex:501' else (None, 0)
html = page('plex')
row = html[html.index('<h3>Bleach Collection'):html.index('<h3>The Matrix Collection')]
check('Search results: title, channel, length, warning flags, an on-page ▶ Preview player and "Use this" (nothing installs by itself)', 'Bleach Movie Theme' in row and 'Some Channel · 3:05' in row
      and '>cover<' in row and 'data-col-play="zyxwvutsrqp"' in row and 'class="col-player" hidden' in row and 'youtube-nocookie.com/embed/' in html and 'data-col-use="https://www.youtube.com/watch?v=zyxwvutsrqp"' in row and 'name="url" value="https://www.youtube.com/watch?v=zyxwvutsrqp"' not in row and 'Use this' in row)
ys.settings, ys.suggest, ys._cached = real_settings, real_suggest, real_cached
check('Without a YouTube API key the panel explains where to turn search on', 'Settings → Download Clients → YouTube search' in page('plex'))
os.environ.pop('THEMEARR_MANUAL_INSTALL_ENABLED', None)

# ---- Jellyfin (experimental: collection folder)
refresh('jellyfin')
jf = mc.load(app)['servers']['jellyfin']
check('Jellyfin (no library folders): collections with at least one movie in the library are shown', '<h3>Bleach Collection' in page('jellyfin'))
saved = json.loads((Path(app.CONFIG) / mc.FILE).read_text())
moved = json.loads(json.dumps(saved))
for it in moved['servers']['plex']['items']:
    it['locations'] = ['/plexmedia/somewhere/else']   # Plex folders named differently from every root folder
(Path(app.CONFIG) / mc.FILE).write_text(json.dumps(moved))
fallback = page('plex')
(Path(app.CONFIG) / mc.FILE).write_text(json.dumps(saved))
check('If no Plex library folder matches any root folder, each collection goes to the root folder holding its movies', '<h3>Bleach Collection' in fallback and '<h3>Unknown Collection' not in fallback)
check('Jellyfin: BoxSets and their movies are read', not jf['error'] and jf['items'][0]['path_name'] == 'Bleach Collection [boxset]'
      and len(jf['items'][0]['members']) == 2, jf)
h = H('/collections/install', {'server': 'jellyfin', 'id': 'abc123', 'source': memories}); mc.post(app, h, redirect)
check('Jellyfin: without a collections folder, installing explains what to set up', 'collections folder first' in h.loc, h.loc)
for bad in ('relative/path', str(work / 'missing'), '/proc/../etc', '/', '/usr/lib', str(Path(app.CONFIG)), '/src/preludarr'):
    h = H('/collections/folder', {'server': 'jellyfin', 'folder': bad}); mc.post(app, h, redirect)
    check('Folder setting refused: ' + bad, 'Not done' in h.loc, h.loc)
h = H('/collections/folder', {'server': 'jellyfin', 'folder': str(jfcol)}); mc.post(app, h, redirect)
check('Folder setting saved when it exists and is writable', 'folder saved' in h.loc and mc.load(app)['folders']['jellyfin'] == str(jfcol))
dest = jfcol / 'Bleach Collection [boxset]' / 'theme.mp3'
h = H('/collections/install', {'server': 'jellyfin', 'id': 'abc123', 'source': memories}); mc.post(app, h, redirect)
check('Jellyfin: theme.mp3 written into the collection\'s folder and the collection refreshed', dest.read_bytes() == mp3('memories')
      and writes()[-1] == {'server': 'jellyfin', 'op': 'refresh', 'path': '/Items/abc123/Refresh'}, h.loc)
check('…with no temporary files left behind', sorted(p.name for p in dest.parent.iterdir()) == ['theme.mp3'])
diamond = str(movies / 'Bleach - The DiamondDust Rebellion (2007)')
jf_item = mc.load(app)['servers']['jellyfin']['items'][0]
check('Jellyfin candidates: only movies in the collection (DiamondDust is not in this BoxSet)', diamond not in {c['path'] for c in mc.candidates(app, jf_item)})
fade = str(movies / 'Bleach - Fade to Black (2008)')
(Path(fade) / 'theme.mp3').write_bytes(mp3('fade'))
h = H('/collections/install', {'server': 'jellyfin', 'id': 'abc123', 'source': fade}); mc.post(app, h, redirect)
rec = mc.load(app)['installed']['jellyfin']['abc123']
backup = Path(app.CONFIG) / mc.BACKUPS / rec['backup']
check('Replacing keeps the previous collection theme in /config/collection_theme_backups', dest.read_bytes() == mp3('fade') and backup.read_bytes() == mp3('memories'))
h = H('/collections/remove', {'server': 'jellyfin', 'id': 'abc123', 'confirm': '1'}); mc.post(app, h, redirect)
check('Remove (Jellyfin) puts the previous theme back', dest.read_bytes() == mp3('memories'), h.loc)

# ---- Emby (same API under /emby)
refresh('emby')
r = poster('jellyfin', 'abc123')
check('Jellyfin poster: the BoxSet\'s Primary image (PNG)', r.code == 200 and r.hdr['Content-Type'] == 'image/png')
check('Emby: collections read through the /emby API', not mc.load(app)['servers']['emby']['error'] and mc.load(app)['servers']['emby']['items'][0]['id'] == 'abc123')

# ---- wiring and safety
src = Path('/src/preludarr/app.py').read_text()
check('Routes wired: /collections page and its four forms', "movie_collections.page(sys.modules[__name__],self)" in src and "'/collections/install'" in src)
ms._write(app.CONFIG, 'servers', {})
check('No media server connected: the page explains where to connect one', 'Settings → Connect' in page())
check('The module name cannot shadow Python\'s own "collections"', not Path('/src/preludarr/collections.py').exists())

srv.terminate()
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
