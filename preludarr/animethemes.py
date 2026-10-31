# SPDX-License-Identifier: GPL-3.0-or-later
"""Polite, resilient access to the AnimeThemes API (Themearr V1.6.4).

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
_STATE = {'next_at': 0.0, 'paused_until': 0.0, 'last_problem': '', 'last_problem_at': 0.0, 'requests': 0, 'hits': 0}


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


def _problem(text):
    _STATE['last_problem'] = text
    _STATE['last_problem_at'] = time.monotonic()


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
                return copy.deepcopy(data)
            except urllib.error.HTTPError as error:
                last_error = error
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
                _problem(f'AnimeThemes server error (HTTP {error.code}).')
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as error:
                last_error = error
                _problem('AnimeThemes did not answer in time.' if 'timed out' in str(error) or isinstance(error, TimeoutError)
                         else 'AnimeThemes could not be reached.')
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


def status_note(window=900):
    """A short user-facing note when AnimeThemes had trouble in the last `window` seconds, else ''."""
    if _STATE['last_problem'] and time.monotonic() - _STATE['last_problem_at'] < window:
        return _STATE['last_problem'] + ' Preludarr retries automatically and reuses recent answers.'
    return ''


def stats():
    with _CACHE_LOCK:
        size = len(_CACHE)
    return {'remembered': size, 'requests': _STATE['requests'], 'reused': _STATE['hits']}
