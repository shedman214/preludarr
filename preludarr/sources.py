# SPDX-License-Identifier: GPL-3.0-or-later
"""Secondary theme sources (Themearr V1.9.0). AnimeThemes always stays the primary source.

Fallbacks, used only when AnimeThemes has nothing for a title (and only if the user switched them on):
  * Plex TV themes  — https://tvthemes.plexapp.com/<TVDB id>.mp3 (TV series only)
  * ThemerrDB       — community list of YouTube theme links by TMDB id, downloaded with the YouTube download client

The right TVDB/TMDB id is essential (a wrong id = another show's music), so ids come only from Sonarr/Radarr (exact)
or from a TMDB search that matches the folder's exact title AND year with a single result. Fallback results are
always proposals for the user to review (Bulk Theme Queue / Theme Browser); they are never installed silently.
Installs use the existing safe installer with replace=False (an existing theme.mp3 is never replaced).
"""
import html
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

FILE = 'themearr_sources.json'
SOURCES = (
    ('plex', 'Plex TV Themes', 'TV series only · by TVDB id · direct MP3'),
    ('themerrdb', 'ThemerrDB', 'TV + movies · by TMDB id · YouTube link via the YouTube download client'),
)
PLEX_HOST = 'tvthemes.plexapp.com'
THEMERR = 'https://app.lizardbyte.dev/ThemerrDB/{kind}/themoviedb/{id}.json'
THEMERR_IMDB = 'https://app.lizardbyte.dev/ThemerrDB/movies/imdb/{id}.json'  # Preludarr 1.11: movies without a TMDB id
MAX_AUDIO = 40 * 1024 * 1024
UA = {'User-Agent': 'Preludarr/1.12 (self-hosted theme manager)'}


def _e(value):
    return html.escape(str(value), quote=True)


def settings(config):
    data = {'plex': False, 'themerrdb': False, 'tmdb_search': True}
    try:
        saved = json.loads((Path(config) / FILE).read_text())
        for k in data:
            if isinstance(saved.get(k), bool):
                data[k] = saved[k]
    except (OSError, ValueError):
        pass
    return data


def save_settings(config, data):
    path = Path(config) / FILE
    tmp = path.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({k: bool(data.get(k)) for k in ('plex', 'themerrdb', 'tmdb_search')}, f)
    os.replace(tmp, path)


def any_enabled(config):
    s = settings(config)
    return s['plex'] or s['themerrdb']


# ------------------------------------------------------------------------------------ ids ----
def _norm(text):
    return ''.join(ch for ch in unicodedata.normalize('NFKC', str(text)).casefold() if ch.isalnum())


def ids(app, path, name, library):
    """{'tvdb','tmdb','imdb','source'} for a folder: Sonarr/Radarr ids, else an exact TMDB title+year match, else {}."""
    with app.db() as c:
        row = c.execute('SELECT tvdb,tmdb,imdb,source FROM media_ids WHERE path=?', (path,)).fetchone()
    if row and (row['tvdb'] or row['tmdb'] or (row['imdb'] and __import__('kinds').is_regular(library))):  # IMDb-only: Shows/Movies (anime unchanged)
        return {'tvdb': row['tvdb'], 'tmdb': row['tmdb'], 'imdb': row['imdb'], 'source': row['source']}
    if not settings(app.CONFIG)['tmdb_search']:
        return {}
    key = app.poster_connect.effective_values(app.CONFIG)[0]
    yr = app.year(name)
    if not key or not yr:
        return {}  # without a year the match would not be certain enough
    movie = __import__('kinds').is_movie(library)
    title = app.clean(name)
    qs = urllib.parse.urlencode({'api_key': key, 'query': title, ('year' if movie else 'first_air_date_year'): yr})
    try:
        res = app.request_json('https://api.themoviedb.org/3/search/' + ('movie' if movie else 'tv') + '?' + qs).get('results') or []
    except Exception:
        return {}
    exact = [r for r in res if (str(r.get('release_date' if movie else 'first_air_date') or '')[:4] == str(yr))
             and _norm(title) in (_norm(r.get('title') or r.get('name') or ''), _norm(r.get('original_title') or r.get('original_name') or ''))]
    if len(exact) != 1:
        return {}
    tmdb = int(exact[0]['id'])
    tvdb = None
    if not movie:
        try:
            ext = app.request_json(f'https://api.themoviedb.org/3/tv/{tmdb}/external_ids?' + urllib.parse.urlencode({'api_key': key}))
            tvdb = int(ext.get('tvdb_id') or 0) or None
        except Exception:
            tvdb = None
    with app.db() as c:
        c.execute('INSERT INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES(?,?,?,NULL,?) ON CONFLICT(path) DO UPDATE SET '
                  'tvdb=excluded.tvdb,tmdb=excluded.tmdb,source=excluded.source', (path, tvdb, tmdb, 'tmdb-search'))
    return {'tvdb': tvdb, 'tmdb': tmdb, 'imdb': None, 'source': 'tmdb-search'}


# ---------------------------------------------------------------------------------- lookup ----
def _get(url, limit, timeout=25):
    __import__('ratelimit').wait(__import__('ratelimit').for_host(urllib.parse.urlsplit(url).hostname))  # V1.9.14
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        final = urllib.parse.urlsplit(r.geturl())
        return r.status, r.headers.get('Content-Type', ''), final, r.read(limit + 1)


def find(app, path, name, library, interactive=True, everywhere=False):
    """Proposals (never installed here). Each: source, label, url, note. Anime: fallbacks after AnimeThemes.
    Preludarr 1.11, Shows / Movies: these are the main sources, then (optional, preview only) YouTube search."""
    kinds = __import__('kinds')
    s = settings(app.CONFIG)
    found, notes = [], []
    if not (s['plex'] or s['themerrdb']):
        notes.append('Plex TV Themes and ThemerrDB are off (Settings → Download Clients).')
        got = {}
    else:
        got = ids(app, path, name, library)
        if not got:
            notes.append('No exact TVDB/TMDB/IMDb id for this title (connect Sonarr/Radarr, or the folder needs a year that TMDB matches exactly).')
    if got:
        found, more = _lookup(app, s, got, library)
        notes += more
    if not found and kinds.is_regular(library):
        yt = __import__('youtube_search')
        results, note = yt.suggest(app, path, name, library, interactive=interactive, everywhere=everywhere)
        found += results
        if note:
            notes.append(note)
    return found, notes


def _lookup(app, s, got, library):
    kinds = __import__('kinds')
    found, notes = [], []
    via = {'sonarr': 'Sonarr', 'radarr': 'Radarr', 'tmdb-search': 'exact TMDB title + year match'}.get(got.get('source'), got.get('source'))
    if s['plex'] and not kinds.is_movie(library) and got.get('tvdb'):
        url = f"https://{PLEX_HOST}/{int(got['tvdb'])}.mp3"
        try:
            status, ctype, final, body = _get(url, 4096, timeout=20)
            if status == 200 and ctype.startswith('audio/') and final.hostname == PLEX_HOST and body:
                found.append({'source': 'plex', 'label': 'Plex TV Themes', 'url': url, 'track': 'TV theme (Plex)',
                              'note': f"TVDB {got['tvdb']} (id from {via})"})
            else:
                notes.append('Plex TV Themes has no theme for this series.')
        except Exception as error:
            notes.append('Plex TV Themes: ' + ('no theme for this series' if getattr(error, 'code', 0) == 404 else 'unavailable right now') + '.')
    imdb = str(got.get('imdb') or '')
    by_imdb = library == 'Movies' and not got.get('tmdb') and re.fullmatch(r'tt\d{5,10}', imdb)  # anime lookups unchanged
    if s['themerrdb'] and (got.get('tmdb') or by_imdb):
        kind = 'movies' if kinds.is_movie(library) else 'tv_shows'
        try:
            url = THEMERR_IMDB.format(id=imdb) if by_imdb else THEMERR.format(kind=kind, id=int(got['tmdb']))
            status, ctype, final, body = _get(url, 2 * 1024 * 1024)
            yt = str(json.loads(body).get('youtube_theme_url') or '')
            if re.fullmatch(r'https://(www\.)?youtube\.com/watch\?v=[A-Za-z0-9_-]{11}', yt):
                found.append({'source': 'themerrdb', 'label': 'ThemerrDB (YouTube)', 'url': yt, 'track': 'Theme from YouTube (ThemerrDB)',
                              'note': (f"IMDb {imdb}" if by_imdb else f"TMDB {got['tmdb']}") + f" (id from {via})"})
            else:
                notes.append('ThemerrDB has no YouTube theme for this title.')
        except Exception as error:
            notes.append('ThemerrDB: ' + ('no entry for this title' if getattr(error, 'code', 0) == 404 else 'unavailable right now') + '.')
    return found, notes


SUGGEST_DAYS = 7  # Plex/ThemerrDB answers for Shows/Movies are reused for a week (YouTube results keep their own 30-day limit)


def cached_find(app, path, name, library, refresh=False, interactive=True, everywhere=False):
    """find() for Shows / Movies, remembered in theme_suggestions so title pages, the queue and installs agree. Returns (found, notes)."""
    if not refresh:
        with app.db() as c:
            row = c.execute('SELECT found,notes,fetched FROM theme_suggestions WHERE path=?', (path,)).fetchone()
        if row and row['fetched'] >= time.time() - SUGGEST_DAYS * 86400:
            try:
                return json.loads(row['found']), json.loads(row['notes'])
            except ValueError:
                pass
    else:
        __import__('youtube_search').forget(app, path)
    found, notes = find(app, path, name, library, interactive=interactive, everywhere=everywhere)
    waiting = any(('spacing its requests' in n or 'budget is used up' in n or 'paused until' in n or 'cookies.txt' in n) for n in notes)
    if not waiting:  # a skipped YouTube search is not an answer — don't remember it
        with app.db() as c:
            c.execute('INSERT INTO theme_suggestions(path,found,notes,fetched) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET '
                      'found=excluded.found,notes=excluded.notes,fetched=excluded.fetched', (path, json.dumps(found), json.dumps(notes), int(time.time())))
        summary = ('Suggestions: ' + str(len(found)) + ' (' + ', '.join(dict.fromkeys(f['label'] for f in found)) + ')') if found else 'No theme found yet: ' + ' '.join(notes)[:200]
        with app.db() as c:
            c.execute("UPDATE shows SET detail=? WHERE path=? AND state IN ('missing','review')", (summary[:300], path))
    return found, notes


# --------------------------------------------------------------------------------- install ----
def install(app, path, proposal, progress=None):
    """Download the approved fallback and install it with the existing safe installer (never replaces).
    Preludarr 1.11.8: optional progress(phase, message) callback; the folder is checked BEFORE anything is downloaded."""
    folder = Path(path)
    say = progress or (lambda phase, message: None)
    say('checking', 'Checking that the folder can take a theme.mp3')
    __import__('library_health').preflight(folder)  # read-only / missing folder: stop before downloading
    if proposal['source'] == 'plex':
        parts = urllib.parse.urlsplit(proposal['url'])
        if parts.scheme != 'https' or parts.hostname != PLEX_HOST or not re.fullmatch(r'/\d{1,9}\.mp3', parts.path):
            raise ValueError('Unexpected Plex theme address')
        stage = Path(tempfile.mkdtemp(prefix='themearr-source-', dir=app.CONFIG))
        try:
            say('downloading', 'Downloading the theme from Plex TV Themes')
            status, ctype, final, body = _get(proposal['url'], MAX_AUDIO, timeout=60)
            if status != 200 or final.hostname != PLEX_HOST or not ctype.startswith('audio/') or not 1024 <= len(body) <= MAX_AUDIO:
                raise ValueError('Plex did not return usable theme audio')
            audio = stage / 'source.mp3'
            audio.write_bytes(body)
            say('converting', 'Converting and checking the audio')
            return app.manual_install_mp3(folder, audio, replace=False, anime_slug='source_plex', video_id=None, progress=progress,
                                          activity_label='Plex TV Themes · ' + proposal.get('note', ''))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    if proposal['source'] == 'themerrdb':
        import youtube_download as yt
        say('downloading', 'Downloading the audio from YouTube (usually 10–60 seconds)')
        details = yt.download_to_stage(proposal['url'], app.CONFIG)
        say('converting', 'Converting and checking the audio')
        try:
            return app.manual_install_mp3(folder, Path(details['audio']), replace=False, anime_slug='source_themerrdb', video_id=None, progress=progress,
                                          activity_label='ThemerrDB · YouTube · ' + str(details.get('title') or '')[:80])
        finally:
            yt.cleanup_stage(details['stage'], app.CONFIG)
    raise ValueError('Unknown source')


# --------------------------------------------------------------------------------------- UI ----
def _logo(key):
    """Official logos, downloaded 2026-10-26 (sources in branding/source-logos/SOURCES.txt)."""
    from login import static_url
    return ('<span class="brand-mark" aria-hidden="true"><img src="' + static_url(key + '-logo.png') + '" alt="" width="40" height="40" '
            'style="width:40px;height:40px;object-fit:contain;display:block;border-radius:8px"></span>')


# Plain-language explanation shown on each card (user asked 2026-10-26 what each source does).
EXPLAIN = {
    'plex': ('Plex’s own library of TV theme songs. Works for TV series only and looks the show up by its TVDB id, '
             'then downloads the MP3 directly.'),
    'themerrdb': ('A community list of theme-song YouTube links for TV series and movies, looked up by TMDB id. '
                  'The song is downloaded through your YouTube download client.'),
}


def cards_html(config):
    s = settings(config)
    out = ['<section class="connection" id="dc-animethemes"><div class="conn-head">', _logo('animethemes'),
           '<div><div class="conn-name">AnimeThemes</div><span class="tag on">Primary · always on</span></div></div>',
           '<div class="stats"><div>The main library of anime openings and endings — Preludarr always searches here first for Anime TV and Anime Movies. '
           'It is not used for Shows and Movies.</div></div></section>']
    for key, name, desc in SOURCES:
        on = s[key]
        out += ['<section class="connection" id="dc-', key, '"><div class="conn-head">', _logo(key), '<div><div class="conn-name">', _e(name),
                '</div><span class="tag ', 'on' if on else '', '">', 'On' if on else 'Off', '</span></div></div>',
                '<div class="stats"><div>', _e(EXPLAIN[key]), '</div><div>For anime it is only asked when AnimeThemes has nothing; for '
                + ('Shows it is asked first.' if key == 'plex' else 'Shows (after Plex) and Movies it is a main source.') +
                ' It suggests a theme for you to review — it never installs anything on its own.</div></div>',
                '<form method="post" action="/download-clients/sources"><input type="hidden" name="source" value="', key, '">',
                '<button type="submit" name="turn" value="', 'off' if on else 'on', '">', 'Turn off' if on else 'Turn on', '</button></form></section>']
    out.append(__import__('youtube_search').card_html())  # Preludarr 1.11
    return ''.join(out)


def post(config, handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 512:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), max_num_fields=3)
        src, turn = form.get('source', [''])[0], form.get('turn', [''])[0]
        if src not in ('plex', 'themerrdb', 'tmdb_search') or turn not in ('on', 'off'):
            raise ValueError('Unknown source.')
        data = settings(config)
        data[src] = turn == 'on'
        save_settings(config, data)
        msg = dict((k, n) for k, n, _ in SOURCES).get(src, src) + (' turned on (fallback only).' if turn == 'on' else ' turned off.')
    except (ValueError, OSError, UnicodeError) as error:
        msg = 'Not saved: ' + (str(error) if isinstance(error, ValueError) else 'settings could not be written.')
    return redirect(handler, '/download-clients?' + urllib.parse.urlencode({'tab': 'sources', 'notice': msg}))
