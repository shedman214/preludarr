# SPDX-License-Identifier: GPL-3.0-or-later
"""Forgotten password (1.14.3): a one-time reset code that only appears in the container log. Offline."""
import contextlib
import hashlib
import io
import os
import re
import secrets
import sys
import tempfile
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ['CONFIG_DIR'] = str(work / 'config')
os.environ.pop('THEMEARR_WRITE_AUTH', None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import login as tl  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


class AH:
    def __init__(self, path, body='', host='localhost:8788', origin=None, cookie=None):
        b = body.encode()
        self.headers = {'Host': host, 'Origin': origin or 'http://' + host, 'Content-Length': str(len(b)),
                        'Content-Type': 'application/x-www-form-urlencoded'}
        if cookie:
            self.headers['Cookie'] = cookie
        self.path = path; self.rfile = io.BytesIO(b); self.client_address = ('127.0.0.1', 5000)
        self.sent = None; self.status = None; self.hdrs = {}

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass


def account(auth, username, password):
    salt = secrets.token_bytes(16)
    auth.write_account(username, salt.hex() + ':' + hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 240000).hex())


def request_code(auth):
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        h = AH('/reset-request'); auth.post(h)
    m = re.search(r'reset code on the reset page: ([A-Z2-9]{4}-[A-Z2-9]{4})', log.getvalue())
    return h, (m.group(1) if m else None), log.getvalue()


def reset(auth, code, password='a brand new pass phrase', confirm=None, **kw):
    h = AH('/reset', body=urllib.parse.urlencode({'code': code, 'password': password, 'confirm': confirm or password}), **kw)
    with contextlib.redirect_stdout(io.StringIO()):
        auth.post(h)
    return h


auth = tl.Auth()
account(auth, 'theowner', 'the original password')
old_session = auth.issue()

h = AH('/login'); auth.get(h)
check('The sign-in page links to "Forgot password?"', h.sent[0] == 200 and 'href="/reset"' in h.sent[1] and 'Forgot password?' in h.sent[1])
h = AH('/reset'); auth.get(h)
check('The reset page explains the container-log code and asks for a 16+ character password',
      h.sent[0] == 200 and 'container log' in h.sent[1] and 'action="/reset-request"' in h.sent[1] and 'minlength="16"' in h.sent[1])
h = AH('/reset', cookie='themearr_session=' + old_session); auth.get(h)
check('A signed-in visitor is sent back to the app', h.status == 303 and h.hdrs.get('Location') == '/')

h = reset(auth, 'AAAA-BBBB')
check('Without a requested code nothing can be reset', h.sent[0] == 403 and auth.verify('the original password', 'theowner'))

h, code, log = request_code(auth)
check('Requesting a code writes it ONLY to the container log and confirms on the page',
      code and h.status == 303 and h.hdrs.get('Location') == '/reset?sent=1' and 'If you did not ask for it' in log)
h2 = AH('/reset?sent=1'); auth.get(h2)
check('…and the page says the code is in the log', 'now in the container log' in h2.sent[1] and code not in h2.sent[1])

h = reset(auth, 'ZZZZ-ZZZZ')
check('A wrong code is refused', h.sent[0] == 403 and auth.verify('the original password', 'theowner'))
h = reset(auth, code + '\u200b')
check('A code with a pasted invisible character is refused normally (no crash)', h.sent and h.sent[0] == 403
      and 'not right' in h.sent[1] and auth.verify('the original password', 'theowner'))
h = reset(auth, code, password='short')
check('A short password is refused', h.sent[0] == 400 and '16–128' in h.sent[1])
h = reset(auth, code, origin='http://evil.example')
check('A cross-site request is refused', h.sent and h.sent[0] == 403 and auth.verify('the original password', 'theowner'))

h = reset(auth, code.lower().replace('-', '- '))
check('The right code sets the new password, keeps the username and signs this browser in',
      h.status == 303 and h.hdrs.get('Location') == '/' and 'themearr_session=' in h.hdrs.get('Set-Cookie', '')
      and auth.verify('a brand new pass phrase', 'theowner') and not auth.verify('the original password', 'theowner'))
check('Every earlier session is signed out', not auth.active(AH('/', cookie='themearr_session=' + old_session)))
check('The new password is stored hashed, privately', 'brand new' not in auth.account_file.read_text()
      and oct(auth.account_file.stat().st_mode & 0o777) == '0o600')
h = reset(auth, code, password='yet another long password')
check('A code works only once', h.sent[0] == 403 and auth.verify('a brand new pass phrase', 'theowner'))

_, code2, _ = request_code(auth)
auth.reset_expires = 0.0
h = reset(auth, code2, password='expired code password!')
check('An expired code (15 minutes) is refused', h.sent[0] == 403 and auth.verify('a brand new pass phrase', 'theowner'))

auth3 = tl.Auth()
for _ in range(3):
    request_code(auth3)
h, extra, _ = request_code(auth3)
check('At most 3 reset codes per 15 minutes', h.sent and h.sent[0] == 429 and extra is None)
auth4 = tl.Auth(); _, real, _ = request_code(auth4)
for _ in range(5):
    reset(auth4, 'QQQQ-QQQQ')
h = reset(auth4, real)
check('Guessing is throttled: after 5 wrong codes even the right one waits', h.sent[0] == 429 and auth4.verify('a brand new pass phrase', 'theowner'))

# ---- the code is used up atomically (CodeRabbit, CWE-367) ----
import threading  # noqa: E402
auth7 = tl.Auth()
_, c7, _ = request_code(auth7)
h = reset(auth7, c7, password='short')
check('A too-short password does not use up the code', h.sent[0] == 400 and auth7.reset_code == c7)
real_write = auth7.write_account
auth7.write_account = lambda *a: (_ for _ in ()).throw(ValueError('disk full'))
h = reset(auth7, c7, password='survives a failed write ok')
auth7.write_account = real_write
check('A failed save keeps the code valid', h.sent[0] == 500 and auth7.reset_code == c7)
slow = auth7.write_account

def slow_write(*a):
    time_sleep(0.2); return slow(*a)
from time import sleep as time_sleep  # noqa: E402
auth7.write_account = slow_write
outcomes = []
barrier = threading.Barrier(2)

def racer(pw):
    barrier.wait(); outcomes.append(reset(auth7, c7, password=pw))
threads = [threading.Thread(target=racer, args=(pw,)) for pw in ('first racing password!', 'second racing password')]
[t.start() for t in threads]; [t.join() for t in threads]
ok = [h for h in outcomes if h.status == 303]
check('Two simultaneous resets with one code: exactly one succeeds', len(ok) == 1 and sum(1 for h in outcomes if h.sent and h.sent[0] == 403) == 1
      and (auth7.verify('first racing password!', 'theowner') != auth7.verify('second racing password', 'theowner')))
auth7.write_account = real_write

fresh = Path(tempfile.mkdtemp())
os.environ['CONFIG_DIR'] = str(fresh / 'config')
auth5 = tl.Auth()
h = AH('/reset'); auth5.get(h)
check('Before any account exists, /reset leads to first-run setup instead', h.status == 303 and h.hdrs.get('Location') == '/setup')
fresh2 = Path(tempfile.mkdtemp())
os.environ['CONFIG_DIR'] = str(fresh2 / 'config')
auth6 = tl.Auth()
with contextlib.redirect_stdout(io.StringIO()):
    setup_code = auth6.ensure_setup_code()
h = AH('/setup', body=urllib.parse.urlencode({'code': setup_code + '\u00e9', 'username': 'someone', 'password': 'p' * 20, 'confirm': 'p' * 20}))
with contextlib.redirect_stdout(io.StringIO()):
    auth6.post(h)
check('First-run setup also refuses a non-ASCII code normally (no crash)', h.sent and h.sent[0] == 403 and auth6.needs_setup())
check('No LAN address is hard-coded in the sign-in code', not re.search(r'192\.168\.\d+\.\d+', (Path(tl.__file__)).read_text()))

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
