# SPDX-License-Identifier: GPL-3.0-or-later
"""Themearr manual poster tools (V1.6.3, roadmap Feature 11 phase 2).

Adds, without changing automatic poster search (app.fetch_poster):
  * choose a poster from saved candidates ("needs your choice") or from a manual title search;
  * upload your own PNG / JPEG / WebP poster with validation and a preview before it is applied;
  * reset a title back to automatic artwork;
  * a missing-poster report grouped by the last search outcome.

Safety: image URLs are resolved on the server from stored candidates (never taken from the browser);
downloads only from TMDB/TVDB image hosts over HTTPS; every image is re-encoded to a bounded JPEG
(validates it and drops metadata); replacing a poster needs explicit confirmation and keeps the old
file in /config/posters/.replaced/; nothing is written outside /config; media folders are never touched.
All functions receive the running `app` module so this module never imports app.py.
"""
import contextlib
import difflib
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from email import policy
from email.parser import BytesParser
from pathlib import Path

IMAGE_HOSTS = ('image.tmdb.org', 'artworks.thetvdb.com', 'www.thetvdb.com', 'thetvdb.com')
# AnimeThemes serves cover art from this public bucket (verified 2026-10-25); used only for Smart Matching artwork.
ANIMETHEMES_ART_HOSTS = ('pub-92474f7785774e91a790e086dfa6b2ef.r2.dev',)
ART_CACHE_FILES = 400          # bounded on-disk cache of re-encoded candidate artwork
ART_CACHE_SECONDS = 30 * 86400
_ART_SLOTS = threading.BoundedSemaphore(2)  # at most two artwork downloads at a time
_ANIME_ART = {}                # AnimeThemes slug -> (cover URL or '', expiry)
_SLUG = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
MAX_UPLOAD = 10 * 1024 * 1024
MAX_DOWNLOAD = 3 * 1024 * 1024
MAX_OUTPUT = 3 * 1024 * 1024
MIN_SIDE = 100
STAGE_SECONDS = 600
SEARCH_SECONDS = 900
SEARCH_LIMIT = (10, 600)  # at most 10 manual searches per 10 minutes
_LOCK = threading.Lock()
_SEARCHES = {}
_STAGED = {}
_SEARCH_TIMES = []
_TOKEN = re.compile(r'[A-Za-z0-9_-]{20,64}\Z')


# ------------------------------------------------------------------ images ----
def _ffmpeg(args, data, timeout=25):
    # Same isolation as poster_format: separate process, 768 MiB address space, single thread.
    launcher = ('import os,resource,sys;resource.setrlimit(resource.RLIMIT_AS,(805306368,805306368));'
                'os.execvp(sys.argv[1],sys.argv[1:])')
    return subprocess.run([sys.executable, '-c', launcher] + args, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, check=True).stdout


def normalize_image(content):
    """Re-encode PNG / JPEG / WebP bytes to a bounded JPEG (max 720x1080). Returns (jpeg, kind, w, h)."""
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_UPLOAD:
        raise ValueError('The image must be between 1 byte and 10 MB.')
    if content.startswith(b'\xff\xd8\xff'):
        kind = 'JPEG'
    elif content.startswith(b'\x89PNG\r\n\x1a\n'):
        kind = 'PNG'
    elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        kind = 'WebP'
    else:
        raise ValueError('Only PNG, JPEG or WebP images are supported.')
    try:
        jpeg = _ffmpeg(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                        '-f', 'image2pipe', '-i', 'pipe:0', '-an', '-frames:v', '1', '-map_metadata', '-1',
                        '-vf', 'scale=w=min(720\\,iw):h=min(1080\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2',
                        '-pix_fmt', 'yuvj420p', '-c:v', 'mjpeg', '-q:v', '4', '-f', 'image2pipe', 'pipe:1'], content)
        probe = json.loads(_ffmpeg(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                    'stream=width,height', '-of', 'json', '-i', 'pipe:0'], jpeg, timeout=15) or b'{}')
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError(f'This {kind} file could not be read as an image.') from None
    stream = (probe.get('streams') or [{}])[0]
    width, height = int(stream.get('width') or 0), int(stream.get('height') or 0)
    if not (4 <= len(jpeg) <= MAX_OUTPUT and jpeg.startswith(b'\xff\xd8') and jpeg.endswith(b'\xff\xd9')):
        raise ValueError('The converted poster failed its final check.')
    if min(width, height) < MIN_SIDE:
        raise ValueError(f'The image is too small ({width}×{height}); use at least {MIN_SIDE}×{MIN_SIDE} pixels.')
    return jpeg, kind, width, height


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('The image host tried to redirect; refused.')


_OPENER = urllib.request.build_opener(_NoRedirect)


def _download(url, hosts=IMAGE_HOSTS):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in hosts or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError('That poster address is not from an approved artwork provider.')
    req = urllib.request.Request(url, headers={'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'})
    with _OPENER.open(req, timeout=20) as response:
        data = response.read(MAX_DOWNLOAD + 1)
        ctype = response.headers.get('Content-Type', '')
    if len(data) > MAX_DOWNLOAD or not ctype.startswith('image/'):
        raise ValueError('The provider did not return a usable image.')
    return data


def cached_art(app, url, hosts=IMAGE_HOSTS):
    """Re-encoded JPEG for an approved provider image URL, from the bounded cache or downloaded once."""
    folder = Path(app.CONFIG) / 'poster-cache' / 'art'
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    file = folder / (hashlib.sha256(url.encode()).hexdigest() + '.jpg')
    try:
        if time.time() - file.stat().st_mtime < ART_CACHE_SECONDS:
            return file.read_bytes()
    except FileNotFoundError:
        pass
    if not _ART_SLOTS.acquire(timeout=30):
        raise ValueError('Artwork downloads are busy; try again in a moment.')
    try:
        jpeg, _, _, _ = normalize_image(_download(url, hosts))
    finally:
        _ART_SLOTS.release()
    tmp = folder / ('.' + secrets.token_hex(8) + '.part')
    tmp.write_bytes(jpeg)
    os.replace(tmp, file)
    files = sorted(folder.glob('*.jpg'), key=lambda f: f.stat().st_mtime)
    for old in files[:max(0, len(files) - ART_CACHE_FILES)]:
        old.unlink(missing_ok=True)
    return jpeg


def saved_candidates(app, path):
    with app.db() as c:
        row = c.execute('SELECT at,outcome,candidates FROM poster_attempts WHERE path=?', (path,)).fetchone()
    try:
        items = json.loads(row['candidates']) if row and row['candidates'] else []
    except ValueError:
        items = []
    return (row['at'] if row else 0), (row['outcome'] if row else ''), [c for c in items if isinstance(c, dict)]


def candidate_art(app, path, index):
    """Artwork of saved candidate `index` for this title; the URL comes only from the stored search result."""
    _, _, items = saved_candidates(app, path)
    if not 0 <= index < len(items):
        raise ValueError('Unknown candidate.')
    return cached_art(app, str(items[index].get('poster') or ''))


def anime_art(app, slug):
    """AnimeThemes 'Large Cover' of exactly this AnimeThemes entry (Smart Matching suggestions)."""
    if not _SLUG.fullmatch(slug or ''):
        raise ValueError('Invalid AnimeThemes slug.')
    now = time.monotonic()
    with _LOCK:
        hit = _ANIME_ART.get(slug)
    if not hit or hit[1] <= now:
        data = app.request_json('https://api.animethemes.moe/anime/' + urllib.parse.quote(slug, safe='') + '?include=images')
        item = data.get('anime') if isinstance(data, dict) else None
        link = ''
        if isinstance(item, dict) and item.get('slug') == slug:
            for image in item.get('images') or []:
                if isinstance(image, dict) and image.get('facet') == 'Large Cover' and isinstance(image.get('link'), str):
                    link = image['link']
                    break
        hit = (link, now + 86400)
        with _LOCK:
            if len(_ANIME_ART) > 500:
                _ANIME_ART.clear()
            _ANIME_ART[slug] = hit
    if not hit[0]:
        raise ValueError('AnimeThemes has no cover art for this entry.')
    return cached_art(app, hit[0], ANIMETHEMES_ART_HOSTS)


def candidates_payload(app, path):
    """JSON for the manual poster popup: the saved candidates exactly as the providers returned them."""
    row = _anime(app, path)
    if row is None:
        raise ValueError('Unknown anime.')
    at, outcome, items = saved_candidates(app, path)
    folder_key = app.poster_key(app.clean(row['name']))
    out = []
    for i, c in enumerate(items):
        title, original = str(c.get('title') or ''), str(c.get('original') or '')
        keys = [app.poster_key(v) for v in (title, original) if v]
        ratio = max([difflib.SequenceMatcher(None, k, folder_key).ratio() for k in keys if k and folder_key] or [0.0])
        other_script = any(k and not k.isascii() for k in keys) and folder_key.isascii()
        why = f"Same year as your folder ({c.get('year') or '?'}) · title similarity {round(ratio * 100)}%"
        if other_script:
            why += ' (title is in another language)'
        tier, order, labels = rank(app, row['name'], c)
        why = _TIERS[tier][0][:-2] + ' · ' + ' · '.join(labels) + (' (title is in another language)' if other_script else '')  # V1.9.11
        out.append({'index': i, 'title': title, 'original': original, 'year': str(c.get('year') or ''), 'order': order,
                    'provider': str(c.get('provider') or ''), 'id': str(c.get('id') or ''), 'why': why,
                    'thumb': '/poster-candidate-art?' + urllib.parse.urlencode({'path': path, 'i': i, 'v': at})})
    ids = app.media_ids_for(path) if hasattr(app, 'media_ids_for') else {}
    for o in out:  # V1.9.14: same TMDB/TVDB ID as Sonarr/Radarr = this exact title → first, and said so
        want = ids.get(o['provider'].lower())
        if want and o['id'] == str(want):
            o['order'] = (-1,) + tuple(o['order'][1:])
            o['why'] = 'Same ' + o['provider'] + ' ID as ' + str(ids.get('source') or 'Sonarr/Radarr').capitalize() + ' · ' + o['why']
    out.sort(key=lambda x: x.pop('order'))  # best matches first; each keeps its own index for Select Poster
    return {'name': row['name'], 'library': row['library'], 'has_poster': app.poster_file(path).is_file(),
            'outcome': outcome, 'candidates': out}


# ----------------------------------------------------------------- storage ----
def _anime(app, path):
    with app.db() as c:
        row = c.execute('SELECT name,library FROM shows WHERE path=?', (path,)).fetchone()
    return row


@contextlib.contextmanager
def _claim(app, path):
    """Hold the title's poster slot so no automatic search starts or finishes while we change it."""
    with app.POSTER_LOCK:
        if path in app.POSTER_BUSY:
            raise ValueError('An automatic poster search for this title is running; try again in a moment.')
        app.POSTER_BUSY.add(path)
    try:
        yield
    finally:
        with app.POSTER_LOCK:
            app.POSTER_BUSY.discard(path)


def apply_poster(app, path, jpeg, label, replace, reason):
    """Install `jpeg` as this title's poster. Existing poster only with replace=True; the old one is kept."""
    row = _anime(app, path)
    if row is None:
        raise ValueError('Unknown anime.')
    _, _, kept = saved_candidates(app, path)
    with _claim(app, path):
        _install(app, path, jpeg, label, replace)
    app.record_poster_attempt(path, 'found', reason, kept)  # keep the choices so selection can be reopened
    app.event(row['name'], 'poster', reason)


def _install(app, path, jpeg, label, replace):
    target = app.poster_file(path)
    if target.is_file() and not replace:
        raise ValueError('This title already has a poster. Tick "Replace the current poster" to change it.')
    folder = target.parent
    tmp = folder / ('.manual-' + secrets.token_hex(8) + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(jpeg)
            out.flush()
            os.fsync(out.fileno())
        if target.is_file():
            keep = folder / '.replaced'
            keep.mkdir(mode=0o700, exist_ok=True)
            os.link(target, keep / (target.stem + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3) + '.jpg'))
        os.replace(tmp, target)  # atomic swap: the title always has either the old or the new poster
    finally:
        tmp.unlink(missing_ok=True)
    with app.db() as c:
        c.execute('INSERT OR REPLACE INTO posters VALUES(?,?,?,?)', (path, 'ok', int(time.time()), label))


def reset_poster(app, path):
    """Move the current poster aside and make the title eligible for automatic search again."""
    row = _anime(app, path)
    if row is None:
        raise ValueError('Unknown anime.')
    with _claim(app, path):
        target = app.poster_file(path)
        if target.is_file():
            keep = target.parent / '.replaced'
            keep.mkdir(mode=0o700, exist_ok=True)
            os.replace(target, keep / (target.stem + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3) + '.jpg'))
        with app.db() as c:
            c.execute('DELETE FROM posters WHERE path=?', (path,))
            c.execute('DELETE FROM poster_attempts WHERE path=?', (path,))
    app.event(row['name'], 'poster', 'Poster reset to automatic artwork')


def _expire():
    now = time.monotonic()
    for store in (_SEARCHES, _STAGED):
        for key, item in list(store.items()):
            if item['expiry'] <= now:
                if 'file' in item:
                    Path(item['file']).unlink(missing_ok=True)
                del store[key]


def sweep_stale(config):
    """Remove staged uploads left behind by a restart (staging lives only in memory)."""
    folder = Path(config) / 'poster-staging'
    if folder.is_dir() and not folder.is_symlink():
        for item in folder.glob('*.jpg'):
            if time.time() - item.stat().st_mtime > STAGE_SECONDS:
                item.unlink(missing_ok=True)


# ------------------------------------------------------------------ search ----
def search(app, path, query):
    """Manual TMDB / TVDB search with the user's own wording. Returns a token for the cached results."""
    row = _anime(app, path)
    if row is None:
        raise ValueError('Unknown anime.')
    query = ' '.join(str(query or '').split())[:120]
    if not query:
        raise ValueError('Type a title to search for.')
    now = time.time()
    with _LOCK:
        _SEARCH_TIMES[:] = [t for t in _SEARCH_TIMES if now - t < SEARCH_LIMIT[1]]
        if len(_SEARCH_TIMES) >= SEARCH_LIMIT[0]:
            raise ValueError('Poster search limit reached (10 searches per 10 minutes). Try again shortly.')
        _SEARCH_TIMES.append(now)
    tmdb_key, tvdb_key, tvdb_pin, _ = app.poster_connect.effective_values(app.CONFIG)
    movie = __import__('kinds').is_movie(row['library'])
    found, problems = [], []
    if tmdb_key:
        try:
            qs = urllib.parse.urlencode({'api_key': tmdb_key, 'query': query})
            for item in (app.request_json('https://api.themoviedb.org/3/search/' + ('movie' if movie else 'tv') + '?' + qs).get('results') or [])[:12]:
                if item.get('poster_path'):
                    found.append({'provider': 'TMDB', 'id': item.get('id'), 'title': item.get('title') or item.get('name') or '',
                                  'original': item.get('original_title') or item.get('original_name') or '',
                                  'year': (item.get('release_date') or item.get('first_air_date') or '')[:4],
                                  'poster': 'https://image.tmdb.org/t/p/w342' + item['poster_path']})
        except Exception as error:
            problems.append('TMDB: ' + (f"HTTP {error.code}" if getattr(error, 'code', None) else type(error).__name__))
    if tvdb_key:
        try:
            login = app.request_json('https://api4.thetvdb.com/v4/login', app.poster_connect.tvdb_login_payload(tvdb_key, tvdb_pin))
            token = (login.get('data') or {}).get('token')
            if token:
                req = urllib.request.Request('https://api4.thetvdb.com/v4/search?' + urllib.parse.urlencode({'query': query, 'type': 'movie' if movie else 'series'}),
                                             headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'})
                __import__('ratelimit').wait('tvdb')  # V1.9.14
                with urllib.request.urlopen(req, timeout=18) as response:
                    items = json.load(response).get('data') or []
                for item in items[:12]:
                    url = item.get('image_url') or ''
                    if urllib.parse.urlsplit(url).hostname in IMAGE_HOSTS and url.startswith('https://'):
                        found.append({'provider': 'TVDB', 'id': item.get('tvdb_id') or item.get('id'), 'title': item.get('name') or '',
                                      'original': '', 'year': str(item.get('first_air_time') or item.get('year') or '')[:4], 'poster': url})
        except Exception as error:
            problems.append('TVDB: ' + (f"HTTP {error.code}" if getattr(error, 'code', None) else type(error).__name__))
    token = secrets.token_urlsafe(24)
    with _LOCK:
        _expire()
        _SEARCHES[token] = {'path': path, 'query': query, 'candidates': found[:24], 'problems': problems,
                            'expiry': time.monotonic() + SEARCH_SECONDS}
    return token


def _candidates(app, path, source, token):
    if source == 'search':
        with _LOCK:
            _expire()
            item = _SEARCHES.get(token) if _TOKEN.fullmatch(token or '') else None
        if not item or item['path'] != path:
            raise ValueError('Those search results have expired. Search again.')
        return item['candidates']
    with app.db() as c:
        row = c.execute('SELECT candidates FROM poster_attempts WHERE path=?', (path,)).fetchone()
    return json.loads(row['candidates']) if row and row['candidates'] else []


def choose(app, path, source, token, index, replace):
    candidates = _candidates(app, path, source, token)
    if not (isinstance(index, int) and 0 <= index < len(candidates)):
        raise ValueError('That poster choice is no longer available. Reload the page.')
    pick = candidates[index]
    jpeg = cached_art(app, str(pick.get('poster') or ''))
    label = 'Chosen: ' + str(pick.get('provider') or 'provider')
    apply_poster(app, path, jpeg, label, replace,
                 f"{label} '{pick.get('title') or pick.get('original') or ''}' ({pick.get('year') or '?'}) chosen manually")
    return label


# ------------------------------------------------------------------ upload ----
def stage_upload(app, handler):
    """Read a multipart upload (fields: path, image), validate it, stage it for preview. Returns token."""
    if handler.headers.get('Transfer-Encoding'):
        raise ValueError('Chunked uploads are not supported.')
    lengths = handler.headers.get_all('Content-Length') or []
    size = lengths[0] if len(lengths) == 1 else ''
    if not size.isdigit() or not 64 <= int(size) <= MAX_UPLOAD + 65536:
        raise ValueError('The upload must be smaller than 10 MB.')
    ctype = handler.headers.get('Content-Type', '')
    if not ctype.lower().startswith('multipart/form-data'):
        raise ValueError('Upload form expected.')
    with _LOCK:
        _expire()
        if len(_STAGED) >= 3:
            raise ValueError('Three uploaded posters are already waiting for review. Confirm or cancel them first.')
    handler.connection.settimeout(30)
    body = handler.rfile.read(int(size))
    if len(body) != int(size):
        raise ValueError('The upload was interrupted.')
    message = BytesParser(policy=policy.HTTP).parsebytes(b'Content-Type: ' + ctype.encode('latin-1') + b'\r\n\r\n' + body)
    fields = {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if name in ('path', 'image') and name not in fields:
            fields[name] = part.get_payload(decode=True) or b''
    path = fields.get('path', b'').decode('utf-8', 'strict')
    if not path or _anime(app, path) is None:
        raise ValueError('Unknown anime.')
    handler.poster_upload_path = path  # lets the route send later errors back to this title's poster panel
    jpeg, kind, width, height = normalize_image(fields.get('image') or b'')
    folder = Path(app.CONFIG) / 'poster-staging'
    folder.mkdir(mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(24)
    file = folder / (token + '.jpg')
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(jpeg)
    with _LOCK:
        _expire()
        _STAGED[token] = {'path': path, 'file': str(file), 'kind': kind, 'size': f'{width}×{height}',
                          'expiry': time.monotonic() + STAGE_SECONDS}
    return token, path


def staged(token):
    with _LOCK:
        _expire()
        return dict(_STAGED[token]) if _TOKEN.fullmatch(token or '') and token in _STAGED else None


def confirm_upload(app, token, replace):
    item = staged(token)
    if item is None:
        raise ValueError('This upload preview expired. Upload the image again.')
    jpeg = Path(item['file']).read_bytes()
    apply_poster(app, item['path'], jpeg, 'Uploaded', replace, 'Uploaded poster applied')
    cancel_upload(token)
    return item['path']


def cancel_upload(token):
    with _LOCK:
        item = _STAGED.pop(token, None) if _TOKEN.fullmatch(token or '') else None
    if item:
        Path(item['file']).unlink(missing_ok=True)
        return item['path']
    return ''


# --------------------------------------------------------------------- UI -----
def _e(value):
    return html.escape(str(value), quote=True)


def _themes(path, **extra):
    return '/themes?' + urllib.parse.urlencode({'path': path, **extra})


def page(app, title, body):
    from login import full_sidebar
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + _e(title) + ' - Preludarr</title><style>' + app.CSS + '</style></head><body>'
            + full_sidebar('activity', fixed=True) + '<div class="themearr-content"><main>'
            '<div class="panel">' + body + '</div></main></div></body></html>')


def rank(app, name, c):
    """V1.9.11 presentation-only ranking of one poster candidate against the folder name.
    Returns (tier, sort key, labels): tier 0 = exact title + year, 1 = close, 2 = weak. Nothing is chosen automatically."""
    folder_key = app.poster_key(app.clean(name))
    want = app.year(name)
    keys = [app.poster_key(v) for v in (str(c.get('title') or ''), str(c.get('original') or '')) if v]
    exact = bool(folder_key) and folder_key in keys
    ratio = 1.0 if exact else max([difflib.SequenceMatcher(None, k, folder_key).ratio() for k in keys if k and folder_key] or [0.0])
    try:
        got = int(str(c.get('year') or '')[:4])
    except ValueError:
        got = None
    year_ok = want is None or got == want
    near_year = want is not None and got is not None and abs(got - want) == 1
    if exact and year_ok:
        tier = 0
    elif (exact and (near_year or got is None)) or (ratio >= 0.85 and year_ok):
        tier = 1
    else:
        tier = 2
    labels = ['Exact title' if exact else f'Title {round(ratio * 100)}% similar']
    if want is None:
        labels.append('folder has no year')
    elif got is None:
        labels.append('no year given')
    else:
        labels.append('same year' if got == want else f'year {got} (folder {want})')
    return tier, (tier, -ratio, 0 if year_ok else 1), labels


_TIERS = (('Best matches', 'Exact title and the same year as your folder.'),
          ('Close matches', 'Very similar title, or the exact title with the year one off or missing.'),
          ('Weak matches', 'Different title or year — check carefully before using one.'))


def _grid(path, candidates, source, token, has_poster, app=None, name=''):
    """Search results ranked exact title + year first; artwork for the same title (any provider) grouped; weak matches folded away.
    Every card keeps its original index, so Use this poster / Replace / approval work exactly as before."""
    items = list(enumerate(candidates[:24]))
    if not items:
        return ''
    ranked = [(i, c) + rank(app, name, c) for i, c in items] if app is not None and name else [(i, c, 0, (0, 0, i), []) for i, c in items]

    def card(i, c, labels):
        return ('<form method="post" action="/poster-choose" class="pm-card">'
                '<img loading="lazy" referrerpolicy="no-referrer" alt="" src="' + _e(c.get('poster') or '') + '">'
                '<div><strong>' + _e(c.get('title') or c.get('original') or '—') + '</strong>'
                + ('<br><small class="muted">' + _e(c.get('original')) + '</small>' if c.get('original') and c.get('original') != c.get('title') else '')
                + '<br><small class="pm-src">' + _e(c.get('provider')) + ' · ' + _e(c.get('year') or '?') + '</small>'
                + ('<br><small class="pm-why">' + _e(' · '.join(labels)) + '</small>' if labels else '') + '</div>'
                '<input type="hidden" name="path" value="' + _e(path) + '"><input type="hidden" name="source" value="' + source + '">'
                '<input type="hidden" name="token" value="' + _e(token) + '"><input type="hidden" name="index" value="' + str(i) + '">'
                + ('<label class="pm-replace"><input type="checkbox" name="replace" value="1"> Replace the current poster</label>' if has_poster else '')
                + '<button type="submit">Use this poster</button></form>')

    out = []
    for tier, (heading, hint) in enumerate(_TIERS):
        rows = sorted([r for r in ranked if r[2] == tier], key=lambda r: r[3])
        if not rows:
            continue
        groups = {}
        for i, c, _, _, labels in rows:  # same title + year from different providers = alternate artwork for one title
            gk = ((app.poster_key(str(c.get('title') or c.get('original') or '')) if app else str(i)), str(c.get('year') or ''))
            groups.setdefault(gk, []).append((i, c, labels))
        body, singles = [], []

        def flush():
            if singles:
                body.append('<div class="pm-grid">' + ''.join(singles) + '</div>')
                singles.clear()
        for members in groups.values():
            c0 = members[0][1]
            if len(members) > 1:
                flush()
                body.append('<div class="pm-group"><p class="pm-gh">' + _e(c0.get('title') or c0.get('original') or '—') + ' (' + _e(c0.get('year') or '?') + ') — '
                            + str(len(members)) + ' artworks: ' + _e(', '.join(sorted({str(m[1].get('provider') or '?') for m in members}))) + '</p>'
                            '<div class="pm-grid">' + ''.join(card(*m) for m in members) + '</div></div>')
            else:
                singles.append(card(*members[0]))
        flush()
        block = ('<p class="pm-th"><strong>' + heading + '</strong> <small class="muted">(' + str(len(rows)) + ') ' + hint + '</small></p>' + ''.join(body))
        out.append('<details class="pm-weak"><summary>' + heading + ' (' + str(len(rows)) + ') — different title or year</summary>' + ''.join(body) + '</details>'
                   if tier == 2 and len(out) else block)
    return '<div class="pm-results">' + ''.join(out) + '</div>'


PANEL_CSS = ('<style>#themearr-poster-panel .pm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin:12px 0}'
             '#themearr-poster-panel .pm-card{border:1px solid #3a4864;border-radius:10px;padding:8px;display:flex;flex-direction:column;gap:6px}'
             '#themearr-poster-panel .pm-card img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:6px;background:#1c263b}'
             '#themearr-poster-panel .pm-current{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}'
             '#themearr-poster-panel .pm-current img,#themearr-poster-panel .pm-current .pm-empty{width:120px;aspect-ratio:2/3;object-fit:cover;border-radius:8px;background:#1c263b;display:flex;align-items:center;justify-content:center;font-size:.8rem}'
             '#themearr-poster-panel .pm-replace{font-size:.8rem}#themearr-poster-panel form.pm-inline{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}'
             '#themearr-poster-panel form.pm-inline input[type=search]{flex:1 1 220px;padding:10px}'
             '#themearr-poster-panel .pm-results .pm-grid{margin:8px 0}'
             '#themearr-poster-panel .pm-results{display:flex;flex-direction:column;gap:4px}'
             '#themearr-poster-panel .pm-results>.pm-th{margin:12px 0 0}'
             '#themearr-poster-panel .pm-group{border:1px dashed #4a5d80;border-radius:10px;padding:6px 10px;margin:6px 0}#themearr-poster-panel .pm-gh{margin:4px 0;font-size:.9rem;color:#cfe0f8}'
             '#themearr-poster-panel .pm-src{color:#cfe0f8;font-weight:600}#themearr-poster-panel .pm-why{color:#9fb0cc}'
             '#themearr-poster-panel .pm-weak{margin-top:10px;border:1px solid #3a4864;border-radius:10px;padding:8px 12px}#themearr-poster-panel .pm-weak>summary{cursor:pointer;color:#ffcf8a;font-weight:600}</style>')


def panel(app, path, query):
    """Poster section for a title's Theme Browser page."""
    row = _anime(app, path)
    if row is None:
        return ''
    has_poster = app.poster_file(path).is_file()
    with app.db() as c:
        record = c.execute('SELECT status,provider FROM posters WHERE path=?', (path,)).fetchone()
        attempt = c.execute('SELECT outcome,candidates FROM poster_attempts WHERE path=?', (path,)).fetchone()
    state = app.poster_job_status(path)
    source = (record['provider'] if record and record['provider'] else 'automatic search') if has_poster else ''
    poster_url = '/poster?' + urllib.parse.urlencode({'path': path})
    parts = [PANEL_CSS, '<section class="panel" id="themearr-poster-panel"><h3>Poster</h3><div class="pm-current">',
             ('<img alt="Current poster" data-poster-current="' + _e(path) + '" src="' + _e(poster_url) + '">' if has_poster
              else '<div class="pm-empty" data-poster-current="' + _e(path) + '">No poster yet</div>'),
             '<div><p>', _e('Current poster from ' + source + '.' if has_poster else state['message']), '</p>']
    note = query.get('poster_msg', [''])[0][:200]
    if note:
        parts += ['<p role="status"><strong>', _e(note), '</strong></p>']
    if has_poster:
        parts += ['<form method="post" action="/poster-reset" class="pm-inline"><input type="hidden" name="path" value="', _e(path), '">',
                  '<button type="submit" style="background:#52617c">Reset to automatic artwork</button></form>']
    parts += ['</div></div>']
    _, _, saved = saved_candidates(app, path)
    if saved:
        parts += ['<p>', str(len(saved)), ' possible poster', '' if len(saved) == 1 else 's',
                  ' found by the automatic search. ', pick_button(path, row['name']), '</p>']
    token = query.get('poster_search', [''])[0]
    with _LOCK:
        _expire()
        found = _SEARCHES.get(token) if _TOKEN.fullmatch(token or '') else None
    if found and found['path'] == path:
        parts += ['<h4>Search results for “', _e(found['query']), '”</h4>']
        if found['problems']:
            parts += ['<p>Some providers were unavailable: ', _e('; '.join(found['problems'])), '</p>']
        parts += [_grid(path, found['candidates'], 'search', token, has_poster, app, row['name']) or '<p>No posters found for that title. Try other wording.</p>']
    parts += ['<form method="post" action="/poster-search" class="pm-inline"><input type="hidden" name="path" value="', _e(path), '">',
              '<input type="search" name="query" maxlength="120" aria-label="Search posters by title" value="', _e(app.clean(row['name'])), '">',
              '<button type="submit">Search posters</button></form>',
              '<form method="post" action="/poster-upload" enctype="multipart/form-data" class="pm-inline">',
              '<input type="hidden" name="path" value="', _e(path), '">',
              '<input type="file" name="image" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" required>',
              '<button type="submit">Upload your own poster</button></form>',
              '<p class="muted">Uploads: PNG, JPEG or WebP up to 10 MB, at least 100×100 pixels. You will see a preview before anything changes. '
              'Replaced posters are kept in Preludarr’s poster folder; media folders are never touched.</p></section>']
    return ''.join(parts)


def upload_review(app, token):
    item = staged(token)
    if item is None:
        return None
    row = _anime(app, item['path'])
    has_poster = app.poster_file(item['path']).is_file()
    body = ('<h2>Review uploaded poster</h2><p><strong>Anime:</strong> ' + _e(row['name'] if row else '') + '</p>'
            '<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">'
            '<figure style="margin:0"><img alt="Uploaded poster preview" style="width:220px;border-radius:8px" src="/poster-staged?'
            + _e(urllib.parse.urlencode({'token': token})) + '"><figcaption class="muted">New (' + _e(item['kind']) + ', ' + _e(item['size']) + ')</figcaption></figure>'
            + ('<figure style="margin:0"><img alt="Current poster" style="width:220px;border-radius:8px;opacity:.8" src="/poster?'
               + _e(urllib.parse.urlencode({'path': item['path']})) + '"><figcaption class="muted">Current</figcaption></figure>' if has_poster else '')
            + '</div><form method="post" action="/poster-upload-confirm"><input type="hidden" name="token" value="' + _e(token) + '">'
            + ('<p><label><input type="checkbox" name="replace" value="1" required> Replace the current poster (it is kept as a backup)</label></p>' if has_poster else '')
            + '<button type="submit">Use this poster</button></form>'
            '<form method="post" action="/poster-upload-cancel"><input type="hidden" name="token" value="' + _e(token) + '">'
            '<button type="submit" style="background:#52617c">Cancel</button></form>'
            '<p class="muted">This preview expires in ten minutes.</p>')
    return page(app, 'Review uploaded poster', body)


def report(app):
    """Missing-poster report grouped by the last search outcome."""
    with app.db() as c:
        rows = c.execute('SELECT s.path,s.name,s.library,p.status,a.outcome,a.reason FROM shows s '
                         'LEFT JOIN posters p ON p.path=s.path LEFT JOIN poster_attempts a ON a.path=s.path ORDER BY s.name').fetchall()
    groups = {'needs_choice': [], 'no_match': [], 'provider_error': [], 'waiting': []}
    total = len(rows)
    for r in rows:
        if app.poster_file(r['path']).is_file():
            continue
        outcome = r['outcome'] if r['outcome'] in groups else ('provider_error' if r['status'] == 'error' else
                                                               'no_match' if r['status'] == 'none' else 'waiting')
        groups[outcome].append(r)
    missing = sum(len(v) for v in groups.values())
    titles = {'needs_choice': 'Needs your choice — possible posters were found',
              'no_match': 'No matching poster found — try another title or upload one',
              'provider_error': 'Provider was unavailable — retry later',
              'waiting': 'Not searched yet or waiting in the queue'}
    body = ['<h2>Missing posters</h2><p>', str(missing), ' of ', str(total), ' titles have no poster. ',
            'Open a title to choose, search for, or upload a poster. Bulk retries stay on the dashboard '
            '(<a href="/?poster=missing#library-results">Missing posters</a>).</p>']
    for key in ('needs_choice', 'no_match', 'provider_error', 'waiting'):
        items = groups[key]
        body += ['<h3>', _e(titles[key]), ' (', str(len(items)), ')</h3>']
        if items:
            body += ['<ul>'] + ['<li><a href="' + _e(_themes(r['path'])) + '#themearr-poster-panel">' + _e(r['name']) + '</a> <small class="muted">'
                                + _e(r['library']) + '</small></li>' for r in items] + ['</ul>']
    return page(app, 'Missing posters', ''.join(body))


# ------------------------------------------------- in-page poster popup (V1.6.3) ----
# One accessible <dialog> shared by the manual poster picker (dashboard cards, Theme Browser) and the
# Smart Matching V5 artwork preview. Opening, previewing, Back, Cancel and Close never change anything;
# only "Use This Poster" posts a choice. All artwork is served by Themearr itself (never hot-linked).
PICKER_ASSETS = r'''<style>
#themearr-picker{width:min(920px,96vw);max-height:92vh;overflow:auto;padding:0;border:1px solid #4a6181;border-radius:12px;background:#111b2d;color:#eaf2ff}
#themearr-picker::backdrop{background:rgba(5,10,20,.78)}
#themearr-picker .tp-head{position:sticky;top:0;z-index:1;display:flex;gap:12px;justify-content:space-between;align-items:center;padding:12px 16px;background:#14233e;border-bottom:1px solid #324158}
#themearr-picker .tp-head h2{margin:0;font-size:1.05rem;overflow-wrap:anywhere}
#themearr-picker .tp-x{flex:0 0 auto;width:40px;height:40px;padding:0;font-size:1.4rem;line-height:1;background:transparent;border:1px solid #4a6181;border-radius:8px;color:#eaf2ff;cursor:pointer}
#themearr-picker .tp-body{padding:16px}
#themearr-picker .tp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px;margin:12px 0}
#themearr-picker .tp-card{display:flex;flex-direction:column;gap:6px;padding:8px;border:1px solid #324158;border-radius:10px;background:#0d1626;min-width:0}
#themearr-picker .tp-thumb,#themearr-picker .tp-big .tp-art{width:100%;aspect-ratio:2/3;object-fit:contain;display:block;background:#1c263b;border-radius:6px}
#themearr-picker .tp-thumb-btn{padding:0;border:0;background:none;cursor:zoom-in;width:100%}
#themearr-picker .tp-t{font-weight:650;overflow-wrap:anywhere}
#themearr-picker .tp-meta{color:#9baac5;font-size:.8rem;overflow-wrap:anywhere}
#themearr-picker .tp-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:auto}
#themearr-picker .tp-actions button{flex:1 1 auto}
#themearr-picker .tp-footer{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;margin-top:16px}
#themearr-picker .tp-big{display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start}
#themearr-picker .tp-big .tp-art{width:min(340px,100%)}
#themearr-picker .tp-big dl{margin:0;flex:1 1 220px;min-width:0}
#themearr-picker .tp-big dt{color:#9baac5;font-size:.8rem;margin-top:10px}
#themearr-picker .tp-big dd{margin:2px 0 0;overflow-wrap:anywhere}
#themearr-picker .tp-error{color:#ffb4b4}
.tp-secondary{background:#52617c!important}
.tp-noart{display:flex;align-items:center;justify-content:center;text-align:center;color:#9baac5;font-size:.85rem;padding:8px;box-sizing:border-box;background:#1c263b;border-radius:6px}
.poster-tools{display:flex;flex-direction:column;gap:8px;margin:0 0 8px}
.poster-tools form{margin:0}
.poster-tools [data-poster-retry-status]{margin:0;font-size:.8rem;line-height:1.4;overflow-wrap:anywhere}
.poster-tools [data-poster-retry-status]:empty{display:none}
.sm-card{display:flex;gap:16px;align-items:flex-start;margin:12px 0;padding:12px;border:1px solid #52627c;border-radius:10px}
.sm-art{flex:0 0 auto;width:160px;padding:0;border:0;background:none;cursor:zoom-in;border-radius:8px}
.sm-art img,.sm-art .tp-noart{width:160px;height:240px;object-fit:contain;display:block;background:#1c263b;border-radius:8px}
.sm-art .tp-noart{display:flex}
.sm-info{flex:1 1 220px;min-width:0}
.sm-info h4{margin:0 0 4px;overflow-wrap:anywhere}
.sm-info p{margin:4px 0}
.sm-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px}
.sm-actions form{margin:0}
@media(max-width:640px){#themearr-picker .tp-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}#themearr-picker .tp-body{padding:12px}.sm-art,.sm-art img,.sm-art .tp-noart{width:110px;height:165px}}
@media(max-width:340px){#themearr-picker .tp-grid{grid-template-columns:1fr}}
</style>
<script>/* themearr-poster-picker (V1.6.3) */
(function () {
  "use strict";
  if (window.themearrPicker) return;
  function el(tag, attrs, kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'text') node.textContent = v; else if (k === 'on') node.addEventListener('click', v); else node.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of [].concat(kids || [])) if (kid) node.append(kid);
    return node;
  }
  function art(src, alt, cls) {
    if (!src) return el('div', {class: cls + ' tp-noart', text: 'No artwork available'});
    const img = el('img', {src, alt, class: cls, loading: 'lazy', decoding: 'async'});
    img.addEventListener('error', () => img.replaceWith(el('div', {class: cls + ' tp-noart', text: 'No artwork available'})));
    return img;
  }
  const button = (text, on, secondary, extra) => el('button', Object.assign({type: 'button', text, on, class: secondary ? 'tp-secondary' : null}, extra || {}));
  const dlg = el('dialog', {id: 'themearr-picker', 'aria-labelledby': 'tp-title'});
  const heading = el('h2', {id: 'tp-title'});
  const body = el('div', {class: 'tp-body'});
  dlg.append(el('div', {class: 'tp-head'}, [heading, button('×', () => dlg.close(), false, {class: 'tp-x', 'aria-label': 'Close'})]), body);
  let opener = null, current = null, generation = 0;
  dlg.addEventListener('close', () => { generation++; current = null; body.textContent = ''; if (opener && document.contains(opener)) opener.focus(); });
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });  // click on the backdrop
  function view(title, nodes, focus) {
    heading.textContent = title;
    body.replaceChildren(...nodes);
    if (!dlg.open) { if (!dlg.isConnected) document.body.append(dlg); dlg.showModal(); }
    dlg.scrollTop = 0;
    (focus || body.querySelector('button, input') || dlg.querySelector('.tp-x')).focus();
  }

  /* ---- refresh every place that shows this title's poster, without reloading the page ---- */
  function refresh(path) {
    const url = '/poster?' + new URLSearchParams({path}).toString() + '&fresh=' + Date.now();
    for (const link of document.querySelectorAll('a[href^="/themes?"]')) {
      let linked; try { linked = new URL(link.href, location.href).searchParams.get('path'); } catch (_) { continue; }
      if (linked !== path) continue;
      const old = link.querySelector('.card-empty, .poster-empty, img.card-poster, img.poster');
      if (!old) continue;
      const card = old.classList.contains('card-empty') || old.classList.contains('card-poster');
      old.replaceWith(el('img', {class: card ? 'card-poster' : 'poster', alt: '', src: url}));
    }
    for (const box of document.querySelectorAll('[data-poster-current]')) {
      if (box.getAttribute('data-poster-current') === path) box.replaceWith(el('img', {alt: 'Current poster', 'data-poster-current': path, src: url}));
    }
  }

  /* ---- manual poster picker ---- */
  async function openPicker(path, name, from) {
    opener = from || document.activeElement;
    const mine = ++generation;
    view('Select a poster — ' + name, [el('p', {role: 'status', text: 'Loading the posters Preludarr found…'})]);
    try {
      const reply = await fetch('/poster-candidates?' + new URLSearchParams({path}).toString(), {credentials: 'same-origin', cache: 'no-store'});
      if (new URL(reply.url, location.href).pathname === '/login') throw new Error('Your session expired. Log in again.');
      if (!reply.ok) throw new Error('Poster choices could not be loaded (HTTP ' + reply.status + ').');
      const data = await reply.json();
      if (mine !== generation) return;
      current = {path, data};
      results();
    } catch (error) {
      if (mine === generation) view('Select a poster — ' + name, [el('p', {class: 'tp-error', role: 'alert', text: error.message}), footer([button('Close', () => dlg.close(), true)])]);
    }
  }
  const footer = buttons => el('div', {class: 'tp-footer'}, buttons);
  function meta(c) { return [c.provider, c.year].filter(Boolean).join(' · '); }
  function results(focusIndex) {
    const {data} = current;
    const title = 'Select a poster — ' + data.name;
    if (!data.candidates.length) {
      view(title, [el('p', {text: 'Preludarr has no saved poster choices for this title. Use Retry Poster first, or open the title in the Theme Browser to search by another name or upload your own poster.'}),
                   footer([button('Close', () => dlg.close(), true)])]);
      return;
    }
    let focus = null;
    const cards = data.candidates.map(c => {
      const pick = button('Select Poster', () => confirm(c));
      if (c.index === focusIndex) focus = pick;
      return el('div', {class: 'tp-card'}, [
        el('button', {type: 'button', class: 'tp-thumb-btn', 'aria-label': 'View larger poster: ' + (c.title || c.original), on: () => preview(c)}, art(c.thumb, '', 'tp-thumb')),
        el('div', {class: 'tp-t', text: c.title || c.original || 'Untitled'}),
        c.original && c.original !== c.title ? el('div', {class: 'tp-meta', text: c.original}) : null,
        el('div', {class: 'tp-meta', text: meta(c) || 'Source unknown'}),
        el('div', {class: 'tp-meta', text: c.why}),
        el('div', {class: 'tp-actions'}, [button('View Larger', () => preview(c), true), pick])]);
    });
    view(title, [el('p', {text: data.candidates.length + (data.candidates.length === 1 ? ' possible poster was' : ' possible posters were') + ' found for ' + data.library + '. None was certain enough to apply automatically — choose the right one, or cancel. Nothing changes until you click Use This Poster.'}),
                 el('div', {class: 'tp-grid'}, cards), footer([button('Cancel', () => dlg.close(), true)])], focus);
  }
  function details(rows) {
    return el('dl', {}, rows.filter(r => r[1]).flatMap(r => [el('dt', {text: r[0]}), el('dd', {text: r[1]})]));
  }
  function preview(c) {
    view(c.title || c.original || 'Poster preview', [
      el('div', {class: 'tp-big'}, [art(c.thumb, 'Poster: ' + (c.title || c.original), 'tp-art'),
        details([['Title', c.title], ['Original title', c.original !== c.title ? c.original : ''], ['Year', c.year], ['Source', c.provider + (c.id ? ' #' + c.id : '')], ['Why it is shown', c.why]])]),
      footer([button('Back to Results', () => results(c.index), true), button('Select Poster', () => confirm(c))])]);
  }
  function confirm(c) {
    const {path, data} = current;
    const replace = data.has_poster ? el('input', {type: 'checkbox', id: 'tp-replace'}) : null;
    const error = el('p', {class: 'tp-error', role: 'alert'});
    const use = button('Use This Poster', async () => {
      if (replace && !replace.checked) { error.textContent = 'Tick “Replace the current poster” to confirm the replacement.'; replace.focus(); return; }
      use.disabled = true; use.textContent = 'Saving…'; error.textContent = '';
      try {
        const reply = await fetch('/poster-choose', {method: 'POST', credentials: 'same-origin', cache: 'no-store',
          body: new URLSearchParams({path, source: 'attempt', token: '', index: String(c.index), replace: replace && replace.checked ? '1' : '', ajax: '1'})});
        if (new URL(reply.url, location.href).pathname === '/login') throw new Error('Your session expired. Log in again.');
        const result = await reply.json().catch(() => ({ok: false, message: 'Unexpected reply (HTTP ' + reply.status + ').'}));
        if (!result.ok) throw new Error(result.message);
        dlg.close();
        refresh(path);
        document.dispatchEvent(new CustomEvent('themearr:poster-chosen', {detail: {path, message: result.message}}));
      } catch (e) { error.textContent = e.message; use.disabled = false; use.textContent = 'Use This Poster'; }
    });
    view('Use this poster?', [
      el('div', {class: 'tp-big'}, [art(c.thumb, 'Poster: ' + (c.title || c.original), 'tp-art'),
        el('div', {}, [details([['Anime in your library', data.name], ['Poster', c.title || c.original], ['Year', c.year], ['Source', c.provider]]),
          replace ? el('p', {}, [replace, el('label', {for: 'tp-replace', text: ' Replace the current poster (the old one is kept as a backup)'})]) : null,
          error])]),
      footer([button('Back to Results', () => results(c.index), true), button('Cancel', () => dlg.close(), true), use])], use);
  }

  /* ---- Smart Matching V5 artwork preview (read-only; never saves a match) ---- */
  function openArt(info, from) {
    opener = from || document.activeElement;
    generation++;
    view(info.title || 'Anime artwork', [
      el('div', {class: 'tp-big'}, [art(info.img, 'Cover art: ' + info.title, 'tp-art'),
        details([['Title', info.title], ['Alternative titles', (info.alts || []).join(' · ')], ['Year', info.year], ['Type', info.type], ['Matching', info.match]])]),
      el('p', {class: 'tp-meta', text: 'Viewing artwork does not save a match. Use Remember this anime to confirm it.'}),
      footer([button('Back to suggestions', () => dlg.close(), true), button('Close', () => dlg.close(), true)])]);
  }

  document.addEventListener('click', event => {
    const pick = event.target.closest('[data-poster-pick]');
    if (pick) { event.preventDefault(); openPicker(pick.getAttribute('data-poster-pick'), pick.getAttribute('data-poster-name') || 'this anime', pick); return; }
    const artButton = event.target.closest('[data-art-preview]');
    if (artButton) { event.preventDefault(); try { openArt(JSON.parse(artButton.getAttribute('data-art-preview')), artButton); } catch (_) {} }
  });
  document.addEventListener('error', event => {
    const img = event.target;
    if (img && img.tagName === 'IMG' && img.hasAttribute('data-noart')) img.replaceWith(el('div', {class: 'tp-noart', text: img.getAttribute('data-noart')}));
  }, true);
  window.themearrPicker = {open: openPicker, refresh, pickButton(path, name) {
    return el('button', {type: 'button', 'data-poster-pick': path, 'data-poster-name': name, text: 'Select Poster Manually'});
  }};
})();
</script>'''


def pick_button(path, name):
    return ('<button type="button" data-poster-pick="' + _e(path) + '" data-poster-name="' + _e(name) + '">'
            'Select Poster Manually</button>')
