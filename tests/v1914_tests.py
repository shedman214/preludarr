# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.14 — status clarity, honest labels, API key hardening, session lifetime, provider limits, backup password step,
poster ID matching, per-source rate limits. Run in a Themearr image, --network none. Uses only local fixtures."""
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime' / 'Grimgar, Ashes and Illusions (2016)').mkdir(parents=True); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import general_settings as g  # noqa: E402
import login as tl  # noqa: E402
import ratelimit as rl  # noqa: E402
import system as sysv  # noqa: E402
sys.modules['app'] = app
app.discover()
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


class Headers(dict):
    def get_all(self, k, d=None):
        return [self[k]] if k in self else d


class H:
    def __init__(self, path='/', body='', host='192.168.1.20:8788', peer='10.0.0.9', **hdr):
        b = body.encode()
        self.headers = Headers({'Host': host, 'Content-Length': str(len(b)), 'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'http://' + host})
        self.headers.update({k.replace('_', '-'): v for k, v in hdr.items()})
        self.path = path; self.rfile = io.BytesIO(b); self.client_address = (peer, 5555)
        self.sent = None; self.code = None; self.hdrs = []; self.wfile = io.BytesIO()

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body, ctype)

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        self.hdrs.append((k, v))

    def end_headers(self):
        pass


# ------------------------------------------------------------------ A1 status line ----
grim = str(work / 'anime' / 'Grimgar, Ashes and Illusions (2016)')
check('Idle before anything ran', app.status_display() == 'Idle' and 'No library scan' in app.scan_line())
app.save(grim, 'Anime TV', 'Grimgar, Ashes and Illusions (2016)', 'review', 'No exact alternative-title match on AnimeThemes')
app.last_op(grim, 'Grimgar, Ashes and Illusions (2016)', 'review', 'No exact alternative-title match on AnimeThemes')
t = app.status_display()
check('Last outcome names the title, the result, the reason and a time with a zone', t.startswith('Last title: Grimgar, Ashes and Illusions (2016) — Needs review: No exact')
      and re.search(r'(AM|PM) [A-Z]{2,5}', t) and '(now' not in t, t)
app.save(grim, 'Anime TV', 'Grimgar, Ashes and Illusions (2016)', 'installed', 'Manual YouTube theme installation')
check('…and says when the title changed since (no contradiction)', app.status_display().endswith('(now Theme installed)'), app.status_display())
app.STATUS.update(running=True, message='Matching: X')
check('While running: what runs now', app.status_display() == 'Matching: X (in progress…)')
app.STATUS.update(running=False, last_scan=app.now(), scan_summary='Discovered 1 titles')
check('Readable last scan line with zone + summary', re.match(r'Last library scan: \w{3} \d+, \d{4} · \d+:\d\d (AM|PM) [A-Z]{2,5} · Discovered 1 titles', app.scan_line()), app.scan_line())
src = Path('/src/preludarr/app.py').read_text()
check('Dashboard + /api/status use the new text (old fields kept)', "esc(status_display())" in src and "'display':status_display()" in src and "'message':STATUS['message']" in src)

# ------------------------------------------------------------------ A2 timestamps ----
utc = app.activity_time('2026-10-28T15:50:45+00:00')
local = app.activity_time('2026-10-28 10:51:46')
check('UTC ISO shown in local time with zone', re.search(r'(AM|PM) [A-Z]{2,5}$', utc), utc)
check('Old local "YYYY-MM-DD HH:MM:SS" rows are read as local (not shifted 5 h)', '10:51 AM' in local, local)
writers = ''.join(Path('/src/preludarr/' + f).read_text() for f in ('app.py', 'theme_management.py', 'theme_queue.py', 'custom_upload.py', 'youtube_manual.py'))
check('No writer stores local naive times any more', "strftime('%Y-%m-%d %H:%M:%S')" not in writers and 'strftime("%Y-%m-%d %H:%M:%S")' not in writers)

# ------------------------------------------------------------------ A4/A5 honest labels, nav ----
import media_servers as v4  # noqa: E402
ov = v4.connect_overview(app.CONFIG)
check('Status table: Last check / Status columns, no "Ready to use"', '<th>Last check</th><th>Status</th>' in ov and 'Ready to use' not in ov)
check('Status table explains Configured ≠ working', 'it is not proof that the service works right now' in ov)
side = tl.full_sidebar('themes')
check('Sidebar tells the four theme pages apart', all(x in side for x in ('Find and preview themes', 'Titles that have no theme yet', 'Themes already installed', 'waiting for approval')))

# ------------------------------------------------------------------ B1 docker log redaction ----
import app_logs as lg  # noqa: E402
import logging  # noqa: E402
buf = io.StringIO(); lgr = logging.getLogger('t1914'); lgr.propagate = False
tee = lg._Tee(buf, lgr, logging.ERROR)
tee.write('1.2.3.4 - - [x] "GET /api/v1/status?apikey=0123456789abcdef0123456789abcdef HTTP/1.1" 401 -\n')
check('docker logs stream is redacted too', '0123456789abcdef' not in buf.getvalue() and '[REDACTED]' in buf.getvalue(), buf.getvalue())

# ------------------------------------------------------------------ B2-B4 API key gate ----
key = g.api_key()
check('No key → normal sign-in rules (None)', g.api_auth(H('/api/v1/status'), '/api/v1/status', 'GET') is None)
check('Not an API route → None', g.api_auth(H('/settings', X_Api_Key=key), '/settings', 'GET') is None)
check('Header key → allowed', g.api_auth(H('/api/v1/status', X_Api_Key=key), '/api/v1/status', 'GET') is True)
h = H('/api/v1/status?apikey=' + key)
r = g.api_auth(h, '/api/v1/status', 'GET')
check('?apikey= refused by default with a clear JSON reason', r is False and h.sent[0] == 401 and 'X-Api-Key header' in h.sent[1] and 'application/json' in h.sent[2], h.sent)
g._save(dict(g.settings(), api_query_keys=True)) if hasattr(g, '_save') else None
check('…allowed again when the user turns the old way on', g.api_auth(H('/api/v1/status?apikey=' + key), '/api/v1/status', 'GET') is True)
h = H('/api/v1/command/scan?apikey=' + key)
check('…but never for the scan command (POST)', g.api_auth(h, '/api/v1/command/scan', 'POST') is False and h.sent[0] == 401)
h = H('/api/v1/status', X_Api_Key='f' * 32)
check('Wrong key → 401 "Wrong API key."', g.api_auth(h, '/api/v1/status', 'GET') is False and 'Wrong API key' in h.sent[1])
h = H('/api/v1/command/scan', X_Api_Key=key)
check('1.12: scan refused with the default read-only scope', g.api_auth(h, '/api/v1/command/scan', 'POST') is False and h.sent[0] == 403)
g._save(dict(g.settings(), api_scan=True))
check('Scan allowed with header key once scans are allowed', g.api_auth(H('/api/v1/command/scan', X_Api_Key=key), '/api/v1/command/scan', 'POST') is True)
h = H('/api/v1/command/scan', X_Api_Key=key)
check('Second API scan within 5 min → 429 cooldown', g.api_auth(h, '/api/v1/command/scan', 'POST') is False and h.sent[0] == 429)
g._API_STATE['scan_at'] = 0
g._save(dict(g.settings(), api_scan=False))
h = H('/api/v1/command/scan', X_Api_Key=key)
check('Read-only scope → 403 explains how to allow scans', g.api_auth(h, '/api/v1/command/scan', 'POST') is False and h.sent[0] == 403 and 'read-only' in h.sent[1])
g._API_HITS.clear()
codes = [g.api_auth(H('/api/v1/status', X_Api_Key=key, peer='10.9.9.9'), '/api/v1/status', 'GET') for _ in range(61)]
h = H('/api/v1/status', X_Api_Key=key, peer='10.9.9.9'); g.api_auth(h, '/api/v1/status', 'GET')
check('Rate limit: 60 per minute per client, then 429', all(c is True for c in codes[:60]) and h.sent and h.sent[0] == 429, (codes[58:], h.sent))
check('Rate limit is per client (others unaffected)', g.api_auth(H('/api/v1/status', X_Api_Key=key, peer='10.9.9.8'), '/api/v1/status', 'GET') is True)

# ------------------------------------------------------------------ B5 tunnel warning ----
os.environ['THEMEARR_PUBLIC_URL'] = 'https://themearr.example.org'
os.environ['THEMEARR_WRITE_AUTH'] = 'aa' * 16 + ':' + 'bb' * 32
page = g.panel_html(H('/settings', peer='172.17.0.1')) if hasattr(g, 'panel_html') else tl.Auth().settings(H('/settings?section=security', peer='172.17.0.1'))
check('Security page warns: public address + no trusted proxy = shared sign-in limit', 'Needs attention' in page and 'shares one sign-in limit' in page and '172.17.0.1' in page)
g._save(dict(g.settings(), trusted_proxies=['172.17.0.1/32']))
page = tl.Auth().settings(H('/settings?section=security', peer='172.17.0.1'))
check('…warning gone once a trusted proxy is set', 'shares one sign-in limit' not in page)
check('Security page shows the key scope (read-only now) and both switches', 'read-only' in page and 'name="api_scan"' in page and 'name="api_query_keys"' in page)

# ------------------------------------------------------------------ B6 session lifetime ----
auth = tl.Auth()
raw = auth.issue(); k = __import__('hashlib').sha256(raw.encode()).hexdigest()
hh = H('/', Cookie='themearr_session=' + raw)
check('Fresh session active', auth.active(hh))
auth.born[k] -= tl.SESSION_MAX + 1
check('Session older than 7 days ends even if it stayed active', not auth.active(H('/', Cookie='themearr_session=' + raw)) and k not in auth.sessions)

# ------------------------------------------------------------------ B7 provider answer limits ----
class P(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == '/big':
            body = b'[' + b'0,' * (4 * 1024 * 1024) + b'0]'
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        elif self.path == '/redir':
            self.send_response(302); self.send_header('Location', 'http://127.0.0.1:1/x'); self.end_headers()
        else:
            body = b'{"ok": 1}'
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


srv = ThreadingHTTPServer(('127.0.0.1', 0), P); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:%d' % srv.server_address[1]
check('Normal provider answer still works', app.request_json(base + '/ok') == {'ok': 1})
try:
    app.request_json(base + '/big'); ok = False
except ValueError as e:
    ok = '8 MB' in str(e)
check('Answer over 8 MB refused', ok)
try:
    app.request_json(base + '/redir'); ok = False
except Exception as e:
    ok = 'non-HTTPS' in str(e)
check('Redirect to plain http refused', ok)

# ------------------------------------------------------------------ B8 backup download password step ----
name = sysv.create_backup(app.CONFIG)
h = H('/system/backup/download?name=' + name)
pg = sysv.page(app.CONFIG, {'tab': ['backup'], '_download': [name]})
check('Download link opens a password step (no file yet)', 'Enter your password to download it' in pg and 'name="password"' in pg and 'name="csrf"' in pg)


class App:
    def __init__(self, ok): self.auth = type('A', (), {'verify': lambda s, p: p == ok})()


sys.modules['app'] = App('right-password')
def post(fields):
    body = urllib.parse.urlencode(fields); h = H('/system/backup/download', body); sysv.post_route(h, app.CONFIG); return h
h = post({'csrf': 'nope', 'name': name, 'password': 'right-password'})
check('Expired form → back to the step, no file', h.code == 303 and 'expired' in urllib.parse.unquote(dict(h.hdrs)['Location']) and not h.wfile.getvalue())
h = post({'csrf': sysv._new_token(), 'name': name, 'password': 'wrong'})
check('Wrong password → no file, clear message', h.code == 303 and 'Wrong password' in urllib.parse.unquote_plus(dict(h.hdrs)['Location']) and not h.wfile.getvalue())
h = post({'csrf': sysv._new_token(), 'name': name, 'password': 'right-password'})
check('Right password → the ZIP is sent (no-store)', h.code == 200 and h.wfile.getvalue()[:2] == b'PK' and ('Cache-Control', 'no-store') in h.hdrs)
for _ in range(5):
    post({'csrf': sysv._new_token(), 'name': name, 'password': 'wrong'})
h = post({'csrf': sysv._new_token(), 'name': name, 'password': 'right-password'})
check('5 wrong passwords → paused even for the right one', h.code == 303 and 'Too many' in urllib.parse.unquote_plus(dict(h.hdrs)['Location']))
sys.modules['app'] = app

# ------------------------------------------------------------------ C poster ID matching ----
with app.db() as c:
    c.execute('INSERT OR REPLACE INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES(?,?,?,?,?)', (grim, 303868, 65369, 'tt5341430', 'sonarr'))
cands = [{'provider': 'TMDB', 'id': 99999, 'title': 'Grimgar Other', 'year': '2016', 'poster': 'https://image.tmdb.org/t/p/w342/a.jpg'},
         {'provider': 'TMDB', 'id': 65369, 'title': 'Grimgar of Fantasy and Ash', 'original': '灰と幻想のグリムガル', 'year': '2016', 'poster': 'https://image.tmdb.org/t/p/w342/b.jpg'}]
hit = app.poster_id_match(grim, cands)
check('Candidate with the same TMDB ID as Sonarr is chosen, with the reason', hit and hit['id'] == 65369 and hit['why'] == 'same TMDB ID as Sonarr (65369)', hit)
check('No IDs known → no automatic choice', app.poster_id_match(str(work / 'anime' / 'nope'), cands) is None)
check('Candidate without artwork is never chosen', app.poster_id_match(grim, [dict(cands[1], poster='')]) is None)
app.record_poster_attempt(grim, 'needs_choice', 'test', cands)
pay = __import__('poster_manual').candidates_payload(app, grim)
check('Select-a-poster popup lists the ID match first and says why', pay['candidates'][0]['index'] == 1 and pay['candidates'][0]['why'].startswith('Same TMDB ID as Sonarr'), pay['candidates'][0])

# ------------------------------------------------------------------ D rate limits ----
check('Defaults are conservative', rl.settings()['animethemes'] == 60 and rl.settings()['youtube_downloads'] == 15)
saved = rl.save({'animethemes': 500, 'anilist': 0, 'tmdb': 'x'})
check('Saved values are clamped to the safe range (500→80, 0→5, junk→default)', (saved['animethemes'], saved['anilist'], saved['tmdb']) == (80, 5, 40), saved)
check('Settings file private (0600)', oct((Path(app.CONFIG) / rl.FILE).stat().st_mode & 0o777) == '0o600')
rl.save({'tvdb': 60})
t0 = time.monotonic(); rl.wait('tvdb'); rl.wait('tvdb'); rl.wait('tvdb')
check('wait() spaces requests evenly (60/min → ~1 s apart)', 1.8 <= time.monotonic() - t0 <= 3.5, time.monotonic() - t0)
rl.save({'youtube_downloads': 2}); rl._HOURLY.clear()
rl.take_hourly('youtube_downloads'); rl.take_hourly('youtube_downloads')
try:
    rl.take_hourly('youtube_downloads'); ok = False
except ValueError as e:
    ok = 'limit of 2 per hour' in str(e) and 'Rate limits' in str(e)
check('Hourly download cap refuses with a plain message', ok)
rl.backoff('tmdb', 2); t0 = time.monotonic(); rl.wait('tmdb')
check('429 back-off pauses that source', time.monotonic() - t0 >= 1.5)
check('Hosts map to the right source', (rl.for_host('graphql.anilist.co'), rl.for_host('tvthemes.plexapp.com'), rl.for_host('app.lizardbyte.dev'), rl.for_host('example.com')) == ('anilist', 'plex_themes', 'themerrdb', None))
import download_clients as dc  # noqa: E402
seen = {}
dc.limits_post(H('/download-clients/limits', urllib.parse.urlencode({'animethemes': '200', 'youtube': '3'})), lambda h, url: seen.setdefault('url', url))
q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen['url']).query)
check('Saving from the page: clamped and says so', 'Adjusted to the safe range: AnimeThemes API' in q['notice'][0] and rl.settings()['animethemes'] == 80 and rl.settings()['youtube'] == 3, q)
seen.clear(); dc.limits_post(H('/download-clients/limits', 'reset=1'), lambda h, url: seen.setdefault('url', url))
check('"Use the defaults" resets everything', rl.settings() == {k: v[2] for k, v in rl.SOURCES.items()})
check('Rate limits tab rendered', 'id="dc-sec-limits"' in rl.panel_html() and 'safe maximum' in rl.panel_html())

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
