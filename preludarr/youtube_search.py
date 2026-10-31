# SPDX-License-Identifier: GPL-3.0-or-later
"""YouTube search for Shows and Movies (Preludarr 1.11, user 2026-10-29: "for shows and movies i want it to search youtube for theme
songs without hurting api or being banned").

Uses the YouTube Data API v3 with the user's OWN key, only for titles where Plex TV Themes and ThemerrDB found nothing.
  * 1 search.list call per title (YouTube's separate search allowance: 100 a day) + 1 videos.list call (1 unit) for lengths.
  * Daily budget: default 80 searches, clamped to 1..95 so Preludarr can never use YouTube's whole allowance.
  * Pacing: interactive searches at least MIN_GAP seconds apart; the optional background search spreads the budget over the day.
  * quotaExceeded / dailyLimitExceeded / rateLimitExceeded: no retries — YouTube search pauses until the next daily reset
    (midnight Pacific time, when YouTube resets quotas).
  * Results are cached for at most 30 days (YouTube API Developer Policies III.E.4), then searched again or dropped.
  * Preview only. YouTube's API policies forbid an API client from downloading or separating the audio of what it finds
    (III.E.1, III.I.7-8), so results are never downloaded or installed by Preludarr. Installing one is the user's own separate
    step: pasting the link into the existing YouTube tool on the title page.
The key travels in the X-goog-api-key header (never in a URL), is stored 0600 in /config and is never shown or logged.
User rule 2026-10-29: "for any youtube search its mandatory to set up YouTube cookies.txt" — search can't be turned on or run
until the YouTube download client's private cookies.txt is saved (the same file the YouTube download tool uses).
"""
import datetime
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
from zoneinfo import ZoneInfo

FILE = 'themearr_youtube_search.json'
USAGE = 'themearr_youtube_search_usage.json'
API = 'https://www.googleapis.com/youtube/v3/'
PACIFIC = ZoneInfo('America/Los_Angeles')
DEFAULT_BUDGET, MAX_BUDGET = 80, 95
MIN_GAP = 10          # seconds between two searches started from a title page
CACHE_DAYS = 30       # YouTube policy maximum for stored API data
MAX_RESULTS = 5
MAX_BYTES = 512 * 1024
UA = 'Preludarr/1.12 (self-hosted theme manager)'
_LOCK = threading.Lock()
_BG = {'thread': None}
QUOTA_REASONS = ('quotaExceeded', 'dailyLimitExceeded', 'rateLimitExceeded', 'userRateLimitExceeded')


def _e(value):
    return html.escape(str(value), quote=True)


def _cfg():
    return Path(os.environ.get('CONFIG_DIR', '/config'))


def _read(name, default):
    p = _cfg() / name
    try:
        if p.is_symlink():
            return default
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _write(name, data):
    p = _cfg() / name
    tmp = p.with_name('.' + name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, p)


# ------------------------------------------------------------------------------ settings ----
def clamp_budget(value):
    try:
        return max(1, min(MAX_BUDGET, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET


def settings():
    s = _read(FILE, {})
    key = str(s.get('api_key') or '')
    return {'enabled': bool(s.get('enabled')), 'api_key': key if re.fullmatch(r'[A-Za-z0-9_-]{20,80}', key) else '',
            'budget': clamp_budget(s.get('budget', DEFAULT_BUDGET)), 'background': bool(s.get('background'))}


def save_settings(data):
    _write(FILE, {'enabled': bool(data['enabled']), 'api_key': data['api_key'], 'budget': clamp_budget(data['budget']),
                  'background': bool(data['background'])})


def cookies_ok():
    """Mandatory (user rule): the YouTube download client's cookies.txt is saved and private."""
    try:
        st = __import__('youtube_settings').status(str(_cfg()))
    except Exception:
        return False
    return bool(st.get('configured') and st.get('private'))


COOKIES_NEEDED = ('YouTube search needs your YouTube cookies.txt first (Settings → Download Clients → Download engines → YouTube → Configure; a step-by-step guide is there). '
                  'It is required for every YouTube search.')


def ready():
    s = settings()
    return s['enabled'] and bool(s['api_key']) and cookies_ok()


# --------------------------------------------------------------------------------- usage ----
def _today():
    return datetime.datetime.now(PACIFIC).strftime('%Y-%m-%d')


def usage():
    """{'day', 'used', 'paused' (bool), 'last', 'reason'} for today's Pacific day; a new day starts fresh."""
    u = _read(USAGE, {})
    if u.get('day') != _today():
        u = {'day': _today(), 'used': 0, 'paused': False, 'last': u.get('last', 0), 'reason': ''}
    return {'day': u['day'], 'used': int(u.get('used') or 0), 'paused': bool(u.get('paused')), 'last': float(u.get('last') or 0),
            'reason': str(u.get('reason') or '')[:120]}


def _count(paused_reason=''):
    u = usage()
    u['used'] += 0 if paused_reason else 1
    u['last'] = time.time()
    if paused_reason:
        u['paused'], u['reason'] = True, paused_reason
    _write(USAGE, u)


def reset_time_text():
    nxt = (datetime.datetime.now(PACIFIC) + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.astimezone().strftime('%H:%M')


def reserve(budget):
    """1.12 (user 2026-10-30): background search never uses the last quarter of the daily budget — it is kept for your own searches."""
    return budget // 4


def background_share(budget):
    return max(0, budget - reserve(budget))


def background_gap(budget):
    return max(60, 86400 // max(1, background_share(budget)))


def can_search(interactive=True):
    """(ok, reason) — budget, pause and spacing checks; nothing is called here."""
    s, u = settings(), usage()
    if not s['enabled']:
        return False, 'YouTube search is off (Settings → Download Clients → YouTube search).'
    if not s['api_key']:
        return False, 'YouTube search needs your YouTube Data API key (Settings → Download Clients → YouTube search).'
    if not cookies_ok():
        return False, COOKIES_NEEDED
    if u['paused']:
        return False, f'YouTube search is paused until the daily reset at {reset_time_text()} ({u["reason"] or "YouTube asked us to stop"}).'
    if u['used'] >= s['budget']:
        return False, f'Today’s YouTube search budget is used up ({u["used"]} of {s["budget"]}); it resets at {reset_time_text()}.'
    if not interactive and u['used'] >= background_share(s['budget']):
        return False, f'Background search has used its share today; the last {reserve(s["budget"])} searches are kept for you.'
    gap = MIN_GAP if interactive else background_gap(s['budget'])
    wait = u['last'] + gap - time.time()
    if wait > 0:
        return False, f'YouTube search is spacing its requests — try again in {int(wait) + 1} seconds.'
    return True, ''


# ------------------------------------------------------------------------------- the API ----
def _call(endpoint, params, key):
    url = API + endpoint + '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'X-goog-api-key': key, 'Accept': 'application/json', 'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if urllib.parse.urlsplit(r.geturl()).hostname != 'www.googleapis.com':
                raise ValueError('Unexpected redirect')
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('Response too large')
            return json.loads(body)
    except urllib.error.HTTPError as error:
        reason, detail = '', ''
        try:
            err = json.loads(error.read(65536)).get('error', {})
            reason = str((err.get('errors') or [{}])[0].get('reason') or '')
            # Google's ErrorInfo names the exact cause (API_KEY_INVALID, SERVICE_DISABLED, API_KEY_SERVICE_BLOCKED, …)
            detail = next((str(d.get('reason')) for d in err.get('details') or [] if isinstance(d, dict) and d.get('reason')), '')
        except (ValueError, AttributeError, OSError):
            pass
        raise ApiError(error.code, reason, detail) from None


class ApiError(Exception):
    def __init__(self, code, reason, detail=''):
        super().__init__(f'HTTP {code} {detail or reason}'.strip())
        self.code, self.reason, self.detail = code, reason, detail


# ------------------------------------------------------------------------- key test ----
# Preludarr 1.11.3 (user 2026-10-29: "add a test button to test the api key"). One videos.list call for a single long-lived
# public video: 1 unit from the 10,000-a-day pool, NOT one of the 100 daily searches. The key is never stored by the test.
KEY_TEST_FILE = 'themearr_youtube_key_test.json'
KEY_TEST_VIDEO = 'jNQXAC9IVRw'  # "Me at the zoo" (2005), the first YouTube video
KEY_ERRORS = (
    (('API_KEY_INVALID', 'keyInvalid'), 'Google says this API key is not valid. Copy it again from Credentials (no spaces).'),
    (('SERVICE_DISABLED', 'accessNotConfigured'), 'YouTube Data API v3 is not enabled for this key’s project. Open the API page, press Enable, wait a few minutes and test again.'),
    (('API_KEY_SERVICE_BLOCKED',), 'This key is restricted to other APIs. Under API restrictions, tick YouTube Data API v3 and save.'),
    (('API_KEY_HTTP_REFERRER_BLOCKED', 'API_KEY_IP_ADDRESS_BLOCKED', 'API_KEY_ANDROID_APP_BLOCKED', 'API_KEY_IOS_APP_BLOCKED'),
     'The key’s “Application restrictions” block Preludarr. Set them to None (keep the API restriction to YouTube Data API v3).'),
    (('quotaExceeded', 'dailyLimitExceeded', 'RATE_LIMIT_EXCEEDED'), 'The key works, but today’s YouTube quota is used up. It resets at midnight Pacific time.'),
)


def test_key(key):
    """(ok, text). Network: one videos.list request."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', key or ''):
        return False, 'That does not look like a YouTube Data API key (they usually start with “AIza”).'
    try:
        data = _call('videos', {'part': 'id', 'id': KEY_TEST_VIDEO}, key)
    except ApiError as error:
        for names, text in KEY_ERRORS:
            if error.detail in names or error.reason in names:
                return ('quota' in text), text
        return False, f'YouTube refused the key ({error}).'
    except Exception as error:
        return False, 'Could not reach YouTube from this server (' + type(error).__name__ + '). Check the internet connection and try again.'
    if any((it or {}).get('id') == KEY_TEST_VIDEO for it in data.get('items') or []):
        return True, 'The API key works: YouTube Data API v3 answered. (The test used 1 of 10,000 daily units, not one of your searches.)'
    return False, 'YouTube answered, but not as expected — try again in a minute.'


def last_key_test():
    t = _read(KEY_TEST_FILE, {})
    return t if t.get('at') else None


def _duration(text):
    m = re.fullmatch(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', str(text or ''))
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


FLAG_WORDS = (('cover', r'\bcover\b'), ('remix', r'\bremix\b|\bmashup\b'), ('live', r'\blive\b|\bconcert\b'),
              ('extended', r'\bextended\b|\b\d+\s*hours?\b|\b1\s*hour\b|\bloop(ed)?\b'), ('compilation', r'\bfull album\b|\bcompilation\b|\ball songs\b|\bplaylist\b|\bost\s+full\b'),
              ('trailer', r'\btrailer\b|\bteaser\b'), ('reaction', r'\breaction\b|\breview\b'), ('karaoke / instrumental', r'\bkaraoke\b|\binstrumental\b'),
              ('sped up / slowed', r'\bsped\s*up\b|\bslowed\b|\bnightcore\b|\breverb\b'), ('piano / guitar version', r'\bpiano\b|\bguitar\b|\borchestral cover\b'))


def flags(title, seconds, folder_year, movie):
    t = html.unescape(str(title or '')).casefold()
    out = [label for label, rx in FLAG_WORDS if re.search(rx, t)]
    years = {int(y) for y in re.findall(r'\b(19[3-9]\d|20[0-4]\d)\b', t)}
    if folder_year and years and folder_year not in years:
        out.append('different year (' + ', '.join(str(y) for y in sorted(years)) + ')')
    if seconds is not None:
        if seconds < 20:
            out.append('very short')
        elif seconds > (12 * 60 if movie else 6 * 60):
            out.append('very long (' + str(seconds // 60) + ' min)')
    return out


def query_for(name, library, clean, year):
    title = clean(name)[:90]
    yr = year(name)
    words = 'main theme' if __import__('kinds').is_movie(library) else 'theme song'
    return ' '.join(x for x in (title, str(yr) if yr else '', words) if x)


# ------------------------------------------------------------------------------- caching ----
def _cached(app, path, query):
    with app.db() as c:
        c.execute('DELETE FROM youtube_search_cache WHERE fetched<?', (int(time.time()) - CACHE_DAYS * 86400,))
        row = c.execute('SELECT query,results,fetched FROM youtube_search_cache WHERE path=?', (path,)).fetchone()
    if row and row['query'] == query:
        try:
            return json.loads(row['results']), row['fetched']
        except ValueError:
            return None, 0
    return None, 0


def forget(app, path):
    with app.db() as c:
        c.execute('DELETE FROM youtube_search_cache WHERE path=?', (path,))


# ------------------------------------------------------------------- MainThemeSoundtracks ----
# Preludarr 1.12 (user 2026-10-30: "make it auto search first from MainThemeSoundtracks and then globally" — Collections and Movies).
# The channel's upload list is read with playlistItems.list (1 unit per 50 videos from the 10,000-unit pool, NOT one of the 100 daily
# searches), kept for a week, and matched by exact title locally. Only when it has nothing does the normal YouTube search run.
CHANNEL_NAME = 'MainThemeSoundtracks'
CHANNEL_URL = 'https://www.youtube.com/@mainthemesoundtracks'
CHANNEL_UPLOADS = 'UUmsBjRtiuFIEkVr_rMKXorA'   # uploads playlist of channel UCmsBjRtiuFIEkVr_rMKXorA
CHANNEL_FILE = 'themearr_youtube_channel.json'
CHANNEL_DAYS = 7
CHANNEL_PAGES = 10                             # at most 500 videos
_CH_LOCK = threading.Lock()


def _norm(text):
    t = html.unescape(str(text or '')).casefold().replace('&', ' and ')
    t = re.sub(r"['’®™©]", '', t)
    t = ' '.join(re.sub(r'[^0-9a-z]+', ' ', t).split())
    return t[4:] if t.startswith('the ') else t


def _core(title):
    """'Mission: Impossible - Rogue Nation Main Theme' -> 'Mission: Impossible - Rogue Nation'."""
    return re.sub(r'\s*[-–|:]?\s*(official\s+)?main\s+theme\b.*$', '', html.unescape(str(title or '')), flags=re.I).strip()


def channel_videos(key):
    """[{'id', 'title'}] of the channel's uploads: the saved list while it is under a week old, else read again.
    If YouTube can't be reached, an older list is used for up to 30 days (YouTube policy maximum), then dropped."""
    with _CH_LOCK:
        saved = _read(CHANNEL_FILE, {})
        age = time.time() - float(saved.get('fetched') or 0)
        if saved.get('items') is not None and age < CHANNEL_DAYS * 86400:
            return saved['items']
        items, token = [], ''
        try:
            for _ in range(CHANNEL_PAGES):
                params = {'part': 'snippet', 'playlistId': CHANNEL_UPLOADS, 'maxResults': 50}
                if token:
                    params['pageToken'] = token
                data = _call('playlistItems', params, key)
                for it in data.get('items') or []:
                    sn = it.get('snippet') or {}
                    vid = str((sn.get('resourceId') or {}).get('videoId') or '')
                    if re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
                        items.append({'id': vid, 'title': html.unescape(str(sn.get('title') or ''))[:200]})
                token = str(data.get('nextPageToken') or '')
                if not token:
                    break
        except Exception as error:
            print('MainThemeSoundtracks list: ' + type(error).__name__, flush=True)
            return (saved.get('items') or []) if age < CACHE_DAYS * 86400 else []
        _write(CHANNEL_FILE, {'fetched': int(time.time()), 'items': items})
        return items


def channel_hits(app, name, key):
    """Suggestions from MainThemeSoundtracks whose title (without "Main Theme") is exactly this title."""
    want = _norm(app.clean(name))
    if not want:
        return []
    out = []
    for v in channel_videos(key):
        if _norm(_core(v['title'])) == want:
            out.append({'source': 'youtube_search', 'label': CHANNEL_NAME, 'trusted': True, 'video_id': v['id'],
                        'url': 'https://www.youtube.com/watch?v=' + v['id'], 'track': v['title'], 'channel': CHANNEL_NAME, 'seconds': None,
                        'flags': flags(v['title'], None, app.year(name), True), 'note': 'Exact title match on ' + CHANNEL_NAME + ' (official main themes)'})
    return out[:3]


def _merge(first, rest):
    seen = {f.get('video_id') for f in first}
    return first + [r for r in rest if r.get('video_id') not in seen]


# -------------------------------------------------------------------------------- search ----
def suggest(app, path, name, library, interactive=True, everywhere=False):
    """([suggestions], note). Shows, Movies and collections look on MainThemeSoundtracks first; if it has the title, no search is spent
    unless `everywhere` asks for all of YouTube too. Cached results cost nothing; a new search only within budget, spacing and pause rules."""
    kinds = __import__('kinds')
    if not kinds.is_regular(library):
        return [], ''
    hits = []
    if kinds.is_regular(library) and ready():  # Movies, collections and (user 2026-10-31) Shows
        try:
            hits = channel_hits(app, name, settings()['api_key'])
        except Exception:
            hits = []
    query = query_for(name, library, app.clean, app.year)
    cached, fetched = _cached(app, path, query)
    if cached is not None and (not everywhere or any(not c.get('trusted') for c in cached)):
        age = max(0, int((time.time() - fetched) // 86400))
        return _merge(hits, cached), 'YouTube search results from ' + ('today' if age == 0 else f'{age} day(s) ago') + ' (kept up to 30 days).'
    if hits and not everywhere:
        with app.db() as c:
            c.execute('INSERT INTO youtube_search_cache(path,query,results,fetched) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET '
                      'query=excluded.query,results=excluded.results,fetched=excluded.fetched', (path, query, json.dumps(hits), int(time.time())))
        return hits, 'Found on ' + CHANNEL_NAME + ' — no YouTube search used. Not the right one? Search all of YouTube.'
    if not settings()['enabled']:
        return hits, ''
    with _LOCK:  # one search at a time, so the budget and spacing checks can't be raced
        ok, why = can_search(interactive)
        if not ok:
            return hits, why
        key = settings()['api_key']
        try:
            found = _call('search', {'part': 'snippet', 'type': 'video', 'maxResults': MAX_RESULTS, 'q': query,
                                     'videoEmbeddable': 'true', 'safeSearch': 'moderate'}, key)
            _count()
        except ApiError as error:
            if error.reason in QUOTA_REASONS:
                _count(paused_reason='YouTube: ' + error.reason)
                return hits, f'YouTube says the search allowance is used up ({error.reason}); Preludarr stops until {reset_time_text()}.'
            _count()
            if error.code in (400, 403) and error.reason in ('keyInvalid', 'badRequest', 'forbidden', 'accessNotConfigured', ''):
                return hits, 'YouTube rejected the API key (' + (error.reason or str(error.code)) + '). Check it in Settings → Download Clients → YouTube search.'
            return hits, 'YouTube search failed (' + str(error) + ').'
        except Exception as error:
            return hits, 'YouTube search is unavailable right now (' + type(error).__name__ + ').'
        ids = [str((it.get('id') or {}).get('videoId') or '') for it in (found.get('items') or [])]
        ids = [v for v in ids if re.fullmatch(r'[A-Za-z0-9_-]{11}', v)][:MAX_RESULTS]
        details = {}
        if ids:
            try:
                for it in _call('videos', {'part': 'contentDetails,snippet,status', 'id': ','.join(ids)}, key).get('items') or []:
                    details[it.get('id')] = it
            except Exception:
                details = {}  # lengths are a nice-to-have; the search itself already succeeded
    movie = __import__('kinds').is_movie(library)
    out = []
    for vid in ids:
        d = details.get(vid) or {}
        sn = d.get('snippet') or next(((it.get('snippet') or {}) for it in found.get('items') or [] if (it.get('id') or {}).get('videoId') == vid), {})
        if d and (d.get('status') or {}).get('embeddable') is False:
            continue
        secs = _duration((d.get('contentDetails') or {}).get('duration'))
        title = html.unescape(str(sn.get('title') or ''))[:200]
        out.append({'source': 'youtube_search', 'label': 'YouTube search', 'video_id': vid, 'url': 'https://www.youtube.com/watch?v=' + vid,
                    'track': title, 'channel': html.unescape(str(sn.get('channelTitle') or ''))[:100], 'seconds': secs,
                    'flags': flags(title, secs, app.year(name), movie), 'note': 'Search: “' + query + '”'})
    out = _merge(hits, out)
    with app.db() as c:
        c.execute('INSERT INTO youtube_search_cache(path,query,results,fetched) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET '
                  'query=excluded.query,results=excluded.results,fetched=excluded.fetched', (path, query, json.dumps(out), int(time.time())))
    return out, ('YouTube search found nothing embeddable for “' + query + '”.' if not out else '')


# ---------------------------------------------------------------------------- background ----
def _next_title(app):
    """A missing Shows / Movies title with no saved suggestions (oldest first)."""
    with app.db() as c:
        row = c.execute("SELECT s.path,s.name,s.library FROM shows s LEFT JOIN theme_suggestions t ON t.path=s.path "
                        "WHERE s.library IN ('Shows','Movies') AND s.state='missing' AND t.path IS NULL ORDER BY s.name LIMIT 1").fetchone()
    return row


def _background(app):
    while True:
        pause = 300
        try:
            s = settings()
            if s['background'] and ready() and not app.STATUS.get('running'):
                ok, _ = can_search(interactive=False)
                row = _next_title(app) if ok else None
                if row:
                    before = usage()['used']
                    __import__('sources').cached_find(app, row['path'], row['name'], row['library'], interactive=False)
                    # a YouTube search was spent: wait the budget interval; only Plex/ThemerrDB were asked: move on soon
                    pause = background_gap(s['budget']) if usage()['used'] > before else 20
        except Exception as error:
            print('YouTube background search error: ' + type(error).__name__, flush=True)
        time.sleep(pause)


def start_background(app):
    if _BG['thread'] is None or not _BG['thread'].is_alive():
        _BG['thread'] = threading.Thread(target=_background, args=(app,), daemon=True, name='preludarr-youtube-search')
        _BG['thread'].start()


# ------------------------------------------------------------------------------------ UI ----
# Preludarr 1.11.1 (user 2026-10-29: "need steps or link on how to get an api key here"). Steps follow Google's own pages
# (developers.google.com/youtube/v3/getting-started and /youtube/registering_an_application), checked 2026-10-29.
API_KEY_GUIDE = (
    '<details class="yt-guide" id="youtube-api-key-guide"><summary>🔑 How to get a YouTube Data API key (step by step)</summary>'
    '<div class="yt-guide-body"><ol>'
    '<li>Sign in to the <a href="https://console.cloud.google.com/" target="_blank" rel="noopener noreferrer">Google Cloud console ↗</a> with a Google account.</li>'
    '<li>Create a project: click the project picker at the top → <strong>New project</strong> → name it e.g. <em>Preludarr</em> → <strong>Create</strong>, and make sure it is selected.</li>'
    '<li>Open <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noopener noreferrer">YouTube Data API v3 ↗</a> and press <strong>Enable</strong>.</li>'
    '<li>Open <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Credentials ↗</a> → <strong>Create credentials</strong> → <strong>API key</strong>.</li>'
    '<li>Recommended: on that key choose <strong>Edit / Restrict key</strong> → <strong>API restrictions</strong> → <strong>Restrict key</strong> → tick only '
    '<strong>YouTube Data API v3</strong> → <strong>Save</strong>. Then the key can’t be used for anything else.</li>'
    '<li>Copy the key, paste it into <strong>API key</strong> below, tick <strong>Use YouTube search for Shows and Movies</strong> and press <strong>Save</strong>.</li>'
    '</ol><p class="muted">Every project gets a free daily allowance (100 searches); Preludarr stays under it. Keep the key private — it is stored only on this server and never shown again.</p>'
    '<p class="muted">Official guides: <a href="https://developers.google.com/youtube/v3/getting-started" target="_blank" rel="noopener noreferrer">YouTube Data API — Getting started ↗</a> · '
    '<a href="https://developers.google.com/youtube/registering_an_application" target="_blank" rel="noopener noreferrer">Obtaining an API key ↗</a></p></div></details>')


def card_html():
    s, u = settings(), usage()
    state = ('On' if s['enabled'] else 'Off') + (' · key saved' if s['api_key'] else ' · no key yet') + ('' if cookies_ok() else ' · cookies.txt missing')
    status = (f'Paused until {reset_time_text()} — {u["reason"]}' if u['paused'] else f'{u["used"]} of {s["budget"]} searches used today (resets {reset_time_text()})')
    return ''.join([
        '<section class="connection" id="dc-youtube-search" style="grid-column:1/-1"><div class="conn-head"><span class="brand-mark" aria-hidden="true" style="font-size:26px">🔎</span>',
        '<div><div class="conn-name">YouTube search (preview only)</div><span class="tag ', 'on' if ready() else '', '">Shows &amp; Movies · ', _e(state), '</span></div></div>',
        '<div class="stats"><div>Searches YouTube for a theme song <strong>only when Plex TV Themes and ThemerrDB have nothing</strong> for a Show or Movie. ',
        'Results are shown as suggestions you can preview in YouTube’s player. Preludarr never downloads them: YouTube’s API rules don’t allow it. ',
        'To install one, copy its link into the title’s <em>YouTube</em> box — that download is your own choice.</div>',
        '<div>Uses your own free YouTube Data API key (steps below). ',
        'One search per title, remembered for 30 days. Preludarr stays under YouTube’s 100 searches a day and stops for the day if YouTube says the limit is reached. ',
        'For movies and collections it first looks on the <a href="', CHANNEL_URL, '" target="_blank" rel="noopener noreferrer">MainThemeSoundtracks ↗</a> channel, which uses none of those searches.</div>',
        '<div class="muted">With your own key you use YouTube under the <a href="https://www.youtube.com/t/terms" target="_blank" rel="noopener noreferrer">YouTube Terms of Service ↗</a> ',
        'and the <a href="https://policies.google.com/privacy" target="_blank" rel="noopener noreferrer">Google Privacy Policy ↗</a>.</div>',
        '<div><strong>YouTube cookies.txt (required):</strong> ', ('✅ saved' if cookies_ok() else '❌ not set up — <a href="/download-clients?tab=engines&amp;cookies=1#youtube-settings">add it under Download engines → YouTube → Configure</a> before turning search on'), '</div>',
        '' if cookies_ok() else __import__('youtube_ui').COOKIE_GUIDE.replace('Upload <code>cookies.txt</code> here.', 'Upload <code>cookies.txt</code> under Download engines → YouTube → Configure.'),
        '<div><strong>Today:</strong> ', _e(status), '</div></div>',
        API_KEY_GUIDE,
        '<form method="post" action="/download-clients/youtube-search" class="ys-form" autocomplete="off">',
        '<label class="ys-check"><input type="checkbox" name="enabled" value="1"', ' checked' if s['enabled'] else '', '> <span>Use YouTube search for Shows and Movies</span></label>',
        '<label class="ys-field"><span>API key <a class="ys-help" href="#youtube-api-key-guide" onclick="const d=document.getElementById(&quot;youtube-api-key-guide&quot;);if(d)d.open=true">How do I get one?</a></span><input type="password" name="api_key" maxlength="80" placeholder="',
        '•••••••• saved — leave empty to keep' if s['api_key'] else 'Paste your YouTube Data API key', '"></label>',
        '<label class="ys-field"><span>Searches per day (1–', str(MAX_BUDGET), ')</span><input type="number" name="budget" min="1" max="', str(MAX_BUDGET), '" value="', str(s['budget']), '"></label>',
        '<label class="ys-check"><input type="checkbox" name="background" value="1"', ' checked' if s['background'] else '', '> <span>Background search: work through missing Shows and Movies slowly, spread over the day ',
        f'(about one search every {background_gap(s["budget"]) // 60} minutes, at most {background_share(s["budget"])} a day — the last {reserve(s["budget"])} are kept for your own searches)</span></label>',
        '<div class="ys-actions"><button type="submit">Save</button>',
        '<button type="submit" class="btn ghost" formaction="/download-clients/youtube-search-test" formnovalidate>Test API key</button>', '<label class="ys-check"><input type="checkbox" name="remove_key" value="1"> <span>Remove saved key</span></label>' if s['api_key'] else '',
        '</div><p class="muted ys-testnote">Test API key checks the key in the box (or the saved key if the box is empty) with one tiny request — it does not save anything and does not use your searches.</p>',
        _key_test_html(), '</form>',
        '<style>#dc-youtube-search .stats{max-width:900px}.ys-form{display:flex;flex-direction:column;gap:10px;margin-top:12px;max-width:640px}.ys-field{display:flex;flex-direction:column;gap:4px}',
        '.ys-field span{font-weight:600}.ys-field input{box-sizing:border-box;width:100%;max-width:420px;padding:9px;background:#101c30;color:#fff;border:1px solid #647da0;border-radius:7px}',
        '.ys-check{display:flex!important;gap:8px;align-items:flex-start}.ys-check input{width:18px;height:18px;margin:2px 0 0;flex:0 0 auto}',
        '.ys-actions{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.ys-help{font-weight:400;font-size:.9em;margin-left:8px;color:#9fd0ff}</style></section>'])


def _key_test_html():
    t = last_key_test()
    if not t:
        return ''
    return ('<p class="ys-result ' + ('ok' if t.get('ok') else 'bad') + '"><strong>Last key test ' + _e(time.strftime('%Y-%m-%d %H:%M', time.localtime(int(t['at'])))) +
            ':</strong> ' + ('✅ ' if t.get('ok') else '❌ ') + _e(str(t.get('text') or '')[:300]) + '</p>'
            '<style>.ys-result{margin:4px 0 0;padding:8px 10px;border-radius:8px;max-width:640px}.ys-result.ok{background:#173a2a;border:1px solid #3f8f5f}'
            '.ys-result.bad{background:#3a1d24;border:1px solid #9b394a}.ys-testnote{margin:0;font-size:.9em;max-width:640px}</style>')


def post_test(handler, redirect):
    """Test the typed key (or the saved one). Never saves the key; stores only the outcome."""
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 2048:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=8)
        key = (form.get('api_key', [''])[0] or '').strip() or settings()['api_key']
        if not key:
            raise ValueError('Paste an API key into the box first, then press Test API key.')
        ok, text = test_key(key)
        _write(KEY_TEST_FILE, {'ok': ok, 'text': text, 'at': int(time.time())})
        msg = ('API key test passed: ' if ok else 'API key test failed: ') + text
    except (ValueError, UnicodeError) as error:
        msg = 'API key test: ' + str(error)
    return redirect(handler, '/download-clients?' + urllib.parse.urlencode({'tab': 'sources', 'notice': msg[:300]}) + '#dc-youtube-search')


def post(handler, redirect):
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 2048:
            raise ValueError('Invalid form.')
        form = urllib.parse.parse_qs(handler.rfile.read(size).decode('utf-8', 'strict'), keep_blank_values=True, max_num_fields=8)
        if any(len(v) != 1 for v in form.values()):
            raise ValueError('Invalid form.')
        f = lambda k: form.get(k, [''])[0].strip()  # noqa: E731
        data = settings()
        key = f('api_key')
        if key:
            if not re.fullmatch(r'[A-Za-z0-9_-]{20,80}', key):
                raise ValueError('That does not look like a YouTube Data API key.')
            data['api_key'] = key
        if f('remove_key') == '1':
            data['api_key'] = ''
        data['enabled'] = f('enabled') == '1'
        data['background'] = f('background') == '1'
        data['budget'] = clamp_budget(f('budget') or DEFAULT_BUDGET)
        if data['enabled'] and not data['api_key']:
            raise ValueError('Add your API key to turn YouTube search on.')
        if data['enabled'] and not cookies_ok():
            raise ValueError('Set up YouTube cookies.txt first (Download engines → YouTube → Configure, with a step-by-step guide) — it is required for YouTube search.')
        save_settings(data)
        msg = 'YouTube search saved: ' + ('on' if data['enabled'] else 'off') + f', {data["budget"]} searches a day' + (', background search on.' if data['background'] and data['enabled'] else '.')
    except (ValueError, OSError, UnicodeError) as error:
        msg = 'Not saved: ' + (str(error) if isinstance(error, ValueError) else 'settings could not be written.')
    return redirect(handler, '/download-clients?' + urllib.parse.urlencode({'tab': 'sources', 'notice': msg}))
