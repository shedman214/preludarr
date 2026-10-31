# SPDX-License-Identifier: GPL-3.0-or-later
"""Sign in with Plex (1.12, user 2026-10-30: "like Seerr") for Settings → Connect → Plex.

1. start(): create a PIN at plex.tv; the browser opens app.plex.tv/auth in a popup, where the user signs in and approves Preludarr.
2. status(): Preludarr's server (never the browser) asks plex.tv whether the PIN was approved. When it was, it lists the account's
   Plex servers with their addresses, then forgets the account token.
3. choose(): saves the chosen server's address and that server's own access token as the Plex connection, then tests it.
Tokens never reach the browser or the logs. Only plex.tv and clients.plex.tv are contacted, over HTTPS.
"""
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

PLEX_TV = 'https://plex.tv'
CLIENTS_TV = 'https://clients.plex.tv'
AUTH_APP = 'https://app.plex.tv/auth#?'
CLIENT_FILE = 'preludarr_plex_client_id'
PIN_LIFETIME = 900          # seconds the sign-in window stays usable here
MAX_PENDING = 20
_LOCK = threading.Lock()
_PENDING = {}               # pin id -> {'code', 'created', 'servers' (server-side only, with tokens) or None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise ValueError('Plex answered with a redirect')


def _client_id(config):
    """A stable id for this Preludarr install, as Plex asks of every app (shown in Plex → Authorized Devices)."""
    path = Path(config) / CLIENT_FILE
    try:
        value = path.read_text().strip()
        if re.fullmatch(r'[0-9a-f-]{36}', value):
            return value
    except OSError:
        pass
    value = str(uuid.uuid4())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(value)
    return value


def _headers(config, token=None):
    version = getattr(__import__('login'), 'APP_VERSION', '1.12')
    h = {'Accept': 'application/json', 'X-Plex-Product': 'Preludarr', 'X-Plex-Version': version, 'X-Plex-Client-Identifier': _client_id(config),
         'X-Plex-Device-Name': 'Preludarr', 'X-Plex-Platform': 'Web', 'User-Agent': 'Preludarr/' + version + ' (self-hosted theme manager)'}
    if token:
        h['X-Plex-Token'] = token
    return h


def _call(method, url, headers, limit=2_000_000):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect)
    req = urllib.request.Request(url, data=b'' if method == 'POST' else None, headers=headers, method=method)
    with opener.open(req, timeout=15) as r:
        body = r.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Plex answer too large')
    return json.loads(body or b'null')


def _prune():
    now = time.monotonic()
    for k in [k for k, v in _PENDING.items() if now - v['created'] > PIN_LIFETIME]:
        _PENDING.pop(k, None)


def start(config):
    """Create a PIN. Returns {'pin': id, 'auth_url': …} for the browser's popup."""
    data = _call('POST', PLEX_TV + '/api/v2/pins?strong=true', _headers(config))
    pin, code = str(data.get('id', '')), str(data.get('code', ''))
    if not re.fullmatch(r'[0-9]{1,20}', pin) or not re.fullmatch(r'[A-Za-z0-9]{4,64}', code):
        raise ValueError('Plex did not return a sign-in code')
    with _LOCK:
        _prune()
        if len(_PENDING) >= MAX_PENDING:
            _PENDING.pop(next(iter(_PENDING)))
        _PENDING[pin] = {'code': code, 'created': time.monotonic(), 'servers': None}
    query = urllib.parse.urlencode({'clientID': _client_id(config), 'code': code, 'context[device][product]': 'Preludarr'})
    return {'pin': pin, 'auth_url': AUTH_APP + query}


def _servers_for(config, account_token):
    resources = _call('GET', CLIENTS_TV + '/api/v2/resources?includeHttps=1&includeRelay=0&includeIPv6=0', _headers(config, account_token))
    out = []
    for r in resources if isinstance(resources, list) else []:
        if 'server' not in str(r.get('provides', '')).split(','):
            continue
        token = str(r.get('accessToken') or '')
        if not token:
            continue
        conns = []
        for c in r.get('connections') or []:
            uri = str(c.get('uri') or '')
            try:
                safe = __import__('media_servers')._base_url(uri)
            except ValueError:
                continue
            local = bool(c.get('local'))
            plain_ip = uri.startswith('http://')
            conns.append({'uri': safe, 'local': local,
                          'label': ('Local network' if local else 'Remote') + (' · ' + str(c.get('address', '')) + ':' + str(c.get('port', '')) if plain_ip else ' · secure (plex.direct)'),
                          'rank': (0 if local else 2) + (0 if plain_ip else 1)})
        conns.sort(key=lambda c: c['rank'])
        if conns:
            out.append({'name': str(r.get('name') or 'Plex Media Server')[:80], 'owned': bool(r.get('owned')), 'token': token, 'connections': conns[:8],
                        'machine': re.sub(r'[^A-Za-z0-9-]', '', str(r.get('clientIdentifier') or ''))[:64]})
    return out[:10]


def status(config, pin):
    """{'state': 'waiting' | 'ready' | 'expired', 'servers': [...without tokens]}"""
    with _LOCK:
        _prune()
        item = _PENDING.get(str(pin))
    if item is None:
        return {'state': 'expired'}
    if item['servers'] is None:
        data = _call('GET', PLEX_TV + '/api/v2/pins/' + str(pin), _headers(config))
        account_token = str((data or {}).get('authToken') or '')
        if not account_token:
            return {'state': 'waiting'}
        servers = _servers_for(config, account_token)   # the account token is used only here and then dropped
        with _LOCK:
            if str(pin) in _PENDING:
                _PENDING[str(pin)]['servers'] = servers
        item = dict(item, servers=servers)
    return {'state': 'ready', 'servers': [{'name': s['name'], 'owned': s['owned'], 'connections': [{'label': c['label'], 'uri': c['uri']} for c in s['connections']]}
                                          for s in item['servers']]}


def _host(url):
    return (urllib.parse.urlsplit(str(url)).hostname or '').lower()


def choose(config, pin, picks, main, enabled):
    """picks: {server index: connection index} for every server to use; main: the index of the main Plex server.
    The main server is stored as 'plex' (its per-title links are kept when it is the same server as before); the others as 'plex-2' …"""
    with _LOCK:
        item = _PENDING.get(str(pin))
    if not item or not item.get('servers'):
        raise ValueError('The Plex sign-in expired. Sign in again.')
    try:
        chosen = [(int(i), item['servers'][int(i)], item['servers'][int(i)]['connections'][int(c)]) for i, c in picks.items()]
        main = int(main)
    except (ValueError, IndexError, TypeError, KeyError):
        raise ValueError('Choose one of the listed servers and addresses.') from None
    if not chosen:
        raise ValueError('Tick at least one Plex server.')
    if main not in [i for i, _, _ in chosen]:
        main = chosen[0][0]
    if len(chosen) > 9:
        raise ValueError('Up to 9 Plex servers can be connected.')
    ms = __import__('media_servers')
    records = []
    for i, server, conn in sorted(chosen, key=lambda x: x[0] != main):
        record = {'kind': 'plex', 'url': conn['uri'], 'api_key': server['token'], 'enabled': bool(enabled), 'name': server['name'], 'machine': server['machine']}
        try:
            ms._test_connection(record)   # each chosen address must answer as a Plex server
        except Exception as error:
            raise ValueError(server['name'] + ' did not answer at ' + conn['uri'] + ' (' + __import__('neterrors').text(error, 'Plex') + '). Choose another address.') from None
        records.append((server, conn, record))
    kept_links = 0
    with ms._LOCK:
        servers = ms._read(config, 'servers')
        old = dict(servers.get('plex') or {})
        new_main = records[0][2]
        main_server = records[0][0]
        same = (old.get('machine') and old.get('machine') == new_main['machine']) or (
            not old.get('machine') and _host(old.get('url')) in {_host(c['uri']) for c in main_server['connections']})
        merged = dict(old) if same else {}
        merged.update(new_main)
        kept_links = len(merged.get('mappings') or {})
        servers['plex'] = merged
        for k in [k for k in servers if ms.EXTRA_PLEX.fullmatch(k)]:
            servers.pop(k)
        for n, (_, _, record) in enumerate(records[1:], 2):
            servers['plex-' + str(n)] = record
        ms._write(config, 'servers', servers)
    with _LOCK:
        _PENDING.pop(str(pin), None)
    return [r[0]['name'] for r in records], kept_links, bool(old.get('mappings')) and not same


# ------------------------------------------------------------------------------ web ----
def get(handler, config):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    try:
        result = status(config, q.get('pin', [''])[0][:20])
    except Exception as error:
        result = {'state': 'error', 'message': __import__('neterrors').text(error, 'Plex')}
    return handler.send(200, json.dumps(result), 'application/json')


def remove_extra(config, key):
    ms = __import__('media_servers')
    if not ms.EXTRA_PLEX.fullmatch(str(key)):
        raise ValueError('Only an extra Plex server can be removed here.')
    with ms._LOCK:
        servers = ms._read(config, 'servers')
        record = servers.pop(key, None)
        if record is None:
            raise ValueError('That Plex server is not connected.')
        ms._write(config, 'servers', servers)
    return record.get('name') or 'Plex'


def post(handler, config, redirect):
    route = urllib.parse.urlsplit(handler.path).path
    if route == '/media-servers/plex-signin/start':
        try:
            return handler.send(200, json.dumps(start(config)), 'application/json')
        except Exception as error:
            return handler.send(502, json.dumps({'error': __import__('neterrors').text(error, 'Plex')}), 'application/json')
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=24)
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        if route == '/media-servers/plex-extra/remove':
            if f('confirm') != '1':
                raise ValueError('Tick the box to confirm.')
            return redirect(handler, '/media-servers?' + urllib.parse.urlencode({'tab': 'media', 'notice': 'Removed ' + remove_extra(config, f('key')) + ' from Preludarr.'}))
        picks = {k[4:]: v[0] for k, v in form.items() if re.fullmatch(r'use_[0-9]', k) and v[0] != ''}
        names, kept, dropped = choose(config, f('pin'), picks, f('main') or '0', f('enabled') == '1')
        msg = 'Signed in with Plex: connected to ' + ', '.join(names) + ' (main: ' + names[0] + ').' + (
            ' Title links kept: %d.' % kept if kept else '') + (' The main server changed, so its old title links were cleared.' if dropped else '')
    except (ValueError, OSError) as error:
        msg = 'Plex not connected: ' + str(error)
    except Exception as error:
        msg = 'Plex not connected: ' + __import__('neterrors').text(error, 'Plex')
    return redirect(handler, '/media-servers?' + urllib.parse.urlencode({'tab': 'media', 'notice': msg[:240]}))


BUTTON = ('<div class="plex-signin"><p class="plex-or">Sign in with</p><button type="button" class="plex-btn" id="plex-signin-btn" aria-label="Sign in with Plex">'
          '<span class="plex-word">ple<span class="plex-x">x</span></span></button>'
          '<p class="muted">Opens Plex in a new window. Sign in there and approve Preludarr; then pick your server below. Or enter the address and token by hand.</p>'
          '<div id="plex-signin-result" aria-live="polite"></div></div>')
CSS = ('.plex-signin{margin:6px 0 16px;padding:14px;border:1px solid #3d5172;border-radius:10px;background:#172137}'
       '.plex-signin .plex-or{text-align:center;color:#b7c6de;font-size:13px;margin:0 0 8px}'
       '.plex-signin button.plex-btn{display:block;width:100%;background:rgba(229,160,13,.28)!important;color:#fff!important;'
       'border:1px solid #e5a00d!important;border-radius:8px;padding:9px 18px;cursor:pointer;box-shadow:none!important}'
       '.plex-signin button.plex-btn:hover{background:rgba(229,160,13,.42)!important}'
       '.plex-word{font-weight:800;font-size:17px;letter-spacing:.2px;font-family:system-ui,-apple-system,Segoe UI,sans-serif}.plex-x{color:#e5a00d}'
       '.plex-btn[disabled]{opacity:.6;cursor:wait}.plex-choice{display:grid;gap:8px;margin-top:10px}'
       '.plex-choice label{display:flex;gap:8px;align-items:flex-start}.plex-choice code{font-size:12px;overflow-wrap:anywhere}'
       '.plex-srv{border:1px solid #3d5172;border-radius:8px;padding:8px 12px;display:grid;gap:6px}.plex-srv legend{font-weight:700;padding:0 6px}')
SCRIPT = r"""
(function(){const b=document.getElementById('plex-signin-btn');if(!b)return;const out=document.getElementById('plex-signin-result');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
b.addEventListener('click',async()=>{const w=window.open('about:blank','plexauth','width=600,height=720');b.disabled=true;out.textContent='Waiting for Plex…';
 try{const r=await fetch('/media-servers/plex-signin/start',{method:'POST',credentials:'same-origin'});const d=await r.json();
  if(!r.ok||!d.auth_url)throw new Error(d.error||'Plex did not answer.');if(w)w.location=d.auth_url;else window.open(d.auth_url,'plexauth');
  const until=Date.now()+600000;
  while(Date.now()<until){await new Promise(z=>setTimeout(z,2000));
   const s=await (await fetch('/media-servers/plex-signin/status?pin='+encodeURIComponent(d.pin),{credentials:'same-origin'})).json();
   if(s.state==='waiting')continue;if(w&&!w.closed)w.close();
   if(s.state!=='ready')throw new Error(s.message||'The sign-in expired. Try again.');
   if(!s.servers.length)throw new Error('Signed in, but this Plex account has no server Preludarr can reach. Enter the address by hand.');
   let h='<form method="post" action="/media-servers/plex-signin/choose" class="plex-choice"><input type="hidden" name="pin" value="'+esc(d.pin)+'"><strong>Choose your Plex server'+(s.servers.length>1?'s':'')+'</strong>';
   s.servers.forEach((sv,i)=>{h+='<fieldset class="plex-srv"><legend><label><input type="checkbox" class="plex-use" data-i="'+i+'"'+(i===0?' checked':'')+'> '+esc(sv.name)+(sv.owned?'':' <small>(shared with you)</small>')+'</label></legend>'
     +'<input type="hidden" name="use_'+i+'" value="'+(i===0?'0':'')+'">';
    sv.connections.forEach((c,j)=>{h+='<label><input type="radio" name="conn_'+i+'" value="'+j+'"'+(j===0?' checked':'')+'><span>'+esc(c.label)+'<br><code>'+esc(c.uri)+'</code></span></label>'});
    if(s.servers.length>1)h+='<label><input type="radio" name="main" value="'+i+'"'+(i===0?' checked':'')+'><span>Main server (title links, refresh after installs)</span></label>';
    h+='</fieldset>'});
   h+='<label><input type="checkbox" name="enabled" value="1" checked><span>Use these connections (collections, refresh after installs)</span></label><button type="submit" class="primary">Connect</button></form>';
   out.innerHTML=h;const f=out.querySelector('form');
   const sync=()=>{f.querySelectorAll('.plex-use').forEach(cb=>{const i=cb.dataset.i;const r=f.querySelector('input[name=conn_'+i+']:checked');f['use_'+i].value=cb.checked&&r?r.value:'';});};
   f.addEventListener('change',sync);sync();
   b.disabled=false;return;}
  throw new Error('The sign-in took too long. Try again.');
 }catch(e){if(w&&!w.closed)w.close();out.textContent=e.message;b.disabled=false;}});})();
"""
