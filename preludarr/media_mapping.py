# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only library discovery and conservative, cached media-server item links.

A link is learned only from a unique translated title folder or a unique
provider identity. Names and years alone are never enough. Manual mappings
win; automatic links expire and are tied to the exact saved connection and
library selections. Rebuilds never refresh the media server.
"""
import hashlib
import html
import json
import os
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import arr
import tasks

PAGE_SIZE = 200
MAX_ITEMS = 30000
MAX_LIBRARIES = 100
MAX_PATH_MAPPINGS = 20  # arr._parse_mappings keeps at most 20
MAX_AGE = 2 * 86400
KEY = re.compile(r'(plex|jellyfin|emby)(?:-[2-9])?\Z')
ID_TAG = re.compile(r'[\[{]\s*(tvdb|tmdb|imdb)(?:id)?\s*[-=:]\s*((?:tt)?[0-9]+)\s*[\]}]', re.I)
MOVIE_SUFFIXES = {'.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.wmv', '.webm', '.mpeg', '.mpg', '.m2ts', '.iso'}


def schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS media_server_links (server_key TEXT NOT NULL, path TEXT NOT NULL, '
                 'item_id TEXT NOT NULL, evidence TEXT NOT NULL, library_id TEXT NOT NULL, signature TEXT NOT NULL, '
                 'checked INTEGER NOT NULL, local_identity TEXT NOT NULL, PRIMARY KEY(server_key,path))')
    conn.execute('CREATE TABLE IF NOT EXISTS media_mapping_report (server_key TEXT PRIMARY KEY, signature TEXT NOT NULL, '
                 'checked INTEGER NOT NULL, total INTEGER NOT NULL, matched INTEGER NOT NULL, ambiguous INTEGER NOT NULL, remote_count INTEGER NOT NULL)')


def kind_of(key):
    return key.split('-', 1)[0]


def connections(config):
    import media_servers as ms
    with ms._LOCK:
        raw = ms._read(config, 'servers')
    return {key: value for key, value in raw.items() if KEY.fullmatch(key) and isinstance(value, dict)
            and value.get('kind') == kind_of(key) and value.get('url') and value.get('api_key')}


def refresh_enabled(key, record):
    return bool(record.get('enabled')) if key == kind_of(key) else bool(record.get('mapping_refresh'))


def signature(record):
    return hashlib.sha256(json.dumps([record.get(k) for k in ('kind', 'url', 'api_key', 'machine', 'library_mapping')],
                                     sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _call(record, route):
    return __import__('media_servers')._call(record, 'GET', route, timeout=15, limit=8_000_000)


def _xml_item(node):
    result = dict(node.attrib)
    for tag in ('Location', 'Guid'):
        result[tag] = [dict(n.attrib) for n in node.findall(tag)]
    result['Media'] = [{'Part': [dict(p.attrib) for p in m.findall('Part')]} for m in node.findall('Media')]
    return result


def _plex(record, route, sections=False):
    data = _call(record, route).lstrip(b'\xef\xbb\xbf\t\r\n ')
    if data.startswith(b'{'):
        container = json.loads(data).get('MediaContainer')
        if not isinstance(container, dict):
            raise ValueError('Unexpected Plex response.')
        rows = container.get('Directory' if sections else 'Metadata')
        if rows is None:
            # Plex omits the list for an empty page; anything else is an incomplete reply.
            if str(container.get('size')) != '0':
                raise ValueError('Incomplete Plex response.')
            rows = []
        if not isinstance(rows, list):
            raise ValueError('Unexpected Plex library items.')
        return rows, container.get('totalSize')
    # Only the configured server is queried; replies are size-bounded.
    # Plex needs no DTD or custom entities. Reject them before the standard
    # XML parser sees the UTF-8 document.
    document = data.decode('utf-8-sig', 'strict')
    if '<!DOCTYPE' in document.upper() or '<!ENTITY' in document.upper():
        raise ValueError('Unexpected XML document declaration.')
    root = ET.fromstring(document)  # nosec B314 - bounded XML, DTD/entity declarations rejected
    if root.tag != 'MediaContainer':
        raise ValueError('Unexpected Plex response.')
    tag = ('Directory',) if sections else ('Directory', 'Video')
    rows = [_xml_item(n) for n in root if n.tag in tag]
    if not rows and root.get('size') != '0':
        raise ValueError('Incomplete Plex response.')
    return rows, root.get('totalSize')


def _item_ok(kind, value):
    return __import__('media_servers')._item_ok(kind, value)


def _remote_path(value):
    if (not isinstance(value, str) or len(value) > 4096 or not value
            or any(ord(c) < 32 for c in value) or not (value.startswith('/') or arr.is_windows_path(value))
            or any(p in ('.', '..') for p in re.split(r'[\\/]+', value))):
        return ''
    # norm_remote strips surrounding spaces (fine for typed settings); a server's
    # folder name may really end in one, so Linux paths are only normalized.
    return arr.norm_remote(value) if arr.is_windows_path(value) else os.path.normpath(value)


def discover(record):
    """Read library names, types and remote root paths; does not change anything remotely."""
    kind = record['kind']
    found = []
    if kind == 'plex':
        items, _ = _plex(record, '/library/sections', sections=True)
        for x in items:
            typ = {'show': 'tv', 'movie': 'movie'}.get(x.get('type'))
            item_id = str(x.get('key') or '')
            if not typ or not _item_ok(kind, item_id):
                continue
            found.append({'id': item_id, 'name': str(x.get('title') or item_id)[:120], 'type': typ,
                          'paths': [p for loc in (x.get('Location') or [])[:20] if (p := _remote_path(loc.get('path')))]})
    else:
        prefix = '/emby' if kind == 'emby' else ''
        data = json.loads(_call(record, prefix + '/Library/VirtualFolders'))
        if not isinstance(data, list):
            raise ValueError('Unexpected library response.')
        for x in data:
            if not isinstance(x, dict):
                continue
            typ = {'tvshows': 'tv', 'movies': 'movie', 'mixed': 'mixed', '': 'mixed'}.get(str(x.get('CollectionType') or '').lower())
            item_id = str(x.get('ItemId') or x.get('Id') or '')
            if not typ or not _item_ok(kind, item_id):
                continue
            found.append({'id': item_id, 'name': str(x.get('Name') or item_id)[:120], 'type': typ,
                          'paths': [p for loc in (x.get('Locations') or [])[:20] if (p := _remote_path(loc))]})
    if len(found) > MAX_LIBRARIES:
        raise ValueError('Too many libraries; narrow the server connection.')
    if len({x['id'] for x in found}) != len(found):
        raise ValueError('Server returned duplicate library IDs.')
    return found


def _ids(raw):
    result = {}
    for key, value in (raw or {}).items():
        key, value = str(key).lower(), str(value or '').lower()
        if key in ('tvdb', 'tmdb') and re.fullmatch(r'[1-9][0-9]{0,11}', value):
            result[key] = value
        elif key == 'imdb' and re.fullmatch(r'tt[0-9]{3,15}', value):
            result[key] = value
    return result


def _plex_ids(value):
    result = {}
    conflict = False
    for raw in [value.get('guid')] + [g.get('id') for g in (value.get('Guid') or []) if isinstance(g, dict)]:
        match = re.fullmatch(r'(?:com\.plexapp\.agents\.)?(tvdb|thetvdb|tmdb|themoviedb|imdb)://((?:tt)?[0-9]+)(?:\?.*)?', str(raw or ''), re.I)
        if match:
            key, val = match.groups()
            key = {'thetvdb': 'tvdb', 'themoviedb': 'tmdb'}.get(key.lower(), key.lower())
            val = val.lower()
            if key in result and result[key] != val:
                conflict = True
            result[key] = val
    return _ids(result), conflict


def _item(record, x, lib):
    if not isinstance(x, dict):
        return None
    kind = record['kind']
    if kind == 'plex':
        item_id, typ = str(x.get('ratingKey') or ''), {'show': 'tv', 'movie': 'movie'}.get(x.get('type'))
        ids, conflict = _plex_ids(x)
        folders = [loc.get('path') for loc in (x.get('Location') or []) if isinstance(loc, dict)]
        if typ == 'movie':
            for media in x.get('Media') or []:
                folders += [p.get('file') for p in media.get('Part') or [] if isinstance(p, dict)]
    else:
        item_id, typ = str(x.get('Id') or ''), {'series': 'tv', 'movie': 'movie'}.get(str(x.get('Type') or '').lower())
        ids, conflict = _ids(x.get('ProviderIds') if isinstance(x.get('ProviderIds'), dict) else {}), False
        folders = [x.get('Path')]
    if not typ or not _item_ok(kind, item_id) or (lib['type'] != 'mixed' and typ != lib['type']):
        return None
    paths = set()
    for raw in folders:
        p = _remote_path(raw)
        if not p:
            continue
        parent, basename = arr._parent_and_name(p)
        if typ == 'movie' and Path(basename).suffix.lower() in MOVIE_SUFFIXES:
            p = parent
        if p:
            paths.add(p)
    return {'id': item_id, 'type': typ, 'ids': ids, 'paths': sorted(paths), 'library': lib['id'], 'conflict': conflict}


def read_items(record, lib, check):
    kind, start, seen = record['kind'], 0, set()
    while True:
        check()
        if kind == 'plex':
            q = urllib.parse.urlencode({'type': 2 if lib['type'] == 'tv' else 1, 'includeGuids': 1,
                                       'X-Plex-Container-Start': start, 'X-Plex-Container-Size': PAGE_SIZE})
            raw, total = _plex(record, '/library/sections/' + lib['id'] + '/all?' + q)
        else:
            prefix = '/emby' if kind == 'emby' else ''
            types = {'tv': 'Series', 'movie': 'Movie', 'mixed': 'Series,Movie'}[lib['type']]
            q = urllib.parse.urlencode({'ParentId': lib['id'], 'Recursive': 'true', 'IncludeItemTypes': types,
                                       'Fields': 'Path,ProviderIds', 'StartIndex': start, 'Limit': PAGE_SIZE, 'EnableTotalRecordCount': 'true'})
            value = json.loads(_call(record, prefix + '/Items?' + q))
            raw, total = value.get('Items'), value.get('TotalRecordCount')
        if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
            raise ValueError('Unexpected library page.')
        if not raw:
            if total is not None and start < int(total):
                raise ValueError('Incomplete media server result.')
            return
        start += len(raw)
        if start > MAX_ITEMS:
            raise ValueError('Mapping limit exceeded; select fewer libraries.')
        for value in raw:
            item = _item(record, value, lib)
            if item:
                if item['id'] in seen:
                    raise ValueError('Server repeated media items while paging.')
                seen.add(item['id'])
                yield item
        if total is not None:
            if start >= int(total):
                return
        elif len(raw) < PAGE_SIZE:
            return


def _title_identity(row, typ):
    ids = _ids({k: row[k] for k in ('tvdb', 'tmdb', 'imdb')})
    conflict = False
    for key, value in ID_TAG.findall(str(row['name'] or '')):
        tag = _ids({key: value})
        for k, v in tag.items():
            if k in ids and ids[k] != v:
                conflict = True
            ids[k] = v
    return {'type': typ, 'ids': ids, 'conflict': conflict}


def _identity_signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _local_titles(app, record, libraries):
    roots = {str(root): ('movie' if label in ('Anime Movies', 'Movies') else 'tv') for label, root in app.ROOTS}
    maps = (record.get('library_mapping') or {}).get('paths', [])
    scoped = {m['local'] for m in maps if m['local'] in roots}
    if maps and not scoped:
        return {}  # every configured destination is gone: map nothing rather than everything
    # Explicit mapped roots constrain ID fallback. Without mappings, a unique
    # provider ID can identify a title in the registered libraries of that type.
    allowed_types = {t for l in libraries for t in (('tv', 'movie') if l['type'] == 'mixed' else (l['type'],))}
    with app.db() as conn:
        rows = conn.execute("SELECT s.path,s.name,s.library,m.tvdb,m.tmdb,m.imdb FROM shows s LEFT JOIN media_ids m ON m.path=s.path WHERE s.state<>'unavailable'").fetchall()
    found = {}
    for row in rows:
        path = str(row['path'])
        root = str(Path(path).parent)
        typ = roots.get(root)
        if typ not in allowed_types or (scoped and root not in scoped):
            continue
        found[path] = _title_identity(row, typ)
    return found


def match_items(local, items, mappings):
    """Return {(path): item evidence}, unresolved count. Never guess by name."""
    by_id = defaultdict(set)
    for path, value in local.items():
        for key, provider_id in value['ids'].items():
            by_id[(value['type'], key, provider_id)].add(path)
    choices = defaultdict(list)
    ambiguous_paths = set()
    remote_ids = defaultdict(set)
    unresolved = 0
    for item in items:
        paths = set()
        id_hits = set()
        for remote in item['paths']:
            # Exact literal path is valid only if it is already a known local
            # title; otherwise require an explicit remote->local mapping.
            mapped = arr.to_local({'mappings': mappings}, remote) if mappings else None
            for p in (mapped or remote,):
                if p in local and local[p]['type'] == item['type']:
                    paths.add(p)
        for key, value in item['ids'].items():
            id_hits.update(by_id.get((item['type'], key, value), ()))
        candidates = paths | id_hits
        if item.get('conflict') or len(candidates) != 1:
            if candidates:
                ambiguous_paths.update(candidates)
            unresolved += 1
            continue
        path = next(iter(candidates))
        value = local[path]
        if value['conflict'] or any(k in value['ids'] and value['ids'][k] != i for k, i in item['ids'].items()):
            ambiguous_paths.add(path)
            unresolved += 1
            continue
        # If the server supplies a mapped path and that path doesn't agree
        # with the matched ID, don't link identical provider IDs in another root.
        if not paths and any(arr.to_local({'mappings': mappings}, remote) for remote in item['paths']):
            ambiguous_paths.add(path)
            unresolved += 1
            continue
        evidence = 'Exact folder & verified provider ID' if paths and id_hits else 'Exact folder' if paths else 'Verified provider ID'
        choices[path].append((item, evidence))
        remote_ids[item['id']].add(path)
    links = {}
    for path, found in choices.items():
        if path in ambiguous_paths or len({x[0]['id'] for x in found}) != 1 or len(remote_ids[found[0][0]['id']]) != 1:
            unresolved += 1
            continue
        item, evidence = found[0]
        links[path] = (item['id'], evidence, item['library'])
    return links, unresolved


def rebuild(app, key, check=lambda: None):
    import media_servers as ms
    record = connections(app.CONFIG).get(key)
    if not record:
        return 'Connection removed; no mapping changed.'
    config = record.get('library_mapping') or {}
    selected = set(config.get('libraries') or [])
    if not selected:
        return 'Select server libraries first.'
    sig = signature(record)
    libraries = [l for l in discover(record) if l['id'] in selected]
    if {l['id'] for l in libraries} != selected:
        raise ValueError('A selected library is no longer available.')
    local = _local_titles(app, record, libraries)
    items = []
    for lib in libraries:
        for item in read_items(record, lib, check):
            items.append(item)
            if len(items) > MAX_ITEMS:
                raise ValueError('Mapping limit exceeded; select fewer libraries.')
    links, unresolved = match_items(local, items, config.get('paths', []))
    check()
    # Publish a complete snapshot, never partially replace existing mappings
    # on a network failure or after the user edits the connection.
    with ms._LOCK:
        live = connections(app.CONFIG).get(key)
        if not live or signature(live) != sig:
            return 'Settings changed; rebuild the mapping again.'
        now = int(time.time())
        with app.db() as conn:
            conn.execute('DELETE FROM media_server_links WHERE server_key=?', (key,))
            conn.executemany('INSERT INTO media_server_links(server_key,path,item_id,evidence,library_id,signature,checked,local_identity) VALUES(?,?,?,?,?,?,?,?)',
                             [(key, p, item, why, lib, sig, now, _identity_signature(local[p])) for p, (item, why, lib) in links.items()])
            conn.execute('INSERT OR REPLACE INTO media_mapping_report VALUES(?,?,?,?,?,?,?)',
                         (key, sig, now, len(local), len(links), unresolved, len(items)))
    return f'{len(links)} of {len(local)} local titles mapped · {unresolved} unresolved server items.'


def lookup(config, key, record, path):
    """A cached automatic mapping only; existing explicit mappings are separate."""
    db = Path(config) / 'library.db'
    if not db.is_file() or db.is_symlink() or not (record.get('library_mapping') or {}).get('libraries'):
        return None
    try:
        with closing(sqlite3.connect('file:' + urllib.parse.quote(str(db)) + '?mode=ro', uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT l.item_id,l.local_identity,s.library,s.name,m.tvdb,m.tmdb,m.imdb FROM media_server_links l "
                               "JOIN shows s ON s.path=l.path LEFT JOIN media_ids m ON m.path=s.path WHERE "
                               "l.server_key=? AND l.path=? AND l.signature=? AND l.checked>=? AND s.state<>'unavailable'",
                               (key, str(path), signature(record), int(time.time())-MAX_AGE)).fetchone()
        if not row or not _item_ok(record['kind'], row['item_id']):
            return None
        typ = 'movie' if row['library'] in ('Anime Movies', 'Movies') else 'tv'
        if row['local_identity'] != _identity_signature(_title_identity(row, typ)):
            return None
        return row['item_id']
    except sqlite3.Error:
        return None


def coverage(config, key, record):
    db = Path(config) / 'library.db'
    if not db.is_file() or db.is_symlink() or not (record.get('library_mapping') or {}).get('libraries'):
        return 0
    try:
        with closing(sqlite3.connect('file:' + urllib.parse.quote(str(db)) + '?mode=ro', uri=True, timeout=5)) as conn:
            row = conn.execute('SELECT matched FROM media_mapping_report WHERE server_key=? AND signature=? AND checked>=?',
                               (key, signature(record), int(time.time())-MAX_AGE)).fetchone()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


def _e(value):
    return html.escape(str(value), quote=True)


def page(app, handler):
    import media_servers as ms
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    note = query.get('notice', [''])[0][:200]
    key = query.get('server', [''])[0]
    servers = connections(app.CONFIG)
    with app.db() as conn:
        reports = {r['server_key']: r for r in conn.execute('SELECT * FROM media_mapping_report')}
    parts = [ms.settings_shell('Media server mappings', 'connect'), '</aside><main class="main">',
             '<p><a href="/media-servers?tab=media">← Media servers</a> · <a href="/system?tab=tasks">Tasks</a></p>',
             '<section class="panel"><h1>Automatic media-server mapping</h1>',
             '<p>Discover libraries, choose what to map, and translate server folder paths to your registered Preludarr roots. '
             'Exact path or unique verified provider IDs only; unresolved titles stay unmapped. Existing manual mappings win. '
             'Discovery and rebuilding are read-only on the media server; targeted refresh is separately opt-in.</p>',
             '<p class="notice" role="status">' + _e(note) + '</p>' if note else '', '</section>']
    for k, record in servers.items():
        config = record.get('library_mapping') or {}
        r = reports.get(k)
        valid = r and r['signature'] == signature(record) and r['checked'] >= int(time.time()) - MAX_AGE
        found = record.get('discovered_libraries', [])
        chosen = set(config.get('libraries', []))
        paths = '\n'.join(m['remote'] + ' => ' + m['local'] for m in config.get('paths', []))
        selected = ' open' if key == k else ''
        label = record.get('name') or k.replace('plex', 'Plex').replace('jellyfin', 'Jellyfin').replace('emby', 'Emby')
        parts.extend(['<section class="panel"><h2>' + _e(label) + '</h2>',
                      '<p class="muted">Host: ' + _e(record.get('url')) + '</p>',
                      '<p><strong>' + (f"{r['matched']} / {r['total']} mapped · {r['ambiguous']} unresolved server items" if valid else 'No current automatic mapping')
                      + '</strong> · ' + str(len(record.get('mappings') or {})) + ' manual mappings · Refresh '
                      + ('on' if refresh_enabled(k, record) else 'off') + '</p>',
                      '<form method="post" action="/media-servers/mapping"><input type="hidden" name="server" value="' + _e(k) +
                      '"><button name="action" value="discover" type="submit">Discover libraries</button></form>',
                      '<details' + selected + '><summary>Library selection &amp; path mappings</summary>',
                      '<form method="post" action="/media-servers/mapping"><input type="hidden" name="server" value="' + _e(k) + '">',
                      '<input type="hidden" name="action" value="configure">'])
        for lib in found:
            parts.extend(['<label class="check"><input type="checkbox" name="library" value="' + _e(lib['id']) + '"'
                          + (' checked' if lib['id'] in chosen else '') + '> ' + _e(lib['name']) + ' (' + _e(lib['type']) + ')</label>',
                          '<p class="muted">' + _e(' · '.join(lib['paths'])) + '</p>'])
        if not found:
            parts.append('<p class="muted">Discover your libraries first.</p>')
        parts.extend(['<label class="field"><span>Remote path =&gt; registered Preludarr root (one per line, up to 20, optional for unique provider-ID matching)</span>'
                      '<textarea name="paths" rows="4" placeholder="/data/tv/anime =&gt; /anime">' + _e(paths) + '</textarea></label>',
                      '<label class="check"><input type="checkbox" name="automatic" value="1"' + (' checked' if config.get('automatic') else '') +
                      '> Rebuild automatically once a day</label>',
                      '<label class="check"><input type="checkbox" name="refresh" value="1"' + (' checked' if refresh_enabled(k, record) else '') +
                      '> Enable targeted refresh after successful theme installs</label>',
                      '<button type="submit">Save mapping settings</button></form>',
                      '<form method="post" action="/media-servers/mapping" style="margin-top:10px"><input type="hidden" name="server" value="' + _e(k) +
                      '"><button name="action" value="rebuild" type="submit">Rebuild mapping now</button></form></details>'])
        if '-' in k:
            parts.extend(['<details><summary>Edit extra connection</summary>', _connection_form(k, record),
                          '<form method="post" action="/media-servers/mapping"><input type="hidden" name="server" value="' + _e(k) +
                          '"><input type="hidden" name="action" value="remove"><label class="check"><input type="checkbox" name="confirm" value="1" required> Remove only this connection</label>'
                          '<button type="submit">Remove connection</button></form></details>'])
        if valid:
            with app.db() as conn:
                samples = conn.execute('SELECT path,item_id,evidence FROM media_server_links WHERE server_key=? AND signature=? ORDER BY path LIMIT 20',
                                       (k, signature(record))).fetchall()
            parts.append('<details><summary>Mapped titles (first 20)</summary><ul>')
            for item in samples:
                link = '/media-servers?' + urllib.parse.urlencode({'path': item['path'], 'tab': 'media'})
                parts.append('<li><a href="' + _e(link) + '">' + _e(Path(item['path']).name) + '</a> · ' + _e(item['item_id']) + ' · ' + _e(item['evidence']) + '</li>')
            parts.append('</ul></details>')
        parts.append('</section>')
    parts.extend(['<section class="panel"><h2>Add another media server</h2><p class="muted">Use the original Media servers tab for the first connection of each type. '
                  'Additional instances have independent libraries, path maps, and refresh settings.</p>', _connection_form('', {}),
                  '</section></main></body></html>'])
    return handler.send(200, ''.join(parts))


def _connection_form(key, record):
    hidden = '<input type="hidden" name="server" value="' + _e(key) + '">'
    kind = ('<input type="hidden" name="kind" value="' + _e(record['kind']) + '">' if key else
            '<label class="field"><span>Type</span><select name="kind"><option value="plex">Plex</option><option value="jellyfin">Jellyfin</option><option value="emby">Emby</option></select></label>')
    return ('<form method="post" action="/media-servers/mapping">' + hidden + kind +
            '<input type="hidden" name="action" value="extra"><label class="field"><span>Instance name</span>'
            '<input name="name" maxlength="60" required value="' + _e(record.get('name') or key) + '"></label>'
            '<label class="field"><span>URL (HTTPS or private-LAN HTTP)</span><input type="url" name="url" maxlength="200" required value="' + _e(record.get('url', '')) + '"></label>'
            '<label class="field"><span>API token</span><input type="password" name="api_key" maxlength="256" autocomplete="new-password" placeholder="'
            + ('Leave blank to keep' if key else 'Required') + '"></label><button type="submit">Save connection</button></form>')


def post(app, handler, redirect):
    import media_servers as ms
    key = ''
    try:
        n = int(handler.headers.get('Content-Length', '0'))
        if not 0 < n <= 32768:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(n).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=MAX_LIBRARIES+15)
        if any(len(v) != 1 for k, v in form.items() if k != 'library'):
            raise ValueError('Invalid form.')
        f = lambda k: form.get(k, [''])[0]
        key, action = f('server'), f('action')
        if key and not KEY.fullmatch(key):
            raise ValueError('Unknown connection.')
        if action == 'extra':
            kind = f('kind')
            if kind not in ('plex', 'jellyfin', 'emby') or (key and (key == kind or kind != kind_of(key))):
                raise ValueError('Choose an extra media server.')
            url, token = ms._base_url(f('url')), f('api_key')
            if token and (len(token) > 256 or any(ord(c) < 33 or ord(c) > 126 or c in ('"', '\\') for c in token)):
                raise ValueError('Invalid API token.')
            with ms._LOCK:
                all_servers = ms._read(app.CONFIG, 'servers')
                if not key:
                    if not all_servers.get(kind, {}).get('url'):
                        raise ValueError('Configure the first connection on Media servers first.')
                    key = next((kind+'-'+str(n) for n in range(2, 10) if kind+'-'+str(n) not in all_servers), '')
                    if not key:
                        raise ValueError('Instance limit reached.')
                elif key not in all_servers:
                    raise ValueError('Unknown connection.')
                old = dict(all_servers.get(key, {}))
                if not (token or old.get('api_key')):
                    raise ValueError('API token required.')
                if old.get('url') and old['url'] != url:
                    # Existing explicit item IDs belong to the old server.
                    # Retain them for the owner, but require a deliberate
                    # reconfiguration instead of refreshing the new hostname.
                    raise ValueError('Add a new connection for a different server URL.')
                old.update(kind=kind, url=url, api_key=token or old.get('api_key'), name=f('name')[:60])
                old.setdefault('mapping_refresh', False)
                all_servers[key] = old
                ms._write(app.CONFIG, 'servers', all_servers)
            message = 'Connection saved. Discover libraries to continue.'
        else:
            record = connections(app.CONFIG).get(key)
            if not record:
                raise ValueError('Unknown connection.')
            if action == 'remove':
                if '-' not in key or f('confirm') != '1':
                    raise ValueError('Confirm removal of the extra connection.')
                with ms._LOCK:
                    all_servers = ms._read(app.CONFIG, 'servers')
                    if key not in all_servers or signature(all_servers[key]) != signature(record):
                        raise ValueError('Connection changed; reopen this page.')
                    del all_servers[key]
                    ms._write(app.CONFIG, 'servers', all_servers)
                    # still under the lock, so a new connection cannot reuse the key meanwhile
                    with app.db() as conn:
                        conn.execute('DELETE FROM media_server_links WHERE server_key=?', (key,))
                        conn.execute('DELETE FROM media_mapping_report WHERE server_key=?', (key,))
                message = 'Connection removed. No media or installed themes were changed.'
            elif action == 'discover':
                found = discover(record)
                with ms._LOCK:
                    all_servers = ms._read(app.CONFIG, 'servers')
                    if key not in all_servers or signature(all_servers[key]) != signature(record):
                        raise ValueError('Connection changed while discovering.')
                    all_servers[key]['discovered_libraries'] = found
                    ms._write(app.CONFIG, 'servers', all_servers)
                message = f'Discovered {len(found)} libraries. Select the ones to map.'
            elif action == 'configure':
                selected = list(dict.fromkeys(form.get('library', [])))
                available = {x['id'] for x in record.get('discovered_libraries', [])}
                if not set(selected).issubset(available):
                    raise ValueError('Discover libraries again.')
                if sum(1 for line in f('paths').splitlines() if line.strip()) > MAX_PATH_MAPPINGS:
                    raise ValueError('Too many path mappings.')
                paths = arr._parse_mappings(app, f('paths'))
                if len({arr._key(m['remote']) for m in paths}) != len(paths):
                    raise ValueError('Each remote path must have one mapping.')
                with ms._LOCK:
                    all_servers = ms._read(app.CONFIG, 'servers')
                    if key not in all_servers or signature(all_servers[key]) != signature(record):
                        raise ValueError('Connection changed; reopen this page.')
                    live = all_servers[key]
                    live['library_mapping'] = {'libraries': selected, 'paths': paths, 'automatic': f('automatic') == '1'}
                    live['enabled' if key == kind_of(key) else 'mapping_refresh'] = f('refresh') == '1'
                    ms._write(app.CONFIG, 'servers', all_servers)
                if selected:
                    tasks.enqueue(app, 'mapping', 'mapping:' + key, {'server': key})
                message = 'Saved. Selected libraries will be mapped in Tasks.'
            elif action == 'rebuild':
                if not (record.get('library_mapping') or {}).get('libraries'):
                    raise ValueError('Select libraries first.')
                tasks.enqueue(app, 'mapping', 'mapping:' + key, {'server': key})
                message = 'Mapping task queued. Check System → Tasks for progress.'
            else:
                raise ValueError('Unknown action.')
    except (ValueError, OSError, UnicodeError, TypeError, KeyError, ET.ParseError):
        message = 'Mapping action could not finish. Check the connection, selected libraries and registered paths.'
    return redirect(handler, '/media-servers/mapping?' + urllib.parse.urlencode({'server': key, 'notice': message}))
