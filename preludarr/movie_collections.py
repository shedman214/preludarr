# SPDX-License-Identifier: GPL-3.0-or-later
"""Collections (Preludarr 1.12): a theme song for each movie collection in Plex, Jellyfin and Emby.

A collection only exists inside the media server, so:
  * Plex: the theme is uploaded to the collection (POST /library/metadata/<id>/themes) and can be removed again.
  * Jellyfin / Emby (experimental): theme.mp3 is written into the server's own folder for the collection
    ("<Name> [boxset]"), which the user maps into Preludarr. Neither server documents collection themes officially.
The song is the theme.mp3 already installed for one of the collection's movies: by default the earliest movie
(user 2026-10-30: "first movie's theme"), with the other movies as alternatives. Nothing is downloaded here.
Opening the page never contacts a server; only "Refresh" does, in the background.
"""
import hashlib
import html
import subprocess
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

FILE = 'preludarr_collections.json'
BACKUPS = 'collection_theme_backups'
POSTERS = 'collection_posters'
MAX_POSTER = 3 * 1024 * 1024
_THUMB = re.compile(r'/library/[A-Za-z0-9/_:.-]{1,200}')
_THEME = re.compile(r'/library/metadata/[0-9]{1,20}/theme/[0-9]{1,20}')
KINDS = {'plex': 'Plex', 'jellyfin': 'Jellyfin', 'emby': 'Emby'}
FOLDER_KINDS = ('jellyfin', 'emby')
VIDEO = re.compile(r'\.(mkv|mp4|m4v|avi|mov|wmv|ts|m2ts|webm|mpg|mpeg|iso)\Z', re.I)
MAX_COLLECTIONS, MAX_MEMBERS = 500, 300
WORKERS = 6   # collections read from the media server at once
MIN_MP3, MAX_MP3 = 1024, 40 * 1024 * 1024
_LOCK = threading.RLock()
_RUN = {}  # kind -> {'running': bool, 'done': int, 'total': int}


def _e(value):
    return html.escape(str(value), quote=True)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ------------------------------------------------------------------------------ storage ----
def _file(app):
    return Path(app.CONFIG) / FILE


def load(app):
    try:
        data = json.loads(_file(app).read_text())
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        data = {}
    data.setdefault('folders', {})
    data.setdefault('servers', {})
    data.setdefault('installed', {})
    return data


def _save(app, data):
    path = _file(app)
    tmp = path.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _kind(key):
    return str(key).split('-', 1)[0]


def _label(key):
    """'Plex', or 'Plex — <server name>' for extra Plex servers (1.12, several Plex servers)."""
    return _LABELS.get(key) or KINDS.get(_kind(key), 'Media server')


_LABELS = {}


def _servers(app):
    """Enabled media servers from Settings → Connect: the main Plex, extra Plex servers, Jellyfin, Emby (in that order)."""
    ms = __import__('media_servers')
    found = {k: v for k, v in ms._server_map(app.CONFIG).items() if v.get('enabled') and v.get('url') and v.get('api_key')}
    out = {}
    plex = ms.plex_servers(app.CONFIG)
    for k, v in plex:
        if k == 'plex' and 'plex' not in found:
            continue
        if v.get('enabled', True):
            out[k] = v
            _LABELS[k] = 'Plex' + (' — ' + str(v.get('name'))[:40] if v.get('name') and len(plex) > 1 else '')
    out.update({k: v for k, v in found.items() if k != 'plex'})
    return out


# ------------------------------------------------------------------------------ reading servers ----
def _json(server, path, limit=8_000_000):
    return json.loads(__import__('media_servers')._call(server, 'GET', path, timeout=15, limit=limit))


def _folder_of(file_path):
    """Title folder name of a movie, from the path the server reports (a file, or the movie's folder)."""
    p = str(file_path or '').replace('\\', '/').rstrip('/')
    if not p:
        return ''
    parts = p.split('/')
    return (parts[-2] if VIDEO.search(parts[-1]) and len(parts) > 1 else parts[-1]).strip()


def _year(value):
    try:
        y = int(value)
        return y if 1870 <= y <= 2100 else None
    except (TypeError, ValueError):
        return None


def _plex_members(server, rating_key):
    kids = _json(server, '/library/collections/' + rating_key + '/children').get('MediaContainer', {}).get('Metadata') or []
    members = []
    for k in kids[:MAX_MEMBERS]:
        file = next((p.get('file') for media in (k.get('Media') or []) for p in (media.get('Part') or []) if p.get('file')), '')
        members.append({'title': str(k.get('title', ''))[:200], 'year': _year(k.get('year')), 'folder': _folder_of(file)})
    return members


def _plex_collections(server, progress, previous=None):
    """Plex movie collections. Only collections that are new or changed in Plex (updatedAt / childCount) are read again,
    and up to WORKERS of them at once (1.12: a first read of 311 collections went from ~213 s to well under a minute)."""
    previous = previous or {}
    sections = _json(server, '/library/sections').get('MediaContainer', {}).get('Directory') or []
    found = []
    for sec in sections:
        if sec.get('type') != 'movie' or not re.fullmatch(r'[0-9]{1,10}', str(sec.get('key', ''))):
            continue
        meta = _json(server, '/library/sections/' + str(sec['key']) + '/collections').get('MediaContainer', {}).get('Metadata') or []
        locations = [str(loc.get('path') or '')[:300] for loc in sec.get('Location') or [] if loc.get('path')][:10]
        found += [dict(m, _library=str(sec.get('title', ''))[:80], _locations=locations) for m in meta if re.fullmatch(r'[0-9]{1,20}', str(m.get('ratingKey', '')))]
    found = found[:MAX_COLLECTIONS]
    stamp = lambda m: str(m.get('updatedAt', '')) + '/' + str(m.get('childCount', ''))  # noqa: E731
    todo = [m for m in found if not (previous.get(str(m['ratingKey'])) or {}).get('stamp') or previous[str(m['ratingKey'])]['stamp'] != stamp(m)]
    progress(len(found) - len(todo), len(found))
    members = {}
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for rk, result in zip([str(m['ratingKey']) for m in todo], pool.map(lambda m: _plex_members(server, str(m['ratingKey'])), todo)):
                members[rk] = result
                progress(len(found) - len(todo) + len(members), len(found))
    out = []
    for m in found:
        rk = str(m['ratingKey'])
        thumb = str(m.get('thumb') or m.get('composite') or '')
        theme = str(m.get('theme') or '')
        out.append({'id': rk, 'title': str(m.get('title', ''))[:200], 'library': m['_library'], 'locations': m['_locations'], 'has_theme': bool(theme),
                    'theme_path': theme if _THEME.fullmatch(theme) else '',
                    'members': members[rk] if rk in members else previous[rk]['members'], 'stamp': stamp(m),
                    'poster': thumb if _THUMB.fullmatch(thumb) else ''})
    return out


def _jf_collections(server, kind, progress):
    prefix = '/emby' if _kind(kind) == 'emby' else ''
    q = urllib.parse.urlencode({'IncludeItemTypes': 'BoxSet', 'Recursive': 'true', 'Fields': 'Path', 'EnableImageTypes': 'Primary', 'Limit': str(MAX_COLLECTIONS)})
    sets = [s for s in (_json(server, prefix + '/Items?' + q).get('Items') or []) if re.fullmatch(r'[A-Za-z0-9-]{1,80}', str(s.get('Id', '')))]
    progress(0, len(sets))

    def members(s):
        q = urllib.parse.urlencode({'ParentId': s['Id'], 'Fields': 'Path,ProductionYear', 'Limit': str(MAX_MEMBERS)})
        kids = _json(server, prefix + '/Items?' + q).get('Items') or []
        return [{'title': str(k.get('Name', ''))[:200], 'year': _year(k.get('ProductionYear')), 'folder': _folder_of(k.get('Path'))} for k in kids]
    out = []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, (s, found) in enumerate(zip(sets, pool.map(members, sets)), 1):
            out.append({'id': str(s['Id']), 'title': str(s.get('Name', ''))[:200], 'path_name': _folder_of(s.get('Path')), 'members': found,
                        'poster': 'primary' if (s.get('ImageTags') or {}).get('Primary') else ''})
            progress(i, len(sets))
    return out


def refresh(app, kind):
    """Read the collections of one server in the background. Returns False if a refresh is already running."""
    server = _servers(app).get(kind)
    if server is None:
        raise ValueError(_label(kind) + ' is not connected (Settings → Connect).')
    with _LOCK:
        if _RUN.get(kind, {}).get('running'):
            return False
        _RUN[kind] = {'running': True, 'done': 0, 'total': 0}

    def progress(done, total):
        _RUN[kind].update(done=done, total=total)

    def work():
        try:
            with _LOCK:
                before = {i['id']: i for i in (load(app)['servers'].get(kind) or {}).get('items') or [] if 'poster' in i}
            items = _plex_collections(server, progress, before) if _kind(kind) == 'plex' else _jf_collections(server, kind, progress)
            result = {'refreshed': _now(), 'error': '', 'items': items}
        except Exception as error:  # never show URLs, tokens or response bodies
            result = {'refreshed': _now(), 'error': __import__('neterrors').text(error, _label(kind)), 'items': None}
        with _LOCK:
            data = load(app)
            old = data['servers'].get(kind) or {}
            if result['items'] is None:
                result['items'] = old.get('items') or []
            data['servers'][kind] = result
            _save(app, data)
            _RUN[kind]['running'] = False
        try:
            _prune_posters(app, kind, result['items'])
        except OSError:
            pass
        app.event('Collections', 'refreshed' if not result['error'] else 'failed',
                  _label(kind) + ': ' + (str(len(result['items'])) + ' collections' if not result['error'] else result['error']))

    threading.Thread(target=work, name='preludarr-collections-' + kind, daemon=True).start()
    return True


# ------------------------------------------------------------------------------ choosing a song ----
def _title_index(app):
    """Title folder name → [library paths] for every title Preludarr knows."""
    index = {}
    with app.db() as c:
        for (path,) in c.execute('SELECT path FROM shows'):
            index.setdefault(Path(path).name.casefold(), []).append(path)
    return index


def _has_theme(path):
    try:
        st = os.lstat(Path(path) / 'theme.mp3')
        return stat.S_ISREG(st.st_mode) and MIN_MP3 <= st.st_size <= MAX_MP3
    except OSError:
        return False


def _tail(path, n=2):
    parts = [x.casefold() for x in str(path).replace('\\', '/').split('/') if x]
    return parts[-n:]


def in_your_libraries(item, roots, cands, by_folder=True):
    """1.12 (user: "only collections from the libraries users add here"): a Plex collection belongs when its Plex library's folder
    is one of the root folders (last two folder names match, e.g. /data/media/movies/movies ↔ /media/movies/movies). Servers that do not
    tie collections to a folder (Jellyfin, Emby), and lists read before 1.12, use: at least one of its movies is in a root folder."""
    locations = item.get('locations')
    if locations and by_folder:
        tails = [_tail(r) for r in roots]
        return any(len(_tail(loc)) == 2 and _tail(loc) in tails for loc in locations)
    return any(c['path'] for c in cands)


def _root_for(item, cands, roots, by_folder):
    """The movie root folder a collection belongs to: the one its library folder matches, else the one holding most of its movies."""
    if by_folder and item.get('locations'):
        for loc in item['locations']:
            for r in roots:
                if len(_tail(loc)) == 2 and _tail(loc) == _tail(r):
                    return r
        return None
    best, hits = None, 0
    for r in roots:
        n = sum(1 for c in cands if c['path'] and (c['path'] + '/').startswith(r.rstrip('/') + '/'))
        if n > hits:
            best, hits = r, n
    return best


def candidates(app, item, index=None):
    """The collection's movies in order (earliest first), each with its library path (if found) and whether it has a theme."""
    index = _title_index(app) if index is None else index
    out = []
    for m in item.get('members') or []:
        paths = index.get(str(m.get('folder', '')).casefold()) or []
        path = paths[0] if len(paths) == 1 else ''
        out.append(dict(m, path=path, theme=bool(path) and _has_theme(path)))
    return sorted(out, key=lambda m: (m.get('year') or 9999, m.get('title', '').casefold()))


# ------------------------------------------------------------------------------ installing ----
def _read_source(app, item, source):
    """theme.mp3 bytes of one of THIS collection's movies (never any other file)."""
    allowed = {c['path'] for c in candidates(app, item) if c['path'] and c['theme']}
    if source not in allowed:
        raise ValueError('Choose the theme of one of this collection’s movies.')
    fd = os.open(Path(source) / 'theme.mp3', os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or not MIN_MP3 <= st.st_size <= MAX_MP3:
            raise ValueError('That theme.mp3 is not usable.')
        data = f.read(MAX_MP3 + 1)
    if not (data[:3] == b'ID3' or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0)):
        raise ValueError('That theme.mp3 is not an MP3 file.')
    return data


def _collection_dir(app, kind, item):
    """The server's own folder for this collection, as mapped into Preludarr (Jellyfin/Emby)."""
    base = load(app)['folders'].get(kind) or ''
    name = item.get('path_name') or ''
    if not base:
        raise ValueError('Set the ' + _label(kind) + ' collections folder first (below).')
    if not name or '/' in name or name.startswith('.') or name in ('..',):
        raise ValueError(_label(kind) + ' did not report a folder for this collection.')
    target = Path(base) / name
    if target.is_symlink() or not target.is_dir():
        raise ValueError('The folder “' + name + '” was not found in ' + base + '. Check the mapping, or let ' + _label(kind) + ' create the collection first.')
    return target


def _write_folder_theme(app, kind, item, data):
    target = _collection_dir(app, kind, item)
    dest = target / 'theme.mp3'
    backup = ''
    if dest.is_symlink():
        raise ValueError('theme.mp3 in that folder is a link; not replaced.')
    if dest.exists():
        folder = Path(app.CONFIG) / BACKUPS
        folder.mkdir(mode=0o700, exist_ok=True)
        backup = kind + '-' + re.sub(r'[^A-Za-z0-9-]', '', item['id'])[:80] + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.mp3'
        shutil.copy2(dest, folder / backup)
    fd, tmp = tempfile.mkstemp(prefix='.preludarr-', suffix='.mp3', dir=target)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            __import__('permissions').set_theme_owner(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def _item(app, kind, item_id):
    if kind not in _servers(app):
        raise ValueError(_label(kind) + ' is not connected.')
    with _LOCK:
        data = load(app)
    item = next((i for i in (data['servers'].get(kind) or {}).get('items') or [] if i['id'] == item_id), None)
    if item is None:
        raise ValueError('Collection not found — press Refresh.')
    return item


def _deliver(app, kind, item, audio, record, report=None):
    """Put MP3 bytes on the collection (Plex upload, or the Jellyfin/Emby collection folder) and remember it."""
    servers = _servers(app)
    if report:
        report('installing', ('Uploading the theme to ' + _label(kind)) if _kind(kind) == 'plex' else 'Writing theme.mp3 into the collection folder')
    theme_path = ''
    item_id = item['id']
    record = dict(record, sha256=hashlib.sha256(audio).hexdigest(), at=_now())
    ms = __import__('media_servers')
    if _kind(kind) == 'plex':
        if not re.fullmatch(r'[0-9]{1,20}', item_id):
            raise ValueError('Invalid collection')
        ms._call(servers[kind], 'POST', '/library/metadata/' + item_id + '/themes', data=audio, timeout=120)
        try:  # where Plex now keeps the theme, for "Current theme ▶"
            meta = (_json(servers[kind], '/library/metadata/' + item_id).get('MediaContainer', {}).get('Metadata') or [{}])[0]
            theme_path = str(meta.get('theme') or '') if _THEME.fullmatch(str(meta.get('theme') or '')) else ''
        except Exception:
            theme_path = ''
    else:
        record['backup'] = _write_folder_theme(app, kind, item, audio)
        try:
            ms._refresh_one(servers[kind], item_id)
        except Exception:
            pass  # the file is in place; the server picks it up on its next scan
    with _LOCK:
        data = load(app)
        data['installed'].setdefault(kind, {})[item_id] = record
        for i in (data['servers'].get(kind) or {}).get('items') or []:
            if i['id'] == item_id:
                i['has_theme'] = True
                i['theme_path'] = theme_path or i.get('theme_path', '')
        _save(app, data)
    app.event('Collections', 'installed', item['title'] + ' (' + _label(kind) + '): theme from ' + record['source_name'])
    return item['title'], record['source_name']


def install(app, kind, item_id, source, report=None):
    item = _item(app, kind, item_id)
    audio = _read_source(app, item, source)
    return _deliver(app, kind, item, audio, {'source': source, 'source_name': Path(source).name}, report)


# ------------------------------------------------------------------------------ other sources (1.12, user: YouTube link, own audio, YouTube search) ----
MAX_SECONDS = 600


def manual_enabled():
    return __import__('youtube_manual').enabled()


def _probe_seconds(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=codec_type:format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], capture_output=True, timeout=30, check=False)
    lines = r.stdout.decode('ascii', 'replace').split()
    if r.returncode or 'audio' not in lines:
        raise ValueError('That file contains no usable audio.')
    try:
        return float(next(x for x in lines if re.fullmatch(r'[0-9.]+', x)))
    except StopIteration:
        raise ValueError('Could not read how long that audio is.') from None


def _to_mp3(src, config):
    """Convert any audio file to theme MP3 bytes (inside a private temporary folder in /config)."""
    seconds = _probe_seconds(src)
    if not 1 <= seconds <= MAX_SECONDS:
        raise ValueError('The audio must be between 1 second and 10 minutes long.')
    with tempfile.TemporaryDirectory(prefix='.preludarr-collection-', dir=config) as td:
        out = Path(td) / 'theme.mp3'
        r = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(src), '-vn', '-map_metadata', '-1',
                            '-codec:a', 'libmp3lame', '-q:a', '4', str(out)], capture_output=True, timeout=180, check=False)
        if r.returncode or not out.is_file():
            raise ValueError('The audio could not be converted to MP3.')
        data = out.read_bytes()
    if not MIN_MP3 <= len(data) <= MAX_MP3:
        raise ValueError('The converted MP3 has an unexpected size.')
    return data


def install_youtube(app, kind, item_id, url, report=None):
    report = report or (lambda *a: None)
    if not manual_enabled():
        raise ValueError('YouTube links are off. Turn on manual installs (PRELUDARR_MANUAL_INSTALL_ENABLED=1) to use them.')
    item = _item(app, kind, item_id)
    yd = __import__('youtube_download')
    report('checking', 'Checking the YouTube link')
    yd.canonical_url(url)   # plain error for a wrong link before anything is downloaded
    report('downloading', 'Downloading the audio from YouTube')
    details = yd.download_to_stage(url, app.CONFIG)
    try:
        report('converting', 'Converting “' + details['title'][:80] + '” to MP3')
        audio = _to_mp3(details['audio'], app.CONFIG)
    finally:
        yd.cleanup_stage(details['stage'], app.CONFIG)
    return _deliver(app, kind, item, audio, {'source': 'youtube:' + details['video_id'], 'source_name': 'YouTube: ' + details['title']}, report)


def install_upload(app, handler, report=None):
    """multipart/form-data: server, id, root, audio (one file). Returns (kind, title, source name)."""
    report = report or (lambda *a: None)
    if not manual_enabled():
        raise ValueError('Uploads are off. Turn on manual installs (PRELUDARR_MANUAL_INSTALL_ENABLED=1) to use them.')
    from email import policy
    from email.parser import BytesParser
    ct = handler.headers.get('Content-Type', '')
    size = int(handler.headers.get('Content-Length', '0') or 0)
    if not ct.lower().startswith('multipart/form-data;') or len(ct) > 250 or not 0 < size <= MAX_MP3 + 65536:
        raise ValueError('Choose an audio file of up to 40 MB.')
    body = handler.rfile.read(size)
    msg = BytesParser(policy=policy.default).parsebytes(b'MIME-Version: 1.0\r\nContent-Type: ' + ct.encode('ascii', 'replace') + b'\r\n\r\n' + body)
    fields, upload, name = {}, None, ''
    for part in msg.iter_parts() if msg.is_multipart() else []:
        key = part.get_param('name', header='Content-Disposition')
        if key == 'audio' and part.get_filename():
            upload, name = part.get_payload(decode=True), str(part.get_filename())[:120]
        elif key in ('server', 'id', 'root'):
            fields[key] = (part.get_payload(decode=True) or b'').decode('utf-8', 'replace')[:100]
    kind = fields.get('server', '')
    if not re.fullmatch(r'(plex|jellyfin|emby)(-[2-9])?', kind):
        raise ValueError('Invalid form.')
    if not upload or not re.search(r'\.(mp3|m4a|aac|ogg|opus|flac|wav)$', name, re.I):
        raise ValueError('Choose an audio file (MP3, M4A, OGG, FLAC or WAV).')
    item = _item(app, kind, fields.get('id', ''))
    handler._col_where = (fields.get('root', ''), item['id'])
    handler._col_kind = kind
    report('converting', 'Converting “' + name[:80] + '” to MP3')
    with tempfile.TemporaryDirectory(prefix='.preludarr-upload-', dir=app.CONFIG) as td:
        src = Path(td) / 'upload.audio'
        src.write_bytes(upload)
        audio = _to_mp3(src, app.CONFIG)
    title, source = _deliver(app, kind, item, audio, {'source': 'upload', 'source_name': 'Upload: ' + name}, report)
    return kind, title, source


CHANNEL_LINK = '<a href="https://www.youtube.com/@mainthemesoundtracks" target="_blank" rel="noopener noreferrer">MainThemeSoundtracks ↗</a>'


def _search_key(kind, item):
    return 'collection:' + kind + ':' + item['id']


def _search_name(item):
    return re.sub(r'\s*\b(collection|saga|trilogy|series)\b\s*$', '', item['title'], flags=re.I).strip() or item['title']


def search_youtube(app, kind, item_id, everywhere=False):
    item = _item(app, kind, item_id)
    found, note = __import__('youtube_search').suggest(app, _search_key(kind, item), _search_name(item), 'Movies', interactive=True, everywhere=everywhere)
    return item['title'], found, note


def cached_search(app, kind, item):
    ys = __import__('youtube_search')
    try:
        found, _ = ys._cached(app, _search_key(kind, item), ys.query_for(_search_name(item), 'Movies', app.clean, app.year))
    except Exception:
        found = None
    return found


def remove(app, kind, item_id):
    """Undo an install made by Preludarr."""
    servers = _servers(app)
    with _LOCK:
        data = load(app)
    record = (data['installed'].get(kind) or {}).get(item_id)
    item = next((i for i in (data['servers'].get(kind) or {}).get('items') or [] if i['id'] == item_id), None)
    if record is None or item is None or kind not in servers:
        raise ValueError('Nothing installed by Preludarr for this collection.')
    if _kind(kind) == 'plex':
        if not re.fullmatch(r'[0-9]{1,20}', item_id):
            raise ValueError('Invalid collection')
        __import__('media_servers')._call(servers[kind], 'DELETE', '/library/metadata/' + item_id + '/theme', timeout=30)
    else:
        dest = _collection_dir(app, kind, item) / 'theme.mp3'
        backup = Path(app.CONFIG) / BACKUPS / record.get('backup', '') if record.get('backup') else None
        if backup and backup.is_file() and not backup.is_symlink():
            fd, tmp = tempfile.mkstemp(prefix='.preludarr-', suffix='.mp3', dir=dest.parent)
            os.close(fd)
            shutil.copy2(backup, tmp)
            os.replace(tmp, dest)
        elif dest.is_file() and not dest.is_symlink():
            dest.unlink()
    with _LOCK:
        data = load(app)
        (data['installed'].get(kind) or {}).pop(item_id, None)
        for i in (data['servers'].get(kind) or {}).get('items') or []:
            if i['id'] == item_id:
                i['has_theme'] = _kind(kind) != 'plex' and bool(record.get('backup'))
        _save(app, data)
    app.event('Collections', 'removed', item['title'] + ' (' + _label(kind) + ')')
    return item['title']


def set_folder(app, kind, raw):
    if _kind(kind) not in FOLDER_KINDS or kind not in FOLDER_KINDS:
        raise ValueError('Only Jellyfin and Emby use a collections folder.')
    text = str(raw or '').strip()
    if text:
        p = Path(os.path.normpath(text))
        if not text.startswith('/') or '\x00' in text or len(text) > 512:
            raise ValueError('Enter the folder as Preludarr sees it, starting with /.')
        blocked = ('/bin', '/boot', '/dev', '/etc', '/lib', '/proc', '/root', '/run', '/sbin', '/sys', '/usr', '/var', '/app',
                   str(Path(app.CONFIG)), str(Path(__file__).resolve().parent))
        if p == Path('/') or any(p == Path(b) or Path(b) in p.parents for b in blocked):
            raise ValueError('Choose the folder where ' + _label(kind) + ' keeps its collections, not a system or Preludarr folder.')
        if os.path.realpath(p) != str(p) or not p.is_dir():
            raise ValueError('That folder does not exist inside Preludarr (or is a link). Map it in your Docker settings first.')
        if not os.access(p, os.W_OK):
            raise ValueError('Preludarr cannot write to that folder. Map it read/write, owned by PUID/PGID.')
        text = str(p)
    with _LOCK:
        data = load(app)
        data['folders'][kind] = text
        _save(app, data)


# ------------------------------------------------------------------------------ posters ----
def _poster_file(app, key, item):
    tag = hashlib.sha256((key + '|' + item['id'] + '|' + item.get('poster', '')).encode()).hexdigest()[:16]
    return Path(app.CONFIG) / POSTERS / (re.sub(r'[^a-z0-9-]', '', key) + '-' + tag + '.img')


def _image_type(data):
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def poster(app, handler):
    """The collection's artwork from its media server, cached in /config/collection_posters (the token never leaves Preludarr)."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    key, item_id = q.get('server', [''])[0], q.get('id', [''])[0]

    def missing():
        handler.send_response(404); handler.send_header('Content-Length', '0'); handler.send_header('Cache-Control', 'no-store'); handler.end_headers()
    servers = _servers(app)
    if key not in servers:
        return missing()
    with _LOCK:
        item = next((i for i in (load(app)['servers'].get(key) or {}).get('items') or [] if i['id'] == item_id), None)
    if item is None or not item.get('poster'):
        return missing()
    path = _poster_file(app, key, item)
    data = b''
    try:
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
        else:
            ms = __import__('media_servers')
            if _kind(key) == 'plex':
                if not _THUMB.fullmatch(item['poster']):
                    return missing()
                url = '/photo/:/transcode?' + urllib.parse.urlencode({'width': 240, 'height': 360, 'minSize': 1, 'upscale': 1, 'url': item['poster']})
            else:
                if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', item_id):
                    return missing()
                url = ('/emby' if _kind(key) == 'emby' else '') + '/Items/' + item_id + '/Images/Primary?maxWidth=240&quality=85'
            data = ms._call(servers[key], 'GET', url, timeout=20, limit=MAX_POSTER)
            if not _image_type(data):
                return missing()
            path.parent.mkdir(mode=0o700, exist_ok=True)
            tmp = path.with_name('.' + path.name + '.part')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
    except Exception:
        return missing()
    ctype = _image_type(data)
    if not ctype:
        return missing()
    handler.send_response(200)
    for k, v in (('Content-Type', ctype), ('Content-Length', str(len(data))), ('Cache-Control', 'private, max-age=86400'), ('X-Content-Type-Options', 'nosniff')):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)


def current_theme(app, handler):
    """Play the theme a collection has now (1.12): from Plex through Preludarr, or the theme.mp3 in the Jellyfin/Emby collection folder."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    key, item_id = q.get('server', [''])[0], q.get('id', [''])[0]
    data = b''
    try:
        servers = _servers(app)
        item = _item(app, key, item_id)
        if _kind(key) == 'plex':
            path = item.get('theme_path') or ''
            if not _THEME.fullmatch(path):
                meta = (_json(servers[key], '/library/metadata/' + item['id']).get('MediaContainer', {}).get('Metadata') or [{}])[0]
                path = str(meta.get('theme') or '')
            if _THEME.fullmatch(path):
                data = __import__('media_servers')._call(servers[key], 'GET', path, timeout=60, limit=MAX_MP3)
        else:
            fd = os.open(_collection_dir(app, key, item) / 'theme.mp3', os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as f:
                data = f.read(MAX_MP3 + 1)
    except Exception:
        data = b''
    if not data or len(data) > MAX_MP3:
        handler.send_response(404); handler.send_header('Content-Length', '0'); handler.end_headers()
        return
    handler.send_response(200)
    for k, v in (('Content-Type', 'audio/mpeg'), ('Content-Length', str(len(data))), ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')):
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)


def _prune_posters(app, key, items):
    """After a refresh: drop cached posters of collections that are gone or whose artwork changed."""
    folder = Path(app.CONFIG) / POSTERS
    if not folder.is_dir() or folder.is_symlink():
        return
    keep = {_poster_file(app, key, i).name for i in items if i.get('poster')}
    prefix = re.sub(r'[^a-z0-9-]', '', key) + '-'
    for f in folder.iterdir():
        if f.name.startswith(prefix) and len(f.name) == len(prefix) + 20 and f.name not in keep and f.is_file() and not f.is_symlink():
            f.unlink()


# ------------------------------------------------------------------------------ web ----
def _where_url(kind, notice, root='', at=''):
    """Back to the same tab and the same collection (1.12: user — install used to jump back to the first tab)."""
    q = {'server': kind, 'notice': notice[:300]}
    if root:
        q['root'] = root[:600]
    if at:
        q['at'] = at[:80]
    return '/collections?' + urllib.parse.urlencode(q) + ('#col-' + re.sub(r'[^A-Za-z0-9-]', '', at) if at else '')


def _redirect(handler, redirect, kind, notice, root='', at=''):
    return redirect(handler, _where_url(kind, notice, root, at))


def _stream(handler, kind, root, at, work):
    """Run an install while streaming progress as JSON lines (X-Preludarr-Progress: v1), like the title pages."""
    handler.send_response(200)
    for k, v in (('Content-Type', 'application/x-ndjson; charset=utf-8'), ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')):
        handler.send_header(k, v)
    handler.end_headers()

    def report(phase, message, **extra):
        try:
            handler.wfile.write((json.dumps(dict({'phase': phase, 'message': message}, **extra), separators=(',', ':')) + '\n').encode())
            handler.wfile.flush()
        except OSError:
            pass
    try:
        title, src = work(report)
        msg = 'Theme installed for ' + title + ' (from ' + src + ').'
        root, at = getattr(handler, '_col_where', (root, at))
        report('done', msg, next=_where_url(getattr(handler, '_col_kind', kind), msg, root, at))
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        report('error', 'Not done: ' + (str(error) or type(error).__name__))
    except Exception as error:
        report('error', 'Not done: ' + __import__('neterrors').text(error, _label(kind)))


def post_upload(app, handler, redirect):
    if handler.headers.get('X-Preludarr-Progress') == 'v1':
        return _stream(handler, 'plex', '', '', lambda report: install_upload(app, handler, report)[1:])
    kind = 'plex'
    try:
        kind, title, src = install_upload(app, handler)
        msg = 'Theme installed for ' + title + ' (from ' + src + ').'
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        msg = 'Not done: ' + (str(error) or type(error).__name__)
    root, at = getattr(handler, '_col_where', ('', ''))
    return _redirect(handler, redirect, kind, msg, root, at)


def post(app, handler, redirect):
    route = urllib.parse.urlsplit(handler.path).path
    kind, root, at = 'plex', '', ''
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 4096:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=8)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid form.')
        f = lambda k: form.get(k, [''])[0]  # noqa: E731
        kind = f('server') if re.fullmatch(r'(plex|jellyfin|emby)(-[2-9])?', f('server')) else 'plex'
        root, at = f('root'), f('id')
        stream = handler.headers.get('X-Preludarr-Progress') == 'v1'
        if stream and route == '/collections/youtube':
            if f('confirm') != '1':
                raise ValueError('Tick the box to confirm.')
            return _stream(handler, kind, root, at, lambda report: install_youtube(app, kind, f('id'), f('url').strip(), report))
        if stream and route == '/collections/install':
            return _stream(handler, kind, root, at, lambda report: install(app, kind, f('id'), f('source'), report))
        if route == '/collections/refresh':
            msg = 'Reading collections from ' + _label(kind) + '…' if refresh(app, kind) else 'A refresh is already running.'
        elif route == '/collections/install':
            title, src = install(app, kind, f('id'), f('source'))
            msg = 'Theme installed for ' + title + ' (from ' + src + ').'
        elif route == '/collections/youtube':
            if f('confirm') != '1':
                raise ValueError('Tick the box to confirm.')
            title, src = install_youtube(app, kind, f('id'), f('url').strip())
            msg = 'Theme installed for ' + title + ' (from ' + src + ').'
        elif route == '/collections/search':
            title, found, note = search_youtube(app, kind, f('id'), everywhere=f('scope') == 'all')
            msg = (str(len(found)) + ' YouTube result' + ('' if len(found) == 1 else 's') + ' for ' + title + '. Open “Other sources” to preview and choose.') if found else (note or 'YouTube found nothing for ' + title + '.')
        elif route == '/collections/remove':
            if f('confirm') != '1':
                raise ValueError('Tick the box to confirm.')
            msg = 'Theme removed from ' + remove(app, kind, f('id')) + '.'
        elif route == '/collections/folder':
            set_folder(app, kind, f('folder'))
            msg = _label(kind) + ' collections folder saved.'
        else:
            return handler.send(404, 'Not found', 'text/plain')
    except (ValueError, OSError, UnicodeError) as error:
        msg = 'Not done: ' + str(error)
    except Exception as error:
        msg = 'Not done: ' + __import__('neterrors').text(error, _label(kind))
    return _redirect(handler, redirect, kind, msg, root, at)


def _sources(kind, item, extra, ident):
    """'Other sources' for one collection: smart YouTube search, your own YouTube link, your own audio file."""
    results = extra.get('results') or []
    parts = ['<details class="col-more"', ' open' if results else '', '><summary>Other sources</summary>']
    channel_only = bool(results) and all(r.get('trusted') for r in results)
    if extra.get('search') and channel_only:
        parts += ['<form method="post" action="/collections/search" class="col-form">', ident, '<input type="hidden" name="scope" value="all">',
                  '<button type="submit" class="ghost">Search all of YouTube</button>',
                  '<span class="muted">Found on the ', CHANNEL_LINK, ' channel (official main themes). Not the right version? This uses one search from your daily allowance.</span></form>']
    elif extra.get('search'):
        parts += ['<form method="post" action="/collections/search" class="col-form">', ident,
                  '<button type="submit" class="ghost">', 'Search YouTube again' if results else 'Search YouTube', '</button>',
                  '<span class="muted">Looks for “', _e(_search_name(item)), '” on the ', CHANNEL_LINK, ' channel first (free); if it is not there, searches all of YouTube ',
                  '(one search from your daily allowance).</span></form>']
    if extra.get('search'):
        for r in results[:6]:
            mins = ('%d:%02d' % divmod(r['seconds'], 60)) if r.get('seconds') else ''
            parts += ['<div class="col-yt"><div><strong>', _e(r.get('track', '')), '</strong><br><small class="muted">', '⭐ ' if r.get('trusted') else '', _e(r.get('channel', '')),
                      (' · ' + mins) if mins else '', '</small> ',
                      ''.join('<span class="tag warn">' + _e(fl) + '</span> ' for fl in r.get('flags') or []),
                      '<br><a class="col-ytlink" href="', _e(r['url']), '" target="_blank" rel="noopener noreferrer">Open on YouTube ↗</a></div>',
                      # 1.12 (user: "can the preview be on the same page like the anime themes are"): click-to-load player, as on movie title pages
                      '<button type="button" class="ghost col-play" data-col-play="', _e(r.get('video_id') or ''), '" aria-label="Preview ', _e(r.get('track', '')), '">▶ Preview</button>']
            if extra.get('manual'):
                # 1.12 #14: YouTube API rules — Preludarr never downloads a search result by itself. "Use this" only fills the YouTube link
                # box below; the user then ticks "I may use this audio" and installs (the same own step as pasting a link on movie pages).
                parts += ['<button type="button" data-col-use="', _e(r['url']), '">Use this</button>']
            parts.append('<div class="col-player" hidden></div></div>')
    elif extra.get('search_note'):
        parts += ['<p class="muted">', extra['search_note'], '</p>']
    if extra.get('manual'):
        parts += ['<form method="post" action="/collections/youtube" class="col-form col-dl col-js">', ident,
                  '<label>YouTube link <input type="url" name="url" required maxlength="300" placeholder="https://www.youtube.com/watch?v=…"></label>',
                  '<label class="check"><input type="checkbox" name="confirm" value="1" required> I may use this audio</label>',
                  '<button type="submit">Install from YouTube</button></form>',
                  '<form method="post" action="/collections/upload" enctype="multipart/form-data" class="col-form col-dl col-js">', ident,
                  '<label>Your own audio <input type="file" name="audio" required accept=".mp3,.m4a,.aac,.ogg,.opus,.flac,.wav,audio/*"></label>',
                  '<button type="submit">Upload and install</button></form>',
                  '<p class="muted">Audio up to 10 minutes / 40 MB; Preludarr converts it to MP3.</p>']
    else:
        parts += ['<p class="muted">YouTube links and your own audio need manual installs turned on (<code>PRELUDARR_MANUAL_INSTALL_ENABLED=1</code>).</p>']
    parts.append('</details>')
    return parts


def _row(kind, item, cands, record, extra=None):
    first = cands[0] if cands else None
    usable = [c for c in cands if c['theme']]
    in_lib = sum(1 for c in cands if c['path'])
    art = ('<img class="col-poster" loading="lazy" alt="" src="/collections/poster?' + _e(urllib.parse.urlencode({'server': kind, 'id': item['id']}))
           + '" onerror="this.replaceWith(Object.assign(document.createElement(\'div\'),{className:\'col-poster col-noart\',textContent:\'🎬\'}))">') if item.get('poster') \
        else '<div class="col-poster col-noart">🎬</div>'
    extra = extra or {}
    ident = ('<input type="hidden" name="server" value="' + kind + '"><input type="hidden" name="id" value="' + _e(item['id'])
             + '"><input type="hidden" name="root" value="' + _e(extra.get('root', '')) + '">')
    current = ('<p class="col-current"><span class="muted">Current theme</span> <audio controls preload="none" class="col-audio" src="/collections/theme?'
               + _e(urllib.parse.urlencode({'server': kind, 'id': item['id']})) + '"></audio></p>') if (record or item.get('has_theme')) else ''
    haystack = ' '.join([item['title']] + [str(m.get('title', '')) for m in item['members']]).casefold()
    parts = ['<article class="col-row" id="col-', _e(re.sub(r'[^A-Za-z0-9-]', '', item['id'])), '" data-search="', _e(haystack[:2000]), '">', art,
             '<div class="col-main"><h3>', _e(item['title']), '</h3>',
             ('<p class="col-lib"><span class="tag">' + _e(item['library']) + '</span>'
              + ((' <span class="muted">· folder: ' + _e(extra['where']) + '</span>') if extra.get('where') and extra['where'] != item['library'] else '')
              + '</p>' if item.get('library') else ''), '<p class="muted">',
             str(len(cands)), ' movie', '' if len(cands) == 1 else 's', ' · ', str(in_lib), ' in your library</p>']
    if record:
        parts += ['<p><span class="tag ok">Theme installed by Preludarr</span> from <strong>', _e(record['source_name']), '</strong></p>', current]
    elif item.get('has_theme'):
        parts += ['<p><span class="tag ok">Has a theme in ', _label(kind), '</span></p>', current]
    else:
        parts += ['<p><span class="tag warn">No theme</span></p>']
    parts.append('</div><div class="col-act">')
    if extra.get('notice'):
        parts += ['<p class="notice', ' warn' if extra['notice'].startswith('Not done') else '', '" role="status">', _e(extra['notice']), '</p>']
    parts.append('<p class="col-status" role="status" aria-live="polite" hidden></p>')
    if usable:
        if first and not first['theme']:
            parts += ['<p class="muted col-note">The first movie, <strong>', _e(first['title']), '</strong>',
                      (' (' + str(first['year']) + ')' if first.get('year') else ''),
                      (' has no theme yet' if first['path'] else ' is not in your library'), ' — using the next movie that has one.</p>']
        opts = ''.join('<option value="' + _e(c['path']) + '"' + (' selected' if c is usable[0] else '') + '>' + _e(c['title'])
                       + (' (' + str(c['year']) + ')' if c.get('year') else '') + '</option>' for c in usable)
        parts += ['<form method="post" action="/collections/install" class="col-form col-js">', ident,
                  '<label>Song <select name="source" class="col-src">', opts, '</select></label>',
                  '<audio controls preload="none" class="col-audio" src="/current-theme?path=', _e(urllib.parse.quote(usable[0]['path'])), '"></audio>',
                  '<button type="submit">', 'Replace theme' if (record or item.get('has_theme')) else 'Install theme', '</button></form>']
    elif in_lib:
        parts += ['<p class="muted col-note">None of its movies has a theme yet. Install one on a movie’s title page first, for example ',
                  '<a href="/themes?path=', _e(urllib.parse.quote(next(c['path'] for c in cands if c['path']))), '">',
                  _e(next(c['title'] for c in cands if c['path'])), '</a>.</p>']
    else:
        parts += ['<p class="muted col-note">None of its movies were found in your Preludarr root folders.</p>']
    parts += _sources(kind, item, extra, ident)
    if record:
        parts += ['<form method="post" action="/collections/remove" class="col-form col-remove">', ident,
                  '<label class="check"><input type="checkbox" name="confirm" value="1" required> ',
                  'Remove the theme' + (' (restores the previous file)' if record.get('backup') else ''), '</label> <button type="submit" class="ghost">Remove</button></form>']
    parts.append('</div></article>')
    return parts


def page(app, handler):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    servers = _servers(app)
    kind = q.get('server', [''])[0]
    if kind not in servers:
        kind = next(iter(servers), '')
    notice = q.get('notice', [''])[0][:300]
    at = q.get('at', [''])[0][:80]
    data = load(app)
    run = _RUN.get(kind, {})
    state = data['servers'].get(kind) or {}
    items = sorted(state.get('items') or [], key=lambda i: (i['title'].casefold(), i.get('library', '')))
    index = _title_index(app)
    cands = {id(i): candidates(app, i, index) for i in items}
    kinds = __import__('kinds')
    rf = __import__('root_folders')
    movie_roots = [str(r) for t, r in app.ROOTS if kinds.is_movie(t)]
    by_folder = any(in_your_libraries(i, movie_roots, [], True) for i in items if i.get('locations'))
    home = {id(i): _root_for(i, cands[id(i)], movie_roots, by_folder) for i in items}
    counts = {r: sum(1 for i in items if home[id(i)] == r) for r in movie_roots}
    other = sum(1 for i in items if home[id(i)] is None)
    tab = q.get('root', [''])[0]
    if tab != 'other' and tab not in movie_roots:
        tab = next((r for r in movie_roots if counts[r]), movie_roots[0] if movie_roots else 'other')
    term = q.get('q', [''])[0].strip()[:100]
    if term:  # 1.12 (user: "a search bar to search by name"): every tab, collection names and their movies' names
        tf = term.casefold()
        shown = [i for i in items if tf in i['title'].casefold() or any(tf in str(m.get('title', '')).casefold() for m in i['members'])]
    else:
        shown = [i for i in items if home[id(i)] == (None if tab == 'other' else tab)]
    ready = sum(1 for i in shown if any(c['theme'] for c in cands[id(i)]))
    keep = {'server': kind} if len(servers) > 1 else {}
    link = lambda **v: '/collections?' + _e(urllib.parse.urlencode(dict(keep, **v)))  # noqa: E731
    parts = ['<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Collections - Preludarr</title>',
             '<meta http-equiv="refresh" content="3">' if run.get('running') else '', '<style>', app.CSS, CSS, '</style></head><body>',
             app.full_sidebar('collections', fixed=True), '<div class="themearr-content"><main>',
             '<section class="panel" id="collections-heading"><h1>🎬 Collections</h1>']
    if servers and (movie_roots or other):
        parts.append('<nav class="tm-subnav tm-tabs" aria-label="Collection sections" id="collection-tabs">')
        parts += ['<a href="' + link(root=r) + '"' + (' class="on" aria-current="page"' if r == tab and not term else '') + ' title="' + _e(r) + '">'
                  + _e(rf.name_of(r)) + ' <span class="col-count">' + str(counts[r]) + '</span></a>' for r in movie_roots]
        if other:
            parts += ['<a href="' + link(root='other') + '"' + (' class="on" aria-current="page"' if tab == 'other' and not term else '')
                      + ' title="Collections from libraries that are not Preludarr root folders">Other libraries <span class="col-count">' + str(other) + '</span></a>']
        parts.append('</nav>')
        parts += ['<form method="get" action="/collections" class="col-search" role="search">',
                  ''.join('<input type="hidden" name="' + k + '" value="' + _e(v) + '">' for k, v in keep.items()),
                  '<input type="hidden" name="root" value="', _e(tab), '">',
                  '<input type="search" name="q" id="col-q" maxlength="100" value="', _e(term), '" placeholder="Search collections or their movies…" aria-label="Search collections">',
                  '<button type="submit">Search</button>', ('<a class="btn ghost" href="' + link(root=tab) + '">Clear</a>') if term else '', '</form>']
        if term:
            parts += ['<p class="muted">', str(len(shown)), ' collection', '' if len(shown) == 1 else 's', ' match “', _e(term), '” in all folders · ', str(ready), ' ready to install.</p>']
        else:
            where = 'other libraries' if tab == 'other' else rf.name_of(tab)
            parts += ['<p class="muted">', str(len(shown)), ' collection', '' if len(shown) == 1 else 's', ' in ', _e(where), ' · ', str(ready), ' ready to install',
                      ' (a movie already has a theme) · the song is the theme.mp3 of one of the collection’s movies, by default the first.',
                      ' <span id="col-filter-note"></span></p>']
    else:
        parts += ['<p class="muted">A theme song for each movie collection, for example <em>The Matrix Collection</em>. The song is the theme.mp3 of one of the ',
                  'collection’s movies — by default the first movie. Nothing is downloaded here.</p>']
    parts.append('</section><section class="panel" id="collections-list">')
    if notice and not (at and any(i['id'] == at for i in shown)):
        parts += ['<p class="notice" role="status">', _e(notice), '</p>']
    if not servers:
        parts += ['<p>Connect Plex, Jellyfin or Emby in <a href="/media-servers">Settings → Connect</a> to see your collections.</p></section></main></div></body></html>']
        return handler.send(200, ''.join(parts), 'text/html')
    if not movie_roots:
        parts += ['<p class="notice">Add an <strong>Anime Movies</strong> or <strong>Movies</strong> root folder in <a href="/media-management">Settings → Media Management</a>; ',
                  'collections are shown per movie root folder.</p>']
    if len(servers) > 1:
        parts.append('<nav class="tq-tabs col-tabs" aria-label="Media servers">')
        parts += ['<a href="/collections?server=' + k + '"' + (' class="active" aria-current="page"' if k == kind else '') + '>' + _e(_label(k))
                  + (' <small>experimental</small>' if _kind(k) in FOLDER_KINDS else '') + '</a>' for k in servers]
        parts.append('</nav>')
    ui = __import__('ui')
    parts += ['<div class="col-bar"><form method="post" action="/collections/refresh"><input type="hidden" name="server" value="', kind, '">',
              '<button type="submit"', ' disabled' if run.get('running') else '', '>Refresh from ', _label(kind), '</button></form><span class="muted">']
    if run.get('running'):
        parts += ['Reading collections… ', str(run.get('done', 0)), ' of ', str(run.get('total') or '?')]
    elif state.get('refreshed'):
        parts += ['Last read ', _e(ui.when(datetime.fromisoformat(state['refreshed'])))]
    else:
        parts.append('Not read yet — press Refresh.')
    parts.append('</span></div>')
    if state.get('error'):
        parts += ['<p class="notice warn" role="alert">Last refresh failed: ', _e(state['error']), '</p>']
    if state.get('items') and any('poster' not in i for i in state['items']) and not run.get('running'):
        parts += ['<p class="notice" role="status">Press <strong>Refresh from ', _e(_label(kind)), '</strong> once to load the collection artwork.</p>']
    if _kind(kind) in FOLDER_KINDS:
        folder = data['folders'].get(kind, '')
        parts += ['<details class="tm-help col-folder"', '' if folder else ' open', '><summary>', _label(kind), ' collections folder (experimental)</summary>',
                  '<p>', _label(kind), ' keeps each collection in a folder of its own, named like <code>Bleach Collection [boxset]</code>, inside its config folder ',
                  '(<code>data/collections</code>). Map that folder into Preludarr read/write (for example <code>/collections/', kind, '</code>) and enter it here; ',
                  'Preludarr puts theme.mp3 there. ', _label(kind), ' does not officially support collection themes, so some apps may not play them.</p>',
                  '<form method="post" action="/collections/folder" class="col-form"><input type="hidden" name="server" value="', kind, '">',
                  '<label>Folder <input name="folder" maxlength="512" value="', _e(folder), '" placeholder="/collections/', kind, '"></label> ',
                  '<button type="submit">Save</button></form></details>']
    if tab == 'other':
        parts += ['<p class="muted">These come from ', _e(_label(kind)), ' libraries that are not Preludarr root folders (for example a 4K library), so their movies have no themes here.</p>']
    installed = data['installed'].get(kind) or {}
    ys = __import__('youtube_search')
    yset = ys.settings()
    search_on = bool(yset.get('enabled') and yset.get('api_key'))
    note = '' if search_on else ('Smart YouTube search: turn it on and add an API key in <a href="/download-clients">Settings → Download Clients → YouTube search</a>.')
    manual = manual_enabled()
    if shown:
        parts.append('<div class="col-list">')
        for item in shown:
            extra = {'manual': manual, 'search': search_on, 'search_note': note, 'results': cached_search(app, kind, item) if search_on else None,
                     'root': (home[id(item)] or 'other') if term else tab, 'notice': notice if at and item['id'] == at else '',
                     'where': (rf.name_of(home[id(item)]) if home[id(item)] else 'Other libraries') if term else ''}
            parts += _row(kind, item, cands[id(item)], installed.get(item['id']), extra)
        parts.append('</div>')
    elif term:
        parts += ['<p>No collection or movie matches “', _e(term), '”.</p>']
    elif state.get('refreshed') and not state.get('error'):
        parts += ['<p>No collections in this root folder.</p>' if items else '<p>' + _label(kind) + ' has no movie collections.</p>']
    parts += ['</section></main></div><script>', SCRIPT, '</script></body></html>']
    return handler.send(200, ''.join(parts), 'text/html')

CSS = ('.col-bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:12px 0}.col-list{display:grid;gap:12px;margin-top:12px}'
       '.col-row{display:flex;gap:16px;justify-content:space-between;flex-wrap:wrap;padding:14px 16px;border:1px solid #33435f;border-radius:12px;background:#172137}'
       '.col-row h3{margin:0 0 4px;font-size:1.05rem}.col-row p{margin:4px 0}.col-main{flex:1 1 260px;min-width:0}.col-act{flex:1 1 340px;min-width:0}'
       '.col-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}.col-form select,.col-form input[name=folder]{max-width:100%;min-width:0}'
       '.col-audio{height:34px;max-width:100%}.col-note{font-size:13.5px}.col-tabs small{font-size:11px;opacity:.75}.col-folder{margin:10px 0}'
       '.col-remove{font-size:13.5px}.col-lib{margin:2px 0 4px}.col-filter{margin:8px 0 0}'
       '.col-poster{flex:0 0 80px;width:80px;height:120px;object-fit:cover;border-radius:8px;background:#0f1828;border:1px solid #33435f}'
       '.col-noart{display:flex;align-items:center;justify-content:center;font-size:28px;opacity:.6}'
       '.col-tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 4px;padding:6px;border:1px solid #33435f;border-radius:10px;background:#141f33}'
       '.col-tabs a{padding:8px 14px;border-radius:8px;color:#dce9ff;text-decoration:none;font-weight:600}.col-tabs a:hover{background:#22314c}'
       '.col-tabs a.active{background:#2f4a73;color:#fff}.col-tabs small{margin-left:4px}'
       '#collection-tabs{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 14px;padding:8px;border:1px solid #3b4b6b;border-radius:10px;background:#152038;box-sizing:border-box;width:100%}'
       '#collection-tabs a{padding:9px 18px;border-radius:8px;color:#cfe0f8;text-decoration:none;font-weight:600;font-size:15px;line-height:1.2}'
       '#collection-tabs a:hover{background:#22324f;color:#fff}#collection-tabs a.on{background:#2a4a7a;color:#fff;box-shadow:inset 0 0 0 1px #4f6a96}'
       '#collection-tabs a:focus-visible{outline:3px solid #8fc1ff;outline-offset:2px}.col-count{font-weight:500;opacity:.7;font-size:13px;margin-left:4px}'
       '#collections-list{margin-top:14px}'
       '.col-search{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 10px}.col-search input[type=search]{flex:1 1 320px;max-width:560px;min-width:0;'
       'padding:10px 12px;border-radius:8px;border:1px solid #3e5174;background:#0d1626;color:#eef4ff;font-size:15px}.col-row[hidden]{display:none}'
       '.col-status{margin:6px 0 8px;padding:9px 12px;border-radius:8px;background:#22324f;color:#e8f0ff;font-weight:600}.col-status.bad{background:#4a2330}'
       '.col-current{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.col-current .col-audio{max-width:260px}'
       '.col-more{margin-top:8px;font-size:14px}.col-more summary{cursor:pointer;color:#b7c6de;font-weight:600}'
       '.col-yt{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:space-between;padding:8px 0;border-top:1px solid #2b3a55}'
       '.col-picked{outline:2px solid #4f8cff;outline-offset:4px;border-radius:8px}.col-form input[type=file]{max-width:100%;min-width:0}.col-yt>div{flex:1 1 220px;min-width:0}.col-yt .tag{margin-left:4px}.col-ytlink{font-size:12.5px}'
       '.col-yt .col-player{flex:1 1 100%;max-width:480px;aspect-ratio:16/9;background:#0b1220;border:1px solid #334967;border-radius:8px}'
       '.col-yt .col-player[hidden]{display:none}.col-yt .col-player iframe{width:100%;height:100%;border:0;border-radius:8px}'
       # 1.12 (user: "make the text a little bigger, use the empty space but not all of it"): poster | details | song & actions
       '.col-row{display:grid;grid-template-columns:110px minmax(220px,460px) minmax(320px,720px);justify-content:start;gap:12px 32px;align-items:start;padding:18px 20px}'
       '.col-row .col-poster{width:110px;height:165px}.col-row h3{font-size:1.25rem;margin:2px 0 8px}.col-main p{font-size:15.5px;margin:5px 0}'
       '.col-row>*{min-width:0}.col-form label{display:flex;gap:8px;align-items:center;min-width:0;max-width:100%}'
       '.col-form select{min-width:0;max-width:100%;flex:1 1 auto;text-overflow:ellipsis}.col-act{font-size:15px}.col-act .col-note{font-size:15px;line-height:1.5}.col-form label,.col-form select,.col-form button{font-size:15px}'
       '.col-more summary{font-size:15px}.col-yt strong{font-size:15px}'
       '@media(max-width:1100px){.col-row{grid-template-columns:96px minmax(0,1fr)}.col-row .col-poster{width:96px;height:144px}.col-act{grid-column:1/-1}}'
       '@media(max-width:640px){.col-row{padding:12px;gap:12px}.col-form{flex-direction:column;align-items:stretch}.col-form select,.col-audio{width:100%}}')
SCRIPT = r"""
document.addEventListener('click',function(e){var u=e.target.closest('[data-col-use]');if(!u)return;
 var row=u.closest('.col-row'),box=row&&row.querySelector('form[action="/collections/youtube"] input[name=url]');if(!box)return;
 box.value=u.getAttribute('data-col-use');var cb=box.form.querySelector('input[name=confirm]');
 box.scrollIntoView({block:'center'});if(cb)cb.focus();box.form.classList.add('col-picked');});
document.addEventListener('click',function(e){var b=e.target.closest('[data-col-play]');if(!b)return;
 var box=b.parentElement.querySelector('.col-player'),v=b.getAttribute('data-col-play');if(!box||!/^[A-Za-z0-9_-]{11}$/.test(v))return;
 if(!box.hidden){box.hidden=true;box.replaceChildren();b.textContent='▶ Preview';return;}
 document.querySelectorAll('.col-player').forEach(function(o){if(!o.hidden){o.hidden=true;o.replaceChildren();var ob=o.parentElement.querySelector('[data-col-play]');if(ob)ob.textContent='▶ Preview';}});
 var f=document.createElement('iframe');f.src='https://www.youtube-nocookie.com/embed/'+encodeURIComponent(v)+'?autoplay=1&rel=0';f.title='YouTube preview';
 f.allow='autoplay; encrypted-media; picture-in-picture';f.referrerPolicy='strict-origin-when-cross-origin';f.allowFullscreen=true;
 box.replaceChildren(f);box.hidden=false;b.textContent='■ Close preview';});
document.querySelectorAll('form.col-js').forEach(function(f){f.addEventListener('submit',function(ev){
 if(!window.XMLHttpRequest||!window.FormData)return;ev.preventDefault();
 var row=f.closest('.col-row'),st=row.querySelector('.col-status'),b=f.querySelector('button');
 var multi=f.enctype==='multipart/form-data',data=new FormData(f),x=new XMLHttpRequest(),seen=0,last=null;
 function show(t,bad){st.hidden=false;st.textContent=t;st.className='col-status'+(bad?' bad':'');}
 x.open('POST',f.action);x.setRequestHeader('X-Preludarr-Progress','v1');
 var body=data;if(!multi){body=new URLSearchParams(data).toString();x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');}
 if(b)b.disabled=true;show(multi?'Uploading your file…':'Starting…');
 if(multi&&x.upload)x.upload.onprogress=function(e){if(e.lengthComputable)show('Uploading your file… '+Math.round(e.loaded*100/e.total)+'%');};
 function parse(){var lines=x.responseText.split('\n');for(;seen<lines.length-1;seen++){try{last=JSON.parse(lines[seen]);}catch(e){continue;}
  if(last.phase==='done'){show('✓ '+last.message);var u=last.next.split('#');location.href=u[0]+'&r='+Date.now()+(u[1]?'#'+u[1]:'');return;}
  if(last.phase==='error'){show(last.message,true);if(b)b.disabled=false;return;}show(last.message+'…');}}
 x.onprogress=parse;
 x.onload=function(){parse();if(!last||(last.phase!=='done'&&last.phase!=='error')){show('Something went wrong (HTTP '+x.status+'). Try again.',true);if(b)b.disabled=false;}};
 x.onerror=function(){show('The connection to Preludarr was lost. Try again.',true);if(b)b.disabled=false;};
 x.send(body);});});
(function(){var q=document.getElementById('col-q'),note=document.getElementById('col-filter-note');if(!q||!note)return;
 q.addEventListener('input',function(){var t=q.value.trim().toLowerCase(),rows=document.querySelectorAll('.col-list .col-row'),n=0;
  rows.forEach(function(r){var hit=!t||r.dataset.search.indexOf(t)>=0;r.hidden=!hit;if(hit)n++;});
  note.textContent=t?('· showing '+n+' on this tab — press Enter to search all folders'):'';});})();
document.querySelectorAll('.col-src').forEach(function(s){s.addEventListener('change',function(){var a=s.closest('form').querySelector('.col-audio');
 if(a){a.pause();a.src='/current-theme?path='+encodeURIComponent(s.value);}});});
"""
