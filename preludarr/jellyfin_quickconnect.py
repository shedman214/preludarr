# SPDX-License-Identifier: GPL-3.0-or-later
"""Jellyfin Quick Connect (1.12, user 2026-10-30: "like sign in with plex") for Settings → Connect → Jellyfin.

1. start(url): checks that Quick Connect is enabled on that Jellyfin server and asks it for a code (POST /QuickConnect/Initiate).
2. The user approves the code in Jellyfin (profile → Quick Connect), signed in as an administrator.
3. status(): Preludarr's server (never the browser) polls /QuickConnect/Connect; once approved it signs in with
   /Users/AuthenticateWithQuickConnect, tests the connection and saves the address and token as the Jellyfin connection.
The token never reaches the browser or the logs. Only the address the user entered is contacted.
"""
import json
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LIFETIME = 600   # seconds a code stays usable here
MAX_PENDING = 10
_LOCK = threading.Lock()
_PENDING = {}    # id -> {'url', 'secret', 'code', 'created', 'result'}
AUTH = 'MediaBrowser Client="Preludarr", Device="Server", DeviceId="themearr-connect-v3", Version="3.0"'  # same device as media_servers._call


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise ValueError('Jellyfin answered with a redirect')


def _call(base, method, path, body=None, limit=1_000_000):
    data = json.dumps(body).encode() if body is not None else (b'' if method == 'POST' else None)
    headers = {'Accept': 'application/json', 'Authorization': AUTH, 'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect)
    with opener.open(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=15) as r:
        raw = r.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Jellyfin answer too large')
    return json.loads(raw or b'null')


def _prune():
    now = time.monotonic()
    for k in [k for k, v in _PENDING.items() if now - v['created'] > LIFETIME]:
        _PENDING.pop(k, None)


def start(config, raw_url):
    base = __import__('media_servers')._base_url(raw_url)   # same address rules as a manual connection
    try:
        enabled = _call(base, 'GET', '/QuickConnect/Enabled')
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError('That address does not look like a Jellyfin server (10.8 or newer).') from None
        raise
    if enabled is not True:
        raise ValueError('Quick Connect is turned off on this Jellyfin server. Turn it on in Jellyfin: Dashboard → General → Quick Connect.')
    try:
        data = _call(base, 'POST', '/QuickConnect/Initiate')
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise ValueError('Quick Connect is turned off on this Jellyfin server.') from None
        raise
    secret, code = str((data or {}).get('Secret', '')), str((data or {}).get('Code', ''))
    if not re.fullmatch(r'[A-Za-z0-9]{8,128}', secret) or not re.fullmatch(r'[0-9A-Za-z]{4,12}', code):
        raise ValueError('Jellyfin did not return a Quick Connect code.')
    ident = secrets.token_hex(12)
    with _LOCK:
        _prune()
        if len(_PENDING) >= MAX_PENDING:
            _PENDING.pop(next(iter(_PENDING)))
        _PENDING[ident] = {'url': base, 'secret': secret, 'code': code, 'created': time.monotonic(), 'result': None}
    return {'id': ident, 'code': code}


def _save(config, base, token, user):
    ms = __import__('media_servers')
    record = {'kind': 'jellyfin', 'url': base, 'api_key': token, 'enabled': True, 'user': user['name'], 'admin': user['admin']}
    ms._test_connection(record)
    with ms._LOCK:
        servers = ms._read(config, 'servers')
        old = dict(servers.get('jellyfin') or {})
        same = urllib.parse.urlsplit(old.get('url') or '').netloc == urllib.parse.urlsplit(base).netloc
        merged = dict(old) if same else {}
        merged.update(record)
        servers['jellyfin'] = merged
        ms._write(config, 'servers', servers)


def status(config, ident):
    """{'state': 'waiting' | 'done' | 'expired', …}. On approval the connection is saved here."""
    with _LOCK:
        _prune()
        item = _PENDING.get(str(ident))
    if item is None:
        return {'state': 'expired'}
    if item['result']:
        return item['result']
    state = _call(item['url'], 'GET', '/QuickConnect/Connect?' + urllib.parse.urlencode({'secret': item['secret']}))
    if not (state or {}).get('Authenticated'):
        return {'state': 'waiting', 'code': item['code']}
    auth = _call(item['url'], 'POST', '/Users/AuthenticateWithQuickConnect', {'Secret': item['secret']})
    token = str((auth or {}).get('AccessToken') or '')
    who = (auth or {}).get('User') or {}
    user = {'name': str(who.get('Name') or 'Jellyfin user')[:60], 'admin': bool((who.get('Policy') or {}).get('IsAdministrator'))}
    if not token:
        raise ValueError('Jellyfin approved the code but did not return a token.')
    _save(config, item['url'], token, user)
    result = {'state': 'done', 'user': user['name'], 'admin': user['admin']}
    with _LOCK:
        _PENDING.pop(str(ident), None)
    return result


# ------------------------------------------------------------------------------ web ----
def get(handler, config):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    try:
        result = status(config, q.get('id', [''])[0][:40])
    except Exception as error:
        result = {'state': 'error', 'message': str(error) if isinstance(error, ValueError) else __import__('neterrors').text(error, 'Jellyfin')}
    return handler.send(200, json.dumps(result), 'application/json')


def post(handler, config):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 2048:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=4)
        return handler.send(200, json.dumps(start(config, form.get('url', [''])[0])), 'application/json')
    except ValueError as error:
        return handler.send(400, json.dumps({'error': str(error)}), 'application/json')
    except Exception as error:
        return handler.send(502, json.dumps({'error': __import__('neterrors').text(error, 'Jellyfin')}), 'application/json')


def block(current_url):
    import html
    return ('<div class="jf-qc"><p class="jf-or">Quick Connect</p>'
            '<label class="field"><span>Jellyfin address</span><input type="url" id="jf-qc-url" maxlength="200" placeholder="http://192.168.1.10:8096" value="'
            + html.escape(current_url or '', quote=True) + '"></label>'
            '<button type="button" class="jf-btn" id="jf-qc-btn">Connect with Quick Connect</button>'
            '<p class="muted">Preludarr shows a code. In Jellyfin, signed in as an administrator, open your profile → <strong>Quick Connect</strong> and enter it. '
            'Or enter the address and an API key by hand below.</p><div id="jf-qc-result" aria-live="polite"></div></div>')


CSS = ('.jf-qc{margin:6px 0 16px;padding:14px;border:1px solid #3d5172;border-radius:10px;background:#172137}.jf-qc .jf-or{text-align:center;color:#b7c6de;font-size:13px;margin:0 0 8px}'
       '.jf-qc button.jf-btn{display:block;width:100%;margin:8px 0;background:rgba(170,92,195,.28)!important;color:#fff!important;border:1px solid #aa5cc3!important;'
       'border-radius:8px;padding:9px 18px;font-weight:700;cursor:pointer;box-shadow:none!important}.jf-qc button.jf-btn:hover{background:rgba(170,92,195,.42)!important}'
       '.jf-qc button.jf-btn[disabled]{opacity:.6;cursor:wait}.jf-code{font-size:32px;font-weight:800;letter-spacing:6px;text-align:center;margin:10px 0;color:#fff}')
SCRIPT = r"""
(function(){const b=document.getElementById('jf-qc-btn');if(!b)return;const out=document.getElementById('jf-qc-result');const u=document.getElementById('jf-qc-url');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
b.addEventListener('click',async()=>{b.disabled=true;out.textContent='Asking Jellyfin for a code…';
 try{const r=await fetch('/media-servers/jellyfin-qc/start',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'url='+encodeURIComponent(u.value.trim())});
  const d=await r.json();if(!r.ok)throw new Error(d.error||'Jellyfin did not answer.');
  out.innerHTML='<p>Enter this code in Jellyfin (profile → Quick Connect):</p><p class="jf-code">'+esc(d.code)+'</p><p class="muted">Waiting for approval…</p>';
  const until=Date.now()+600000;
  while(Date.now()<until){await new Promise(z=>setTimeout(z,3000));
   const s=await (await fetch('/media-servers/jellyfin-qc/status?id='+encodeURIComponent(d.id),{credentials:'same-origin'})).json();
   if(s.state==='waiting')continue;
   if(s.state==='done'){location.href='/media-servers?tab=media&notice='+encodeURIComponent('Jellyfin connected with Quick Connect as '+s.user+(s.admin?'.':' — this user is not an administrator, so refresh after installs may not work.'));return;}
   throw new Error(s.message||'The code expired. Try again.');}
  throw new Error('The code expired. Try again.');
 }catch(e){out.textContent=e.message;b.disabled=false;}});})();
"""
