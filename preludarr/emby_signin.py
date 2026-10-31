# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign in to Emby (1.12, user 2026-10-30: "make one for emby for whatever it uses"). Emby has no Quick Connect, so the user
signs in once with an Emby username and password: Preludarr sends them to that Emby server only (POST /emby/Users/AuthenticateByName),
keeps the access token Emby returns, and never stores or logs the password.
"""
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH = 'Emby Client="Preludarr", Device="Server", DeviceId="themearr-connect-v3", Version="3.0"'
_LOCK = threading.Lock()
_FAILS = []          # monotonic times of failed sign-ins (at most 5 per 5 minutes)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise ValueError('Emby answered with a redirect')


def _authenticate(base, username, password):
    body = json.dumps({'Username': username, 'Pw': password}).encode()
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Emby-Authorization': AUTH,
               'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'}
    path = '/Users/AuthenticateByName' if base.lower().endswith('/emby') else '/emby/Users/AuthenticateByName'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect)
    with opener.open(urllib.request.Request(base + path, data=body, headers=headers, method='POST'), timeout=15) as r:
        raw = r.read(1_000_001)
    return json.loads(raw or b'null')


def sign_in(config, raw_url, username, password):
    ms = __import__('media_servers')
    base = ms._base_url(raw_url)
    if not 1 <= len(username) <= 128 or len(password) > 256 or any(c in username for c in '\r\n'):
        raise ValueError('Enter your Emby username and password.')
    now = time.monotonic()
    with _LOCK:
        _FAILS[:] = [t for t in _FAILS if now - t < 300]
        if len(_FAILS) >= 5:
            raise ValueError('Too many failed sign-ins. Wait five minutes and try again.')
    try:
        data = _authenticate(base, username, password)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            with _LOCK:
                _FAILS.append(now)
            raise ValueError('Emby refused the username or password.') from None
        if error.code == 404:
            raise ValueError('That address does not look like an Emby server.') from None
        raise
    token = str((data or {}).get('AccessToken') or '')
    who = (data or {}).get('User') or {}
    if not token:
        raise ValueError('Emby did not return a sign-in token.')
    user, admin = str(who.get('Name') or username)[:60], bool((who.get('Policy') or {}).get('IsAdministrator'))
    record = {'kind': 'emby', 'url': base, 'api_key': token, 'enabled': True, 'user': user, 'admin': admin}
    ms._test_connection(record)
    with ms._LOCK:
        servers = ms._read(config, 'servers')
        old = dict(servers.get('emby') or {})
        same = urllib.parse.urlsplit(old.get('url') or '').netloc == urllib.parse.urlsplit(base).netloc
        merged = dict(old) if same else {}
        merged.update(record)
        servers['emby'] = merged
        ms._write(config, 'servers', servers)
    return user, admin


def post(handler, config, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 2048:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=4)
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        user, admin = sign_in(config, f('url'), f('username').strip(), f('password'))
        msg = 'Emby connected: signed in as ' + user + ('.' if admin else ' — this user is not an administrator, so refresh after installs may not work.')
    except (ValueError, OSError) as error:
        msg = 'Emby not connected: ' + (str(error) if isinstance(error, ValueError) else __import__('neterrors').text(error, 'Emby'))
    except Exception as error:
        msg = 'Emby not connected: ' + __import__('neterrors').text(error, 'Emby')
    return redirect(handler, '/media-servers?' + urllib.parse.urlencode({'tab': 'media', 'notice': msg[:240]}))


def block(current_url):
    import html
    return ('<form method="post" action="/media-servers/emby-signin" class="emby-si" autocomplete="off"><p class="emby-or">Sign in with your Emby account</p>'
            '<label class="field"><span>Emby address</span><input type="url" name="url" required maxlength="200" placeholder="http://192.168.1.10:8096" value="'
            + html.escape(current_url or '', quote=True) + '"></label>'
            '<label class="field"><span>Username</span><input type="text" name="username" required maxlength="128" autocomplete="off"></label>'
            '<label class="field"><span>Password</span><input type="password" name="password" maxlength="256" autocomplete="new-password"></label>'
            '<button type="submit" class="emby-btn">Sign in to Emby</button>'
            '<p class="muted">Use an Emby <strong>administrator</strong>. The password goes only to your Emby server and is not saved; Preludarr keeps the access token '
            'Emby gives back. Or enter the address and an API key by hand below.</p></form>')


CSS = ('.emby-si{margin:6px 0 16px;padding:14px;border:1px solid #3d5172;border-radius:10px;background:#172137}.emby-si .emby-or{text-align:center;color:#b7c6de;font-size:13px;margin:0 0 8px}'
       '.emby-si button.emby-btn{display:block;width:100%;margin:8px 0;background:rgba(82,181,75,.28)!important;color:#fff!important;border:1px solid #52b54b!important;'
       'border-radius:8px;padding:9px 18px;font-weight:700;cursor:pointer;box-shadow:none!important}.emby-si button.emby-btn:hover{background:rgba(82,181,75,.42)!important}')
