# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.7 — Settings → General: Host, API key, trusted proxies, Logging, site emblem. Run in a Themearr image, --network none."""
import io
import json
import logging
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir(); (work / 'config').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import general_settings as g  # noqa: E402
import login as tl  # noqa: E402
import app_logs as lg  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


class Headers(dict):
    def get_all(self, k, d=None):
        return [self[k]] if k in self else d


class H:
    def __init__(self, path='/', host='192.168.1.20:8788', body='', origin='http://192.168.1.20:8788', peer='10.0.0.9', **hdr):
        hd = Headers({'Host': host, 'Content-Length': str(len(body.encode())), 'Content-Type': 'application/x-www-form-urlencoded'})
        if origin:
            hd['Origin'] = origin
        hd.update({k.replace('_', '-'): v for k, v in hdr.items()})
        self.headers = hd; self.path = path; self.rfile = io.BytesIO(body.encode()); self.client_address = (peer, 5555)
        self.sent = None; self.code = None; self.hdrs = []; self.wfile = io.BytesIO()

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body, ctype)

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.hdrs.append((k, v))

    def end_headers(self):
        pass


def post(fields):
    seen = {}
    g.post(H('/settings/general', body=urllib.parse.urlencode(fields)), lambda h, url: seen.setdefault('url', url))
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen['url']).query)
    return next(v[0] for k, v in q.items() if k.endswith('_notice')), seen['url']


# ------------------------------------------------------------------ defaults ----
check('Defaults', g.settings() == g.DEFAULTS, g.settings())

# ------------------------------------------------------------------ host ----
msg, url = post({'section': 'host', 'instance_name': 'Theme Forge', 'allowed_hosts': 'themearr.lan, 192.168.1.20:9000'})
check('Host saved + returns to the Host tab', msg.startswith('Host saved') and 'section=host' in url, msg)
check('Settings file private (0600)', oct((work / 'config' / g.FILE).stat().st_mode & 0o777) == '0o600')
check('Allowed host accepted over http', tl.origin_ok(H(host='themearr.lan', origin='http://themearr.lan')) and tl.origin_ok(H(host='192.168.1.20:9000', origin='http://192.168.1.20:9000')))
check('…but not over https (https needs Application URL)', not tl.origin_ok(H(host='themearr.lan', origin='https://themearr.lan')))
for bad in ('evil..com', 'http://x.com', 'a.com:99999', 'a_b'):
    msg, _ = post({'section': 'host', 'instance_name': 'X', 'allowed_hosts': bad})
    check('Allowed hosts refuses ' + bad, msg.startswith('✗'), msg)
msg, _ = post({'section': 'host', 'instance_name': '<script>', 'allowed_hosts': ''})
check('Instance name refuses markup', msg.startswith('✗'))
check('Bad saves left the good settings alone', g.settings()['instance_name'] == 'Theme Forge' and 'themearr.lan' in g.settings()['allowed_hosts'])
page = tl.add_icons('<!doctype html><html><head><title>Settings - Preludarr</title></head><body>x</body></html>')
check('Instance name in the browser tab', '<title>Settings - Theme Forge</title>' in page, page[:200])

# ------------------------------------------------------------------ emblem ----
check('Icons added once, inside <head>', page.count('rel="icon"') == 3 and page.index('rel="icon"') < page.index('</head>') and 'apple-touch-icon' in page and 'rel="manifest"' in page)
check('add_icons is idempotent', tl.add_icons(page) == page)
check('Non-HTML text untouched', tl.add_icons('plain text') == 'plain text')
os.environ['THEMEARR_WRITE_AUTH'] = 'aa' * 16 + ':' + 'bb' * 32  # an existing install (1.12: no account → first-run setup instead)
auth = tl.Auth()
h = H('/favicon.ico', origin=None); auth.get(h)
check('/favicon.ico served without sign-in (PNG emblem)', h.code == 200 and ('Content-Type', 'image/png') in h.hdrs and h.wfile.getvalue()[:4] == b'\x89PNG', (h.code, h.hdrs[:2]))
h = H('/manifest.webmanifest', origin=None); auth.get(h)
man = json.loads(h.wfile.getvalue())
check('Web manifest: instance name + 192/512 icons', man['name'] == 'Theme Forge' and {i['sizes'] for i in man['icons']} == {'192x192', '512x512'})
h = H('/login', origin=None); auth.get(h)
check('Login page carries the emblem', h.sent and 'rel="icon"' in auth.decorate(h, h.sent[1]))

# ------------------------------------------------------------------ API key ----
key = g.api_key()
check('API key: 32 hex, private file', re.fullmatch(r'[0-9a-f]{32}', key) and oct((work / 'config' / g.KEY_FILE).stat().st_mode & 0o777) == '0o600')
check('Same key on the next read', g.api_key() == key)
check('Header accepted', g.key_ok(H('/api/v1/status', X_Api_Key=key)))
check('?apikey= accepted on GET', g.key_ok(H('/api/v1/status?apikey=' + key)))
check('Wrong key refused', not g.key_ok(H('/api/v1/status', X_Api_Key='0' * 32)))
h = H('/api/v1/status', origin=None, X_Api_Key=key)
check('auth.get lets an API key through for API endpoints (no sign-in)', auth.get(h) is True)
h = H('/api/v1/status', origin=None, X_Api_Key='0' * 32); auth.get(h)
check('Wrong key → 401 JSON (not a login page)', h.sent and h.sent[0] == 401 and 'application/json' in h.sent[2])
h = H('/settings', origin=None, X_Api_Key=key); r = auth.get(h)
check('API key does NOT open normal pages', r is False and h.code == 303)
h = H('/api/v1/command/scan', origin=None, X_Api_Key=key); r = auth.post(h)
check('1.12: POST scan refused by default — the key is read-only until scans are allowed', r is False and h.sent and h.sent[0] == 403)
g._save(dict(g.settings(), api_scan=True))
h = H('/api/v1/command/scan', origin=None, X_Api_Key=key)
check('POST scan allowed with header key once scans are allowed (no cookie/origin)', auth.post(h) is True)
h = H('/api/v1/command/scan?apikey=' + key, origin=None); r = auth.post(h)
check('POST with ?apikey= only is refused (header required)', r is False)
h = H('/settings/general', origin=None, X_Api_Key=key); r = auth.post(h)
check('API key cannot change settings', r is False)
h = H('/api/v1/status', host='evil.example.com', origin=None, X_Api_Key=key)
check('API key on an unknown host is refused', auth.get(h) is False)
msg, url = post({'section': 'regenerate'})
new = g.api_key()
check('Regenerate: new key, old key stops working', new != key and not g.key_ok(H('/x', X_Api_Key=key)) and g.key_ok(H('/x', X_Api_Key=new)) and 'section=security' in url, msg)

# ------------------------------------------------------------------ trusted proxies ----
cf = {'CF_Connecting_IP': '203.0.113.7'}
check('Without trusted proxies the peer IP is used', g.client_ip(H(peer='172.17.0.1', **cf)) == '172.17.0.1')
msg, _ = post({'section': 'security', 'trusted_proxies': '172.17.0.0/16'})
check('Trusted proxies saved', g.settings()['trusted_proxies'] == ['172.17.0.0/16'], msg)
check('Through a trusted proxy the visitor IP is used', g.client_ip(H(peer='172.17.0.1', **cf)) == '203.0.113.7')
check('A spoofed header from an untrusted peer is ignored', g.client_ip(H(peer='10.9.9.9', **cf)) == '10.9.9.9')
check('Garbage forwarded IP falls back to the peer', g.client_ip(H(peer='172.17.0.1', CF_Connecting_IP='not-an-ip')) == '172.17.0.1')
msg, _ = post({'section': 'security', 'trusted_proxies': '300.1.1.1'})
check('Bad network refused', msg.startswith('✗'))

# ------------------------------------------------------------------ logging ----
logdir = work / 'logs-test'; logdir.mkdir()
logger = logging.getLogger('t.v197'); logger.setLevel(logging.INFO); logger.propagate = False
hand = lg._PrivateRotatingHandler(str(logdir / 'themearr.log'), maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
logger.addHandler(hand); lg._HANDLERS.append(hand)
tee = lg._Tee(io.StringIO(), logger, logging.ERROR)
msg, _ = post({'section': 'logging', 'log_level': 'info', 'log_max_mb': '3', 'log_keep': '2'})
tee.write('10.0.0.9 - - [27/Oct/2026 03:00:00] "GET /system HTTP/1.1" 200 -\n')
tee.write('10.0.0.9 - - [27/Oct/2026 03:00:01] "POST /login HTTP/1.1" 401 -\n')
tee.write('Real error happened\n')
hand.flush(); text = (logdir / 'themearr.log').read_text()
check('Info: successful web requests left out, errors kept', '"GET /system' not in text and '"POST /login' in text and 'Real error' in text, text)
check('Size limit + archives applied live', hand.maxBytes == 3 * 1024 * 1024 and hand.backupCount == 2 and 'Active now' in msg, (hand.maxBytes, hand.backupCount))
post({'section': 'logging', 'log_level': 'debug', 'log_max_mb': '1', 'log_keep': '5'})
tee.write('10.0.0.9 - - [27/Oct/2026 03:00:02] "GET /queue HTTP/1.1" 200 -\n'); hand.flush()
check('Debug: every web request logged', '"GET /queue' in (logdir / 'themearr.log').read_text())
for bad in ({'log_level': 'trace', 'log_max_mb': '1', 'log_keep': '1'}, {'log_level': 'info', 'log_max_mb': '0', 'log_keep': '1'}, {'log_level': 'info', 'log_max_mb': '1', 'log_keep': '9'}):
    msg, _ = post(dict(section='logging', **bad))
    check('Logging refuses ' + str(bad), msg.startswith('✗'), msg)

# ------------------------------------------------------------------ settings page ----
os.environ['THEMEARR_WRITE_AUTH'] = 'aa' * 16 + ':' + 'bb' * 32
html_page = ''.join(auth.settings(H('/settings?section=' + t)) for t in ('host', 'remote', 'security', 'logging'))
check('General shows Host, Application URL, API key & security, Logging', all(x in html_page for x in ('id="settings-host"', 'id="settings-remote-access"', 'id="settings-security"', 'id="settings-logging"', 'Application URL')))
check('API key shown with Copy and Regenerate', new in html_page and 'id="api-copy"' in html_page and 'Regenerate' in html_page)
check('V1.9.8: each General tab shows only its own section', all(auth.settings(H('/settings?section=' + t)).count('{display:block!important}') >= 1 and ('#' + pid + '{display:block!important}') in auth.settings(H('/settings?section=' + t))
      for t, pid in (('host', 'settings-host'), ('security', 'settings-security'), ('logging', 'settings-logging'), ('recycle', 'settings-recycle-bin'), ('general', 'settings-authentication'))))
check('V1.9.8: General tab links are real links with the current one marked', 'href="/settings?section=logging"' in html_page and 'aria-current="page">Logging</a>' in auth.settings(H('/settings?section=logging')))
check('Password managers told to ignore the new fields', html_page.count('data-bwignore="true"') >= 5)

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
