# SPDX-License-Identifier: GPL-3.0-or-later
"""Polite, resilient access to the AnimeThemes API (Themearr V1.6.4).

1.16.0: answers come from AnimeThemes' GraphQL API (animethemes_graphql) whenever the request has a
GraphQL equivalent; the deprecated JSON:API is only a fallback.

Used by app.request_json for GET requests to api.animethemes.moe only. It never changes what the API
returns, so matching results are identical; it only changes how often and how patiently we ask:

  * remembers answers by exact URL (fresh for 6 h; an older copy is used only when AnimeThemes fails);
  * at most 2 requests at a time, spaced at least 0.75 s apart (AnimeThemes allows ~90/minute);
  * 30 s per attempt; a timeout, connection error or 5xx is retried once after a short pause;
  * HTTP 429 honours Retry-After (capped) and pauses all further AnimeThemes requests until then;
  * fresh=True (install verification) always asks AnimeThemes and never uses a remembered answer.
Callers receive a private copy of the data, so they cannot change the remembered answer.
"""
import copy
import json
import animethemes_graphql
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict

API_HOST = 'api.animethemes.moe'
FRESH_SECONDS = 6 * 3600
STALE_SECONDS = 7 * 86400
MAX_ENTRIES = 600
TIMEOUT = 30
MIN_SPACING = 0.75
RETRY_PAUSE = 3
MAX_RETRY_AFTER = 60
SLOW_SECONDS = 10

_CACHE = OrderedDict()  # url -> (stored_at monotonic, data)
_CACHE_LOCK = threading.Lock()
_SLOTS = threading.BoundedSemaphore(2)
_PACE_LOCK = threading.Lock()
_STATE = {'next_at': 0.0, 'paused_until': 0.0, 'last_problem': '', 'last_problem_at': 0.0, 'requests': 0, 'hits': 0,
          # 1.15.1: for System → Overview — wall-clock times of the last good answer and of the
          # first failure in the current outage (0 = none), from real requests only
          'last_ok_wall': 0.0, 'down_since_wall': 0.0, 'down_detail': ''}


class AnimeThemesBusy(OSError):
    """AnimeThemes asked us to slow down (HTTP 429) and no remembered answer exists."""


def _remember(url, data):
    with _CACHE_LOCK:
        _CACHE[url] = (time.monotonic(), data)
        _CACHE.move_to_end(url)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)


def _cached(url, max_age):
    with _CACHE_LOCK:
        item = _CACHE.get(url)
        if item and time.monotonic() - item[0] <= max_age:
            _CACHE.move_to_end(url)
            return item[1]
    return None


def _problem(text, down=False):
    _STATE['last_problem'] = text
    _STATE['last_problem_at'] = time.monotonic()
    if down:  # a server error or no answer: AnimeThemes itself is not serving requests
        if not _STATE['down_since_wall']:
            _STATE['down_since_wall'] = time.time()
        _STATE['down_detail'] = text


def _answered():
    _STATE['last_ok_wall'] = time.time()
    _STATE['down_since_wall'] = 0.0
    _STATE['down_detail'] = ''


def _rate_spacing():
    try:
        return __import__('ratelimit').spacing('animethemes')
    except Exception:
        return MIN_SPACING


def _pace():
    with _PACE_LOCK:
        now = time.monotonic()
        wait = max(_STATE['next_at'], _STATE['paused_until']) - now
        _STATE['next_at'] = max(now, _STATE['next_at']) + max(MIN_SPACING, _rate_spacing())  # V1.9.14: user limit (never faster than 0.75 s)
    if wait > 0:
        time.sleep(min(wait, MAX_RETRY_AFTER))


def _retry_after(error):
    value = (error.headers or {}).get('Retry-After', '') if hasattr(error, 'headers') else ''
    try:
        return max(1, min(MAX_RETRY_AFTER, int(str(value).strip())))
    except ValueError:
        return 30


def get_json(url, headers, fresh=False):
    """GET url (an api.animethemes.moe URL) and return parsed JSON, politely."""
    if not fresh:
        hit = _cached(url, FRESH_SECONDS)
        if hit is not None:
            _STATE['hits'] += 1
            return copy.deepcopy(hit)
    if _STATE['paused_until'] - time.monotonic() > 15:
        # A long pause was requested: don't make pages wait; reuse an older answer or say so.
        stale = None if fresh else _cached(url, STALE_SECONDS)
        if stale is not None:
            _STATE['hits'] += 1
            return copy.deepcopy(stale)
        raise AnimeThemesBusy('AnimeThemes asked Preludarr to slow down; try again shortly.')
    # 1.16.0: AnimeThemes deprecated this JSON:API and it answers 503; ask its GraphQL API first
    # (same pacing, caching and retries) and fall back to the JSON:API only if GraphQL fails.
    planned = animethemes_graphql.plan(url)
    if planned:
        try:
            data = _graphql(planned, headers)
            _remember(url, data)
            return copy.deepcopy(data)
        except AnimeThemesBusy:
            raise
        except (ValueError, TimeoutError, urllib.error.URLError, ConnectionError, OSError):
            pass  # try the JSON:API below
    last_error = None
    for attempt in range(2):
        with _SLOTS:
            _pace()
            started = time.monotonic()
            try:
                _STATE['requests'] += 1
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                    data = json.load(response)
                took = time.monotonic() - started
                if took >= SLOW_SECONDS:
                    _problem(f'AnimeThemes is responding slowly (last answer took {took:.0f} s).')
                _remember(url, data)
                _answered()
                return copy.deepcopy(data)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code < 500:
                    _answered()  # 404, 429 …: AnimeThemes answered, so it is up
                if error.code == 429:
                    pause = _retry_after(error)
                    with _PACE_LOCK:
                        _STATE['paused_until'] = time.monotonic() + pause
                    _problem(f'AnimeThemes asked Preludarr to slow down (HTTP 429); pausing {pause} s.')
                    if attempt == 0 and pause <= 15:
                        continue
                    break
                if error.code < 500:
                    raise  # 404 etc.: a real answer, not a hiccup
                _problem(f'AnimeThemes server error (HTTP {error.code}).', down=True)
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as error:
                last_error = error
                _problem('AnimeThemes did not answer in time.' if 'timed out' in str(error) or isinstance(error, TimeoutError)
                         else 'AnimeThemes could not be reached.', down=True)
        if attempt == 0:
            time.sleep(RETRY_PAUSE)
    if not fresh:
        stale = _cached(url, STALE_SECONDS)
        if stale is not None:
            _STATE['hits'] += 1
            return copy.deepcopy(stale)
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
        raise AnimeThemesBusy('AnimeThemes asked Preludarr to slow down; try again shortly.') from last_error
    raise last_error


def _graphql(planned, headers):
    """Run one planned GraphQL query politely and return the converted JSON:API-shaped answer."""
    gql, variables, convert = planned
    body = json.dumps({'query': gql, 'variables': variables}).encode()
    gql_headers = {'User-Agent': headers.get('User-Agent', 'Preludarr'), 'Accept': 'application/json',
                   'Content-Type': 'application/json'}
    last_error = None
    for attempt in range(2):
        with _SLOTS:
            _pace()
            started = time.monotonic()
            try:
                _STATE['requests'] += 1
                req = urllib.request.Request(animethemes_graphql.ENDPOINT, data=body, headers=gql_headers, method='POST')
                with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                    reply = json.load(response)
                took = time.monotonic() - started
                if took >= SLOW_SECONDS:
                    _problem(f'AnimeThemes is responding slowly (last answer took {took:.0f} s).')
                if not isinstance(reply, dict) or reply.get('errors') and not reply.get('data'):
                    _answered()  # it answered; the query itself was refused
                    raise ValueError('AnimeThemes GraphQL refused the query: '
                                     + str((reply.get('errors') or [{}])[0].get('message', ''))[:120])
                _answered()
                return convert(reply.get('data'))
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code < 500:
                    _answered()
                if error.code == 429:
                    pause = _retry_after(error)
                    with _PACE_LOCK:
                        _STATE['paused_until'] = time.monotonic() + pause
                    _problem(f'AnimeThemes asked Preludarr to slow down (HTTP 429); pausing {pause} s.')
                    raise AnimeThemesBusy('AnimeThemes asked Preludarr to slow down; try again shortly.') from error
                if error.code < 500:
                    raise ValueError(f'AnimeThemes GraphQL answered HTTP {error.code}') from error
                _problem(f'AnimeThemes server error (HTTP {error.code}).', down=True)
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as error:
                last_error = error
                _problem('AnimeThemes did not answer in time.' if 'timed out' in str(error) or isinstance(error, TimeoutError)
                         else 'AnimeThemes could not be reached.', down=True)
        if attempt == 0:
            time.sleep(RETRY_PAUSE)
    raise last_error


def status_note(window=900):
    """A short user-facing note when AnimeThemes had trouble in the last `window` seconds, else ''."""
    if _STATE['last_problem'] and time.monotonic() - _STATE['last_problem_at'] < window:
        return _STATE['last_problem'] + ' Preludarr retries automatically and reuses recent answers.'
    return ''


def health():
    """1.15.1: AnimeThemes' state as Preludarr last saw it (from real requests; never checks by itself).
    state: 'down' (server errors or no answer since `since`), 'busy' (asked us to slow down), 'ok', or
    'unknown' (not contacted since Preludarr started)."""
    if _STATE['down_since_wall']:
        return {'state': 'down', 'since': _STATE['down_since_wall'], 'detail': _STATE['down_detail'],
                'last_ok': _STATE['last_ok_wall']}
    if _STATE['paused_until'] > time.monotonic():
        return {'state': 'busy', 'since': 0.0, 'detail': _STATE['last_problem'], 'last_ok': _STATE['last_ok_wall']}
    if _STATE['last_ok_wall']:
        return {'state': 'ok', 'since': 0.0, 'detail': '', 'last_ok': _STATE['last_ok_wall']}
    return {'state': 'unknown', 'since': 0.0, 'detail': '', 'last_ok': 0.0}


def stats():
    with _CACHE_LOCK:
        size = len(_CACHE)
    return {'remembered': size, 'requests': _STATE['requests'], 'reused': _STATE['hits']}


def anime_page_link(slug):
    """Render a read-only website link from an AnimeThemes API anime slug.

    Callers must pass the provider's slug, never a folder or search title.
    Missing or malformed provider identifiers have no website link.
    """
    import html
    import re
    if not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', slug):
        return ''
    url = 'https://animethemes.moe/anime/' + slug
    return ('<a class="animethemes-page-link" style="display:inline-block;max-width:100%;'
            'overflow-wrap:anywhere;text-decoration:underline" href="' + html.escape(url, quote=True) +
            '" target="_blank" rel="noopener noreferrer">View on AnimeThemes</a>')


def search_result(candidate, selected):
    """One manual result; the preview route is distinct from the external link."""
    import html
    import urllib.parse
    slug = str(candidate.get('slug') or '')
    if not slug:
        return ''
    preview = '/themes?' + urllib.parse.urlencode({'path': selected, 'theme_slug': slug})
    year = ' (' + html.escape(str(candidate['year'])) + ')' if candidate.get('year') else ''
    external = anime_page_link(candidate.get('slug'))
    return ('<p style="overflow-wrap:anywhere"><a href="' + html.escape(preview, quote=True) + '">' +
            html.escape(str(candidate.get('name') or slug)) + '</a>' + year +
            ' — Preview OP/ED tracks' + (' · ' + external if external else '') + '</p>')
