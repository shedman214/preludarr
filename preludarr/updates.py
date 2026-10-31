# SPDX-License-Identifier: GPL-3.0-or-later
"""The "Update available" notice (Preludarr 1.12, pre-release task 3).

Once a day (and on "Check now") Preludarr asks GitHub for the project's published releases and compares versions.
  * Only the public releases list is requested (https://api.github.com/repos/<repo>/releases); no account, key, library or
    usage data is sent. GitHub sees the request like any web request (the server's IP address and a Preludarr User-Agent).
  * Nothing is ever downloaded or installed: the user updates the Docker image themselves.
  * Pre-releases (betas) are offered only to people already running a pre-release.
  * Off switch: Settings in System → Releases, or the environment variable PRELUDARR_UPDATE_CHECK=0 (forces it off).
"""
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FILE = 'themearr_updates.json'
REPO = os.environ.get('PRELUDARR_UPDATE_REPO') or os.environ.get('SPLICARR_UPDATE_REPO') or 'shedman214/preludarr'
API = 'https://api.github.com/repos/{repo}/releases?per_page=30'
INTERVAL = 24 * 3600
FIRST_DELAY = 120
MAX_BYTES = 1024 * 1024
_LOCK = threading.Lock()
_BG = {'thread': None}


def _e(value):
    return html.escape(str(value), quote=True)


def _cfg():
    return Path(os.environ.get('CONFIG_DIR', '/config'))


def _read():
    try:
        p = _cfg() / FILE
        data = json.loads(p.read_text()) if p.is_file() and not p.is_symlink() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    p = _cfg() / FILE
    tmp = p.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, p)


def forced_off():
    return any(os.environ.get(k, '').strip().lower() in ('0', 'false', 'no', 'off') for k in ('PRELUDARR_UPDATE_CHECK', 'SPLICARR_UPDATE_CHECK'))


def enabled():
    return not forced_off() and _read().get('enabled', True) is not False


# ------------------------------------------------------------------------------ versions ----
_VER = re.compile(r'v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$')


def parse(text):
    """Comparable key for a semantic version ('1.12.0', '1.12.0-beta.2', 'v1.12.0', '1.12.0-dev'), or None."""
    m = _VER.match(str(text or '').strip())
    if not m:
        return None
    major, minor, patch, pre = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if not pre:
        return (major, minor, patch, 1, ())
    if pre == 'dev' or pre.startswith('dev.'):  # development builds sort before every alpha/beta/rc of that version
        return (major, minor, patch, 0, ((-1, 0, ''),))
    parts = tuple((0, int(x), '') if x.isdigit() else (1, 0, x) for x in pre.split('.'))
    return (major, minor, patch, 0, parts)


def is_prerelease(text):
    k = parse(text)
    return bool(k) and k[3] == 0


def newer(candidate, current):
    a, b = parse(candidate), parse(current)
    return bool(a and b and a > b)


# --------------------------------------------------------------------------------- check ----
def _fetch(repo):
    url = API.format(repo=urllib.parse.quote(repo, safe='/'))
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                                               'User-Agent': 'Preludarr update check'})
    with urllib.request.urlopen(req, timeout=20) as r:
        if urllib.parse.urlsplit(r.geturl()).hostname != 'api.github.com':
            raise ValueError('unexpected redirect')
        body = r.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('response too large')
        data = json.loads(body)
    return data if isinstance(data, list) else []


def pick(releases, current):
    """The newest published release newer than `current` (pre-releases only for pre-release users), or None."""
    allow_pre = is_prerelease(current)
    best = None
    for rel in releases:
        if not isinstance(rel, dict) or rel.get('draft'):
            continue
        tag = str(rel.get('tag_name') or '')
        if not parse(tag) or (rel.get('prerelease') or is_prerelease(tag)) and not allow_pre:
            continue
        if newer(tag, current) and (best is None or parse(tag) > parse(best['version'])):
            link = str(rel.get('html_url') or '')
            best = {'version': tag.lstrip('v'), 'url': link if link.startswith('https://github.com/') else 'https://github.com/' + REPO + '/releases',
                    'name': str(rel.get('name') or tag)[:120], 'prerelease': bool(rel.get('prerelease') or is_prerelease(tag))}
    return best


def check(current):
    """Ask GitHub once. Stores {checked_at, latest|None, error} and returns it."""
    with _LOCK:
        state = _read()
        state['checked_at'] = int(time.time())
        try:
            state['latest'] = pick(_fetch(REPO), current)
            state['error'] = ''
        except urllib.error.HTTPError as error:
            state['latest'] = None
            state['error'] = 'No releases published yet.' if error.code == 404 else f'GitHub answered HTTP {error.code}.'
        except Exception as error:
            state['error'] = 'Could not reach GitHub (' + type(error).__name__ + ').'
        _write(state)
        return state


def available(current):
    """The stored newer release (no network), or None."""
    if not enabled():
        return None
    latest = _read().get('latest')
    return latest if isinstance(latest, dict) and newer(latest.get('version'), current) else None


def _loop(current):
    time.sleep(FIRST_DELAY)
    while True:
        try:
            if enabled() and time.time() - int(_read().get('checked_at') or 0) >= INTERVAL - 300:
                check(current)
        except Exception as error:
            print('Update check error: ' + type(error).__name__, flush=True)
        time.sleep(3600)


def start_background(current):
    if _BG['thread'] is None or not _BG['thread'].is_alive():
        _BG['thread'] = threading.Thread(target=_loop, args=(current,), daemon=True, name='preludarr-update-check')
        _BG['thread'].start()


# ------------------------------------------------------------------------------------ UI ----
def footer_html(current):
    a = available(current)
    if not a:
        return ''
    return ('<a class="tm-update" href="/system?tab=releases" title="A newer Preludarr release is available">⬆ Update available: '
            + _e(a['version']) + '</a><style>.tm-update{display:block;margin:6px 0 0;font-size:12.5px;font-weight:700;color:#ffd38a!important}</style>')


def panel_html(current, notice=''):
    s = _read()
    a = available(current)
    when = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(s['checked_at']))) if s.get('checked_at') else 'never'
    if forced_off():
        status = 'Turned off by the PRELUDARR_UPDATE_CHECK=0 setting in Docker.'
    elif not enabled():
        status = 'Turned off.'
    elif a:
        status = ('⬆ <strong>Preludarr ' + _e(a['version']) + '</strong> is available' + (' (beta)' if a.get('prerelease') else '') +
                  ' — <a href="' + _e(a['url']) + '" target="_blank" rel="noopener noreferrer">release notes ↗</a>. Update by pulling the new Docker image.')
    elif s.get('error'):
        status = _e(s['error'])
    elif s.get('checked_at'):
        status = '✅ You are on the newest release.'
    else:
        status = 'Not checked yet.'
    return ''.join([
        '<section class="panel" id="updates"><h2>Updates</h2>',
        '<p class="notice" role="status">' + _e(notice) + '</p>' if notice else '',
        '<p><strong>Running:</strong> Preludarr ', _e(current), ' · <strong>Last check:</strong> ', _e(when), '</p><p>', status, '</p>',
        '<p class="muted">Once a day Preludarr asks GitHub for the list of published Preludarr releases (', _e(REPO), '). Nothing about you or your library is sent, '
        'and nothing is downloaded or installed automatically.', ' Beta releases are offered because you are running a beta.' if is_prerelease(current) else '', '</p>',
        '<form method="post" action="/system/updates/check" style="display:inline-block;margin-right:10px"><button type="submit"', ' disabled' if not enabled() else '',
        '>Check now</button></form>',
        '' if forced_off() else ('<form method="post" action="/system/updates/settings" style="display:inline-block"><input type="hidden" name="enabled" value="' +
                                 ('0' if enabled() else '1') + '"><button type="submit" class="button" style="background:#46546d">' +
                                 ('Turn off update checks' if enabled() else 'Turn on update checks') + '</button></form>'),
        '</section>'])


def post(handler, redirect, current):
    path = urllib.parse.urlsplit(handler.path).path
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 256:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), max_num_fields=2)
        if path == '/system/updates/settings':
            data = _read()
            data['enabled'] = form.get('enabled', ['1'])[0] == '1'
            _write(data)
            msg = 'Update checks turned ' + ('on.' if data['enabled'] else 'off.')
        else:
            if not enabled():
                raise ValueError('Update checks are turned off.')
            s = check(current)
            a = available(current)
            msg = ('Preludarr ' + a['version'] + ' is available.') if a else (s.get('error') or 'You are on the newest release.')
    except (ValueError, UnicodeError) as error:
        msg = str(error)
    return redirect(handler, '/system?' + urllib.parse.urlencode({'tab': 'releases', 'update_msg': msg[:200]}) + '#updates')
