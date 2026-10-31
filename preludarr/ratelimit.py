# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-source rate limits (Themearr V1.9.14, user 2026-10-28: "users can adjust their rate limit for download sources … but Themearr
itself focuses on not hitting the rate limit for any download source. dont want to get banned for any source").

Every outside source Themearr calls goes through here. Each source has a conservative default and a hard safe maximum; whatever the
user saves is clamped to [minimum, maximum], so a setting can never push Themearr past a provider's published limit.
  * wait(source): spaces requests evenly (60 / per-minute limit seconds apart) — callers simply take a little longer.
  * take_hourly(source): for downloads with an hourly cap (YouTube) — refuses with a clear message instead of waiting an hour.
  * backoff(source, seconds): a provider said "slow down" (HTTP 429) — pause that source (capped).
Settings: /config/themearr_rate_limits.json (0600). Nothing here makes a request itself.
"""
import html
import json
import os
import threading
import time
from pathlib import Path

FILE = 'themearr_rate_limits.json'
# key: (label, unit, default, minimum, maximum, why the maximum)
SOURCES = {
    'animethemes': ('AnimeThemes API', 'requests per minute', 60, 10, 80, 'AnimeThemes allows about 90 requests per minute.'),
    'anilist': ('AniList', 'requests per minute', 20, 5, 60, 'AniList allows 90 per minute normally and 30 when degraded.'),
    'tmdb': ('TMDB', 'requests per minute', 40, 10, 120, 'Far below TMDB’s per-second limit.'),
    'tvdb': ('TVDB', 'requests per minute', 30, 5, 60, 'Kept conservative; TVDB does not publish a fixed number.'),
    'plex_themes': ('Plex TV Themes', 'requests per minute', 30, 5, 60, 'Static files; still polite.'),
    'themerrdb': ('ThemerrDB', 'requests per minute', 30, 5, 60, 'Static files on GitHub Pages; still polite.'),
    'youtube': ('YouTube (yt-dlp) look-ups', 'requests per minute', 4, 1, 10, 'YouTube is quick to block busy addresses.'),
    'youtube_downloads': ('YouTube downloads', 'downloads per hour', 15, 1, 40, 'Each download is a full video request.'),
}
MAX_BACKOFF = 600
_LOCK = threading.Lock()
_NEXT = {}        # source -> monotonic time the next request may start
_PAUSED = {}      # source -> monotonic time a 429 pause ends
_HOURLY = {}      # source -> [monotonic times]


def _path():
    return Path(os.environ.get('CONFIG_DIR', '/config')) / FILE


def clamp(source, value):
    _, _, default, lo, hi, _ = SOURCES[source]
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def settings():
    data = {k: v[2] for k, v in SOURCES.items()}
    try:
        p = _path()
        if not p.is_symlink():
            saved = json.loads(p.read_text())
            if isinstance(saved, dict):
                for k in SOURCES:
                    if k in saved:
                        data[k] = clamp(k, saved[k])
    except (OSError, ValueError):
        pass
    return data


def save(values):
    data = {k: clamp(k, values.get(k, v[2])) for k, v in SOURCES.items()}
    p = _path()
    tmp = p.with_name('.' + p.name + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=1)
    os.replace(tmp, p)
    return data


def spacing(source):
    """Seconds between two requests to a per-minute source."""
    return 60.0 / settings()[source]


def wait(source):
    """Block until this source may be asked again (even spacing + any 429 pause)."""
    if source not in SOURCES:
        return
    gap = spacing(source)
    with _LOCK:
        now = time.monotonic()
        start = max(now, _NEXT.get(source, 0.0), _PAUSED.get(source, 0.0))
        _NEXT[source] = start + gap
    if start > now:
        time.sleep(min(start - now, MAX_BACKOFF))


def take_hourly(source):
    """Use one slot of an hourly limit. Raises ValueError (plain message) when the hour's slots are used up."""
    limit = settings()[source]
    with _LOCK:
        now = time.monotonic()
        recent = [t for t in _HOURLY.get(source, []) if now - t < 3600]
        if len(recent) >= limit:
            wait_min = int((3600 - (now - recent[0])) // 60) + 1
            _HOURLY[source] = recent
            raise ValueError(f'{SOURCES[source][0]}: the limit of {limit} per hour is reached (Settings → Download Clients → Rate limits). '
                             f'Try again in about {wait_min} minute' + ('' if wait_min == 1 else 's') + '.')
        recent.append(now)
        _HOURLY[source] = recent


def backoff(source, seconds):
    """A provider answered 429 / 'slow down': pause this source (Retry-After honoured, capped)."""
    if source in SOURCES:
        with _LOCK:
            _PAUSED[source] = max(_PAUSED.get(source, 0.0), time.monotonic() + max(1.0, min(float(seconds or 30), MAX_BACKOFF)))


HOSTS = {'graphql.anilist.co': 'anilist', 'api.themoviedb.org': 'tmdb', 'api4.thetvdb.com': 'tvdb'}


def for_host(host):
    host = (host or '').lower()
    if host in HOSTS:
        return HOSTS[host]
    if 'plex' in host:
        return 'plex_themes'
    if 'lizardbyte' in host or 'themerrdb' in host:
        return 'themerrdb'
    return None


def panel_html(notice=''):
    s = settings()
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    rows = ''.join('<tr><th scope="row"><label for="rl-' + k + '">' + e(v[0]) + '</label></th><td><input id="rl-' + k + '" name="' + k + '" type="number" min="' + str(v[3]) +
                   '" max="' + str(v[4]) + '" value="' + str(s[k]) + '" required> <span class="muted">' + e(v[1]) + '</span></td><td class="muted">default ' + str(v[2]) +
                   ' · safe maximum ' + str(v[4]) + ' — ' + e(v[5]) + '</td></tr>' for k, v in SOURCES.items())
    return ('<section class="dc-sec" id="dc-sec-limits"><h2 class="dc-h">Rate limits</h2>'
            + ('<p class="notice" role="status">' + e(notice) + '</p>' if notice else '') +
            '<p class="muted">How fast Preludarr may ask each outside source. Preludarr always stays under every source’s limit so your address is not blocked: '
            'requests are spaced evenly, a “slow down” answer (HTTP 429) pauses that source, and values above the safe maximum are lowered to it. '
            'Lower numbers are always allowed — things just take a little longer.</p>'
            '<form method="post" action="/download-clients/limits"><div class="scroll"><table class="rl-table"><thead><tr><th>Source</th><th>Your limit</th><th>Notes</th></tr></thead><tbody>'
            + rows + '</tbody></table></div><button type="submit">Save rate limits</button> <button type="submit" name="reset" value="1">Use the defaults</button></form>'
            '<style>.rl-table th,.rl-table td{text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid #2b3a55;vertical-align:middle}'
            '.rl-table input{width:90px;padding:8px;background:#0d1626;color:#eef4ff;border:1px solid #3e5174;border-radius:8px}</style></section>')
