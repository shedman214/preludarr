# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.6 — Remote access (public HTTPS address for a reverse proxy / Cloudflare tunnel). Run in a Themearr image, --network none."""
import io
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
os.environ.pop('THEMEARR_PUBLIC_URL', None)
sys.path.insert(0, '/src/preludarr')
import login as tl  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


class Headers(dict):
    def get_all(self, k, d=None):
        return [self[k]] if k in self else d


class H:
    def __init__(self, path='/', host='192.168.1.20:8788', origin=None, referer=None, body='', cookie=None):
        hd = Headers({'Host': host, 'Content-Length': str(len(body.encode())), 'Content-Type': 'application/x-www-form-urlencoded'})
        if origin is not None:
            hd['Origin'] = origin
        if referer is not None:
            hd['Referer'] = referer
        if cookie:
            hd['Cookie'] = cookie
        self.headers = hd; self.path = path; self.rfile = io.BytesIO(body.encode()); self.client_address = ('10.0.0.9', 5555)
        self.sent = None; self.code = None; self.hdrs = []

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.hdrs.append((k, v))

    def end_headers(self):
        pass


# ------------------------------------------------------------ parse ----
good = {'https://themes.example.net': 'themes.example.net', 'https://Themes.Example.net/': 'themes.example.net',
        'https://a.example.com:8443': 'a.example.com:8443', 'https://a.example.com:443': 'a.example.com'}
for raw, want in good.items():
    check('accepts ' + raw, tl.parse_public_url(raw) == want, tl.parse_public_url(raw))
for raw in ('http://themes.example.net', 'https://themes.example.net/login', 'https://localhost', 'themes.example.net',
            'https://user:pw@x.example.com', 'https://x.example.com:99999', 'https://*.example.com', 'javascript:alert(1)'):
    try:
        tl.parse_public_url(raw); ok = False
    except ValueError:
        ok = True
    check('refuses ' + raw, ok)

# ------------------------------------------------------------ before: today's behaviour ----
pub = 'themes.example.net'
check('Without a public address the tunnel host is rejected (old behaviour)', not tl.origin_ok(H(host=pub, origin='https://' + pub)))
check('LAN still works (http origin)', tl.origin_ok(H(origin='http://192.168.1.20:8788')))

# ------------------------------------------------------------ save via Settings ----
(work / 'config').mkdir(exist_ok=True)
seen = {}
tl.remote_post(H('/settings/remote-access', body='url=' + urllib.parse.quote('https://Themes.example.net/')), lambda h, url: seen.setdefault('url', url))
msg = urllib.parse.parse_qs(urllib.parse.urlsplit(seen['url']).query)['remote_notice'][0]
check('Saving the address works and says so', 'https://themes.example.net is accepted now' in msg, msg)
check('Saved privately (0600)', oct((work / 'config' / tl.PUBLIC_FILE).stat().st_mode & 0o777) == '0o600')
seen.clear()
tl.remote_post(H('/settings/remote-access', body='url=http%3A%2F%2Fevil.example.com'), lambda h, url: seen.setdefault('url', url))
msg = urllib.parse.parse_qs(urllib.parse.urlsplit(seen['url']).query)['remote_notice'][0]
check('A plain-http address is refused with a reason', msg.startswith('✗') and 'HTTPS' in msg, msg)
check('…and the saved address is unchanged', tl.public_hosts() == {pub})

# ------------------------------------------------------------ after ----
check('Tunnel host + https origin accepted', tl.origin_ok(H(host=pub, origin='https://' + pub)))
check('Tunnel host + https referer accepted', tl.origin_ok(H(host=pub, referer='https://' + pub + '/settings')))
check('Tunnel host + http origin refused (scheme must be https)', not tl.origin_ok(H(host=pub, origin='http://' + pub)))
check('Tunnel host + other site origin refused (CSRF)', not tl.origin_ok(H(host=pub, origin='https://evil.example.com')))
check('Tunnel host without origin/referer refused', not tl.origin_ok(H(host=pub)))
check('Unknown host still refused', not tl.origin_ok(H(host='evil.example.com', origin='https://evil.example.com')))
check('LAN host with https origin refused (LAN is http)', not tl.origin_ok(H(origin='https://192.168.1.20:8788')))
check('LAN still works after saving', tl.origin_ok(H(origin='http://192.168.1.20:8788')))

# env var also works
os.environ['THEMEARR_PUBLIC_URL'] = 'https://other.example.org'
check('THEMEARR_PUBLIC_URL also accepted', tl.origin_ok(H(host='other.example.org', origin='https://other.example.org')) and tl.origin_ok(H(host=pub, origin='https://' + pub)))
os.environ.pop('THEMEARR_PUBLIC_URL')

# ------------------------------------------------------------ sign-in through the tunnel ----
auth = tl.Auth()
pw = 'correct-horse-battery-staple-123'
import hashlib  # noqa: E402
import secrets  # noqa: E402
salt = secrets.token_bytes(16)
os.environ['THEMEARR_WRITE_AUTH'] = salt.hex() + ':' + hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 240000).hex()
auth.password_file = work / 'none'; auth.account_file = work / 'none.json'
h = H('/login', host=pub, origin='https://' + pub, body=urllib.parse.urlencode({'username': 'admin', 'password': pw}))
auth.login(h)
cookies = [v for k, v in h.hdrs if k == 'Set-Cookie']
check('Sign-in through the tunnel works and the cookie is Secure', h.code == 303 and cookies and '; Secure' in cookies[0] and 'HttpOnly' in cookies[0], (h.code, cookies, h.sent))
token = cookies[0].split(';')[0]
check('Session works on the tunnel host', auth.active(H('/', host=pub, cookie=token)))
h = H('/login', origin='http://192.168.1.20:8788', body=urllib.parse.urlencode({'username': 'admin', 'password': pw}))
auth.login(h)
lan_cookie = [v for k, v in h.hdrs if k == 'Set-Cookie'][0]
check('LAN sign-in cookie is not marked Secure (plain http would drop it)', '; Secure' not in lan_cookie, lan_cookie)
check('POST from another site through the tunnel is blocked', not auth.same_origin(H('/scan', host=pub, origin='https://evil.example.com')))

# settings page shows the panel
page = auth.settings(H('/settings?section=remote', host=pub))
check('Settings → General shows Remote access with the saved address', 'id="settings-remote-access"' in page and 'value="https://themes.example.net"' in page
      and '#settings-remote-access{display:block!important}' in page and 'Cloudflare Access' in page)

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
