# SPDX-License-Identifier: GPL-3.0-or-later
"""Library Health / Path Validator (Themearr V1.9.3, ROADMAP Feature 15).

Strictly read-only: it never writes test files, never changes folders, theme files, Docker mappings,
permissions or media-server settings. It reports and explains; fixing stays with the user (or with
existing, confirmed tools such as Permissions → Fix existing theme.mp3 files).

* Root folders  — exists / readable / writable (os.access + statvfs only), free space, mount source and
                  read-only mounts from /proc/self/mountinfo.
* Path mapping  — Host (mount info) → Themearr → Plex / Jellyfin / Emby → Sonarr / Radarr. Media-server
                  paths come from the server's own API for up to SAMPLE already-mapped titles per root.
                  Anything that can't be checked is shown as "Not verified" with the reason.
* Health scan   — background, read-only walk of every registered title folder (see FINDINGS).
* preflight()   — checks one destination right before an install (writable, enough free space).
"""
import html
import json
import os
import re
import stat
import threading
import time
import urllib.parse
from collections import Counter
from pathlib import Path, PurePosixPath

FILE = 'themearr_library_health.json'
SAMPLE = 8                # media-server items checked per root per scan
SPACING = 0.25            # seconds between media-server requests (throttle)
MIN_FREE = 50 * 1024 * 1024
TINY = 10 * 1024          # a real theme.mp3 is far bigger than this
MAX_FINDINGS = 5000
PAGE = 100
OTHER_THEME_EXT = ('.ogg', '.flac', '.m4a', '.wav', '.opus', '.aac', '.wma', '.webm')
_LOCK = threading.Lock()
_RUN = {'running': False, 'done': 0, 'total': 0, 'stage': ''}

# kind → (severity, title, what it means, what to do)
FINDINGS = {
    'missing_folder': ('warn', 'Folder missing', 'Preludarr has this title in its list, but the folder is gone (moved, renamed or deleted).',
                       'Scan the root folder (Media Management) so the list matches the disk again.'),
    'symlink_folder': ('warn', 'Folder is a symlink', 'Preludarr never installs through symlinks, so this title can’t get a theme.',
                       'Point the library at the real folder instead of a link.'),
    'unreadable': ('bad', 'Folder not readable', 'Preludarr can’t look inside this folder.', 'Check the folder’s permissions on the server.'),
    'unwritable': ('bad', 'Folder not writable', 'Preludarr can’t create theme.mp3 here.',
                   'Check the folder’s permissions, and that the Docker mapping is not read-only.'),
    'theme_symlink': ('warn', 'theme.mp3 is a symlink', 'Media servers may follow it somewhere unexpected; Preludarr won’t replace it.',
                      'Replace the link with a real file (e.g. install again from the Theme Browser after removing it).'),
    'theme_not_file': ('bad', 'theme.mp3 is not a file', 'Something named theme.mp3 exists but is a folder or special file.',
                       'Rename or remove it so a real theme can be installed.'),
    'theme_tiny': ('warn', 'theme.mp3 looks broken', 'The file is empty or far too small to be a song.',
                   'Open the title in the Theme Browser and install the theme again.'),
    'duplicate_theme': ('warn', 'Extra theme file', 'Another theme file sits beside theme.mp3. Some players pick the wrong one.',
                        'Keep one theme per folder: remove or rename the extra file.'),
    'nested_theme': ('info', 'theme.mp3 in a sub-folder', 'A theme.mp3 inside a season/extras folder. Plex/Jellyfin only use the one in the title folder.',
                     'Move it to the title folder if it is the right song, or remove it.'),
    'stray_temp': ('info', 'Leftover temporary file', 'A temporary file from an interrupted install is still in the folder.',
                   'Safe to delete .themearr-theme.tmp once no install is running.'),
    'state_mismatch': ('info', 'List out of date', 'Preludarr’s list and the folder disagree about whether a theme is installed.',
                       'Scan the root folder (Media Management) to refresh the list.'),
    'wrong_owner': ('info', 'Wrong owner / group', 'theme.mp3 belongs to a different user or group than your Permissions settings (other programs may not be able to change it).',
                    'Use Media Management → Permissions → Fix existing theme.mp3 files → “Owner and group only”.'),
    'readonly_root': ('bad', 'Library mounted read-only', 'The whole root folder is mounted read-only in Docker, so Preludarr can’t install any theme there.',
                      'Make the volume read/write in your Docker settings (Unraid: Docker → Preludarr → Edit; Compose: remove “:ro”).'),
}
SEV_ORDER = {'bad': 0, 'warn': 1, 'info': 2}


def _e(value):
    return html.escape(str(value), quote=True)


# --------------------------------------------------------------------------- mount info ----
def _unescape_mount(text):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), text)


def mounts_info(path='/proc/self/mountinfo'):
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            left, _, right = line.partition(' - ')
            f = left.split()
            r = right.split()
            if len(f) < 6 or len(r) < 2:
                continue
            out.append({'point': _unescape_mount(f[4]), 'root': _unescape_mount(f[3]), 'opts': f[5].split(','),
                        'fstype': r[0], 'source': _unescape_mount(r[1])})
    except OSError:
        pass
    return out


def mount_for(path, info=None):
    """The mount that holds `path` (longest mount point that is a parent), or None."""
    p = PurePosixPath(str(path))
    best = None
    for m in info if info is not None else mounts_info():
        mp = PurePosixPath(m['point'])
        if p == mp or mp in p.parents:
            if best is None or len(m['point']) > len(best['point']):
                best = m
    return best


# ---------------------------------------------------------------------------- preflight ----
def preflight(folder):
    """Right before an install: the folder must be a real, writable directory with room for a theme. ValueError if not."""
    folder = Path(folder)
    if folder.is_symlink():
        raise ValueError('The title folder is a symlink — Preludarr only installs into real folders.')
    if not folder.is_dir():
        raise ValueError('The title folder no longer exists — scan the root folder to refresh the list.')
    if not os.access(folder, os.W_OK | os.X_OK):
        m = mount_for(folder)
        if m and 'ro' in m['opts']:
            raise ValueError('The library is mounted read-only in Docker — make the mapping read/write to install themes.')
        raise ValueError('Preludarr can’t write to this folder — check its permissions.')
    try:
        st = os.statvfs(folder)
        if st.f_bavail * st.f_frsize < MIN_FREE:
            raise ValueError('Not enough free space on this disk (under 50 MB) — free some space first.')
    except OSError:
        pass


# ---------------------------------------------------------------------------- roots ----
def roots_report(app):
    import root_folders as rf
    info = mounts_info()
    out = []
    for label, root in list(app.ROOTS):
        st = rf.stats(app, root)
        m = mount_for(root, info)
        out.append({'path': str(root), 'type': label, 'name': rf._NAMES.get(str(root), label), 'exists': st['exists'],
                    'readable': st['readable'], 'writable': st['writable'], 'free': st['free'], 'folders': st['folders'],
                    'in_library': st['in_library'], 'themes': st['themes'], 'unmapped': len(st['unmapped']),
                    'mount_root': (m or {}).get('root', ''), 'mount_source': (m or {}).get('source', ''),
                    'mount_ro': bool(m and 'ro' in m['opts']), 'mount_point': (m or {}).get('point', '')})
    return out


# ------------------------------------------------------------------- media-server paths ----
def _json_or_xml(raw):
    """Plex answers JSON or XML; Jellyfin/Emby JSON. Return a dict-ish tree (XML → ElementTree element)."""
    raw = raw.lstrip(b'\xef\xbb\xbf\t\r\n ')
    if raw.startswith(b'{'):
        return json.loads(raw)
    import xml.etree.ElementTree as ET
    return ET.fromstring(raw)


def _plex_item_folder(raw):
    """Folder Plex has for one item: a show's Location, or the parent folder of a movie's file."""
    doc = _json_or_xml(raw)
    if isinstance(doc, dict):
        items = (doc.get('MediaContainer') or {}).get('Metadata') or []
        if not items:
            return None
        it = items[0]
        loc = it.get('Location') or []
        if loc and loc[0].get('path'):
            return loc[0]['path']
        for media in it.get('Media') or []:
            for part in media.get('Part') or []:
                if part.get('file'):
                    return str(PurePosixPath(part['file']).parent)
        return None
    for tag in ('Directory', 'Video'):
        node = doc.find(tag)
        if node is not None:
            loc = node.find('Location')
            if loc is not None and loc.get('path'):
                return loc.get('path')
            part = node.find('Media/Part')
            if part is not None and part.get('file'):
                return str(PurePosixPath(part.get('file')).parent)
    return None


def _plex_sections(raw):
    doc = _json_or_xml(raw)
    out = []
    if isinstance(doc, dict):
        for d in (doc.get('MediaContainer') or {}).get('Directory') or []:
            out += [loc.get('path') for loc in d.get('Location') or [] if loc.get('path')]
    else:
        out += [loc.get('path') for loc in doc.iter('Location') if loc.get('path')]
    return out


def _jf_item_folder(raw):
    doc = json.loads(raw)
    items = doc.get('Items') if isinstance(doc, dict) and 'Items' in doc else [doc]
    if not items or not isinstance(items[0], dict):
        return None
    path = items[0].get('Path') or ''
    if not path:
        return None
    p = PurePosixPath(path.replace('\\', '/'))
    return str(p if items[0].get('IsFolder', True) else p.parent)


def media_paths(app):
    """Per media server and root: what the server calls that root, from a throttled sample of mapped titles."""
    import neterrors
    import media_servers as v4
    names = {'plex': 'Plex', 'jellyfin': 'Jellyfin', 'emby': 'Emby'}
    out = {}
    try:
        servers = v4._server_map(app.CONFIG)
    except Exception:
        servers = {}
    for kind in ('plex', 'jellyfin', 'emby'):
        server = servers.get(kind)
        rec = {'name': names[kind], 'state': 'not_set_up', 'detail': 'Not set up (Settings → Connect).', 'roots': {}, 'sections': []}
        out[kind] = rec
        if not server:
            continue
        if not server.get('enabled'):
            rec.update(state='off', detail='Turned off on Settings → Connect — not checked.')
            continue
        mappings = server.get('mappings') or {}
        try:
            if kind == 'plex':
                rec['sections'] = _plex_sections(v4._call(server, 'GET', '/library/sections', timeout=10))
            rec.update(state='ok', detail='Connected.')
        except Exception as error:
            rec.update(state='error', detail=neterrors.text(error, names[kind]))
            continue
        for _, root in app.ROOTS:
            r = {'state': 'unverified', 'detail': '', 'server_root': '', 'agree': 0, 'checked': 0, 'mapped': 0}
            rec['roots'][str(root)] = r
            in_root = [(p, i) for p, i in mappings.items() if PurePosixPath(p).parent == PurePosixPath(str(root)) and v4._item_ok(kind, str(i))]
            r['mapped'] = len(in_root)
            if not in_root:
                r['detail'] = 'No titles in this root are linked to ' + names[kind] + ' items yet, so the path can’t be checked.'
                continue
            parents = Counter()
            step = max(1, len(in_root) // SAMPLE)
            for path, item in sorted(in_root)[::step][:SAMPLE]:
                time.sleep(SPACING)
                try:
                    if kind == 'plex':
                        folder = _plex_item_folder(v4._call(server, 'GET', '/library/metadata/' + str(item), timeout=10))
                    else:
                        prefix = '/emby' if kind == 'emby' else ''
                        folder = _jf_item_folder(v4._call(server, 'GET', prefix + '/Items?' + urllib.parse.urlencode({'Ids': item, 'Fields': 'Path'}), timeout=10))
                except Exception:
                    folder = None
                r['checked'] += 1
                if folder and PurePosixPath(folder).name == PurePosixPath(path).name:
                    r['agree'] += 1
                    parents[str(PurePosixPath(folder).parent)] += 1
            if parents:
                server_root, n = parents.most_common(1)[0]
                r['server_root'] = server_root
                if r['agree'] * 2 >= r['checked'] and n * 2 >= r['checked']:
                    r['state'] = 'verified'
                    r['detail'] = (f"{r['agree']} of {r['checked']} sampled titles found in the same folder in {names[kind]}.")
                    if kind == 'plex' and rec['sections'] and server_root not in rec['sections']:
                        r['detail'] += ' (Not one of Plex’s library folders — it may be a sub-folder.)'
                else:
                    r['detail'] = f"Only {r['agree']} of {r['checked']} sampled titles matched — some links may point at the wrong title."
            else:
                r['detail'] = f"None of {r['checked']} sampled titles could be found in {names[kind]} — the item links may be out of date."
    return out


def arr_paths(app):
    import arr
    out = []
    for inst in arr.load(app.CONFIG)['instances']:
        st = inst.get('status') or {}
        out.append({'name': inst.get('name', ''), 'kind': arr.KINDS[inst['kind']][0], 'enabled': inst.get('enabled', True),
                    'ok': st.get('ok'), 'status': st.get('text', ''), 'mappings': inst.get('mappings', [])})
    return out


# ---------------------------------------------------------------------------- scan ----
def _wanted_perms(app):
    try:
        import permissions as pm
        s = pm.settings(app.CONFIG)
        if not s['enabled']:
            return None
        return int(pm.file_mode(s['folder_mode']), 8), (int(s['user']) if s['user'] else -1), (int(s['group']) if s['group'] else -1)
    except Exception:
        return None


def _check_folder(folder, state, perms, found, ro_mount=False):
    def add(kind, detail=''):
        found.append({'kind': kind, 'path': str(folder), 'detail': detail})
    try:
        lst = os.lstat(folder)
    except FileNotFoundError:
        add('missing_folder'); return
    except OSError:
        add('unreadable'); return
    if stat.S_ISLNK(lst.st_mode):
        add('symlink_folder'); return
    if not stat.S_ISDIR(lst.st_mode):
        add('missing_folder', 'It is a file, not a folder.'); return
    if not os.access(folder, os.R_OK | os.X_OK):
        add('unreadable'); return
    if not ro_mount and not os.access(folder, os.W_OK):
        add('unwritable')
    try:
        entries = list(os.scandir(folder))
    except OSError:
        add('unreadable'); return
    names = {e.name: e for e in entries}
    theme = names.get('theme.mp3')
    has_theme = False
    if theme is not None:
        try:
            tst = os.lstat(theme.path)
            if stat.S_ISLNK(tst.st_mode):
                add('theme_symlink')
            elif not stat.S_ISREG(tst.st_mode):
                add('theme_not_file')
            else:
                has_theme = tst.st_size > 0
                if tst.st_size < TINY:
                    add('theme_tiny', f'{tst.st_size} bytes')
                if perms:
                    mode, uid, gid = perms
                    if (uid >= 0 and tst.st_uid != uid) or (gid >= 0 and tst.st_gid != gid):  # mode-only differences are harmless
                        add('wrong_owner', f'owner {tst.st_uid}:{tst.st_gid}')
        except OSError:
            add('unreadable')
    for name in names:
        low = name.lower()
        if name != 'theme.mp3' and low.startswith('theme.') and low.endswith(OTHER_THEME_EXT + ('.mp3',)):
            add('duplicate_theme', name)
    if '.themearr-theme.tmp' in names:
        add('stray_temp')
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith('.'):
                if os.path.lexists(os.path.join(e.path, 'theme.mp3')):
                    add('nested_theme', e.name + '/theme.mp3')
        except OSError:
            continue
    if state is not None and (state == 'installed') != has_theme:
        add('state_mismatch', 'List says ' + ('installed' if state == 'installed' else state) + ', folder ' + ('has' if has_theme else 'has no') + ' theme.mp3')


def _load(config):
    try:
        data = json.loads((Path(config) / FILE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(config, data):
    path = Path(config) / FILE
    tmp = path.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out)
    os.replace(tmp, path)


def scan(app, check_servers=True):
    """Read-only health scan of every registered title folder. Saves and returns the report."""
    started = time.time()
    roots = [root for _, root in app.ROOTS]
    with app.db() as c:
        rows = [(r['path'], r['state']) for r in c.execute('SELECT path,state FROM shows ORDER BY path')]
    rows = [(p, s) for p, s in rows if any(PurePosixPath(p).parent == PurePosixPath(str(r)) for r in roots)]
    perms = _wanted_perms(app)
    found = []
    info = mounts_info()
    ro_roots = {str(r) for r in roots if (lambda m: bool(m and 'ro' in m['opts']))(mount_for(r, info))}
    for r in sorted(ro_roots):
        found.append({'kind': 'readonly_root', 'path': r, 'detail': '', 'root': True})
    _RUN.update(total=len(rows), done=0, stage='Checking title folders')
    for path, state in rows:
        _check_folder(path, state, perms, found, str(PurePosixPath(path).parent) in ro_roots)
        _RUN['done'] += 1
    report = {'at': int(time.time()), 'roots': roots_report(app), 'arr': arr_paths(app)}
    if check_servers:
        _RUN['stage'] = 'Checking media-server paths'
        report['media'] = media_paths(app)
    else:
        report['media'] = _load(app.CONFIG).get('media', {})
    found.sort(key=lambda f: (SEV_ORDER[FINDINGS[f['kind']][0]], f['kind'], f['path']))
    report['counts'] = dict(Counter(f['kind'] for f in found))
    report['findings'] = found[:MAX_FINDINGS]
    report['checked'] = len(rows)
    report['duration'] = round(time.time() - started, 1)
    _save(app.CONFIG, report)
    return report


def refresh_title(app, path):
    """Re-check one title folder after a change and update the saved report (V1.9.4)."""
    with _LOCK:
        report = _load(app.CONFIG)
        if not report.get('at'):
            return
        with app.db() as c:
            row = c.execute('SELECT state FROM shows WHERE path=?', (str(path),)).fetchone()
        fresh = []
        _check_folder(str(path), row['state'] if row else None, _wanted_perms(app), fresh)
        found = [f for f in report.get('findings', []) if f['path'] != str(path)] + fresh
        found.sort(key=lambda f: (SEV_ORDER[FINDINGS[f['kind']][0]], f['kind'], f['path']))
        report['findings'] = found
        report['counts'] = dict(Counter(f['kind'] for f in found))
        _save(app.CONFIG, report)


def start_scan(app):
    with _LOCK:
        if _RUN['running']:
            raise ValueError('A health scan is already running.')
        _RUN.update(running=True, done=0, total=0, stage='Starting', error='')

    def worker():
        try:
            scan(app)
            app.event('Library Health', 'scanned', 'Health scan finished')
        except Exception as error:  # never let the worker die silently
            _RUN['error'] = type(error).__name__
            print('Library health scan failed: ' + type(error).__name__, flush=True)
        finally:
            _RUN['running'] = False
    threading.Thread(target=worker, name='themearr-library-health', daemon=True).start()


def status():
    return {k: _RUN.get(k) for k in ('running', 'done', 'total', 'stage', 'error')}


def title_findings(config, path):
    """Findings of the last scan for one title (for the Theme Browser warning)."""
    return [f for f in _load(config).get('findings', []) if f['path'] == str(path)]


def title_box(config, path):
    items = [f for f in title_findings(config, path) if FINDINGS[f['kind']][0] != 'info' or f['kind'] in ('duplicate_theme', 'nested_theme')]
    if not items:
        return ''
    parts = ['<div class="panel" id="themearr-health-note" role="note" style="border-color:#e0b85f;background:#3a321c">',
             '<h3 style="margin-top:0">⚠ Library Health</h3><ul style="margin:0;padding-left:18px">']
    for f in items:
        sev, title, meaning, fix = FINDINGS[f['kind']]
        parts += ['<li><strong>', _e(title), '</strong>', (' — <code>' + _e(f['detail']) + '</code>' if f['detail'] else ''), ': ', _e(meaning), ' ', _e(fix), '</li>']
    review = any(f['kind'] in ('duplicate_theme', 'nested_theme') for f in items)
    parts += ['</ul><p style="margin:8px 0 0">', ('<a href="/library-health/review?' + _e(urllib.parse.urlencode({'path': str(path)})) + '">Review theme files</a> · ' if review else ''),
              '<a href="/system?tab=health#findings">Open Library Health</a></p></div>']
    return ''.join(parts)


# ------------------------------------------------------------------------------ page ----
def _badge(ok, yes='OK', no='Problem', unknown=False):
    if unknown:
        return '<span class="lh-badge unk">' + _e(no) + '</span>'
    return '<span class="lh-badge ' + ('ok' if ok else 'bad') + '">' + _e(yes if ok else no) + '</span>'


def body(app, query):
    """Library Health content for System → Library Health (V1.9.5). Returns HTML (no page shell)."""
    import root_folders as rf
    q = query or {}
    notice = q.get('notice', [''])[0][:240]
    kind_filter = q.get('kind', [''])[0]
    try:
        page_no = max(1, int(q.get('page', ['1'])[0]))
    except ValueError:
        page_no = 1
    report = _load(app.CONFIG)
    roots = roots_report(app)  # cheap, always live
    run = status()
    parts = ['<div class="lh-scope"><div class="lh-heading"><div><h2 class="lh-h">Library Health</h2>',
             '<p class="muted">Checks that Preludarr can reach and write your library folders, that Plex/Jellyfin and Sonarr/Radarr see the same folders, '
             'and looks for broken or duplicate theme files. <strong>Read-only — nothing is changed.</strong></p></div>',
             '<div class="lh-head-actions"><a class="btn" href="/library-health/trash">Recycle bin</a>',
             '<form method="post" action="/library-health/scan"><button type="submit" class="btn primary"', ' disabled' if run['running'] else '', '>',
             'Scanning…' if run['running'] else 'Run health scan', '</button></form></div></div>']
    if notice:
        parts += ['<p class="notice" role="status">', _e(notice), '</p>']
    if run['running']:
        parts += ['<p class="notice" role="status" id="lh-run" data-running="1">', _e(run['stage']), '… ', str(run['done']), ' / ', str(run['total']), '</p>']
    elif run.get('error'):
        parts += ['<p class="notice lh-bad" role="alert">The last scan stopped early (', _e(run['error']), '). Try again; if it repeats, check the Logs page.</p>']

    # Root folders
    parts += ['<section class="panel" id="roots"><h2>Root folders</h2><div class="lh-grid">']
    for r in roots:
        parts += ['<div class="lh-card"><div class="lh-title"><strong>', _e(r['name']), '</strong> <small class="muted">', _e(r['type']), '</small></div>',
                  '<code>', _e(r['path']), '</code><ul class="lh-checks">',
                  '<li>', _badge(r['exists'], 'Found', 'Missing'), ' Folder exists</li>',
                  '<li>', _badge(r['readable'], 'Yes', 'No'), ' Preludarr can read it</li>',
                  '<li>', _badge(r['writable'], 'Yes', 'No'), ' Preludarr can write theme.mp3 here',
                  (' <small class="lh-bad-text">— mounted read-only in Docker</small>' if r['mount_ro'] else ''), '</li></ul>',
                  '<dl class="lh-dl"><dt>Free space</dt><dd>', _e(rf._size(r['free'])), '</dd>',
                  '<dt>Title folders</dt><dd>', str(r['folders']), ' · ', str(r['in_library']), ' in library · ', str(r['themes']), ' with theme · ',
                  str(r['unmapped']), ' not scanned yet</dd>',
                  '<dt>Docker source</dt><dd>', ('<code>…' + _e(r['mount_root']) + '</code> <small class="muted">inside <code>' + _e(r['mount_source']) + '</code> '
                                                   '(what Docker reports; your Unraid share path ends the same way)</small>'
                                                   if r['mount_root'] else '<span class="muted">Not verified — no mount information.</span>'), '</dd></dl></div>']
    parts += ['</div></section>']

    # Path mapping
    media = report.get('media') or {}
    arr = report.get('arr') if report else None
    parts += ['<section class="panel" id="mapping"><h2>Path mapping</h2>',
              '<p class="muted">The same folder can have a different path in every program. These are checked by asking each program about a few titles; '
              'anything that can’t be checked says “Not verified”.', (' Last checked ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(report['at'])) + '.' if report.get('at') else
                                                                      ' <strong>Run a health scan to check Plex/Jellyfin.</strong>'), '</p>',
              '<div class="lh-table" role="table"><div class="lh-row lh-head" role="row"><span>Preludarr</span><span>Docker source</span>']
    kinds = [k for k in ('plex', 'jellyfin', 'emby') if (media.get(k) or {}).get('state') not in (None, 'not_set_up')]
    for k in kinds:
        parts += ['<span>', _e(media[k]['name']), '</span>']
    parts += ['<span>Sonarr / Radarr</span></div>']
    for r in roots:
        parts += ['<div class="lh-row" role="row"><span><small class="lh-cl">Preludarr</small><code>', _e(r['path']), '</code></span><span><small class="lh-cl">Docker source</small>',
                  ('<code>…' + _e(r['mount_root']) + '</code>' if r['mount_root'] else '<span class="muted">Not verified</span>'), '</span>']
        for k in kinds:
            rec = media[k]
            if rec['state'] != 'ok':
                parts += ['<span><small class="lh-cl">', _e(rec['name']), '</small>', _badge(False, no=('Turned off' if rec['state'] == 'off' else 'Not connected'), unknown=rec['state'] == 'off'),
                          '<br><small class="muted">', _e(rec['detail']), '</small></span>']
                continue
            rr = rec['roots'].get(r['path']) or {}
            st = rr.get('state', 'unverified')
            parts += ['<span><small class="lh-cl">', _e(rec['name']), '</small>', (('<code>' + _e(rr['server_root']) + '</code><br>') if rr.get('server_root') else ''),
                      _badge(st == 'verified', 'Verified', 'Not verified', unknown=st != 'verified' and not rr.get('checked')),
                      '<br><small class="muted">', _e(rr.get('detail', '')), '</small></span>']
        links = [] if arr is None else [(a, m) for a in arr for m in a['mappings'] if m['local'] == r['path']]
        if arr is None:
            parts += ['<span class="muted"><small class="lh-cl">Sonarr / Radarr</small>Run a health scan.</span>']
        elif links:
            parts += ['<span><small class="lh-cl">Sonarr / Radarr</small>'] + ['<code>' + _e(m['remote']) + '</code> <small class="muted">(' + _e(a['name']) + ' · ' + _e(a['kind']) + ')</small> '
                                    + _badge(bool(a['ok']), 'Connected', 'Not connected') + '<br>' for a, m in links] + ['</span>']
        else:
            parts += ['<span class="muted"><small class="lh-cl">Sonarr / Radarr</small>No Sonarr/Radarr mapping to this folder.</span>']
        parts += ['</div>']
    parts += ['</div></section>']

    # Findings (V1.9.8: grouped by category, last scan on top, explanations once per group)
    ui = __import__('ui')
    head = ''.join(parts)
    diag_start = head.index('<section class="panel" id="roots">')
    header, diagnostics = head[:diag_start], head[diag_start:]
    f = ['<section class="panel" id="findings"><h2>Findings</h2>']
    if not report.get('at'):
        f += ['<p>No scan yet — click <strong>Run health scan</strong>. It only reads; nothing is changed.</p>']
    else:
        counts = report.get('counts') or {}
        total = sum(counts.values())
        f += ['<p class="lh-when">', ui.label('cached'), ' Last scan <strong>', _e(ui.when(report['at'])), '</strong> · ', str(report.get('checked', 0)),
              ' title folders checked in ', str(report.get('duration', 0)), ' s — ',
              ('<strong>no problems found</strong>.' if not total else '<strong>' + str(total) + '</strong> finding' + ('' if total == 1 else 's') + ' in ' +
               str(len(counts)) + ' categor' + ('y' if len(counts) == 1 else 'ies') + '.'), '</p>']
        kinds = sorted(counts, key=lambda k: (SEV_ORDER[FINDINGS[k][0]], k))
        by = {}
        for item in report.get('findings', []):
            by.setdefault(item['kind'], []).append(item)
        sev_name = {'bad': 'Problem', 'warn': 'Warning', 'info': 'Note'}
        for n, kind in enumerate(kinds):
            sev, title, meaning, fix = FINDINGS[kind]
            items = by.get(kind, [])
            show_all = kind == kind_filter
            pages = max(1, (len(items) + PAGE - 1) // PAGE)
            pg = min(page_no, pages) if show_all else 1
            shown = items[(pg - 1) * PAGE: pg * PAGE] if show_all else items[:20]
            opened = show_all or (not kind_filter and n == 0 and sev != 'info')
            f += ['<details class="lh-group sev-', sev, '" id="kind-', kind, '"', ' open' if opened else '', '><summary>',
                  '<span class="lh-sevtag sev-', sev, '">', sev_name[sev], '</span><span class="lh-gtitle">', _e(title), '</span>',
                  '<span class="lh-count">', str(counts[kind]), '</span></summary>',
                  '<p class="lh-explain sev-', sev, '">', _e(meaning), ' <strong>What to do:</strong> ', _e(fix), '</p>',
                  '<div class="lh-list"><table><tr><th>Title folder</th><th>Details</th></tr>']
            for item in shown:
                cell = (('<strong>Root folder</strong><br><code>' + _e(item['path']) + '</code>') if item.get('root') else
                        ('<a href="/themes?' + _e(urllib.parse.urlencode({'path': item['path']})) + '">' + _e(PurePosixPath(item['path']).name) + '</a><br><small class="muted">'
                         + _e(str(PurePosixPath(item['path']).parent)) + '</small>'))
                f += ['<tr><td>', cell, '</td><td>', ('<code>' + _e(item['detail']) + '</code>' if item['detail'] else '<span class="muted">—</span>'),
                      ('<br><a class="lh-review" href="/library-health/review?' + _e(urllib.parse.urlencode({'path': item['path']})) + '">Review theme files</a>'
                       if kind in ('duplicate_theme', 'nested_theme') else ''), '</td></tr>']
            f += ['</table></div>']
            if not show_all and len(items) > len(shown):
                f += ['<p><a href="/system?', _e(urllib.parse.urlencode({'tab': 'health', 'kind': kind})), '#kind-', kind, '">Show all ', str(len(items)), '</a></p>']
            if show_all and pages > 1:
                base = {'tab': 'health', 'kind': kind}
                f += ['<p class="lh-pager">']
                if pg > 1:
                    f += ['<a href="/system?', _e(urllib.parse.urlencode(dict(base, page=pg - 1))), '#kind-', kind, '">← Previous</a> ']
                f += ['Page ', str(pg), ' / ', str(pages)]
                if pg < pages:
                    f += [' <a href="/system?', _e(urllib.parse.urlencode(dict(base, page=pg + 1))), '#kind-', kind, '">Next →</a>']
                f += ['</p>']
            f += ['</details>']
        f += ['<p class="muted">Library Health only reports. Fixes stay with you — or with tools that ask first, like Review theme files or Permissions → Fix existing theme.mp3 files.</p>']
    f += ['</section>']
    parts = [header, ''.join(f), '<h2 class="lh-diag-h">Folder &amp; path diagnostics</h2>', diagnostics]
    parts += ['</div>', STYLE, SCRIPT]
    return ''.join(parts)


def page(app, handler):
    """Old address /library-health → System → Library Health (keeps kind/page/notice)."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    keep = {k: q[k][0] for k in ('kind', 'page', 'notice') if q.get(k)}
    handler.send_response(303)
    handler.send_header('Location', '/system?' + urllib.parse.urlencode(dict({'tab': 'health'}, **keep)) + '#findings')
    handler.send_header('Content-Length', '0')
    handler.end_headers()


STYLE = ('<style>.lh-heading{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}.lh-h{margin:0 0 6px;font-size:1.4rem}'
         '.lh-scope .notice{padding:12px;border-left:3px solid #79b7ff;background:#263751;border-radius:6px;margin:12px 0}'
         '.lh-scope a.btn{display:inline-block;padding:11px 17px;border-radius:7px;border:1px solid #5f7fb0;color:#dce9ff;font-weight:700;text-decoration:none}'
         '.lh-scope a.btn:hover{background:#2c3f62;text-decoration:none}'
         '.lh-group{border:1px solid #36486a;border-radius:10px;margin:10px 0;background:#18243b}.lh-group>summary{display:flex;gap:10px;align-items:center;padding:12px 14px;cursor:pointer;list-style:none}'
         '.lh-group>summary::-webkit-details-marker{display:none}.lh-group>summary::after{content:"▸";margin-left:auto;color:#b4c4dc}.lh-group[open]>summary::after{content:"▾"}'
         '.lh-group[open]>summary{border-bottom:1px solid #2b3a55}.lh-group .lh-explain,.lh-group .lh-list,.lh-group>p{margin:10px 14px}'
         '.lh-sevtag{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;padding:2px 8px;border-radius:6px;background:#2a3a57;color:#cfe0f8}'
         '.lh-sevtag.sev-bad{background:#4a2430;color:#ffc4cc}.lh-sevtag.sev-warn{background:#4a3a1c;color:#ffe2a8}.lh-gtitle{font-weight:700}'
         '.lh-explain.sev-bad{border:1px solid #e07a86;background:#4a2430;padding:10px;border-radius:8px}.lh-explain.sev-warn{border:1px solid #e0b85f;background:#4a3d1f;padding:10px;border-radius:8px}'
         '.lh-explain.sev-info{border:1px solid #617aa2;background:#2b3c5c;padding:10px;border-radius:8px}'
         '.lh-count{min-width:28px;text-align:center;padding:2px 9px;border-radius:999px;background:#2a3c5e;font-weight:800}'
         '.lh-when{margin:6px 0 12px}.lh-diag-h{margin:26px 0 4px;font-size:18px;color:#cfe0f8}'
         '.lh-head-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.lh-head-actions form{margin:0}'
         '.lh-review{display:inline-block;margin-top:6px;font-weight:700}.lh-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:14px}'
         '.lh-card{padding:16px;border:1px solid var(--line,#3b4b6b);border-radius:10px;background:#1c2a44}'
         '.lh-card code{overflow-wrap:anywhere;color:#9fd0ff}.lh-title{margin-bottom:6px}'
         '.lh-checks{list-style:none;padding:0;margin:10px 0}.lh-checks li{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}'
         '.lh-badge{display:inline-block;min-width:64px;text-align:center;padding:2px 8px;border-radius:999px;font-size:.85em;font-weight:700}'
         '.lh-badge.ok{background:#2f8f5b;color:#fff}.lh-badge.bad{background:#b84052;color:#fff}.lh-badge.unk{background:#5d6b85;color:#fff}'
         '.lh-bad-text{color:#ffb4b4}.lh-bad{border-color:#e07a86!important;background:#4a2430!important}'
         '.lh-dl{display:grid;grid-template-columns:110px minmax(0,1fr);gap:4px 12px;margin:8px 0 0}.lh-dl dt{color:var(--muted,#adbed9)}.lh-dl dd{margin:0}'
         '.lh-table{display:flex;flex-direction:column;gap:6px;overflow-x:auto}'
         '.lh-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;padding:10px;border:1px solid var(--line,#3b4b6b);border-radius:9px;align-items:start}'
         '.lh-row code{overflow-wrap:anywhere;color:#9fd0ff}.lh-head{border-color:transparent;font-weight:700;color:var(--muted,#adbed9);padding-top:0;padding-bottom:0}'
         '.lh-kinds{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}#findings a.lh-kind,#findings a.lh-kind:hover{text-decoration:none!important}.lh-kind{padding:6px 10px;border-radius:999px;border:1px solid #5d6b85;color:#dce9ff;text-decoration:none}'
         '.lh-kind.bad{border-color:#e07a86}.lh-kind.warn{border-color:#e0b85f}.lh-kind.active{background:#3468de;border-color:#3468de;color:#fff}'
         '.lh-explain{padding:10px;border-radius:8px;border:1px solid #617aa2;background:#2b3c5c}.lh-explain.bad{border-color:#e07a86;background:#4a2430}.lh-explain.warn{border-color:#e0b85f;background:#4a3d1f}'
         '.lh-list{overflow-x:auto}.lh-list table{width:100%;border-collapse:collapse}.lh-list th,.lh-list td{text-align:left;padding:8px 10px 8px 0;vertical-align:top;border-bottom:1px solid #2b3a55}'
         '.lh-list a{text-decoration:none}.lh-list a:hover{text-decoration:underline}.lh-list code{overflow-wrap:anywhere}.lh-cl{display:none;color:var(--muted,#adbed9);font-weight:700;margin-bottom:2px}.lh-sev{font-weight:700}.lh-sev.bad{color:#ffb4b4}.lh-sev.warn{color:#ffd36b}.lh-sev.info{color:#b9d4ff}'
         '@media(max-width:760px){.lh-head{display:none}.lh-row{grid-template-columns:1fr}.lh-cl{display:block}}</style>')

SCRIPT = ('<script>(function(){const el=document.getElementById("lh-run");if(!el||!el.dataset.running)return;'
          'const lhT=setInterval(async()=>{try{const r=await fetch("/library-health/status",{cache:"no-store"});if((r.status===401||(r.redirected&&new URL(r.url).pathname===\"/login\"))){clearInterval(lhT);el.textContent="Signed out — reload to sign in again.";return}const s=await r.json();'
          'if(!s.running){location.reload();return}el.textContent=s.stage+"… "+s.done+" / "+s.total}catch(e){}},1500)})();</script>')


def post(app, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0') or 0)
        if size > 256:
            raise ValueError('Invalid form.')
        if size:
            handler.rfile.read(size)
        start_scan(app)
        msg = 'Health scan started — it only reads, nothing is changed.'
    except ValueError as error:
        msg = str(error)
    return redirect(handler, '/system?' + urllib.parse.urlencode({'tab': 'health', 'notice': msg}))
