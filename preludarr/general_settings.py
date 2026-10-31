# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → General: Host, Security (API key) and Logging (Themearr V1.9.7, Radarr-style; user 2026-10-27).

Settings live in /config/themearr_general.json (0600); the API key in /config/themearr_api_key (0600).
Only things that make sense for Themearr in Docker are editable: bind address, port and SSL are controlled by the
Unraid template / reverse proxy and are shown read-only. Authentication is always required (Themearr can be
reachable from the internet through a tunnel), and outgoing HTTPS certificate checks are always on.
"""
import hmac
import threading
import time
import html
import ipaddress
import json
import os
import re
import secrets
import urllib.parse
from pathlib import Path

FILE = 'themearr_general.json'
KEY_FILE = 'themearr_api_key'
DEFAULTS = {'instance_name': 'Preludarr', 'allowed_hosts': [], 'trusted_proxies': [], 'log_level': 'info', 'log_max_mb': 2, 'log_keep': 5,
            'api_scan': False, 'api_query_keys': False}  # 1.12: new installs are read-only (scans need explicit permission); ?apikey= off (header only)
_HOST = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*|\[[0-9a-f:]+\])(?::[0-9]{1,5})?')
API_GET = ('/api/status', '/api/v1/status', '/api/v1/activity')
API_POST = ('/api/v1/command/scan',)


def _e(value):
    return html.escape(str(value), quote=True)


def _config():
    return Path(os.environ.get('CONFIG_DIR', '/config'))


def _write(path, text, mode=0o600):
    tmp = path.with_name('.' + path.name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as out:
        out.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------ settings ----
def settings():
    data = dict(DEFAULTS)
    try:
        path = _config() / FILE
        if not path.is_symlink():
            saved = json.loads(path.read_text())
            name = str(saved.get('instance_name') or '').strip()
            if 1 <= len(name) <= 40 and re.fullmatch(r"[\w .'()-]+", name) and name not in ('Themearr', 'Splicarr'):
                data['instance_name'] = name  # a saved old default name (Themearr, Preludarr) now shows Preludarr; custom names are kept
            data['allowed_hosts'] = [h for h in saved.get('allowed_hosts', [])[:10] if isinstance(h, str) and _HOST.fullmatch(h)]
            data['trusted_proxies'] = [p for p in saved.get('trusted_proxies', [])[:10] if isinstance(p, str) and _net(p)]
            if saved.get('log_level') in ('info', 'debug'):
                data['log_level'] = saved['log_level']
            if isinstance(saved.get('log_max_mb'), int) and 1 <= saved['log_max_mb'] <= 50:
                data['log_max_mb'] = saved['log_max_mb']
            if isinstance(saved.get('log_keep'), int) and 1 <= saved['log_keep'] <= 5:
                data['log_keep'] = saved['log_keep']
            for flag in ('api_scan', 'api_query_keys'):
                if isinstance(saved.get(flag), bool):
                    data[flag] = saved[flag]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return data


def migrate_api_scan(existing_install):
    """1.12: the API key became read-only by default. An install from before 1.12 that never saved this choice keeps what it had
    (the key may start scans) and the choice is written down once; a saved choice is never changed; a new install stays read-only."""
    path = _config() / FILE
    try:
        saved = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else {}
        if not isinstance(saved, dict):
            return None
    except (OSError, ValueError):
        return None
    if isinstance(saved.get('api_scan'), bool) or not existing_install:
        return None
    data = settings()
    data['api_scan'] = True
    _save(data)
    return True


def _save(data):
    _write(_config() / FILE, json.dumps(data))


def _net(text):
    try:
        return ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return None


def _list(raw, check, what, limit=10):
    items = [x.strip().lower() for x in re.split(r'[,\s]+', raw or '') if x.strip()]
    if len(items) > limit:
        raise ValueError(f'At most {limit} {what}.')
    for x in items:
        if not check(x):
            raise ValueError(f'“{x}” is not a valid {what[:-1] if what.endswith("s") else what}.')
    return items


# ------------------------------------------------------------------------------ host ----
def instance_name():
    return settings()['instance_name']


def allowed_hosts():
    """Extra LAN addresses accepted over plain http (host or host:port)."""
    return set(settings()['allowed_hosts'])


def client_ip(h):
    """Visitor IP for sign-in limits: the proxy's forwarded IP only when the direct peer is a trusted proxy."""
    peer = (getattr(h, 'client_address', None) or ('',))[0]
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    nets = [n for n in (_net(p) for p in settings()['trusted_proxies']) if n]
    if not any(peer_ip in n for n in nets):
        return peer
    for header in ('CF-Connecting-IP', 'X-Forwarded-For'):
        value = (h.headers.get(header, '') or '').split(',')[0].strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    return peer


# ------------------------------------------------------------------------------ api key ----
def api_key(create=True):
    path = _config() / KEY_FILE
    try:
        if not path.is_symlink():
            key = path.read_text().strip()
            if re.fullmatch(r'[0-9a-f]{32}', key):
                return key
    except OSError:
        pass
    if not create:
        return ''
    key = secrets.token_hex(16)
    _write(path, key + '\n')
    return key


def regenerate_key():
    key = secrets.token_hex(16)
    _write(_config() / KEY_FILE, key + '\n')
    return key


def key_ok(h, allow_query=True):
    """X-Api-Key header (or ?apikey= on GET) matches the API key."""
    key = api_key(create=False)
    if not key:
        return False
    given = (h.headers.get('X-Api-Key', '') or '').strip()
    if not given and allow_query:
        given = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get('apikey', [''])[0].strip()
    return bool(given) and hmac.compare_digest(given.encode(), key.encode())


def api_route(path, method):
    return path in (API_GET if method == 'GET' else API_POST)


# V1.9.14: one gate for every API-key request — header-only by default, explicit errors, scope, rate limit, scan cooldown.
API_PER_MINUTE = 60
SCAN_COOLDOWN = 300
_API_LOCK = threading.Lock()
_API_HITS = {}
_API_STATE = {'scan_at': 0.0, 'legacy_note_at': 0.0}


def api_auth(h, path, method):
    """None = no API key in this request (normal sign-in rules apply); True = allowed; False = refused (JSON answer sent)."""
    if not api_route(path, method):
        return None
    header = (h.headers.get('X-Api-Key', '') or '').strip()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query).get('apikey', [''])[0].strip()
    if not header and not query:
        return None

    def refuse(code, message):
        h.send(code, json.dumps({'error': message}), 'application/json')
        return False
    ip, now = client_ip(h), time.monotonic()
    with _API_LOCK:
        hits = [t for t in _API_HITS.get(ip, []) if now - t < 60] + [now]
        _API_HITS[ip] = hits[-(API_PER_MINUTE + 5):]
        if len(_API_HITS) > 2000:
            for k in list(_API_HITS)[:1000]:
                _API_HITS.pop(k, None)
    if len(hits) > API_PER_MINUTE:
        return refuse(429, 'Too many API requests: the limit is %d per minute.' % API_PER_MINUTE)
    if not __import__('login').host_ok(h):
        return None
    s = settings()
    legacy = not header
    if legacy and (method != 'GET' or not s['api_query_keys']):
        return refuse(401, 'API keys in the address (?apikey=) are turned off. Send the key in the X-Api-Key header '
                           '(or turn the old way back on under Settings → General → API key & security).')
    key = api_key(create=False)
    if not key or not hmac.compare_digest((header or query).encode(), key.encode()):
        return refuse(401, 'Wrong API key.')
    if legacy and time.time() - _API_STATE['legacy_note_at'] > 3600:
        _API_STATE['legacy_note_at'] = time.time()
        try:
            __import__('sys').modules['__main__'].event('API', 'warning', 'An API request sent the key in the address (?apikey=). Move that tool to the X-Api-Key header.')
        except Exception:
            pass
    if method == 'POST' and path == '/api/v1/command/scan':
        if not s['api_scan']:
            return refuse(403, 'This API key is read-only. Allow starting scans under Settings → General → API key & security.')
        with _API_LOCK:
            if time.time() - _API_STATE['scan_at'] < SCAN_COOLDOWN:
                return refuse(429, 'A scan was started through the API less than 5 minutes ago. Try again later.')
            _API_STATE['scan_at'] = time.time()
    return True


# ------------------------------------------------------------------------------ logging ----
def apply_logging():
    try:
        s = settings()
        __import__('app_logs').apply_settings(s['log_level'], s['log_max_mb'], s['log_keep'])
    except Exception as error:
        print('Logging settings not applied (' + type(error).__name__ + ')', flush=True)


# ------------------------------------------------------------------------------ UI ----
def _notice(q, key):
    msg = q.get(key, [''])[0][:300]
    if not msg:
        return ''
    bad = msg.startswith('✗')
    return '<div role="' + ('alert' if bad else 'status') + '" class="notice">' + _e(msg.lstrip('✗ ')) + '</div>'


def panels(h):
    """Host, Security (API key) and Logging panels for Settings → General."""
    import login as tl
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
    s = settings()
    key = api_key()
    here = h.headers.get('Host', '').lower()
    peer = (getattr(h, 'client_address', None) or ('?',))[0]
    port = os.environ.get('PORT', '8787')
    public = sorted(tl.public_hosts())
    copy_js = ('<script>(function(){const b=document.getElementById("api-copy"),i=document.getElementById("api-key");if(!b||!i)return;'
               'b.addEventListener("click",async()=>{let ok=false;try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(i.value);ok=true}}catch(e){}'
               'if(!ok){const t=i.type;i.type="text";i.removeAttribute("readonly");i.select();try{ok=document.execCommand("copy")}catch(e){}i.setAttribute("readonly","");i.type=t;window.getSelection().removeAllRanges()}'
               'b.textContent=ok?"✓ Copied":"Show, then select + Ctrl+C";setTimeout(()=>{b.textContent="📋 Copy"},2000)});'
               'const v=document.getElementById("api-show"),st=document.getElementById("api-key-state");if(v)v.addEventListener("click",()=>{const show=i.type==="password";'
               'i.type=show?"text":"password";v.setAttribute("aria-pressed",String(show));v.textContent=show?"🙈 Hide":"👁 Show";'
               'if(st)st.textContent=show?"The key is visible. Hide it again before sharing your screen.":"The key is hidden; Show reveals it on this screen only. Copy works while it is hidden."})})();</script>')
    host = ('<section class="panel" id="settings-host"><div class="pill">General · Host</div><h2>🖧 Host</h2>'
            + _notice(q, 'host_notice') +
            '<form method="post" action="/settings/general" autocomplete="off"><input type="hidden" name="section" value="host">'
            '<label for="g-name">Instance name<input id="g-name" name="instance_name" maxlength="40" value="' + _e(s['instance_name']) + '" ' + _NOPM + '></label>'
            '<p class="help">Shown in the browser tab and when the page is installed as an app. Default: Preludarr.</p>'
            '<label for="g-hosts">Allowed hosts (LAN)<input id="g-hosts" name="allowed_hosts" maxlength="400" placeholder="e.g. preludarr.lan:8788, nas.local:8788" '
            'value="' + _e(', '.join(s['allowed_hosts'])) + '" ' + _NOPM + '></label>'
            '<p class="help">Extra plain-http addresses Preludarr accepts, comma separated (host or host:port). '
            'Any private IP address of this server (for example <code>192.168.x.x:8788</code>) and <code>localhost</code> always work. '
            'For HTTPS addresses use Application URL below.</p>'
            '<button type="submit">Save Host</button></form>'
            '<dl class="g-info"><dt>Bind address</dt><dd>All interfaces inside the container (fixed)</dd>'
            '<dt>Port</dt><dd>' + _e(port) + ' inside the container; the outside port is set in your Docker settings (Unraid: Docker → Preludarr → Edit; Compose: <code>ports:</code>)</dd>'
            '<dt>SSL</dt><dd>Not handled by Preludarr — use HTTPS through your Cloudflare tunnel or reverse proxy</dd>'
            '<dt>You are using</dt><dd><code>' + _e(here) + '</code></dd></dl></section>')
    app_url = (tl.remote_panel(h).replace('<h2>🌐 Remote access</h2>', '<h2>🌐 Application URL</h2>')
               .replace('General · Remote access', 'General · Host · Application URL').replace('Save Remote Access', 'Save Application URL'))
    sec = ('<section class="panel" id="settings-security"><div class="pill">General · Security</div><h2>🔑 API key &amp; security</h2>'
           + _notice(q, 'security_notice') +
           '<label for="api-key">API key</label><div class="g-key"><input id="api-key" type="password" value="' + _e(key) + '" readonly spellcheck="false" autocomplete="off" '
           'aria-describedby="api-key-state" ' + _NOPM + '>'
           '<button type="button" id="api-show" class="g-copy" aria-controls="api-key" aria-pressed="false">👁 Show</button>'
           '<button type="button" id="api-copy" class="g-copy">📋 Copy</button>'
           '<form method="post" action="/settings/general" class="g-regen" onsubmit="return confirm(\'Make a new API key? The old key stops working right away — update any tool that uses it.\')">'
           '<input type="hidden" name="section" value="regenerate"><button type="submit" class="g-danger" title="Regenerate API key">🔄 Regenerate</button></form></div>'
           '<p class="help" id="api-key-state">The key is hidden; Show reveals it on this screen only. Copy works while it is hidden.</p>'
           '<p class="help">Lets tools like Homepage or Home Assistant read Preludarr without signing in. Send it in the <code>X-Api-Key</code> header. '
           '<strong>What the key can do:</strong> read <code>GET /api/v1/status</code>, <code>GET /api/v1/activity</code> and <code>GET /api/status</code>'
           + (', and start a library scan with <code>POST /api/v1/command/scan</code> (at most once every 5 minutes)' if s['api_scan'] else ' — it is <strong>read-only</strong> (starting scans is off)') +
           '. It can never change settings, install themes or open other pages. Limit: 60 requests per minute. Keep it private; regenerate it if it leaks.</p>'
           '<form method="post" action="/settings/general" autocomplete="off"><input type="hidden" name="section" value="security">'
           '<label class="check g-flag"><input type="checkbox" name="api_scan" value="1"' + (' checked' if s['api_scan'] else '') + '><span>API key may start library scans</span></label>'
           '<label class="check g-flag"><input type="checkbox" name="api_query_keys" value="1"' + (' checked' if s['api_query_keys'] else '') + '><span>Also accept the key in the address '
           '(<code>?apikey=</code>, read-only requests) — <em>less safe: addresses end up in browser history and proxy logs</em></span></label>'
           '<label for="g-proxies">Trusted proxies<input id="g-proxies" name="trusted_proxies" maxlength="400" placeholder="e.g. 172.17.0.1 or 172.17.0.0/16" '
           'value="' + _e(', '.join(s['trusted_proxies'])) + '" ' + _NOPM + '></label>'
           '<p class="help">IP addresses or networks (CIDR) of your tunnel/proxy. Only for these, Preludarr uses the visitor IP the proxy reports '
           '(Cloudflare <code>CF-Connecting-IP</code>) for the sign-in attempt limit — otherwise everyone coming through the tunnel shares one limit. '
           'This request came from <code>' + _e(peer) + '</code>.</p>'
           + (('<p class="g-warn" role="note"><strong>Needs attention:</strong> a public address is set but no trusted proxy, so everyone who comes through '
               'the tunnel shares one sign-in limit — five wrong passwords from anyone lock everybody out for five minutes. If your tunnel connects from '
               '<code>' + _e(peer) + '</code>, add it above as a trusted proxy (Cloudflare then reports each visitor’s own address).</p>')
              if public and not s['trusted_proxies'] else '') +
           '<button type="submit">Save Security</button></form>'
           '<dl class="g-info"><dt>Authentication</dt><dd>Login page — always required (Preludarr can be reachable from the internet)</dd>'
           '<dt>Certificate validation</dt><dd>Always on for outgoing HTTPS (Plex, Sonarr/Radarr, providers)</dd>'
           '<dt>Public HTTPS address</dt><dd>' + (', '.join('<code>https://' + _e(p) + '</code>' for p in public) or 'none — LAN only') + '</dd></dl>'
           + copy_js + '</section>')
    level = s['log_level']
    logging_panel = ('<section class="panel" id="settings-logging"><div class="pill">General · Logging</div><h2>📜 Logging</h2>'
                     + _notice(q, 'logging_notice') +
                     '<form method="post" action="/settings/general"><input type="hidden" name="section" value="logging">'
                     '<label for="g-level">Log level<select id="g-level" name="log_level">'
                     '<option value="info"' + (' selected' if level == 'info' else '') + '>Info — events, warnings and errors</option>'
                     '<option value="debug"' + (' selected' if level == 'debug' else '') + '>Debug — also every web request</option></select></label>'
                     '<p class="help">Use Debug only while troubleshooting; it makes the log grow faster.</p>'
                     '<label for="g-size">Log size limit (MB)<input id="g-size" name="log_max_mb" type="number" min="1" max="50" required value="' + str(s['log_max_mb']) + '"></label>'
                     '<p class="help">Maximum size of a log file before it is archived. Default 2 MB.</p>'
                     '<label for="g-keep">Old logs to keep<input id="g-keep" name="log_keep" type="number" min="1" max="5" required value="' + str(s['log_keep']) + '"></label>'
                     '<p class="help">Archived files kept per log (1–5); the oldest is deleted automatically. View them under <a href="/system?tab=logs">System → Logs</a>.</p>'
                     '<button type="submit">Save Logging</button></form></section>')
    style = ('<style>#settings-logging select{display:block;width:100%;max-width:560px;margin-top:8px;padding:12px;background:#0b1424;color:#fff;'
             'border:1px solid #4a5d80;border-radius:8px;font:inherit;font-weight:600;box-sizing:border-box}'
             '.g-key{display:flex;gap:8px;align-items:center;flex-wrap:wrap;max-width:860px}.g-key input{flex:1 1 260px;font-family:ui-monospace,monospace;letter-spacing:.04em}'
             '.g-key>input,.g-key>button,.g-key form,.g-key form button{margin:0!important;height:42px;box-sizing:border-box}'
             '.g-key form{margin:0}.g-copy,.g-key button{white-space:nowrap}.g-danger{background:#b84052!important;color:#fff!important}'
             '.g-warn{padding:10px 12px;border-radius:8px;border:1px solid #e0b85f;background:#4a3d1f;margin:10px 0}'
             '.g-flag{display:flex!important;gap:10px;align-items:flex-start;margin:10px 0;font-weight:500;max-width:none}'
             '.g-flag input[type=checkbox]{display:inline-block!important;width:18px!important;height:18px;flex:0 0 auto;margin:3px 0 0!important;padding:0!important}'
             '.g-flag span{flex:1 1 auto;min-width:0}'
             '.g-info{display:grid;grid-template-columns:170px minmax(0,1fr);gap:6px 14px;margin:16px 0 0;font-size:.92em}.g-info dt{color:#adbed9}.g-info dd{margin:0}'
             '@media(max-width:700px){.g-info{grid-template-columns:1fr}}</style>')
    return host + app_url + sec + logging_panel + style


_NOPM = 'data-bwignore="true" data-lpignore="true" data-1p-ignore="true"'


def post(h, redirect):
    try:
        size = int(h.headers.get('Content-Length', '0') or 0)
        if not 0 < size <= 4096:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(h.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=8)
        f = lambda k: form.get(k, [''])[0].strip()  # noqa: E731
        section = f('section')
        s = settings()
        if section == 'host':
            name = f('instance_name') or 'Preludarr'
            if not (1 <= len(name) <= 40 and re.fullmatch(r"[\w .'()-]+", name)):
                raise ValueError('Instance name: 1–40 letters, numbers, spaces and . \' ( ) - only.')
            s['instance_name'] = name
            s['allowed_hosts'] = _list(f('allowed_hosts'), lambda x: bool(_HOST.fullmatch(x)) and (':' not in x.split(']')[-1] or 1 <= int(x.rsplit(':', 1)[1]) <= 65535), 'hosts')
            _save(s)
            key, msg = 'host_notice', 'Host saved: ' + name + ('; extra LAN addresses: ' + ', '.join(s['allowed_hosts']) if s['allowed_hosts'] else '') + '.'
        elif section == 'security':
            s['trusted_proxies'] = [str(_net(x)) for x in _list(f('trusted_proxies'), lambda x: _net(x) is not None, 'networks')]
            s['api_scan'] = f('api_scan') == '1'
            s['api_query_keys'] = f('api_query_keys') == '1'
            _save(s)
            key, msg = 'security_notice', 'Security saved' + (': trusted proxies ' + ', '.join(s['trusted_proxies']) if s['trusted_proxies'] else ': no trusted proxies') + '.'
        elif section == 'regenerate':
            regenerate_key()
            key, msg = 'security_notice', 'New API key created — the old key no longer works. Update any tool that uses it.'
        elif section == 'logging':
            lvl = f('log_level')
            if lvl not in ('info', 'debug'):
                raise ValueError('Choose a log level.')
            mb, keep = f('log_max_mb'), f('log_keep')
            if not (mb.isdigit() and 1 <= int(mb) <= 50):
                raise ValueError('Log size limit: 1–50 MB.')
            if not (keep.isdigit() and 1 <= int(keep) <= 5):
                raise ValueError('Old logs to keep: 1–5.')
            s.update(log_level=lvl, log_max_mb=int(mb), log_keep=int(keep))
            _save(s)
            apply_logging()
            key, msg = 'logging_notice', f'Logging saved: {lvl.capitalize()}, {mb} MB per file, keep {keep} old file(s). Active now.'
        else:
            raise ValueError('Unknown section.')
    except (ValueError, UnicodeError) as error:
        section = locals().get('section') or 'host'
        key = {'security': 'security_notice', 'regenerate': 'security_notice', 'logging': 'logging_notice'}.get(section, 'host_notice')
        msg = '✗ ' + str(error)
    except OSError:
        key, msg = 'host_notice', '✗ Could not save the settings.'
    tab = {'host_notice': 'host', 'security_notice': 'security', 'logging_notice': 'logging'}[key]
    return redirect(h, '/settings?' + urllib.parse.urlencode({'section': tab, key: msg}))
