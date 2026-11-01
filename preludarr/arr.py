# SPDX-License-Identifier: GPL-3.0-or-later
"""Sonarr & Radarr connections (Themearr V1.8.0) — several instances of each.

Read-only towards Sonarr/Radarr: Themearr only GETs status, root folders, naming config and the series/movie list.
Their own `path` for each item (already built from the user's Series/Movie Folder Format) is used as-is, so a folder
Themearr creates early has exactly the name Sonarr/Radarr will use. Paths are translated with per-instance path
mappings (Sonarr path prefix → Themearr path), like Sonarr's Remote Path Mappings.

Folder creation for media without a folder is optional: per item with confirmation, or automatically for media
added after the user switches that on (off by default). It only ever creates ONE new folder directly inside a
registered root folder, never overwrites or touches anything else, and applies the user's permissions.
API keys are stored privately (0600) and never shown or logged.
"""
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

FILE = 'themearr_arr.json'
KINDS = {'sonarr': ('Sonarr', 'series', 'Anime TV', '#35c5f4'), 'radarr': ('Radarr', 'movie', 'Anime Movies', '#ffc230')}
MAX_INSTANCES = 10
MAX_BYTES = 64 * 1024 * 1024
_LOCK = threading.RLock()
_STATE = {}  # instance id -> last sync summary (in memory)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _e(value):
    return html.escape(str(value), quote=True)


# ------------------------------------------------------------------------------ storage ----
def _path(config):
    return Path(config) / FILE


def load(config):
    try:
        data = json.loads(_path(config).read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault('instances', [])
    data.setdefault('auto_create', False)
    data.setdefault('auto_create_since', 0)
    return data


def _save(config, data):
    path = _path(config)
    tmp = path.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def instance(config, iid):
    return next((i for i in load(config)['instances'] if i.get('id') == iid), None)


# ---------------------------------------------------------------------------------- API ----
def _get(inst, endpoint, timeout=20):
    import media_servers
    base = media_servers._base_url(inst['url'])
    req = urllib.request.Request(base.rstrip('/') + '/api/v3/' + endpoint,
                                 headers={'X-Api-Key': inst['api_key'], 'Accept': 'application/json', 'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'})
    with _OPENER.open(req, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError('HTTP %s' % response.status)
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('response too large')
    return json.loads(raw)


def _error_text(error, inst=None):
    """V1.9.2: plain reason + what to do (neterrors), never the raw exception name only."""
    import neterrors
    app = KINDS[inst['kind']][0] if inst and inst.get('kind') in KINDS else 'Sonarr/Radarr'
    return neterrors.text(error, app, (inst or {}).get('url', ''))


def _set_status(config, iid, ok, text):
    """Remember the last connection result on the instance, so the card always shows it (V1.9.2)."""
    with _LOCK:
        data = load(config)
        inst = next((i for i in data['instances'] if i.get('id') == iid), None)
        if inst:
            inst['status'] = {'ok': bool(ok), 'at': int(time.time()), 'text': str(text)[:300]}
            _save(config, data)


def test(inst):
    import neterrors
    status = _get(inst, 'system/status', timeout=12)
    expected = KINDS[inst['kind']][0]
    found = str(status.get('appName') or '') if isinstance(status, dict) else ''
    if found.lower() != expected.lower():
        raise neterrors.WrongApp('This address is ' + (found or 'not ' + expected) + (', not ' + expected + '.' if found else '.'))
    roots = _get(inst, 'rootfolder', timeout=12)
    try:
        naming = _get(inst, 'config/naming', timeout=12)
    except Exception:
        naming = {}
    return {'app': status.get('appName') or KINDS[inst['kind']][0], 'version': status.get('version', ''),
            'roots': [r.get('path') for r in roots if isinstance(r, dict) and r.get('path')],
            'naming': {k: naming.get(k) for k in ('seriesFolderFormat', 'seasonFolderFormat', 'movieFolderFormat', 'standardMovieFormat', 'animeEpisodeFormat') if naming.get(k)}}


# ------------------------------------------------------------------------------ mapping ----
# 1.12.1 (GitHub issue #1): Sonarr/Radarr on Windows report paths like G:\TV\Show or \\server\share\TV\Show.
_WINDOWS = re.compile(r'^(?:[A-Za-z]:(?:[\\/]|$)|[\\/]{2}[^\\/]+[\\/][^\\/]+)')


def is_windows_path(path):
    return bool(_WINDOWS.match(str(path or '').strip()))


def norm_remote(path):
    """A Sonarr/Radarr path in its stored form. Linux: normpath. Windows: backslashes, '..' resolved, upper-case drive,
    no trailing backslash except on a drive root ('G:\\')."""
    path = str(path or '').strip()
    if not path:
        return path
    if not is_windows_path(path):
        return os.path.normpath(path)
    unc = path[:2] in ('\\\\', '//', '\\/', '/\\')
    keep = 2 if unc else 1  # never climb above the share or the drive
    parts = []
    for part in re.split(r'[\\/]+', path[2:] if unc else path):
        if part in ('', '.'):
            continue
        if part == '..':
            if len(parts) > keep:
                parts.pop()
            continue
        parts.append(part)
    if unc:
        return '\\\\' + '\\'.join(parts)
    return parts[0].upper() + '\\' + '\\'.join(parts[1:])


def _slashed(path):
    """Stored form with '/' separators and no trailing '/', for prefix matching."""
    n = norm_remote(path)
    return n.replace('\\', '/').rstrip('/') if is_windows_path(n) else n.rstrip('/')


def _key(path):
    """Comparable form: Windows paths compare case-insensitively, like Windows does."""
    s = _slashed(path)
    return s.lower() if is_windows_path(path) else s


def _under(key, root_key):
    return key == root_key or key.startswith(root_key + '/')


def _parent_and_name(path):
    n = norm_remote(path)
    if is_windows_path(n):
        head, _, name = n.rstrip('\\').rpartition('\\')
        return (head + '\\' if head.endswith(':') else head), name
    return str(Path(n).parent), Path(n).name


def to_local(inst, remote):
    """Translate a Sonarr/Radarr path with the instance's mappings (longest prefix wins). None if unmapped."""
    key, best, length = _key(remote), None, -1
    for m in inst.get('mappings', []):
        r = _key(m['remote'])
        if _under(key, r) and len(r) > length:
            best, length = m, len(r)
    if not best:
        return None
    rest = _slashed(remote)[length:].lstrip('/')
    return str(Path(best['local']) / rest) if rest else best['local']


def suggest_mappings(app, inst, items):
    """For each Sonarr/Radarr root folder, the Themearr root holding most of its item folders (by folder name)."""
    out = []
    by_root = {}
    for it in items:
        parent, name = _parent_and_name(it.get('path') or '')
        by_root.setdefault(parent, []).append(name)
    for remote_root, names in sorted(by_root.items(), key=lambda kv: -len(kv[1])):
        best, hits = None, 0
        for _, root in app.ROOTS:
            n = sum(1 for name in names[:400] if (root / name).is_dir())
            if n > hits:
                best, hits = root, n
        if best and hits:
            out.append({'remote': remote_root, 'local': str(best), 'hits': hits, 'checked': min(len(names), 400)})
    return out


# V1.9.10: "Use this mapping" is preview first, then an explicit Save (no silent save + sync + folder creation).
_PREVIEWS = {}
_PREVIEW_TTL = 600


def preview_mapping(app, inst, remote, local, all_items, data=None):
    """What one path mapping would line up, read-only: counts and a few examples. Nothing is saved or created."""
    remote = norm_remote(remote)
    trial = dict(inst, mappings=[m for m in inst.get('mappings', []) if _key(m['remote']) != _key(remote)] + [{'remote': remote, 'local': local}])
    with app.db() as c:
        known = {r[0] for r in c.execute('SELECT path FROM shows')}
    data = data if data is not None else load(app.CONFIG)
    since = data.get('auto_create_since', 0) if data.get('auto_create') else None
    res = {'remote': remote, 'local': local, 'covered': 0, 'found': 0, 'in_library': 0, 'missing': 0, 'outside': 0, 'would_create': 0,
           'found_examples': [], 'missing_examples': [], 'auto_create': since is not None}
    for it in all_items:
        if not _under(_key(it['path']), _key(remote)):
            continue
        res['covered'] += 1
        loc = to_local(trial, it['path'])
        label, _ = _root_for(app, loc) if loc else (None, None)
        if not label:
            res['outside'] += 1  # deeper than one folder below the root, so not a title folder Themearr can use
            continue
        if Path(loc).is_dir():
            res['found'] += 1
            res['in_library'] += loc in known
            if len(res['found_examples']) < 5:
                res['found_examples'].append((it['path'], loc))
        else:
            res['missing'] += 1
            if since is not None and it['added'] >= since:
                res['would_create'] += 1
            if len(res['missing_examples']) < 5:
                res['missing_examples'].append((it['path'], loc))
    return res


def _preview_get(token):
    now = time.time()
    for k in [k for k, v in _PREVIEWS.items() if now - v['at'] > _PREVIEW_TTL]:
        _PREVIEWS.pop(k, None)
    return _PREVIEWS.get(token) if isinstance(token, str) and token else None


def _noun(inst, n):
    noun = KINDS[inst['kind']][1]
    return noun if noun == 'series' or n == 1 else noun + 's'


def _preview_html(inst, p):
    r = p['res']
    ex = lambda rows, ok: ''.join('<li><code>' + _e(a) + '</code> → <code>' + _e(b) + '</code> ' + ('<span class="pv-ok">found</span>' if ok else '<span class="pv-miss">no folder</span>') + '</li>' for a, b in rows)  # noqa: E731
    notes = ['Saving changes <strong>only this setting</strong>. Nothing is synced, scanned, created or moved until you click <strong>Sync now</strong>.']
    if r['auto_create']:
        notes.append('Automatic folder creation is on: the next sync could create up to <strong>' + str(r['would_create']) + '</strong> folder'
                     + ('' if r['would_create'] == 1 else 's') + ' for media added since you turned it on.')
    else:
        notes.append('Automatic folder creation is off, so syncing will not create folders.')
    return ('<div class="arr-preview" role="region" aria-label="Mapping preview"><p class="arr-sub">Preview: <code>' + _e(r['remote']) + '</code> → <code>' + _e(r['local']) + '</code></p>'
            '<ul class="pv-stats"><li><strong>' + str(r['covered']) + '</strong> ' + _e(KINDS[inst['kind']][0]) + ' ' + _e(_noun(inst, r['covered'])) + ' under this path</li>'
            '<li><strong>' + str(r['found']) + '</strong> folder' + ('' if r['found'] == 1 else 's') + ' found in ' + _e(r['local']) + ' (' + str(r['in_library']) + ' already in Preludarr’s library)</li>'
            '<li><strong>' + str(r['missing']) + '</strong> without a folder yet</li>'
            + ('<li><strong>' + str(r['outside']) + '</strong> nested deeper than a title folder (ignored)</li>' if r['outside'] else '') + '</ul>'
            + ('<p class="arr-sub">Examples that line up</p><ul class="arr-list">' + ex(r['found_examples'], True) + '</ul>' if r['found_examples'] else '')
            + ('<p class="arr-sub">Examples without a folder</p><ul class="arr-list">' + ex(r['missing_examples'], False) + '</ul>' if r['missing_examples'] else '')
            + ''.join('<p class="pv-note">' + n + '</p>' for n in notes) +
            '<div class="arr-actions"><form method="post" action="/connect/arr"><input type="hidden" name="action" value="save_suggestion">'
            '<input type="hidden" name="id" value="' + _e(inst['id']) + '"><input type="hidden" name="token" value="' + _e(p['token']) + '">'
            '<button type="submit">Save mapping</button></form><a class="btn ghost" href="/media-servers?tab=arr">Cancel</a></div>'
            '<p class="muted pv-exp">This preview expires after 10 minutes.</p></div>')


# --------------------------------------------------------------------------------- sync ----
def _epoch(text):
    try:
        return int(datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp())
    except (TypeError, ValueError):
        return 0


def _root_for(app, local):
    p = Path(local)
    for label, root in app.ROOTS:
        if p.parent == root and root.is_dir() and not root.is_symlink():
            return label, root
    return None, None


def items(inst):
    kind = inst['kind']
    raw = _get(inst, KINDS[kind][1], timeout=60)
    out = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict) or not it.get('path'):
            continue
        out.append({'id': it.get('id'), 'title': str(it.get('title') or '')[:200], 'year': it.get('year') or 0, 'path': str(it['path']),
                    'added': _epoch(it.get('added')),
                    'has_files': bool(it.get('hasFile')) if kind == 'radarr' else bool((it.get('statistics') or {}).get('episodeFileCount')),
                    'tvdb': int(it.get('tvdbId') or 0), 'tmdb': int(it.get('tmdbId') or 0), 'imdb': str(it.get('imdbId') or '')[:20]})
    return out


def sync(app, iid, create=False):
    """Read the instance's library; update Recently added; optionally auto-create folders. Returns a summary."""
    config = app.CONFIG
    data = load(config)
    inst = next((i for i in data['instances'] if i.get('id') == iid), None)
    if not inst or not inst.get('enabled', True):
        return None
    summary = {'at': int(time.time()), 'items': 0, 'mapped': 0, 'in_library': 0, 'no_folder': [], 'outside': 0, 'created': 0, 'error': ''}
    try:
        all_items = items(inst)
    except Exception as error:
        summary['error'] = _error_text(error, inst)
        _STATE[iid] = summary
        _set_status(config, iid, False, summary['error'])
        return summary
    summary['items'] = len(all_items)
    with app.db() as c:
        known = {r[0] for r in c.execute('SELECT path FROM shows')}
    for it in all_items:
        local = to_local(inst, it['path'])
        label, root = _root_for(app, local) if local else (None, None)
        if not label:
            summary['outside'] += 1
            continue
        summary['mapped'] += 1
        folder = Path(local)
        if it['tvdb'] or it['tmdb'] or it['imdb']:  # V1.9.0: exact IDs for secondary theme sources
            with app.db() as c:
                c.execute('INSERT INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET '
                          'tvdb=excluded.tvdb,tmdb=excluded.tmdb,imdb=excluded.imdb,source=excluded.source',
                          (local, it['tvdb'] or None, it['tmdb'] or None, it['imdb'] or None, inst['kind']))
        if folder.is_dir():
            if local in known:
                summary['in_library'] += 1
                if it['added']:
                    with app.db() as c:  # exact "Recently added" from Sonarr/Radarr
                        c.execute('INSERT INTO show_added(path,added,baseline) VALUES(?,?,0) ON CONFLICT(path) DO UPDATE SET added=excluded.added, baseline=0',
                                  (local, it['added']))
        else:
            entry = {'title': it['title'], 'year': it['year'], 'remote': it['path'], 'local': local, 'added': it['added'], 'library': label}
            if (create and data.get('auto_create') and it['added'] >= data.get('auto_create_since', 0)):
                try:
                    create_folder(app, inst, it['path'], reason='automatic')
                    summary['created'] += 1
                    continue
                except ValueError:
                    pass
            summary['no_folder'].append(entry)
    _STATE[iid] = summary
    if not (inst.get('status') or {}).get('ok'):
        _set_status(config, iid, True, 'Connected.')
    return summary


def sync_all(app, create=True):
    for inst in load(app.CONFIG)['instances']:
        if inst.get('enabled', True):
            try:
                sync(app, inst['id'], create=create)
            except Exception as error:
                print('Sonarr/Radarr sync error: ' + type(error).__name__, flush=True)


def create_folder(app, inst, remote, reason='manual'):
    """Create ONE folder exactly as Sonarr/Radarr names it, directly inside a registered root folder."""
    import permissions
    local = to_local(inst, remote)
    if not local:
        raise ValueError('No path mapping covers this folder.')
    label, root = _root_for(app, local)
    folder = Path(local)
    name = Path(str(remote).rstrip('/')).name
    if not label or folder.name != name or name.startswith('.') or '/' in name or name in ('', '..'):
        raise ValueError('The folder would not be directly inside a Preludarr root folder.')
    if os.path.realpath(root) != str(root):
        raise ValueError('Root folder is a symlink.')
    folder.mkdir(mode=0o775, exist_ok=False)  # never reuses or overwrites anything
    permissions.apply_dir(app.CONFIG, folder)
    app.save(folder, label, folder.name, 'missing', 'Folder created for ' + KINDS[inst['kind']][0])
    app.auto_themes.record_new(app)
    app.event(folder.name, 'folder created', ('Automatically created' if reason == 'automatic' else 'Created') + ' from ' + inst['name'] + ' (' + KINDS[inst['kind']][0] + ' path ' + str(remote) + ')')
    return folder


# ------------------------------------------------------------------------------ UI: Connect ----
def _logo(kind):
    """Official Sonarr / Radarr logos (static/sonarr-logo.png, static/radarr-logo.png — from hotio.dev, user choice 2026-10-26)."""
    from login import static_url
    return ('<span class="brand-mark" aria-hidden="true"><img src="' + static_url(kind + '-logo.png') + '" alt="" width="40" height="40" '
            'style="width:40px;height:40px;object-fit:contain;display:block"></span>')


def connect_html(config, notice='', preview=''):
    data = load(config)
    pv = _preview_get(preview)
    parts = ['<section id="themearr-arr" aria-labelledby="arr-title" style="margin:24px 0;padding:23px;border:1px solid #485c7b;background:#1c283e;border-radius:14px;color:#edf4ff">',
             '<h2 id="arr-title">Sonarr &amp; Radarr</h2>',
             '<p style="color:#c5d4ea">Connect one or more Sonarr and Radarr instances. Preludarr only reads from them: their series/movies, folder paths and dates added '
             '(for an exact “Recently added”). Folder names always come from Sonarr/Radarr themselves, following your naming settings.</p>']
    if notice:
        bad = notice.startswith(FAIL)
        parts += ['<p role="', 'alert' if bad else 'status', '" class="arr-banner ', 'bad' if bad else ('good' if notice.startswith(OK) else 'warn' if notice.startswith(WARN) else ''), '">', _e(notice), '</p>']
    parts.append('<div class="arr-grid">')
    for inst in data['instances']:
        name, noun, lib, _ = KINDS[inst['kind']]
        st = _STATE.get(inst['id'])
        info = inst.get('info') or {}
        maps = ''.join('<li><code>' + _e(m['remote']) + '</code> → <code>' + _e(m['local']) + '</code></li>' for m in inst.get('mappings', []))
        status = inst.get('status') or {}
        conn = status.get('ok')
        sugg = [(n, x) for n, x in enumerate(inst.get('suggested') or []) if x['hits'] * 2 >= x['checked']
                and not any(m['remote'] == x['remote'] for m in inst.get('mappings', []))]
        if not maps:
            maps = ('<li class="muted">None yet — ' + ('use the suggestion below.' if sugg else 'click Test to look for suggestions.') + '</li>')
        naming = ''.join('<li>' + _e(k.replace('Format', ' format').replace('series', 'Series ').replace('season', 'Season ').replace('movie', 'Movie ').replace('standard', 'Standard ').replace('anime', 'Anime ').strip().capitalize())
                         + ': <code>' + _e(v) + '</code></li>' for k, v in (info.get('naming') or {}).items())
        sync_line = ('Not synced yet' if not st else ('Last sync failed — see above.' if status and not conn else 'Sync failed: ' + st['error']) if st['error'] else
                     f"Last sync {time.strftime('%H:%M', time.localtime(st['at']))}: {st['items']} {noun if noun == 'series' else noun + 's'} · {st['mapped']} in Preludarr root folders · "
                     f"{st['in_library']} in library · {len(st['no_folder'])} without a folder" + (f" · {st['created']} folders created" if st['created'] else ''))
        conn_tag = ('' if not inst.get('enabled', True) else
                    ('<span class="tag arr-ok">Last check succeeded</span>' + ('' if inst.get('mappings') else ' <span class="tag warn">Needs attention: path mapping required</span>')) if conn else
                    '<span class="tag arr-bad">Last check failed</span>' if status else '<span class="tag arr-unknown">Not checked</span>')
        parts += ['<section class="arr-card"><div class="conn-head">', _logo(inst['kind']), '<div><div class="conn-name">', _e(inst['name']), '</div>',
                  '<span class="tag ', 'on' if inst.get('enabled', True) else '', '">', _e(name), ' · ', 'On' if inst.get('enabled', True) else 'Off', '</span> ', conn_tag, '</div></div>']
        if status and not conn:
            parts += ['<p class="arr-status bad" role="alert"><strong>Can’t connect</strong> (checked ', time.strftime('%H:%M', time.localtime(status['at'])), '): ', _e(status['text']), '</p>']
        elif not status:
            parts += ['<p class="arr-status unknown">Connection not checked yet — click <strong>Test</strong>.</p>']
        elif st and not st['error'] and st['items'] and not st['mapped']:
            parts += ['<p class="arr-status warn" role="alert"><strong>Connected, but nothing lines up:</strong> none of its ', str(st['items']), ' folders are inside Preludarr’s root folders. ',
                      'Add a path mapping', ' — the suggestion below.' if sugg else ' under Edit / path mappings.', '</p>']
        parts += [
                  '<div class="stats"><div>Host: <strong>', _e(inst['url']), '</strong></div><div>API key: <strong>Saved</strong></div>',
                  '<div>Version: <strong>', _e(info.get('version') or ('unknown — not connected' if status and not conn else 'not tested yet')), '</strong></div>',
                  '<div>Library: <strong>', _e(lib), '</strong></div><div>', _e(sync_line), '</div></div>',
                  '<p class="arr-sub">Path mappings</p><ul class="arr-list">', maps, '</ul>']
        if sugg:
            parts += ['<p class="pv-note">Suggestions come from the last <strong>Test</strong>: for each ', _e(name), ' root folder, Preludarr checked up to 400 of its ',
                      _e(noun if noun == 'series' else noun + 's'), ' and counted how many have a folder with the same name in one of Preludarr’s root folders. '
                      'Preview shows what would line up; nothing is saved until you click Save.</p>']
        for n, x in sugg[:3]:
            parts += ['<form method="post" action="/connect/arr" class="arr-sugg"><input type="hidden" name="action" value="preview_suggestion">',
                      '<input type="hidden" name="id" value="', _e(inst['id']), '"><input type="hidden" name="n" value="', str(n), '">',
                      '<span>Suggested: <code>', _e(x['remote']), '</code> → <code>', _e(x['local']), '</code> <small>(', str(x['hits']), ' of ', str(x['checked']),
                      ' checked folders found)</small></span><button type="submit">Preview mapping</button></form>']
        if pv and pv['id'] == inst['id']:
            parts.append(_preview_html(inst, pv))
        if naming:
            parts += ['<p class="arr-sub">Naming in ', _e(name), ' (read-only)</p><ul class="arr-list">', naming, '</ul>']
        if st and st['no_folder']:
            parts += ['<p><a href="/connect/arr-folders?', _e(urllib.parse.urlencode({'id': inst['id']})), '">', str(len(st['no_folder'])), ' ', noun,
                      ('s' if len(st['no_folder']) != 1 and noun != 'series' else ''), ' without a folder — review / create</a></p>']
        parts += ['<div class="arr-actions">'] + [
            '<form method="post" action="/connect/arr"><input type="hidden" name="id" value="' + _e(inst['id']) + '"><input type="hidden" name="action" value="' + a + '">'
            '<button type="submit"' + (' style="background:#9b394a"' if a == 'remove' else '') + '>' + lbl + '</button></form>'
            for a, lbl in (('test', 'Test'), ('sync', 'Sync now'), ('toggle', 'Turn off' if inst.get('enabled', True) else 'Turn on'), ('remove', 'Remove'))] + ['</div>',
            '<details class="arr-edit"><summary class="btn ghost">Edit / path mappings</summary>', _form(None, inst), '</details></section>']
    if not data['instances']:
        parts.append('<p class="muted">No Sonarr or Radarr connected yet.</p>')
    parts += ['</div><details class="arr-edit" style="margin-top:14px"><summary class="btn primary">＋ Add Sonarr / Radarr</summary>', _form(None, None), '</details>',
              '<form method="post" action="/connect/arr" style="margin-top:16px"><input type="hidden" name="action" value="autocreate">',
              '<label class="check" style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="auto_create" value="1"',
              ' checked' if data.get('auto_create') else '', '> Create folders automatically for media added to Sonarr/Radarr from now on '
              '(named exactly as Sonarr/Radarr will name them; only inside root folders)</label><button type="submit">Save</button></form>',
              '<p style="font-size:.9em;color:#c5d4ea">API keys: Sonarr/Radarr → Settings → General → Security → API Key. Keys are private and never shown after saving.</p>',
              '<style>#themearr-arr .arr-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:16px}',
              '#themearr-arr .arr-card{padding:18px;border:1px solid #455a7a;background:#24334e;border-radius:11px}',
              '#themearr-arr .arr-sub{margin:12px 0 4px;font-weight:700;color:#dce9ff}#themearr-arr .arr-list{margin:0;padding-left:18px;color:#c5d4ea}',
              '#themearr-arr code{overflow-wrap:anywhere;color:#9fd0ff}#themearr-arr .arr-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}#themearr-arr .arr-actions form{margin:0}',
              '#themearr-arr .arr-banner{border:1px solid #617aa2;padding:10px;background:#304565;border-radius:8px}',
              '#themearr-arr .arr-banner.bad{border-color:#e07a86;background:#4a2430}#themearr-arr .arr-banner.good{border-color:#5fbf86;background:#1f4532}',
              '#themearr-arr .arr-banner.warn{border-color:#e0b85f;background:#4a3d1f}',
              '#themearr-arr .arr-status{margin:12px 0 4px;padding:10px;border-radius:8px;border:1px solid #617aa2;background:#2b3c5c}',
              '#themearr-arr .arr-status.bad{border-color:#e07a86;background:#4a2430}#themearr-arr .arr-status.warn{border-color:#e0b85f;background:#4a3d1f}',
              '#themearr-arr .tag.arr-ok{background:#2f8f5b;color:#fff}#themearr-arr .tag.arr-bad{background:#b84052;color:#fff}#themearr-arr .tag.arr-unknown{background:#5d6b85;color:#fff}',
              '#themearr-arr .arr-sugg{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0;padding:8px 10px;border:1px dashed #6d88b3;border-radius:8px}',
              '#themearr-arr .arr-sugg span{flex:1 1 220px}',
              '#themearr-arr .arr-preview{margin:10px 0;padding:12px 14px;border:1px solid #6d88b3;border-radius:10px;background:#1b2840}',
              '#themearr-arr .pv-stats{margin:4px 0 8px;padding-left:18px;color:#dce9ff}#themearr-arr .pv-note{font-size:.92em;color:#c5d4ea;margin:8px 0}',
              '#themearr-arr .pv-ok{color:#8fe0b0;font-size:.85em}#themearr-arr .pv-miss{color:#ffcf8a;font-size:.85em}#themearr-arr .pv-exp{font-size:.85em;margin:6px 0 0}',
              '#themearr-arr .arr-preview .arr-actions a.btn{display:inline-flex;align-items:center;padding:8px 14px;border-radius:8px;text-decoration:none}',
              '#themearr-arr .arr-edit{margin-top:12px}#themearr-arr .arr-edit summary{list-style:none;display:inline-block;cursor:pointer}',
              '#themearr-arr label.af{display:block;margin:10px 0 4px;font-weight:600}#themearr-arr input:not([type=checkbox]),#themearr-arr select,#themearr-arr textarea{display:block;width:100%;padding:10px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px;box-sizing:border-box}',
              '</style></section>']
    return ''.join(parts)


# Tell password managers (Bitwarden, LastPass, 1Password, Dashlane) these are not login fields (V1.9.2).
_NOPM = ' data-bwignore="true" data-lpignore="true" data-1p-ignore="true" data-form-type="other"'


def _form(app, inst):
    kind = inst['kind'] if inst else 'sonarr'
    maps = '\n'.join(m['remote'] + ' => ' + m['local'] for m in (inst or {}).get('mappings', []))
    sugg = (inst or {}).get('suggested') or []
    hint = ''.join('<li><code>' + _e(s['remote']) + ' => ' + _e(s['local']) + '</code> <small class="muted">(' + str(s['hits']) + ' of ' + str(s['checked']) + ' folders found)</small></li>' for s in sugg)
    return ('<form method="post" action="/connect/arr" autocomplete="off"><input type="hidden" name="action" value="save">'
            + ('<input type="hidden" name="id" value="' + _e(inst['id']) + '">' if inst else '') +
            '<label class="af">Type</label><select name="kind"' + (' disabled' if inst else '') + '>'
            + ''.join('<option value="' + k + '"' + (' selected' if k == kind else '') + '>' + v[0] + '</option>' for k, v in KINDS.items()) + '</select>'
            + ('<input type="hidden" name="kind" value="' + kind + '">' if inst else '') +
            '<label class="af">Name</label><input name="name" maxlength="40" required autocomplete="off"' + _NOPM + ' value="' + _e((inst or {}).get('name', '')) + '" placeholder="e.g. Sonarr Anime">'
            '<label class="af">URL (HTTPS or private-LAN HTTP)</label><input name="url" type="url" maxlength="200" required autocomplete="off"' + _NOPM + ' value="' + _e((inst or {}).get('url', '')) + '" placeholder="http://192.168.1.50:8989">'
            '<label class="af">API key</label><input name="api_key" type="password" maxlength="64" autocomplete="new-password"' + _NOPM + ' placeholder="' + ('Leave blank to keep the saved key' if inst else 'Required') + '">'
            '<label class="af">Path mappings — one per line: <code>Sonarr/Radarr path =&gt; Preludarr path</code></label>'
            '<textarea name="mappings" rows="3" placeholder="/data/tv/anime => /anime&#10;G:\\TV => /media/tv_shows">' + _e(maps) + '</textarea>'
            + ('<p class="muted">Suggested from your folders (copy any that are right):</p><ul class="arr-list">' + hint + '</ul>' if hint else '') +
            '<div class="arr-actions"><button type="submit">Save</button></div></form>')


def _parse_mappings(app, text):
    out = []
    for line in str(text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if '=>' not in line:
            raise ValueError('Each mapping line needs “=>”, e.g. /data/tv/anime => /anime')
        remote, local = (x.strip() for x in line.split('=>', 1))
        if not (remote.startswith('/') or is_windows_path(remote)):
            raise ValueError('The Sonarr/Radarr path must start with “/” (Linux, Docker) or a drive letter such as “G:\\” or “\\\\server\\share” (Windows).')
        if not local.startswith('/'):
            raise ValueError('The Preludarr path must start with “/”.')
        local = os.path.normpath(local)
        if not any(Path(local) == r for _, r in app.ROOTS):
            raise ValueError(f'“{local}” is not one of Preludarr’s root folders (Settings → Media Management).')
        out.append({'remote': norm_remote(remote), 'local': local})
    return out[:20]


OK, FAIL, WARN = '✓ ', '✗ ', '⚠ '  # banner prefixes: green success / red problem (V1.9.2) / amber "needs attention", nothing broken (V1.9.14)


def _run_test(app, inst):
    """Test the connection, remember the result on the instance (shown on the card) and return the banner text."""
    try:
        info = test(inst)
    except Exception as error:
        text = _error_text(error, inst)
        inst['status'] = {'ok': False, 'at': int(time.time()), 'text': text[:300]}
        return FAIL + inst['name'] + ': not connected. ' + text
    inst['info'] = {'version': info['version'], 'naming': info['naming']}
    inst['status'] = {'ok': True, 'at': int(time.time()), 'text': 'Connected to ' + info['app'] + ' ' + info['version'] + '.'}
    st = _STATE.get(inst['id'])
    if st and st.get('error'):
        _STATE.pop(inst['id'], None)  # an old sync error no longer applies
    try:
        inst['suggested'] = suggest_mappings(app, inst, items(inst))
    except Exception:
        inst['suggested'] = []
    msg = OK + inst['name'] + ': connected to ' + info['app'] + ' ' + info['version'] + '. Root folders: ' + (', '.join(info['roots']) or 'none') + '.'
    if not inst.get('mappings'):
        msg += (' Next: use the suggested path mapping on the card.' if inst['suggested'] else
                ' None of its folders were found in Preludarr’s root folders — add a path mapping under Edit.')
    return msg


def post(app, handler, redirect):
    import media_servers
    msg = ''
    preview_token = ''
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 8192:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=12)
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        action = f('action')
        with _LOCK:
            data = load(app.CONFIG)
            inst = next((i for i in data['instances'] if i.get('id') == f('id')), None)
            if action == 'save':
                kind = inst['kind'] if inst else f('kind')
                if kind not in KINDS:
                    raise ValueError('Choose Sonarr or Radarr.')
                url = media_servers._base_url(f('url'))
                key = f('api_key').strip()
                if key and not all(c.isalnum() for c in key) or len(key) > 64:
                    raise ValueError('The API key should be letters and numbers only.')
                if not inst:
                    if not key:
                        raise ValueError('An API key is required.')
                    if len(data['instances']) >= MAX_INSTANCES:
                        raise ValueError('Up to %d instances are supported.' % MAX_INSTANCES)
                    inst = {'id': secrets.token_hex(6), 'kind': kind, 'enabled': True}
                    data['instances'].append(inst)
                inst.update(name=(f('name').strip()[:40] or KINDS[kind][0]), url=url, mappings=_parse_mappings(app, f('mappings')))
                if key:
                    inst['api_key'] = key
                result = _run_test(app, inst)  # V1.9.2: always test right away
                msg = (FAIL + 'Saved ' + inst['name'] + ', but it is not connected. ' + result[len(FAIL):].split('not connected. ', 1)[-1]
                       if result.startswith(FAIL) else OK + 'Saved. ' + result[len(OK):])
            elif action == 'autocreate':
                on = f('auto_create') == '1'
                if on and not data.get('auto_create'):
                    data['auto_create_since'] = int(time.time())
                data['auto_create'] = on
                msg = 'Automatic folder creation is ' + ('on for media added from now on.' if on else 'off.')
            elif not inst:
                raise ValueError('Unknown instance.')
            elif action == 'remove':
                data['instances'] = [i for i in data['instances'] if i is not inst]
                _STATE.pop(inst['id'], None)
                msg = 'Removed ' + inst['name'] + ' (nothing on disk was changed).'
            elif action == 'toggle':
                inst['enabled'] = not inst.get('enabled', True)
                msg = inst['name'] + (' turned on.' if inst['enabled'] else ' turned off.')
            elif action == 'test':
                msg = _run_test(app, inst)
            elif action in ('preview_suggestion', 'use_suggestion'):  # use_suggestion = forms from before V1.9.10: preview too, never save
                sugg = inst.get('suggested') or []
                try:
                    pick = sugg[int(f('n'))]
                except (ValueError, IndexError):
                    raise ValueError('That suggestion is no longer available — use Test again.') from None
                new = _parse_mappings(app, pick['remote'] + ' => ' + pick['local'])
                if any(m['remote'] == new[0]['remote'] for m in inst.get('mappings', [])):
                    raise ValueError('That mapping is already saved.')
                try:
                    res = preview_mapping(app, inst, new[0]['remote'], new[0]['local'], items(inst), data)
                except ValueError:
                    raise
                except Exception as error:
                    raise ValueError('Could not read ' + inst['name'] + ' for the preview: ' + _error_text(error, inst)) from None
                preview_token = secrets.token_urlsafe(12)
                _PREVIEWS[preview_token] = {'token': preview_token, 'id': inst['id'], 'at': time.time(), 'res': res}
                msg = 'Preview ready for ' + inst['name'] + ' — nothing has been saved.'
            elif action == 'save_suggestion':
                p = _preview_get(f('token'))
                if not p or p['id'] != inst['id']:
                    raise ValueError('That preview has expired — click Preview mapping again.')
                new = _parse_mappings(app, p['res']['remote'] + ' => ' + p['res']['local'])
                if any(m['remote'] == new[0]['remote'] for m in inst.get('mappings', [])):
                    raise ValueError('That mapping is already saved.')
                inst['mappings'] = (inst.get('mappings') or []) + new
                _PREVIEWS.pop(p['token'], None)
                msg = (OK + inst['name'] + ': mapping saved (' + new[0]['remote'] + ' → ' + new[0]['local'] + '). Nothing was synced or created — '
                       'click Sync now when you are ready.')
            elif action == 'sync':
                pass
            else:
                raise ValueError('Unknown action.')
            _save(app.CONFIG, data)
        if action == 'sync':
            s = sync(app, inst['id'], create=True)
            if s is None:
                msg = FAIL + inst['name'] + ' is turned off — turn it on to sync.'
            elif s['error']:
                msg = FAIL + inst['name'] + ': sync failed. ' + s['error']
            elif s['items'] and not s['mapped']:
                msg = (WARN + inst['name'] + ': needs attention — connected, but none of its ' + str(s['items']) + ' folders are inside Preludarr’s root folders yet. '
                       'Add a path mapping (suggested on the card).')
            else:
                msg = (msg + ' ' if msg.startswith(OK) else OK) + inst['name'] + ': synced — ' + str(s['mapped']) + ' of ' + str(s['items']) + ' are in Preludarr’s root folders.'
            if s and not s['error'] and s['in_library'] < s['mapped']:
                app.job(False)  # register any new folders now present on disk
    except (ValueError, OSError, UnicodeError) as error:
        msg = FAIL + 'Not saved: ' + (str(error) if isinstance(error, ValueError) else 'settings could not be written.')
    q = {'arr_notice': msg[:400]}
    if preview_token:
        q['arr_preview'] = preview_token
    return redirect(handler, '/media-servers?' + urllib.parse.urlencode(q) + '#themearr-arr')


# ----------------------------------------------------------------- UI: media without a folder ----
def folders_page(app, handler):
    import media_servers
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    iid = q.get('id', [''])[0]
    inst = instance(app.CONFIG, iid)
    st = _STATE.get(iid) if inst else None
    notice = q.get('notice', [''])[0][:240]
    parts = [media_servers.settings_shell('Media without a folder', 'connect'),
             '</aside><main class="main"><div class="topline"><div class="crumb">Settings / <a href="/media-servers?tab=arr">Connect</a> / <strong>Media without a folder</strong></div></div>',
             '<h1 class="heading">Media without a folder</h1>']
    if notice:
        parts += ['<p class="notice" role="status">', _e(notice), '</p>']
    if not inst or not st:
        parts += ['<p>Run <strong>Sync now</strong> on the Sonarr/Radarr card first.</p></main></div></body></html>']
        return handler.send(200, ''.join(parts), 'text/html')
    name, noun, _, _ = KINDS[inst['kind']]
    plural = noun if noun == 'series' else noun + 's'
    if not st['no_folder']:
        parts += ['<p>Nothing waiting — every ', plural, ' ', _e(inst['name']), ' knows in your root folders already has a folder. ',
                  '<a href="/media-servers?tab=arr">Back to Connect</a></p></main></div></body></html>']
        return handler.send(200, ''.join(parts), 'text/html')
    parts += ['<p class="muted">', _e(inst['name']), ' knows these ', plural, ', but their folders don’t exist yet. Ticked folders are created exactly as ', name,
              ' names them, directly inside your Preludarr root folder, with your permissions. Then Preludarr can add a theme.mp3 (manually or with Automatic Themes). '
              'Nothing else is created or changed.</p><form method="post" action="/connect/arr-create"><input type="hidden" name="id" value="', _e(iid), '">',
              '<div class="af-table"><div class="af-row af-head"><span></span><span>', name, ' title</span><span>Folder that will be created</span><span>Added</span></div>']
    for n, e in enumerate(st['no_folder'][:300]):
        parts += ['<label class="af-row"><input type="checkbox" name="p" value="', _e(e['remote']), '"', ' checked' if n < 0 else '', '>',
                  '<span><strong>', _e(e['title']), '</strong> ', _e(e['year'] or ''), '<br><small class="muted">', _e(e['library']), '</small></span>',
                  '<code>', _e(e['local']), '</code><span>', _e(time.strftime('%Y-%m-%d', time.localtime(e['added'])) if e['added'] else '—'), '</span></label>']
    parts += ['</div><label class="check" style="display:flex;gap:8px;align-items:center;margin:14px 0"><input type="checkbox" name="confirm" value="1" required> '
              'Create the ticked folders (they will be empty until a theme or media arrives)</label><button type="submit">Create selected folders</button></form>',
              '<style>.af-table{display:flex;flex-direction:column;gap:6px;margin-top:10px}.af-row{display:grid;grid-template-columns:28px minmax(0,1.3fr) minmax(0,2fr) 110px;gap:10px;align-items:center;padding:9px;border:1px solid var(--line,#3b4b6b);border-radius:8px}',
              '.af-head{border:0;font-weight:700;color:var(--muted,#adbed9)}.af-row code{overflow-wrap:anywhere;color:#9fd0ff}@media(max-width:760px){.af-row{grid-template-columns:28px 1fr}.af-row code,.af-row span:last-child{grid-column:2}.af-head{display:none}}</style>',
              '</main></div></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')


def create_post(app, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 65536:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=310)
        iid = form.get('id', [''])[0]
        inst = instance(app.CONFIG, iid)
        st = _STATE.get(iid)
        if not inst or not st:
            raise ValueError('Run Sync now first.')
        if form.get('confirm', [''])[0] != '1':
            raise ValueError('Tick the confirmation box to create folders.')
        allowed = {e['remote'] for e in st['no_folder']}
        chosen = [p for p in form.get('p', []) if p in allowed][:300]
        if not chosen:
            raise ValueError('Tick at least one folder.')
        made, problems = 0, []
        for remote in chosen:
            try:
                create_folder(app, inst, remote)
                made += 1
            except (ValueError, OSError) as error:
                problems.append(Path(remote).name + ': ' + (str(error) if isinstance(error, ValueError) else 'already exists or not allowed'))
        st['no_folder'] = [e for e in st['no_folder'] if not Path(e['local']).is_dir()]
        msg = f'Created {made} folder(s).' + (' Skipped: ' + '; '.join(problems[:3]) if problems else '')
    except (ValueError, UnicodeError) as error:
        iid = iid if 'iid' in locals() else ''
        msg = 'Nothing created: ' + str(error)
    return redirect(handler, '/connect/arr-folders?' + urllib.parse.urlencode({'id': iid, 'notice': msg[:230]}))
