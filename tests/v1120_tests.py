# SPDX-License-Identifier: GPL-3.0-or-later
"""Preludarr 1.12 pre-release tasks: licence files, update-available notice. Run in a Preludarr image, --network none."""
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import updates as up  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


# ------------------------------------------------------------------ 1-2: version, changelog, licence ----
src = Path('/src')
check('Version scheme: 1.12.x-dev, or a release 1.12.x[-alpha|beta|rc.N]', __import__('re').fullmatch(r'1\.12\.(0|[1-9][0-9]*)(-dev|-(alpha|beta|rc)\.[1-9][0-9]*)?', __import__('login').APP_VERSION), __import__('login').APP_VERSION)
cl = (src / 'CHANGELOG.md').read_text()
check('CHANGELOG explains the version rules and lists releases', 'Semantic Versioning' in cl and '## [1.11.9]' in cl and '1.12.0-beta.1' in cl)
lic = (src / 'LICENSE').read_text()
check('LICENSE is the full GPL-3.0 text', lic.lstrip().startswith('GNU GENERAL PUBLIC LICENSE') and 'Version 3, 29 June 2007' in lic and len(lic) > 30000)
notice = (src / 'NOTICE.md').read_text()
check('NOTICE: or-later grant, bundled components, source offer, provider + logo notes',
      all(x in notice for x in ('either **version 3 of the License, or (at your option) any later version**', 'FFmpeg', 'yt-dlp', 'Deno',
                                 'aports', 'not endorsed or certified by TMDB', 'AI image generator', 'does not mean you are allowed to download')))
missing = [p.name for p in list((src / 'preludarr').glob('*.py')) + list((src / 'tests').glob('*.py')) + list((src / 'tools').glob('*.py')) if 'SPDX-License-Identifier: GPL-3.0-or-later' not in p.read_text()[:300]]
check('Every source file carries the SPDX licence tag', not missing, missing)
check('app.py keeps its #! line first', (src / 'preludarr' / 'app.py').read_text().startswith('#!/usr/bin/env python3\n# SPDX-License-Identifier'))
check('THIRD_PARTY_PACKAGES lists Alpine + Python packages with licences', 'ffmpeg-' in (src / 'docker' / 'THIRD_PARTY_PACKAGES.txt').read_text()
      and 'yt-dlp' in (src / 'docker' / 'THIRD_PARTY_PACKAGES.txt').read_text())

# ------------------------------------------------------------------ 3: update-available notice ----
check('Version order: release > beta.2 > beta.1 > dev; 1.12.1 > 1.12.0', up.newer('1.12.0', '1.12.0-beta.2') and up.newer('1.12.0-beta.2', '1.12.0-beta.1')
      and up.newer('1.12.0-beta.1', '1.12.0-dev') and up.newer('1.12.1', '1.12.0') and not up.newer('1.11.9', '1.12.0-dev') and up.newer('v1.13.0', '1.12.0'))
check('Junk version strings are ignored', up.parse('latest') is None and not up.newer('nightly', '1.12.0'))
rels = [{'tag_name': 'v1.12.0-beta.3', 'prerelease': True, 'html_url': 'https://github.com/shedman214/preludarr/releases/tag/v1.12.0-beta.3'},
        {'tag_name': 'v1.12.0', 'prerelease': False, 'html_url': 'https://github.com/shedman214/preludarr/releases/tag/v1.12.0'},
        {'tag_name': 'v1.13.0-beta.1', 'prerelease': True, 'draft': True},
        {'tag_name': 'v1.12.1-beta.1', 'prerelease': True, 'html_url': 'https://evil.example/x'}]
check('Stable users are offered stable releases only', up.pick(rels, '1.11.9')['version'] == '1.12.0')
p = up.pick(rels, '1.12.0-beta.1')
check('Beta users are offered newer betas too (drafts never)', p['version'] == '1.12.1-beta.1' and p['url'] == 'https://github.com/shedman214/preludarr/releases')
check('Nothing offered when already newest', up.pick(rels, '1.12.0') is None)
calls = []
up._fetch = lambda repo: calls.append(repo) or rels
s = up.check('1.11.9')
check('Check stores the result privately; sidebar shows the notice', s['latest']['version'] == '1.12.0' and oct((work / 'config' / up.FILE).stat().st_mode & 0o777) == '0o600'
      and 'Update available: 1.12.0' in up.footer_html('1.11.9') and calls == ['shedman214/preludarr'])
check('No notice once updated', up.footer_html('1.12.0') == '')


def gone(repo):
    raise urllib.error.HTTPError('u', 404, 'nf', {}, None)


up._fetch = gone
s = up.check('1.12.0-dev')
check('Before the repository exists: "No releases published yet", no notice', s['error'] == 'No releases published yet.' and up.footer_html('1.12.0-dev') == '')


class H:
    def __init__(self, path, body):
        b = body.encode(); self.headers = {'Content-Length': str(len(b))}; self.path = path; self.rfile = io.BytesIO(b)


locs = []
up.post(H('/system/updates/settings', 'enabled=0'), lambda h, l: locs.append(urllib.parse.unquote_plus(l)), '1.11.9')
up._fetch = lambda repo: calls.append('should-not-run') or rels
up.post(H('/system/updates/check', ''), lambda h, l: locs.append(urllib.parse.unquote_plus(l)), '1.11.9')
check('Turning checks off stops checks and hides the notice', not up.enabled() and 'turned off' in locs[0] and 'should-not-run' not in calls and up.footer_html('1.11.9') == '', locs)
os.environ['PRELUDARR_UPDATE_CHECK'] = '0'
up.post(H('/system/updates/settings', 'enabled=1'), lambda h, l: None, '1.11.9')
check('PRELUDARR_UPDATE_CHECK=0 forces it off', not up.enabled() and 'PRELUDARR_UPDATE_CHECK=0' in up.panel_html('1.11.9'))
del os.environ['PRELUDARR_UPDATE_CHECK']
os.environ['SPLICARR_UPDATE_CHECK'] = '0'
check('The older SPLICARR_UPDATE_CHECK=0 is still honoured', not up.enabled())
del os.environ['SPLICARR_UPDATE_CHECK']
page = __import__('system')._body('releases', app.CONFIG, {})
check('System → Releases shows the Updates panel', 'id="updates"' in page and 'Check now' in page)

# ------------------------------------------------------------------ 5: addresses + first-run account setup ----
import contextlib  # noqa: E402
import login as tl  # noqa: E402
check('Any private / loopback IP address works on any port; names need allowing',
      all(tl.is_lan_host(h) for h in ('192.168.5.9:1234', '10.0.0.2:8788', '172.20.1.1', '127.0.0.1:9000', '[::1]:8788', 'localhost:7000', '[fe80::1]:8788'))
      and not any(tl.is_lan_host(h) for h in ('8.8.8.8:8788', 'evil.example:8788', '192.168.1.5.evil.com:8788', '[2001:4860::1]:8788', '')))
check('No LAN address is hard-coded in the sign-in code', not __import__('re').search(r'192\.168\.\d+\.\d+', (src / 'preludarr' / 'login.py').read_text()))


class AH:
    def __init__(self, path, body='', host='192.168.5.9:8788'):
        b = body.encode()
        self.headers = {'Host': host, 'Origin': 'http://' + host, 'Content-Length': str(len(b)), 'Content-Type': 'application/x-www-form-urlencoded'}
        self.path = path; self.rfile = io.BytesIO(b); self.client_address = ('192.168.5.20', 5000)
        self.sent = None; self.status = None; self.hdrs = {}

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass


os.environ.pop('THEMEARR_WRITE_AUTH', None)
auth = tl.Auth()
check('Fresh install (no account, no THEMEARR_WRITE_AUTH) needs first-run setup', auth.needs_setup())
log = io.StringIO()
with contextlib.redirect_stdout(log):
    code = auth.ensure_setup_code()
check('The setup code is printed to the container log', code in log.getvalue() and 'first-run setup' in log.getvalue() and re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', code))
h = AH('/'); auth.get(h)
check('Every page leads to "Create your account"', h.status == 303 and h.hdrs.get('Location') == '/setup')
h = AH('/api/v1/status'); auth.get(h)
check('The API answers 503 until setup is finished', h.sent and h.sent[0] == 503)
h = AH('/setup'); auth.get(h)
check('Setup page asks for the code from the log, username and a 16+ character password', h.sent[0] == 200 and 'setup code' in h.sent[1] and 'name="code"' in h.sent[1]
      and 'minlength="16"' in h.sent[1])
h = AH('/setup', body=urllib.parse.urlencode({'code': 'AAAA-BBBB', 'username': 'me', 'password': 'x' * 20, 'confirm': 'x' * 20})); auth.post(h)
check('A wrong code is refused (nobody else can claim the server)', h.sent[0] == 403 and not auth.account_file.exists())
h = AH('/setup', body=urllib.parse.urlencode({'code': code, 'username': 'theowner', 'password': 'short', 'confirm': 'short'})); auth.post(h)
check('A short password is refused', h.sent[0] == 400 and '16–128' in h.sent[1])
h = AH('/setup', body=urllib.parse.urlencode({'code': code.lower(), 'username': 'theowner', 'password': 'correct horse battery staple', 'confirm': 'correct horse battery staple'}))
auth.post(h)
check('Right code + valid account → created and signed in', h.status == 303 and h.hdrs.get('Location') == '/' and 'themearr_session=' in h.hdrs.get('Set-Cookie', '') and not auth.needs_setup())
st = auth.account_file.stat(); dst = auth.account_file.parent.stat()
check('Account saved privately (file 0600, folder 0700), never the plain password', oct(st.st_mode & 0o777) == '0o600' and oct(dst.st_mode & 0o777) == '0o700'
      and 'correct horse' not in auth.account_file.read_text())
check('The new account signs in', auth.verify('correct horse battery staple', 'theowner') and not auth.verify('correct horse battery staple', 'admin'))
h = AH('/setup'); auth.get(h)
check('After setup, /setup only leads to sign-in (cannot be reused)', h.status == 303 and h.hdrs.get('Location') == '/login')
h = AH('/setup', body=urllib.parse.urlencode({'code': code, 'username': 'attacker', 'password': 'y' * 20, 'confirm': 'y' * 20})); auth.post(h)
check('…and posting it again changes nothing', auth.verify('correct horse battery staple', 'theowner') and not auth.verify('y' * 20, 'attacker'))
auth2 = tl.Auth(); auth2.account_file.unlink(); auth2.migrated_marker.unlink()
for _ in range(5):
    h = AH('/setup', body=urllib.parse.urlencode({'code': 'ZZZZ-ZZZZ', 'username': 'x1x', 'password': 'p' * 20, 'confirm': 'p' * 20})); auth2.post(h)
h = AH('/setup', body=urllib.parse.urlencode({'code': 'ZZZZ-ZZZZ', 'username': 'x1x', 'password': 'p' * 20, 'confirm': 'p' * 20})); auth2.post(h)
check('Guessing the code is throttled (5 tries per 5 minutes)', h.sent[0] == 429)
os.environ['THEMEARR_WRITE_AUTH'] = 'aa' * 16 + ':' + 'bb' * 32
check('Existing installs (THEMEARR_WRITE_AUTH set) never see setup', not tl.Auth().needs_setup())
del os.environ['THEMEARR_WRITE_AUTH']

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
