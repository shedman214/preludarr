# SPDX-License-Identifier: GPL-3.0-or-later
"""Themearr Smart Matching V4 and targeted Media Servers V1.

Ships as /app/media_servers.py. No install/download code is changed here.
All mutating routes must be invoked *after* Themearr's auth.post(handler).
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from login import brand_html

_LOCK = threading.RLock()
_REFRESH_LOCK = threading.Lock()
_RECENT = {}
_TYPES = ('plex', 'jellyfin', 'emby')
_DETAIL = 'animethemes.animethemeentries.videos.audio,animethemes.song'
_SLUG = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
_PLEX_ID = re.compile(r'[0-9]{1,20}\Z')
_ITEM_ID = re.compile(r'[A-Za-z0-9-]{1,80}\Z')


def _path(config, kind):
    return Path(config) / ('themearr_v4_matches.json' if kind == 'matches' else 'themearr_media_servers_v1.json')


def _read(config, kind):
    file = _path(config, kind)
    if file.is_symlink():
        raise ValueError('Unsafe settings file')
    if not file.exists():
        return {}
    if file.stat().st_size > 1_000_000:
        raise ValueError('Settings file too large')
    with file.open('r', encoding='utf-8') as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError('Invalid settings structure')
    return value


def _write(config, kind, document):
    folder = Path(config)
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError('Invalid config directory')
    dst = _path(config, kind)
    if dst.is_symlink():
        raise ValueError('Unsafe settings file')
    content = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
    if len(content) > 1_000_000:
        raise ValueError('Settings limit exceeded')
    filename = '.themearr-settings-' + secrets.token_hex(12)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(folder / filename, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(folder / filename, dst)
    finally:
        try:
            (folder / filename).unlink()
        except FileNotFoundError:
            pass


def _valid_anime(path, roots, db):
    if not isinstance(path, str) or not 0 < len(path) <= 4096:
        return None
    folder = Path(path)
    if folder.is_symlink() or not folder.is_dir():
        return None
    if not any(folder.parent == root and root.is_dir() and not root.is_symlink()
               for _, root in roots):
        return None
    with db() as conn:
        row = conn.execute('SELECT name,library FROM shows WHERE path=?', (path,)).fetchone()
    return dict(row) if row else None


def _parse(handler, allowed, max_size=8192):
    try:
        n = int(handler.headers.get('Content-Length', '-1'))
        if not 0 <= n <= max_size:
            return None
        fields = urllib.parse.parse_qs(handler.rfile.read(n).decode('utf-8', 'strict'),
                                       keep_blank_values=True, max_num_fields=15)
        if not set(fields).issubset(allowed) or any(len(v) != 1 for v in fields.values()):
            return None
        return {key: v[0] for key, v in fields.items()}
    except (ValueError, UnicodeError, OSError):
        return None


def _e(s):
    return html.escape(str(s), quote=True)


def _u(route, **query):
    return route + '?' + urllib.parse.urlencode(query)


# --------------------- Smart Matching V4 ------------------------------
def has_override(config, anime_path):
    with _LOCK:
        return bool(_read(config, 'matches').get(anime_path))


def override(config, anime_path):
    with _LOCK:
        item = _read(config, 'matches').get(anime_path)
    return item if isinstance(item, dict) and _SLUG.fullmatch(str(item.get('slug', ''))) else None


def resolve(config, anime_path, name, media, automatic, request_json):
    item = override(config, anime_path)
    if not item:
        return automatic(name, media or {})
    slug = item['slug']
    url = 'https://api.animethemes.moe/anime/' + urllib.parse.quote(slug, safe='') + '?' + urllib.parse.urlencode({'include': _DETAIL})
    try:
        result = request_json(url).get('anime')
        if isinstance(result, dict) and result.get('slug') == slug:
            return result
    except Exception as error:
        print('Saved anime match unavailable: ' + type(error).__name__, flush=True)
    # Never silently fall back to a potentially different anime once explicitly mapped.
    return None



def saved_banner(config, anime_path):
    item = override(config, anime_path)
    if not item:
        return ''
    return (
        '<div class="panel"><strong>Smart Matching V5: saved selection</strong> — ' +
        _e(item.get('name') or item['slug']) +
        '<form method="post" action="/v4-remove-match">' +
        '<input type="hidden" name="path" value="' + _e(anime_path) + '">' +
        '<button type="submit">Forget saved match</button></form></div>'
    )


def fallback_panel(config, anime_path, name, request_json):
    """Display only plausible AnimeThemes alternatives; never auto-save matches."""
    from difflib import SequenceMatcher
    import unicodedata

    MIN_SCORE = 0.83
    MIN_SINGLE_WORD_SCORE = 0.93

    def norm(value):
        return ''.join(ch for ch in unicodedata.normalize('NFKC', str(value)).casefold() if ch.isalnum())

    def words(value):
        return set(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', str(value)).casefold()))

    simplified = re.sub(r'\s*\(\d{4}\)\s*$', '', name).strip()[:90]
    if not simplified:
        return ''
    target = norm(simplified)
    if not target:
        return ''
    target_words = words(simplified)
    yr = re.search(r'\((\d{4})\)', name)
    desired_year = yr.group(1) if yr else ''
    saved = override(config, anime_path)
    # Retain the old HTML id for existing deep links and selectors.
    box = ['<section class="panel" id="smart-matching-v4"><h3>Smart Matching V5</h3>']
    if saved:
        box.extend(['<p>Saved AnimeThemes match: <strong>', _e(saved.get('name') or saved['slug']),
                    '</strong> (', _e(saved['slug']), ')</p>',
                    '<form method="post" action="/v4-remove-match">',
                    '<input type="hidden" name="path" value="', _e(anime_path), '">',
                    '<button type="submit">Forget saved match</button></form>'])
    try:
        result = request_json('https://api.animethemes.moe/anime?' + urllib.parse.urlencode({
            'q': simplified, 'page[size]': '20', 'include': 'animesynonyms'}))
        candidates = [r for r in (result.get('anime') or []) if isinstance(r, dict)]
        ranks = []
        for item in candidates:
            slug = item.get('slug')
            if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
                continue
            year = str(item.get('year') or '')
            # A known contradictory year is not a trustworthy suggestion.
            if desired_year and re.fullmatch(r'\d{4}', year) and year != desired_year:
                continue
            names = [item.get('name'), item.get('name_en'), item.get('name_jp')]
            synonyms = item.get('animesynonyms') or []
            if isinstance(synonyms, list):
                for synonym in synonyms[:100]:
                    if isinstance(synonym, dict):
                        names.append(synonym.get('text') or synonym.get('name'))
                    elif isinstance(synonym, str):
                        names.append(synonym)
            strongest = 0.0
            exact_alias = False
            best_alias = ''
            for alias in names:
                if not isinstance(alias, str) or not alias.strip():
                    continue
                candidate_norm = norm(alias[:250])
                if not candidate_norm:
                    continue
                score = SequenceMatcher(None, target, candidate_norm, autojunk=False).ratio()
                overlap = len(target_words & words(alias[:250])) / max(1, len(target_words))
                # A close character string must also resemble actual title words.
                # Exact normalized aliases are always accepted.
                is_exact = candidate_norm == target
                min_score = MIN_SINGLE_WORD_SCORE if len(target_words) == 1 else MIN_SCORE
                if is_exact or (score >= min_score and overlap >= 0.5):
                    if score > strongest:
                        strongest = score
                        exact_alias = is_exact
                        best_alias = alias.strip()[:250]
            if strongest:
                # names / best_alias are display-only; ranking still uses the first three fields.
                ranks.append((strongest, exact_alias, slug, item, year, names, best_alias))
        ranks.sort(key=lambda r: (-r[0], not r[1], r[2]))
        if not ranks:
            box.append('<p>No reliable AnimeThemes suggestions for this title. '
                       'Unrelated or year-mismatched results were hidden. '
                       'Use <strong>Manual matching</strong> below if you know an alternate title.</p>')
        else:
            box.append('<p>Only plausible title matches are shown. '
                       'Check the anime and year before clicking Remember. '
                       'Saving a match does not install audio.</p>')
            for score, exact_alias, slug, item, year, names, best_alias in ranks[:5]:
                label = str(item.get('name') or slug)
                match_label = 'Exact alternate title' if exact_alias else 'Possible title match'
                year_label = year or 'year unverified'
                kind = {'TV': 'Anime TV', 'MOVIE': 'Anime Movie', 'OVA': 'OVA', 'ONA': 'ONA', 'SPECIAL': 'Special'}.get(
                    str(item.get('media_format') or '').upper(), '')
                alts = []
                for alias in names:
                    if isinstance(alias, str) and alias.strip() and alias.strip() != label and alias.strip() not in alts:
                        alts.append(alias.strip()[:120])
                shown_alt = best_alias if best_alias and best_alias != label else (alts[0] if alts else '')
                similarity = match_label + ' · title similarity ' + str(round(score * 100)) + '%'
                art = _u('/anime-art', slug=slug)
                preview = json.dumps({'img': art, 'title': label, 'alts': alts[:8], 'year': year_label,
                                      'type': kind or 'not stated by AnimeThemes', 'match': similarity}, ensure_ascii=False)
                box.extend(['<div class="sm-card">',
                            '<button type="button" class="sm-art" data-art-preview="', _e(preview), '" aria-label="View larger artwork for ', _e(label), '">',
                            '<img loading="lazy" alt="" src="', _e(art), '" data-noart="No AnimeThemes artwork"></button>',
                            '<div class="sm-info"><h4>', _e(label), '</h4>',
                            ('<p class="muted">' + _e(shown_alt) + '</p>') if shown_alt else '',
                            '<p>', _e(year_label), (' · ' + _e(kind)) if kind else '', '</p>',
                            '<p>', _e(similarity), '</p>',
                            '<div class="sm-actions">',
                            '<a style="color:#b8efff;font-weight:700;text-decoration:underline" href="',
                            _e(_u('/themes', path=anime_path, theme_slug=slug)), '">Preview OP/ED</a>',
                            '<form method="post" action="/v4-save-match">',
                            '<input type="hidden" name="path" value="', _e(anime_path), '">',
                            '<input type="hidden" name="slug" value="', _e(slug), '">',
                            '<button type="submit">Remember this anime</button></form></div></div></div>'])
    except Exception:
        box.append('<p>Alternative search is temporarily unavailable; existing manual search still works.</p>')
        try:
            import animethemes
            if animethemes.status_note():
                box.extend(['<p class="muted">', _e(animethemes.status_note()), '</p>'])
        except ImportError:
            pass
    box.append('</section>')
    return ''.join(box)


def save_override(handler, config, roots, db, request_json, redirect, remove=False):
    form = _parse(handler, {'path'} if remove else {'path', 'slug'})
    if not form or not form.get('path') or (not remove and not form.get('slug')):
        return handler.send(400, 'Invalid match request', 'text/plain')
    path = form['path']
    if _valid_anime(path, roots, db) is None:
        return handler.send(400, 'Invalid anime', 'text/plain')
    if not remove:
        slug = form['slug']
        if not _SLUG.fullmatch(slug):
            return handler.send(400, 'Invalid AnimeThemes slug', 'text/plain')
        url = 'https://api.animethemes.moe/anime/' + urllib.parse.quote(slug, safe='') + '?' + urllib.parse.urlencode({'include': _DETAIL})
        try:
            item = request_json(url).get('anime')
        except Exception:
            return handler.send(502, 'AnimeThemes verification unavailable', 'text/plain')
        if not isinstance(item, dict) or item.get('slug') != slug:
            return handler.send(400, 'AnimeThemes entry not found', 'text/plain')
    with _LOCK:
        data = _read(config, 'matches')
        if remove:
            data.pop(path, None)
        else:
            data[path] = {'slug': slug, 'name': str(item.get('name') or slug)[:200],
                          'year': str(item.get('year') or '')[:10]}
        _write(config, 'matches', data)
    return redirect(handler, _u('/themes', path=path))


# --------------------- Media server settings ----------------------------
def _base_url(raw):
    """Accept verified HTTPS hosts (incl. reverse-proxy prefixes), or RFC1918 HTTP IPs.

    No URL credentials, query, fragment, redirects, proxy inheritance or TLS bypass.
    The HTTPS operator is responsible for supplying a valid trusted certificate.
    """
    text = str(raw or '').strip()
    if not text or len(text) > 2048 or any(ord(x) < 33 or ord(x) == 127 for x in text):
        raise ValueError('Enter a valid media-server URL')
    try:
        p = urllib.parse.urlsplit(text)
        scheme = p.scheme.lower()
        host = p.hostname or ''
        port = p.port
    except (ValueError, TypeError):
        raise ValueError('Invalid media-server host or port') from None
    if (scheme not in ('http','https') or not p.netloc or
            p.username is not None or p.password is not None or p.query or p.fragment or
            '@' in p.netloc or '\\' in p.netloc or '%' in host):
        raise ValueError('Use an HTTP LAN IP or HTTPS host without URL credentials or query')
    # Absolute URL prefixes are supported for a reverse proxy: https://example.com/jellyfin
    path = p.path.rstrip('/')
    if path:
        if (not path.startswith('/') or '//' in path or '%' in path or '\\' in path or
                any(part in ('.','..') for part in path.split('/')) or
                not all(re.fullmatch(r'[A-Za-z0-9_.~-]+', part) for part in path.split('/')[1:])):
            raise ValueError('Unsafe media-server URL prefix')
        if len(path) > 250:
            raise ValueError('Media-server URL prefix too long')
    try:
        ip = ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
        try:
            host = host.encode('idna').decode('ascii').lower()
        except UnicodeError:
            raise ValueError('Invalid media-server hostname') from None
        if (len(host)>253 or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*',host) or
                any(part.startswith('-') or part.endswith('-') or len(part)>63
                    for part in host.split('.'))):
            raise ValueError('Invalid media-server hostname')
    if port is not None and not (1 <= port <= 65535):
        raise ValueError('Invalid media-server port')
    if scheme == 'http':
        # Keep V2 policy for plaintext connections: numeric RFC1918 IPv4 only.
        if not is_ip or not isinstance(ip,ipaddress.IPv4Address) or not any(
            ip in ipaddress.IPv4Network(net) for net in
            ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
            raise ValueError('Plain HTTP requires a private LAN IPv4 address; use HTTPS elsewhere')
        if not port:
            raise ValueError('Specify a port for an HTTP LAN server')
    if is_ip and isinstance(ip,ipaddress.IPv6Address):
        host = '[' + ip.compressed + ']'
    elif is_ip:
        host = str(ip)
    return scheme + '://' + host + ((':'+str(port)) if port is not None else '') + path



def _item_ok(kind, value):
    return bool((_PLEX_ID if kind == 'plex' else _ITEM_ID).fullmatch(value))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirect refused')


def _call(server, method, path, *, timeout=5, data=None, limit=None):
    """Bounded authenticated call, with HTTPS peer verification and no redirects. data: bytes body (1.12, collection themes)."""
    base = _base_url(server['url'])
    kind = server.get('kind')
    if kind not in _TYPES:
        raise ValueError('Unknown server type')
    token = str(server.get('api_key') or '')
    if not 1 <= len(token) <= 256 or any(
            ord(c) < 33 or ord(c) > 126 or c in ('"', '\\') for c in token):
        raise ValueError('Invalid API token format')
    if (method not in ('GET', 'POST', 'PUT', 'DELETE') or not isinstance(path,str) or
            not path.startswith('/') or '..' in path or '://' in path or
            '\r' in path or '\n' in path or '\\' in path):
        raise ValueError('Invalid server request')
    headers = {'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)', 'Accept': 'application/json, application/xml'}
    if kind == 'plex':
        headers['X-Plex-Token'] = token
    elif kind == 'jellyfin':
        headers['Authorization'] = ('MediaBrowser Client="Preludarr", Device="Server", '
                                    'DeviceId="themearr-connect-v3", Version="3.0", '
                                    'Token="' + token + '"')
    else:
        headers['X-Emby-Token'] = token
    # Emby supports /emby/Items; avoid doubling /emby when a reverse-proxy
    # base URL already has that prefix.
    if kind == 'emby' and base.lower().endswith('/emby') and path.lower().startswith('/emby/'):
        path = path[5:]
    if data is not None:
        if not isinstance(data, bytes) or len(data) > 50 * 1024 * 1024:
            raise ValueError('Invalid upload')
        headers['Content-Type'] = 'application/octet-stream'
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    # Do not trust system proxy variables. TLS verifies hostname + CA chain by default.
    import ssl
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect,
    )
    with opener.open(request, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError('Unexpected media server HTTP status')
        if limit is None:
            return response.read(1_000_001)  # unchanged behaviour for existing callers
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('Media server answer too large')
        return body




def _test_connection(server):
    """Validate JSON or XML Plex responses and modern Jellyfin authentication."""
    kind = server['kind']
    path = ('/library/sections' if kind == 'plex' else
            '/emby/System/Info' if kind == 'emby' else '/System/Info')
    body = _call(server, 'GET', path)
    if kind == 'plex':
        value = body.lstrip(b'\xef\xbb\xbf\t\r\n ')
        if value.startswith(b'{'):
            response = json.loads(value)
            if not isinstance(response, dict) or not isinstance(response.get('MediaContainer'), dict):
                raise ValueError('Unexpected Plex JSON')
        elif value.startswith(b'<'):
            tag = ET.fromstring(value).tag.rsplit('}', 1)[-1]
            if tag != 'MediaContainer':
                raise ValueError('Unexpected Plex XML')
        else:
            raise ValueError('Unexpected Plex response format')
    else:
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError('Unexpected media-server response')
    return True



EXTRA_PLEX = re.compile(r'plex-[2-9]\Z')  # 1.12: more Plex servers besides the main one (key 'plex')


def plex_servers(config):
    """[(key, record)] for the main Plex server ('plex') and any extra ones ('plex-2' … 'plex-9'), configured ones only."""
    with _LOCK:
        doc = _read(config, 'servers')
    keys = ['plex'] + sorted(k for k in doc if EXTRA_PLEX.fullmatch(k))
    return [(k, doc[k]) for k in keys if isinstance(doc.get(k), dict) and doc[k].get('url') and doc[k].get('api_key')]


def _server_map(config):
    with _LOCK:
        doc = _read(config, 'servers')
    return {k: v for k, v in doc.items() if k in _TYPES and isinstance(v, dict)}


def _validate_destination(config, roots, db, path):
    row = _valid_anime(path, roots, db)
    if not row:
        raise ValueError('Unknown anime')
    return row


def _logo(kind):
    paths = {'plex': 'M3.987 8.409c-.96 0-1.587.28-2.12.933v-.72H0v8.88s.038.018.127.037c.138.03.821.187 1.331-.249.441-.377.542-.814.542-1.318v-1.283c.533.573 1.147.813 2 .813 1.84 0 3.253-1.493 3.253-3.48 0-2.12-1.36-3.613-3.266-3.613Zm16.748 5.595.406.591c.391.614.894.906 1.492.908.621-.012 1.064-.562 1.226-.755 0 0-.307-.27-.686-.72-.517-.614-1.214-1.755-1.24-1.803l-1.198 1.779Zm-3.205-1.955c0-2.08-1.52-3.64-3.52-3.64s-3.467 1.587-3.467 3.573a3.48 3.48 0 0 0 3.507 3.52c1.413 0 2.626-.84 3.253-2.293h-2.04l-.093.093c-.427.4-.72.533-1.227.533-.787 0-1.373-.506-1.453-1.266h4.986c.04-.214.054-.307.054-.52Zm-7.671-.219c0 .769.11 1.701.868 2.722l.056.069c-.306.526-.742.88-1.248.88-.399 0-.814-.211-1.138-.579a2.177 2.177 0 0 1-.538-1.441V6.409H9.86l-.001 5.421Zm9.283 3.46h-2.39l2.247-3.332-2.247-3.335h2.39l2.248 3.335-2.248 3.332Zm1.593-1.286Zm-17.162-.342c-.933 0-1.68-.773-1.68-1.72s.76-1.666 1.68-1.666c.92 0 1.68.733 1.68 1.68 0 .946-.733 1.706-1.68 1.706Zm18.361-1.974L24 8.622h-2.391l-.87 1.293 1.195 1.773Zm-9.404-.466c.16-.706.72-1.133 1.493-1.133.773 0 1.373.467 1.507 1.133h-3Z', 'jellyfin': 'M12 .002C8.826.002-1.398 18.537.16 21.666c1.56 3.129 22.14 3.094 23.682 0C25.384 18.573 15.177 0 12 0zm7.76 18.949c-1.008 2.028-14.493 2.05-15.514 0C3.224 16.9 9.92 4.755 12.003 4.755c2.081 0 8.77 12.166 7.759 14.196zM12 9.198c-1.054 0-4.446 6.15-3.93 7.189.518 1.04 7.348 1.027 7.86 0 .511-1.027-2.874-7.19-3.93-7.19z', 'emby': 'M11.041 0c-.007 0-1.456 1.43-3.219 3.176L4.615 6.352l.512.513.512.512-2.819 2.791L0 12.961l1.83 1.848c1.006 1.016 2.438 2.46 3.182 3.209l1.351 1.359.508-.496c.28-.273.515-.498.524-.498.008 0 1.266 1.264 2.794 2.808L12.97 24l.187-.182c.23-.225 5.007-4.95 5.717-5.656l.52-.516-.502-.513c-.276-.282-.5-.52-.496-.53.003-.009 1.264-1.26 2.802-2.783 1.538-1.522 2.8-2.776 2.803-2.785.005-.012-3.617-3.684-6.107-6.193L17.65 4.6l-.505.505c-.279.278-.517.501-.53.497-.013-.005-1.27-1.267-2.793-2.805A449.655 449.655 0 0011.041 0zM9.223 7.367c.091.038 7.951 4.608 7.957 4.627.003.013-1.781 1.056-3.965 2.32a999.898 999.898 0 01-3.996 2.307c-.019.006-.026-1.266-.026-4.629 0-3.7.007-4.634.03-4.625Z'}
    if kind not in paths:
        return ''
    return ('<span class="brand-mark brand-mark-' + kind + '" aria-hidden="true">'
            '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" focusable="false">'
            '<path d="' + paths[kind] + '"/></svg></span>')


_SETTINGS_HEAD = """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings - {title} | Preludarr</title>
<style>
:root{color-scheme:dark;--bg:#101829;--side:#151e32;--card:#202b44;--card2:#263550;--line:#3b4b6b;--text:#f2f6ff;--muted:#adbed9;--accent:#66cafa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,-apple-system,'Segoe UI',sans-serif}
a{color:#a9dbff;text-decoration:none}a:hover{text-decoration:underline}button,input{font:inherit}
.shell{display:flex;min-height:100vh}.side{width:228px;flex:none;background:var(--side);border-right:1px solid var(--line);padding:24px 14px}
.brand{font-size:21px;font-weight:800;padding:0 12px 30px}.side-title{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);padding:12px 14px}
.navitem{display:block;padding:12px 14px;color:#ccd9ed;border-radius:8px}.navitem.active{color:white;background:#30425f;border-left:3px solid var(--accent);font-weight:700}
.main{flex:1;min-width:0;padding:32px max(20px,calc((100vw - 1400px)/3)) 60px}.topline{display:flex;gap:16px;justify-content:space-between;align-items:center;flex-wrap:wrap}
.crumb{color:var(--muted);font-size:13px}.heading{font-size:30px;margin:13px 0 5px}.muted{color:var(--muted)}.intro{line-height:1.6;max-width:860px}
.heading-row{display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap;margin:24px 0}
.btn,button{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid #667997;border-radius:7px;padding:10px 15px;cursor:pointer;background:#e2ebf7;color:#16243a;font-weight:700}
.btn:hover,button:hover{filter:brightness(1.08);text-decoration:none}.btn.primary,button.primary{background:#57c4f6;color:#09253c;border-color:#57c4f6}
.btn.ghost{background:transparent;color:#d2e4fd;border-color:#5c718e}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.connection{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px;min-height:245px;display:flex;flex-direction:column}
.conn-head{display:flex;align-items:center;gap:12px}.conn-symbol{width:43px;height:43px;display:grid;place-items:center;border-radius:12px;background:#40567c;font-size:23px;font-weight:800}
.conn-name{font-size:19px;font-weight:750}.tag{display:inline-block;padding:4px 8px;border-radius:15px;font-size:12px;background:#384862;color:#d5e5fb}
.tag.on{background:#224d4b;color:#a6eedc}.tag.off{background:#554529;color:#ffdda0}
.stats{margin:18px 0;line-height:1.85;color:var(--muted);overflow-wrap:anywhere}.stats strong{color:#e5f0ff}.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:auto}.actions form{margin:0}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-top:25px;padding:21px}.panel h2{margin-top:0}
.mapping-row{display:grid;grid-template-columns:110px minmax(180px,1fr) auto;gap:12px;align-items:end;margin:14px 0}
.field{display:flex;flex-direction:column;gap:8px;margin:15px 0;color:#dbe8f9}input[type=text],input[type=password],input[type=url]{background:#111c31;color:white;border:1px solid #6f83a2;padding:11px 12px;border-radius:7px;max-width:100%;width:100%}
input:focus-visible,a:focus-visible,button:focus-visible{outline:3px solid #8bdcff;outline-offset:2px}input[type=checkbox]{accent-color:#57c4f6;width:17px;height:17px}
.check{display:flex;gap:10px;align-items:flex-start;margin:20px 0}.check span{line-height:1.5}
.overlay{position:fixed;inset:0;z-index:30;display:grid;place-items:center;padding:18px;background:#050b19c9}
.dialog{width:min(680px,100%);max-height:92vh;overflow:auto;border:1px solid #617391;border-radius:14px;padding:24px;background:#202c44;box-shadow:0 24px 100px #000a}
.dialog-head{display:flex;justify-content:space-between;align-items:center;gap:15px}.dialog-head h2{margin:0}.choice-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:25px 0}
.choice{border:1px solid #617391;background:#2b3a56;border-radius:11px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;padding:25px 10px;min-height:145px;color:white}
.choice:hover{background:#3a5076;text-decoration:none}.dialog-footer{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:25px}
small{color:var(--muted)}.notice{border-left:3px solid var(--accent);background:#23344d;padding:11px 13px;border-radius:6px;line-height:1.5}
@media(max-width:1000px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){.shell{display:block}.side{width:auto;border-right:0;border-bottom:1px solid var(--line);padding:12px}.brand{padding:5px}.side-title{padding:6px}.navitem{display:inline-block}.main{padding:22px 16px 50px}.grid{grid-template-columns:1fr}.choice-grid{grid-template-columns:1fr}.mapping-row{grid-template-columns:1fr}.dialog{padding:16px}}

/* Themearr Connect V2 — clean white/black high-contrast controls */
.connection,.panel,.dialog{color:#fff}
.conn-name,.panel h2,.heading,.dialog h2{color:#fff}
.intro,.muted,.stats,.stats strong,.field,.field span,small,.notice,.crumb,.check span{color:#fff}
.stats{line-height:1.95}
.tag,.tag.on,.tag.off{background:#fff;color:#000;font-weight:800;border:1px solid #fff}
.btn,button,.btn.primary,button.primary,.btn.ghost,.actions .btn.ghost,.dialog-footer .btn.ghost{
  background:#fff!important;color:#000!important;border:2px solid #fff!important;
  font-weight:800;text-decoration:none;box-shadow:none
}
.btn:hover,button:hover,.btn:focus-visible,button:focus-visible{background:#e4e4e4!important;color:#000!important;filter:none}
a:not(.btn):not(.choice):not(.navitem){color:#fff;text-decoration:underline;text-underline-offset:3px}
a:not(.btn):not(.choice):not(.navitem):hover{color:#e1e1e1}
input[type=text],input[type=password],input[type=url]{color:#fff!important;background:#080c16!important;border:2px solid #d9e0eb}
input::placeholder{color:#d7d7d7;opacity:1}
input:focus-visible,button:focus-visible,a:focus-visible{outline:3px solid #fff;outline-offset:3px}
.choice{color:#fff;border:2px solid #aebbd2}.choice:hover{background:#3e4b63}
.brand-mark{width:50px;height:50px;min-width:50px;border-radius:11px;background:#101828;display:inline-flex;align-items:center;justify-content:center;border:1px solid #63718b}
.brand-mark svg{width:39px;height:39px;display:block;fill:currentColor}
.brand-mark-plex{color:#e5a526}.brand-mark-plex svg{width:44px}
.brand-mark-jellyfin{color:#b2a3ff}.brand-mark-emby{color:#64d89c}

/* Connect V4: status is separate from refresh status. Visible in every card. */
.connect-health{margin-top:14px;padding:10px 12px;border-radius:8px;
 border:1px solid #72829d;background:#152139;color:#fff;
 font-size:13px;font-weight:700;line-height:1.45;min-height:41px;}
.connect-health[data-state="checking"]{border-color:#9cabc2;background:#273751;color:#fff;}
.connect-health[data-state="connected"]{border-color:#63dab8;background:#173a38;color:#fff;}
.connect-health[data-state="failed"]{border-color:#ff9696;background:#5b2934;color:#fff;}
.connect-health[data-state="not-configured"]{border-color:#6b7890;background:#26334a;color:#fff;}
.actions button[disabled]{opacity:.75;cursor:wait;}

</style></head><body>
<div class="shell">"""


def settings_shell(title, active):
    """Shared <head>/CSS for Settings pages + the FULL app sidebar (V1.7.3 rule: Settings pages show every item).
    Callers continue with '</aside><main class="main">', so the sidebar's own closing tag is left off."""
    from login import full_sidebar
    side = full_sidebar(active)
    assert side.endswith('</aside>')
    return _SETTINGS_HEAD.replace('{title}', _e(title)) + side[:-len('</aside>')] + '\n'


def settings_page(handler, config, roots, db):
    """Settings > Connect: Sonarr-inspired presentation; existing POST handlers unchanged."""
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    raw_path = params.get('path', [''])[0]
    selected = _valid_anime(raw_path, roots, db) if raw_path else None
    path = raw_path if selected else ''
    servers = _server_map(config)
    configure = params.get('configure', [''])[0]
    if configure not in _TYPES:
        configure = ''
    choosing = params.get('add', [''])[0] == '1' and not configure
    details = {
        'plex': ('Plex', 'P', 'Your Plex Media Server', 'http://192.168.1.10:32400', 'Plex ratingKey'),
        'jellyfin': ('Jellyfin', 'J', 'Your Jellyfin library', 'http://192.168.1.10:8096', 'Jellyfin Item ID'),
        'emby': ('Emby', 'E', 'Your Emby library', 'http://192.168.1.10:8096', 'Emby Item ID'),
    }
    def link(**values):
        if path:
            values = {'path': path, **values}
        return _u('/media-servers', **values)
    parts = [settings_shell('Connect', 'connect') + """</aside><main class="main">
<div class="topline"><div class="crumb">Settings / <strong>Connect</strong></div></div>
<div class="heading-row"><div><h1 class="heading">Connect</h1><p class="muted">Manage connections to your media servers.</p></div>
""", '<a class="btn primary" href="', _e(link(add='1')), '">＋ Add Connection</a></div>',
"""<section class="cx-sec" id="cx-sec-media">""" + (('<p class="notice" role="status">' + _e(params.get('notice', [''])[0][:240]) + '</p>') if params.get('notice', [''])[0] else '') + """<h2 class="cx-h">Media servers</h2><p class="intro muted">Connections can be saved without enabling refresh. A targeted refresh happens only after an approved theme installation, when that server is enabled and the anime has a mapped item ID. No full-library scan is triggered.</p>
<div class="grid">"""]
    for kind in _TYPES:
        label, symbol, desc, example, id_label = details[kind]
        record = servers.get(kind, {})
        configured = bool(record.get('url') and record.get('api_key'))
        enabled = bool(record.get('enabled'))
        mapped = len(record.get('mappings') or {})
        status = ('Refresh enabled' if enabled else 'Refresh disabled') if configured else 'Not configured'
        klass = 'on' if configured and enabled else ('off' if configured else '')
        parts.extend(['<section class="connection"><div class="conn-head">', _logo(kind),
                      '<div><div class="conn-name">', _e(label), '</div><span class="tag ', klass, '">',
                      _e(status), '</span></div></div>',
                      '<div class="stats"><div>', _e(desc), '</div>',
                      '<div>Host: <strong>', _e(record.get('url') or 'Not set'), '</strong></div>',
                      '<div>API token: <strong>', 'Saved' if record.get('api_key') else 'Not saved', '</strong></div>',
                      '<div>Mapped titles: <strong>', str(mapped), '</strong></div></div>',
                      '<div class="actions"><a class="btn ghost" href="', _e(link(configure=kind)), '">',
                      'Edit' if configured else 'Configure', '</a>'])
        if configured:
            parts.extend(['<form method="post" action="/media-server-test">',
                          '<input type="hidden" name="kind" value="', kind, '">',
                          '<button type="submit">Test Connection</button></form>'])
        parts.extend([
            '</div><div class="connect-health" data-state="',
            'not-checked' if configured else 'not-configured',
            '" role="status" aria-live="polite">',
            'Connection: Not checked' if configured else 'Connection: Not configured',
            '</div></section>'])
    parts.append('</div>')
    extras = [(k, r) for k, r in plex_servers(config) if k != 'plex']
    if extras:  # 1.12: more than one Plex server
        parts.append('<section class="cx-sec" id="cx-plex-extra"><h2 class="cx-h">Other Plex servers</h2><p class="intro muted">Added with Sign in with Plex. '
                     'They are used for Collections; per-title links and refresh after installs use the main Plex server above.</p><div class="grid">')
        for k, r in extras:
            parts.extend(['<section class="card"><h3>', _e(r.get('name') or 'Plex'), '</h3><div>Host: <strong>', _e(r.get('url', '')), '</strong></div>',
                          '<div>API token: <strong>Saved</strong></div>',
                          '<form method="post" action="/media-servers/plex-extra/remove"><input type="hidden" name="key" value="', _e(k), '">',
                          '<label class="check"><input type="checkbox" name="confirm" value="1" required><span>Remove this server from Preludarr</span></label> ',
                          '<button type="submit" class="btn ghost">Remove</button></form></section>'])
        parts.append('</div></section>')
    if selected:  # V1.7.3: the per-title mapping form only (opened from the Theme Browser); no hint box on Connect
        parts.append('<section class="panel"><h2>Media item mapping</h2>')
        parts.extend(['<p><strong>', _e(selected['name']), '</strong></p>',
                      '<p class="muted">Connect this title to the correct item ID on each media server. Plex uses ratingKey; Jellyfin and Emby use their Item IDs.</p>'])
        for kind in _TYPES:
            label = details[kind][0]
            mapped_id = (servers.get(kind, {}).get('mappings') or {}).get(path, '')
            parts.extend(['<form class="mapping-row" method="post" action="/media-server-map">',
                          '<strong>', _e(label), '</strong>',
                          '<input type="hidden" name="kind" value="', kind, '">',
                          '<input type="hidden" name="path" value="', _e(path), '">',
                          '<label class="field" style="margin:0"><span>', _e(label), ' item ID</span>',
                          '<input type="text" name="item_id" maxlength="80" placeholder="Blank removes mapping" value="',
                          _e(mapped_id), '"></label>',
                          '<button type="submit">Save mapping</button></form>'])
        parts.extend(['<p><a href="', _e(_u('/themes', path=path)), '">← Return to theme browser</a></p></section>'])
    parts.append('</section></main></div>')  # V1.9.8: closes the Media servers tab section
    if choosing:
        parts.extend(['<div class="overlay" role="presentation"><section class="dialog" role="dialog" aria-modal="true" aria-labelledby="choose-title">',
                      '<div class="dialog-head"><h2 id="choose-title">Add Connection</h2><a class="btn ghost" href="', _e(link()), '">✕ Close</a></div>',
                      '<p class="muted">Choose a media server to configure.</p><div class="choice-grid">'])
        for kind in _TYPES:
            label, symbol, desc, example, id_label = details[kind]
            parts.extend(['<a class="choice" href="', _e(link(configure=kind)), '">', _logo(kind),
                          '<strong>', _e(label), '</strong><small>', _e(desc), '</small></a>'])
        parts.extend(['</div><div class="dialog-footer"><a class="btn ghost" href="', _e(link()), '">Cancel</a></div></section></div>'])
    elif configure:
        label, symbol, desc, example, id_label = details[configure]
        record = servers.get(configure, {})
        parts.extend(['<div class="overlay" role="presentation"><section class="dialog" role="dialog" aria-modal="true" aria-labelledby="configure-title">',
                      '<div class="dialog-head"><h2 id="configure-title">', _e(label), ' Connection</h2><a class="btn ghost" href="', _e(link()), '">✕ Close</a></div>',
                      '<p class="muted">', _e(desc), '</p>',
                      (__import__('plex_signin').BUTTON if configure == 'plex' else ''),
                      (__import__('jellyfin_quickconnect').block(record.get('url')) if configure == 'jellyfin' else ''),
                      (__import__('emby_signin').block(record.get('url')) if configure == 'emby' else ''),
                      '<form method="post" action="/media-server-save">',
                      '<input type="hidden" name="kind" value="', configure, '">',
                      '<label class="field"><span>Server URL (HTTPS or private-LAN HTTP)</span><input type="url" name="url" required maxlength="200" placeholder="',
                      _e(example), '" value="', _e(record.get('url') or ''), '"></label>',
                      '<label class="field"><span>API token</span><input type="password" name="api_key" maxlength="256" autocomplete="new-password" placeholder="Leave blank to keep an existing key"></label>',
                      '<small>', 'API token configured' if record.get('api_key') else 'API token not configured', '</small>',
                      '<label class="check"><input type="checkbox" name="enabled" value="1"',
                      ' checked' if record.get('enabled') else '',
                      '><span>Enable targeted refresh after an approved theme installation (requires a per-title mapping).</span></label>',
                      '<p class="notice">Use the private LAN IPv4 address and port. Nothing is refreshed merely by saving or testing this connection.</p>',
                      '<div class="dialog-footer"><a class="btn ghost" href="', _e(link()), '">Cancel</a>',
                      '<button class="primary" type="submit">Save ', _e(label), '</button></div></form></section></div>'])
        if configure == 'plex':  # 1.12: Sign in with Plex
            ps = __import__('plex_signin')
            parts.extend(['<style>', ps.CSS, '</style><script>', ps.SCRIPT, '</script>'])
        if configure == 'emby':  # 1.12: sign in to Emby
            parts.extend(['<style>', __import__('emby_signin').CSS, '</style>'])
        if configure == 'jellyfin':  # 1.12: Jellyfin Quick Connect
            jq = __import__('jellyfin_quickconnect')
            parts.extend(['<style>', jq.CSS, '</style><script>', jq.SCRIPT, '</script>'])
    parts.append('<script>\n(function () {\n  \'use strict\';\n  const names = {plex: \'Plex\', jellyfin: \'Jellyfin\', emby: \'Emby\'};\n  const forms = document.querySelectorAll(\'form[action="/media-server-test"]\');\n  function show(box, state, message) {\n    box.dataset.state = state;\n    box.textContent = \'Connection: \' + message;\n  }\n  async function check(form) {\n    const kind = form.querySelector(\'input[name="kind"]\')?.value;\n    const card = form.closest(\'.connection\');\n    const box = card?.querySelector(\'.connect-health\');\n    const button = form.querySelector(\'button[type="submit"]\');\n    if (!box || !button || !Object.prototype.hasOwnProperty.call(names, kind)) return;\n    if (form.dataset.busy === \'1\') return;\n    form.dataset.busy = \'1\';\n    button.disabled = true;\n    button.textContent = \'Testing…\';\n    show(box, \'checking\', \'Checking \' + names[kind] + \'…\');\n    const controller = new AbortController();\n    const timeout = setTimeout(() => controller.abort(), 12000);\n    try {\n      const response = await fetch(form.action, {\n        method: \'POST\',\n        body: new URLSearchParams(new FormData(form)),\n        credentials: \'same-origin\',\n        cache: \'no-store\',\n        redirect: \'follow\',\n        signal: controller.signal\n      });\n      const body = await response.text();\n      // The existing endpoint returns "Connection verified for Plex" (or\n      // equivalent for Jellyfin/Emby). A login redirect or an HTML error\n      // cannot accidentally be treated as a successful connection.\n      const expected = \'Connection verified for \' + names[kind];\n      const ok = response.ok && body.toLowerCase().includes(expected.toLowerCase());\n      if (ok) {\n        const when = new Date().toLocaleTimeString([], {hour: \'numeric\', minute: \'2-digit\'});\n        show(box, \'connected\', \'Connected · checked \' + when);\n      } else {\n        show(box, \'failed\', \'Failed · check the URL, token, and server availability\');\n      }\n    } catch (error) {\n      const reason = error?.name === \'AbortError\' ? \'Timed out\' : \'Could not connect\';\n      show(box, \'failed\', reason + \' · check server or network\');\n    } finally {\n      clearTimeout(timeout);\n      button.disabled = false;\n      button.textContent = \'Test Connection\';\n      delete form.dataset.busy;\n    }\n  }\n  forms.forEach(function (form) {\n    form.addEventListener(\'submit\', function (event) {\n      event.preventDefault();\n      check(form);\n    });\n  });\n  // Recheck live status when opening Connect; do not persist potentially stale\n  // "Connected" results. Tests read metadata only and trigger no refresh.\n  forms.forEach(function (form) { check(form); });\n})();\n</script></body></html>')
    return handler.send(200, ''.join(parts), 'text/html')






def server_post(handler, config, roots, db, redirect):
    route = handler.path
    if route == '/media-server-save':
        form = _parse(handler, {'kind', 'url', 'api_key', 'enabled'})
        if not form or form.get('kind') not in _TYPES:
            return handler.send(400, 'Invalid server settings', 'text/plain')
        try:
            url = _base_url(form.get('url', ''))
        except ValueError:
            return handler.send(400, 'Invalid media server URL', 'text/plain')
        key = form.get('api_key', '')
        if len(key) > 256 or ('\n' in key) or ('\r' in key):
            return handler.send(400, 'Invalid token', 'text/plain')
        with _LOCK:
            all_servers = _read(config, 'servers')
            record = dict(all_servers.get(form['kind']) or {})
            record.update({'kind': form['kind'], 'url': url, 'enabled': form.get('enabled') == '1'})
            if key:
                record['api_key'] = key
            all_servers[form['kind']] = record
            _write(config, 'servers', all_servers)
        return redirect(handler, '/media-servers?tab=media')
    if route == '/media-server-test':
        form = _parse(handler, {'kind'})
        if not form or form.get('kind') not in _TYPES:
            return handler.send(400, 'Invalid test request', 'text/plain')
        server = _server_map(config).get(form['kind'])
        if not server:
            return handler.send(404, 'Server not configured', 'text/plain')
        try:
            _test_connection(server)
            msg = 'Connection verified for ' + form['kind'].capitalize()
        except Exception as error:
            # Never echo URLs, tokens, HTTP response bodies, or headers. V1.9.2: plain reason + what to do.
            import neterrors
            msg = 'Connection failed: ' + neterrors.text(error, {'plex': 'Plex', 'jellyfin': 'Jellyfin', 'emby': 'Emby'}[form['kind']])
        return handler.send(200, '<html><body><p>' + _e(msg) + '</p><a href="/media-servers">Back to Media Servers</a></body></html>')
    if route == '/media-server-map':
        form = _parse(handler, {'kind', 'path', 'item_id'})
        if not form or form.get('kind') not in _TYPES:
            return handler.send(400, 'Invalid mapping', 'text/plain')
        path, kind = form.get('path', ''), form['kind']
        try:
            _validate_destination(config, roots, db, path)
        except ValueError:
            return handler.send(400, 'Unknown anime', 'text/plain')
        item_id = form.get('item_id', '').strip()
        if item_id and not _item_ok(kind, item_id):
            return handler.send(400, 'Invalid server item ID', 'text/plain')
        with _LOCK:
            settings = _read(config, 'servers')
            if kind not in settings:
                return handler.send(404, 'Configure this server first', 'text/plain')
            record = settings[kind]
            mappings = record.setdefault('mappings', {})
            if item_id:
                mappings[path] = item_id
            else:
                mappings.pop(path, None)
            _write(config, 'servers', settings)
        return redirect(handler, _u('/media-servers', path=path))
    return handler.send(404, 'Not found', 'text/plain')



def _refresh_one(server, item_id):
    kind = server['kind']
    if not _item_ok(kind, item_id):
        raise ValueError('Invalid mapped item')
    if kind == 'plex':
        return _call(server, 'PUT', '/library/metadata/' + item_id + '/refresh')
    prefix = '/emby' if kind == 'emby' else ''
    return _call(server, 'POST', prefix + '/Items/' + item_id + '/Refresh?' + urllib.parse.urlencode({
        'metadataRefreshMode': 'Default', 'imageRefreshMode': 'None',
        'Recursive': 'false',
        'replaceAllMetadata': 'false', 'replaceAllImages': 'false',
    }))


def after_install(config, anime_path):
    """Non-blocking and best-effort. Called only on successful confirmed install."""
    try:
        servers = _server_map(config)
        targets = [(k, s, (s.get('mappings') or {}).get(anime_path))
                   for k, s in servers.items() if s.get('enabled')]
        targets = [(k, s, item) for k, s, item in targets if item and _item_ok(k, item)]
        if not targets:
            return
        now = time.monotonic()
        with _REFRESH_LOCK:
            active = []
            for kind, server, item_id in targets:
                token = (kind, item_id)
                if now - _RECENT.get(token, -100000) >= 120:
                    _RECENT[token] = now
                    active.append((kind, server, item_id))
        if not active:
            return

        def worker():
            for kind, server, item_id in active:
                try:
                    _refresh_one(server, item_id)
                    print('Preludarr target item refresh queued: ' + kind, flush=True)
                except Exception as error:
                    print('Preludarr target refresh failed: ' + kind + ' ' + type(error).__name__, flush=True)
        threading.Thread(target=worker, name='themearr-media-refresh', daemon=True).start()
    except Exception as error:
        print('Preludarr post-install refresh skipped: ' + type(error).__name__, flush=True)


def connect_overview(config):
    """Compact Connect status summary. Reads saved settings only — opening it triggers no checks.
    V1.9.14 honest labels: "Last check" = result + time of the most recent Test / Sync of THAT connection; "Status" = Configured /
    Needs attention / Not configured. Saved keys or a saved path mapping are "Configured", never "verified" or "ready"."""
    ui = __import__('ui')
    rows = []

    def badge(text, tone):
        return '<span class="tag ' + tone + '">' + _e(text) + '</span>'

    def checked(ok, at, what):
        if ok is None:
            return badge('Not checked', 'off') + (' <small class="muted">' + _e(what) + '</small>' if what else '')
        return badge('Succeeded' if ok else 'Failed', 'on' if ok else 'bad') + ' <small class="muted">' + _e(ui.when(at)) + '</small>'

    def row(name, tab, last, status, detail):
        rows.append('<tr><th scope="row"><a href="/media-servers?tab=' + tab + '">' + _e(name) + '</a></th><td>' + last + '</td><td>' + status +
                    '</td><td>' + detail + '</td></tr>')
    servers = _server_map(config)
    any_server = False
    for kind, label in (('plex', 'Plex'), ('jellyfin', 'Jellyfin'), ('emby', 'Emby')):
        rec = servers.get(kind) or {}
        if not (rec.get('url') and rec.get('api_key')):
            continue
        any_server = True
        mapped = len(rec.get('mappings') or {})
        on = bool(rec.get('enabled'))
        status = (badge('Configured', 'on') if on and mapped else badge('Needs attention: no titles mapped', 'warn') if on else badge('Configured · refresh off', 'off'))
        row(label, 'media', badge('Checked on the Media servers tab', 'off'), status, f'Refresh {"on" if on else "off"} · {mapped:,} titles mapped')
    if not any_server:
        row('Media servers', 'media', '—', badge('Not configured', 'off'), '<span class="muted">Optional — only for refreshing a title after an install</span>')
    try:
        import poster_connect as pc
        tmdb, tvdb, _, pref = pc.effective_values(config)
        keys = ', '.join(n for n, k in (('TMDB', tmdb), ('TVDB', tvdb)) if k)
        row('Poster providers', 'posters', checked(None, 0, 'keys are only used during poster searches'),
            badge('Configured', 'on') if keys else badge('Needs attention: no provider key', 'warn'),
            ('Keys saved: ' + _e(keys) + ' · preferred ' + _e(str(pref or 'tmdb').upper())) if keys else 'Add a TMDB or TVDB key to search posters')
    except Exception:
        row('Poster providers', 'posters', '—', badge('Unavailable', 'off'), '—')
    try:
        import arr as arr
        insts = arr.load(config)['instances']
        for inst in insts:
            st = inst.get('status') or {}
            name = arr.KINDS[inst['kind']][0] + ' · ' + inst.get('name', '')
            maps = len(inst.get('mappings') or [])
            last = checked(st.get('ok') if st else None, st.get('at', 0), 'click Test')
            if not inst.get('enabled', True):
                status = badge('Turned off', 'off')
            elif st and not st.get('ok'):
                status = badge('Needs attention: last check failed', 'bad')
            elif not maps:
                status = badge('Needs attention: path mapping required', 'warn')
            else:
                status = badge('Configured', 'on')
            sync = (arr._STATE.get(inst.get('id')) or {})
            detail = f'{maps} path mapping' + ('' if maps == 1 else 's')
            if sync and not sync.get('error'):
                detail += f" · last sync {sync.get('mapped', 0)} of {sync.get('items', 0)} matched (" + _e(ui.when(sync.get('at', 0), with_date=False)) + ')'
            row(name, 'arr', last, status, detail)
        if not insts:
            row('Sonarr / Radarr', 'arr', '—', badge('Not configured', 'off'), '<span class="muted">Optional</span>')
    except Exception:
        pass
    try:
        import anidb as ad
        st = ad.status(config)
        row('AniDB title list', 'anidb', badge('Downloaded', 'on') if st['available'] else badge('Not downloaded', 'off'),
            badge('Configured', 'on') if st['available'] else badge('Not configured', 'off'),
            (f"{st['anime']:,} anime · updated " + _e(st['updated'])) if st['available'] and st['updated'] else '—')
    except Exception:
        pass
    return ('<section class="cx-sec" id="cx-sec-status"><div class="panel" id="connect-status"><h2>Status ' + ui.label('cached') + '</h2>'
            '<p class="muted">From saved settings and each connection’s most recent <strong>Test</strong> or <strong>Sync</strong> — opening this page checks nothing. '
            '<strong>Last check</strong> is that result and its time. <strong>Configured</strong> means the settings are saved; it is not proof that the service works right now.</p>'
            '<div class="scroll"><table class="cx-table"><thead><tr><th>Service</th><th>Last check</th><th>Status</th><th>Details</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div>'
            '<style>.cx-table th,.cx-table td{text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid #2b3a55;vertical-align:top}.cx-table th[scope=row]{white-space:nowrap}</style></div></section>')


CONNECT_TABS = (('status', 'Status'), ('media', 'Media servers'), ('posters', 'Poster providers'), ('arr', 'Sonarr & Radarr'), ('anidb', 'AniDB'))


def connect_tab(query):
    """Which Connect tab to show: ?tab=…, else the area that just saved something, else Status."""
    tab = (query.get('tab') or [''])[0]
    if tab in dict(CONNECT_TABS):
        return tab
    for key, t in (('arr_preview', 'arr'), ('arr_notice', 'arr'), ('poster_notice', 'posters'), ('anidb_notice', 'anidb'), ('configure', 'media'), ('add', 'media'), ('path', 'media')):
        if query.get(key):
            return t
    return 'status'


def connect_tabs(current):
    return ('<nav class="tm-subnav tm-tabs" aria-label="Connect">' + ''.join(
        '<a href="/media-servers?tab=' + k + '"' + (' class="on" aria-current="page"' if k == current else '') + '>' + _e(label) + '</a>' for k, label in CONNECT_TABS)
        + '</nav><style>main .cx-sec{display:none}main #cx-sec-' + current + '{display:block}.cx-h{margin:4px 0 10px}</style>')
