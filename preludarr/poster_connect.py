# SPDX-License-Identifier: GPL-3.0-or-later
"""Themearr Settings > Connect: poster provider credentials (stdlib only).

No secret is rendered back to a browser, placed into logs or stored in the media tree.
Credentials are written to a private, atomically replaced /config JSON file.
"""
import contextlib
import html
import json
import os
import re
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_LOCK = threading.RLock()
_FILE = 'poster_connect_v1.json'
_ALLOWED = {'tmdb_key', 'tvdb_key', 'tvdb_pin', 'preferred'}
_TMBDOC = 'https://www.themoviedb.org/settings/api'
_TVBDOC = 'https://support.thetvdb.com/kb/faq.php?id=81'


def _private_path(config):
    return Path(config) / _FILE


def _read(config):
    """No following symlinks or reading files larger than 8KB."""
    path = _private_path(config)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError('Invalid credential store')
        if info.st_mode & 0o077:
            raise ValueError('Credential store permissions are too open')
        raw = f.read(8193)
    if len(raw) > 8192:
        raise ValueError('Invalid credential store size')
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict) or set(data) - _ALLOWED:
        raise ValueError('Invalid credential store contents')
    if any(not isinstance(v, str) or len(v) > 1024 for v in data.values()):
        raise ValueError('Invalid credential store value')
    return data


def _write(config, data):
    if set(data) - _ALLOWED:
        raise ValueError('Invalid provider setting')
    folder = Path(config)
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError('Invalid configuration directory')
    target = _private_path(config)
    if target.is_symlink():
        raise ValueError('Credential store is a symlink')
    # Only the running process can read the store. Never chmod/overwrite an
    # existing file through a symlink, even when applying an update.
    raw = json.dumps(data, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > 8192:
        raise ValueError('Credential store exceeds limit')
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', prefix='.poster-connect-',
                                         suffix='.tmp', dir=str(folder), delete=False) as tmp:
            name = tmp.name
            os.fchmod(tmp.fileno(), 0o600)
            tmp.write(raw)
            tmp.flush()
            os.fsync(tmp.fileno())
        if target.is_symlink():
            raise ValueError('Unsafe credential store')
        os.replace(name, target)
        name = None
        # Sync directory metadata where supported.
        with contextlib.suppress(OSError):
            dirfd = os.open(str(folder), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
    finally:
        if name:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)


def _effective(config):
    with _LOCK:
        saved = _read(config)
    result = {}
    for key, env in (('tmdb_key', 'TMDB_API_KEY'),
                     ('tvdb_key', 'TVDB_API_KEY'),
                     ('tvdb_pin', 'TVDB_PIN'),
                     ('preferred', 'POSTER_PROVIDER')):
        if key in saved:
            result[key] = saved[key]
        else:
            result[key] = (os.getenv('TVDB_SUBSCRIBER_PIN', '').strip() or os.getenv(env, 'tmdb' if key == 'preferred' else '').strip()) if key == 'tvdb_pin' else os.getenv(env, 'tmdb' if key == 'preferred' else '').strip()
    if result['preferred'] not in ('tmdb', 'tvdb'):
        result['preferred'] = 'tmdb'
    return result


def effective_values(config):
    """Snapshot poster credentials; never log/print this return value."""
    e = _effective(config)
    return e['tmdb_key'], e['tvdb_key'], e['tvdb_pin'], e['preferred']


def has_provider(config):
    try:
        a, b, _, _ = effective_values(config)
        return bool(a or b)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def tvdb_login_payload(key, pin):
    payload = {'apikey': key}
    if pin:
        payload['pin'] = pin
    return payload


def _validate_credential(value, maximum=512):
    if not (1 <= len(value) <= maximum) or not re.fullmatch(r'[!-~]+', value):
        raise ValueError('Invalid credential characters or length')
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_TEST_HTTP = urllib.request.build_opener(_NoRedirect)


def _test_provider(config, provider):
    # Only fixed endpoints. Avoid printing error objects: URL errors can embed
    # TMDB api_key values in their messages.
    tmdb, tvdb, pin, _ = effective_values(config)
    if provider == 'tmdb':
        if not tmdb:
            return False, 'TMDB has no configured API key.'
        url = 'https://api.themoviedb.org/3/configuration?' + urllib.parse.urlencode({'api_key': tmdb})
        req = urllib.request.Request(url, headers={'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'})
        try:
            with _TEST_HTTP.open(req, timeout=12) as response:
                data = json.load(response)
                if response.status != 200 or not isinstance(data, dict) or 'images' not in data:
                    return False, 'TMDB did not return a valid configuration.'
        except urllib.error.HTTPError as exc:
            return False, 'TMDB rejected the request (HTTP %s).' % exc.code
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False, 'Could not reach TMDB. Check the key and network.'
        return True, 'TMDB authentication succeeded.'
    if provider == 'tvdb':
        if not tvdb:
            return False, 'TVDB has no configured API key.'
        raw = json.dumps(tvdb_login_payload(tvdb, pin)).encode('utf-8')
        req = urllib.request.Request('https://api4.thetvdb.com/v4/login',
                                     data=raw, method='POST', headers={
                                         'Content-Type': 'application/json',
                                         'Accept': 'application/json',
                                         'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'})
        try:
            with _TEST_HTTP.open(req, timeout=12) as response:
                data = json.load(response)
                if response.status != 200 or not (data.get('data') or {}).get('token'):
                    return False, 'TVDB did not issue an authentication token.'
        except urllib.error.HTTPError as exc:
            return False, 'TVDB rejected the request (HTTP %s).' % exc.code
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False, 'Could not reach TVDB. Check the key, PIN and network.'
        return True, 'TVDB authentication succeeded.'
    return False, 'Unknown provider.'


def _status(config, provider):
    with _LOCK:
        stored = _read(config)
    key = provider + '_key'
    e = _effective(config)
    if key in stored:
        return 'Saved in Preludarr' if e[key] else 'Disabled in Preludarr'
    return 'Using Docker environment' if e[key] else 'Not configured'


def _esc(text):
    return html.escape(str(text), quote=True)


def panel_html(config, notice=""):
    """Add an independent card to existing Settings > Connect layout."""
    try:
        e = _effective(config)
        tmdb_status = _status(config, 'tmdb')
        tvdb_status = _status(config, 'tvdb')
        with _LOCK:
            saved = _read(config)
        pin_status = ('Saved in Preludarr' if saved.get('tvdb_pin') else
                      'Disabled in Preludarr' if 'tvdb_pin' in saved else
                      'Using Docker environment' if e['tvdb_pin'] else 'Not configured')
        warning = ''
    except (OSError, ValueError, json.JSONDecodeError):
        e = {'preferred': 'tmdb'}
        tmdb_status = tvdb_status = pin_status = 'Configuration unavailable'
        warning = '<p role="alert">Stored poster configuration cannot be read safely. Check file permissions.</p>'
    css = '''<style>
#themearr-poster-connect{margin:24px 0;padding:23px;border:1px solid #485c7b;background:#1c283e;border-radius:14px;color:#edf4ff}
#themearr-poster-connect *{box-sizing:border-box}
#themearr-poster-connect h2,#themearr-poster-connect h3{color:#f6f9ff}
#themearr-poster-connect .pc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr));gap:16px}
#themearr-poster-connect .pc-card{padding:18px;border:1px solid #455a7a;background:#24334e;border-radius:11px}
#themearr-poster-connect p{line-height:1.5;color:#c5d4ea}
#themearr-poster-connect label{display:block;margin:11px 0 4px;font-weight:600;color:#fff}
#themearr-poster-connect input,#themearr-poster-connect select{display:block;width:100%;padding:11px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px}
#themearr-poster-connect .pc-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
#themearr-poster-connect button{color:#fff;background:#446de5;border:0;border-radius:7px;padding:10px 12px;cursor:pointer;font-weight:600}
#themearr-poster-connect button.pc-subtle{background:#46546d}
#themearr-poster-connect button.pc-danger{background:#9b394a}
#themearr-poster-connect a{color:#a7c9ff;text-decoration:underline}
#themearr-poster-connect .pc-status{color:#b4e1fa;font-weight:600}
#themearr-poster-connect .pc-feedback{margin-top:12px;padding:10px 12px;border-radius:7px;min-height:20px;font-weight:650;color:#dce7ff}
#themearr-poster-connect .pc-feedback:empty{display:none}
#themearr-poster-connect .pc-feedback.pc-ok{background:#173d37;color:#baffdc;border:1px solid #3b9b78}
#themearr-poster-connect .pc-feedback.pc-error{background:#492a34;color:#ffe0e6;border:1px solid #b56a79}
#themearr-poster-connect .pc-feedback.pc-busy{background:#293d65;color:#e1edff;border:1px solid #5279b0}
#themearr-poster-connect button:disabled{opacity:.6;cursor:wait}
</style>'''
    cards = []
    for provider, title, status, link, detail in (
        ('tmdb', 'TMDB · The Movie Database', tmdb_status, _TMBDOC,
         'Sign in → Account Settings → API → request a key. Enter the API Key (v3 auth), not the bearer token.'),
        ('tvdb', 'TVDB · TheTVDB', tvdb_status, _TVBDOC,
         'Sign in → Dashboard → Account → API Keys → create a v4 project key. A subscriber PIN is needed for user-supported access.')
    ):
        pin_html = ('''<label for="pc-tvdb-pin">TVDB subscriber PIN (only when required)</label>
<input id="pc-tvdb-pin" name="pin" type="password" autocomplete="new-password" maxlength="512" placeholder="Leave blank to keep the current PIN">
<p>PIN: <span class="pc-status">%s</span>. Without a PIN, TVDB requests omit the PIN field.</p>''' % _esc(pin_status)) if provider == 'tvdb' else ''
        cards.append('''<div class="pc-card"><h3>%s</h3><p>Connection: <span class="pc-status">%s</span></p>
<p>%s <a href="%s" target="_blank" rel="noopener noreferrer">Get API key ↗</a></p>
<form method="post" action="/poster-connect-action" autocomplete="off">
<input type="hidden" name="provider" value="%s">
<label for="pc-%s-key">API key</label><input id="pc-%s-key" type="password" name="key" autocomplete="new-password" maxlength="512" placeholder="Enter new key (blank keeps current)">
%s<div class="pc-actions">
<button name="action" value="save">Save</button>
<button type="submit" name="action" value="test" class="pc-subtle" formnovalidate>Test saved connection</button>
%s
<button type="submit" name="action" value="remove" class="pc-danger" formnovalidate>Disconnect</button>
<button type="submit" name="action" value="fallback" class="pc-subtle" formnovalidate>Use Docker fallback</button>
</div><div class="pc-feedback" role="status" aria-live="polite" aria-atomic="true"></div></form></div>''' % (_esc(title), _esc(status), _esc(detail), _esc(link), provider,
                            provider, provider, pin_html,
                            '<button type="submit" name="action" value="clear_pin" class="pc-subtle" formnovalidate>Clear PIN</button>' if provider == 'tvdb' else ''))
    options = ''.join('<option value="%s"%s>%s first (fallback to %s)</option>' % (
        p, ' selected' if e['preferred'] == p else '', p.upper(), ('TVDB' if p == 'tmdb' else 'TMDB'))
        for p in ('tmdb', 'tvdb'))
    accepted = {'Poster preference saved', 'TMDB connected', 'TMDB connection failed',
                'TVDB connected', 'TVDB connection failed',
                'Poster connection settings updated',
                'No changes - enter a key to save'}
    note_html = ('<p role="status" style="border:1px solid #617aa2;padding:10px;background:#304565">'
                 + _esc(notice) + '</p>') if notice in accepted else ''
    return (css + '''<section id="themearr-poster-connect" aria-labelledby="pc-title">
<h2 id="pc-title">🖼️ Poster Providers</h2>
<p>Manage TMDB and TVDB credentials for Poster Recovery. Keys are private, never shown after saving. New credentials are used without restarting Preludarr. Already cached posters are retained.</p>
%s%s<div class="pc-grid">%s</div>
<div class="pc-card" style="margin-top:16px"><h3>Preferred poster source</h3>
<form method="post" action="/poster-connect-action">
<input type="hidden" name="provider" value="priority"><label for="pc-priority">Try this provider first</label>
<select id="pc-priority" name="preferred">%s</select>
<div class="pc-actions"><button type="submit" name="action" value="save">Save preference</button></div>
</form><p>Changes do not start a bulk poster scan. Your existing Poster Recovery controls and rate limits still apply.</p></div>
<p style="font-size:.9em">TMDB and TVDB have their own licensing and access requirements. Do not share API credentials.</p>
</section>''' % (warning, note_html, ''.join(cards), options) + _INLINE_TEST_JS)


_INLINE_TEST_JS = r"""<script>
(() => {
 "use strict";
 const root = document.getElementById("themearr-poster-connect");
 if (!root) return;
 for (const form of root.querySelectorAll('form[action="/poster-connect-action"]')) {
  const provider = form.querySelector('input[name="provider"]')?.value;
  if (!['tmdb', 'tvdb'].includes(provider)) continue;
  const feedback = form.querySelector('.pc-feedback');
  const testButton = form.querySelector('button[name="action"][value="test"]');
  if (!feedback || !testButton) continue;
  let busy = false;
  form.addEventListener('submit', async (event) => {
   if (!event.submitter || event.submitter.value !== 'test') return;
   event.preventDefault();
   if (busy) return;
   busy = true;
   testButton.disabled = true;
   feedback.className = 'pc-feedback pc-busy';
   feedback.textContent = 'Testing saved ' + provider.toUpperCase() + ' connection…';
   try {
    // Deliberately omit any unsaved key or PIN from this test request.
    const body = new URLSearchParams({provider, action:'test'});
    const response = await fetch('/poster-connect-action', {
      method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8',
               'Accept':'application/json'},
      body:body.toString()
    });
    if (!response.ok) throw new Error('Request rejected (HTTP ' + response.status + ').');
    const contentType = response.headers.get('Content-Type') || '';
    if (!contentType.toLowerCase().includes('application/json')) {
      throw new Error('Unexpected response. Reload Preludarr or sign in again.');
    }
    const result = await response.json();
    if (!result || result.provider !== provider || typeof result.ok !== 'boolean' ||
        typeof result.message !== 'string') {
      throw new Error('Invalid test response.');
    }
    feedback.className = 'pc-feedback ' + (result.ok ? 'pc-ok' : 'pc-error');
    feedback.textContent = (result.ok ? '✓ ' : '✕ ') + result.message;
   } catch (error) {
    feedback.className = 'pc-feedback pc-error';
    feedback.textContent = 'Connection test could not complete: ' +
      (error && error.message ? error.message : 'Network error.');
   } finally {
    busy = false;
    testButton.disabled = false;
   }
  });
 }
})();
</script>"""


class _RenderProxy:
    def __init__(self, real, config):
        self._real, self._config = real, config

    def __getattr__(self, name):
        return getattr(self._real, name)

    def send(self, status, body, *args, **kwargs):
        if status == 200:
            if not isinstance(body, str) or '</body>' not in body.lower():
                raise RuntimeError('Connect did not return its expected HTML document')
            low = body.lower()
            marker = low.rfind('</main>') if '<main' in low else -1
            if marker < 0:
                # An older Connect layout uses div containers, not <main>.
                # Keep the full original page intact and add the card before
                # </body> instead of guessing where a div closes.
                marker = low.rfind('</body>')
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(getattr(self._real, 'path', '')).query)
            notice = qs.get('poster_notice', [''])[0]
            import media_servers
            tab = media_servers.connect_tab(qs)
            # V1.9.8 (user): Connect tabs — Status first, each tab shows only its own section. The media-server section
            # closes just before </main>, so the other sections are inserted after it.
            close = body.lower().rfind('</section></main>')
            if close != -1:
                marker = close + len('</section>')
            try:
                arr_html = __import__('arr').connect_html(self._config, qs.get('arr_notice', [''])[0], qs.get('arr_preview', [''])[0])
            except Exception as error:
                print('Sonarr/Radarr card error: ' + type(error).__name__, flush=True)
                arr_html = ''
            try:
                anidb_html = __import__('anidb').settings_html(self._config, qs.get('anidb_notice', [''])[0])
            except Exception as error:
                print('AniDB settings card error: ' + type(error).__name__, flush=True)
                anidb_html = ''
            body = (body[:marker] + '<div class="cx-sec" id="cx-sec-posters">' + panel_html(self._config, notice) + '</div>'
                    '<div class="cx-sec" id="cx-sec-arr">' + arr_html + '</div><div class="cx-sec" id="cx-sec-anidb">' + anidb_html + '</div>' + body[marker:])
            start = body.find('<section class="cx-sec" id="cx-sec-media">')
            try:
                head = media_servers.connect_tabs(tab) + media_servers.connect_overview(self._config)
            except Exception as error:
                print('Connect overview error: ' + type(error).__name__, flush=True)
                head = media_servers.connect_tabs(tab)
            if start != -1:
                body = body[:start] + head + body[start:]
        return self._real.send(status, body, *args, **kwargs)


def render_connect_page(handler, config, original_page, roots, db):
    """Per-request response proxy: no shared handler mutation and no theme code patch."""
    return original_page(_RenderProxy(handler, config), config, roots, db)


def _safe_redirect(handler, message):
    query = urllib.parse.urlencode({'poster_notice': message})
    handler.send_response(303)
    handler.send_header('Location', '/media-servers?' + query + '#themearr-poster-connect')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()


def post(handler, config):
    """Authenticated by app.do_POST before this function. No credential echo."""
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if length < 1 or length > 4096:
            return handler.send(413, 'Invalid form size', 'text/plain')
        content_type = handler.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type != 'application/x-www-form-urlencoded':
            return handler.send(415, 'Unsupported form type', 'text/plain')
        payload = handler.rfile.read(length).decode('utf-8', 'strict')
        form = urllib.parse.parse_qs(payload, keep_blank_values=True, strict_parsing=True,
                                     max_num_fields=8)
        if any(len(v) != 1 for v in form.values()):
            return handler.send(400, 'Invalid form values', 'text/plain')
        action = form.get('action', [''])[0]
        provider = form.get('provider', [''])[0]
        if action not in ('save', 'test', 'remove', 'fallback', 'clear_pin'):
            return handler.send(400, 'Unknown action', 'text/plain')
        if provider not in ('tmdb', 'tvdb', 'priority'):
            return handler.send(400, 'Unknown provider', 'text/plain')
        if provider == 'priority':
            if action != 'save' or set(form) != {'provider', 'action', 'preferred'}:
                return handler.send(400, 'Invalid preference request', 'text/plain')
            pref = form['preferred'][0]
            if pref not in ('tmdb', 'tvdb'):
                return handler.send(400, 'Invalid poster preference', 'text/plain')
            with _LOCK:
                stored = _read(config)
                stored['preferred'] = pref
                _write(config, stored)
            return _safe_redirect(handler, 'Poster preference saved')
        permitted = {'provider', 'action', 'key'} | ({'pin'} if provider == 'tvdb' else set())
        if set(form) - permitted:
            return handler.send(400, 'Invalid provider request', 'text/plain')
        if action == 'clear_pin' and provider != 'tvdb':
            return handler.send(400, 'Invalid PIN action', 'text/plain')
        if action == 'test':
            ok, message = _test_provider(config, provider)
            # Inline test returns only a safe status. Never return or log credentials.
            if 'application/json' in handler.headers.get('Accept', '').lower():
                result = json.dumps({'provider': provider, 'ok': bool(ok), 'message': message})
                return handler.send(200, result, 'application/json')
            # Native form submissions still work without JavaScript.
            return _safe_redirect(handler, ('%s connected' if ok else '%s connection failed') % provider.upper())
        with _LOCK:
            stored = _read(config)
            if action == 'clear_pin':
                stored['tvdb_pin'] = ''
            elif action == 'remove':
                # Explicit empty override disables even a Docker fallback key.
                stored[provider + '_key'] = ''
                if provider == 'tvdb':
                    stored['tvdb_pin'] = ''
            elif action == 'fallback':
                stored.pop(provider + '_key', None)
                if provider == 'tvdb':
                    stored.pop('tvdb_pin', None)
            else:
                key = form.get('key', [''])[0].strip()
                pin = form.get('pin', [''])[0].strip()
                if key:
                    stored[provider + '_key'] = _validate_credential(key)
                if provider == 'tvdb' and pin:
                    stored['tvdb_pin'] = _validate_credential(pin)
                if not key and not pin:
                    return _safe_redirect(handler, 'No changes - enter a key to save')
            _write(config, stored)
        return _safe_redirect(handler, 'Poster connection settings updated')
    except (UnicodeDecodeError, ValueError, OSError, json.JSONDecodeError):
        return handler.send(400, 'Could not update poster settings. Check the input or config permissions.', 'text/plain')
