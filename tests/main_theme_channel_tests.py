# SPDX-License-Identifier: GPL-3.0-or-later
"""1.12 (user): YouTube search for Movies and Collections looks on MainThemeSoundtracks first, then all of YouTube.
Fake YouTube API (no network, no quota). Run in a Preludarr image, --network none."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

work = Path(tempfile.mkdtemp())
for d in ('anime', 'movies', 'films', 'shows'):
    (work / d).mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import youtube_search as ys  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


CHANNEL = ['Batman Main Theme', 'Batman Beyond Main Theme', 'Mission: Impossible - Rogue Nation Main Theme', 'Pain &amp; Gain Main Theme',
           "Assassin's Creed Origins Main Theme", 'Spider-Man (PS4) Main Theme', 'The Dark Knight Main Theme', 'Top Gun: Maverick Main Theme']
CALLS = []
DOWN = {'channel': False}


def vid(i):
    return f'chan{i:07d}'


def fake_call(endpoint, params, key):
    CALLS.append(endpoint)
    if endpoint == 'playlistItems':
        if DOWN['channel']:
            raise OSError('offline')
        assert params['playlistId'] == ys.CHANNEL_UPLOADS
        page = 1 if params.get('pageToken') == 'p2' else 0
        chunk = list(enumerate(CHANNEL))[page * 5:(page + 1) * 5]
        return {'items': [{'snippet': {'title': t, 'resourceId': {'videoId': vid(i)}}} for i, t in chunk], **({} if page else {'nextPageToken': 'p2'})}
    if endpoint == 'search':
        return {'items': [{'id': {'videoId': 'glob0000001'}, 'snippet': {'title': params['q'] + ' (official)', 'channelTitle': 'Someone'}},
                          {'id': {'videoId': vid(0)}, 'snippet': {'title': 'Batman Main Theme', 'channelTitle': 'MainThemeSoundtracks'}}]}
    if endpoint == 'videos':
        return {'items': []}
    raise AssertionError(endpoint)


ys._call = fake_call
ys.cookies_ok = lambda: True
ys.save_settings({'enabled': True, 'api_key': 'A' * 39, 'budget': 80, 'background': False})
ys.MIN_GAP = 0
used = lambda: ys.usage()['used']  # noqa: E731

match = lambda name: [h['track'] for h in ys.channel_hits(app, name, 'k')]  # noqa: E731
check('Exact titles match, ignoring punctuation, "&"/"and" and a year', match('Mission Impossible - Rogue Nation (2015)') == ['Mission: Impossible - Rogue Nation Main Theme']
      and match('Pain and Gain (2013)') == ['Pain & Gain Main Theme'] and match("Assassin’s Creed Origins") == ["Assassin's Creed Origins Main Theme"]
      and match('Dark Knight (2008)') == ['The Dark Knight Main Theme'] and match('Top Gun - Maverick (2022)') == ['Top Gun: Maverick Main Theme'])
check('No near misses: Batman ≠ Batman Beyond, Spider-Man ≠ Spider-Man (PS4), unknown titles find nothing',
      match('Batman (1989)') == ['Batman Main Theme'] and match('Spider-Man (2002)') == [] and match('Inception (2010)') == [])
check('The channel list is read once (2 pages, 1 unit each) and reused — none of the 100 daily searches', CALLS.count('playlistItems') == 2 and 'search' not in CALLS, CALLS)

CALLS.clear(); before = used()
found, note = ys.suggest(app, '/films/Batman (1989)', 'Batman (1989)', 'Movies')
check('Movie on the channel: shown first, marked as MainThemeSoundtracks, no YouTube search spent',
      [f['video_id'] for f in found] == [vid(0)] and found[0]['trusted'] and found[0]['label'] == 'MainThemeSoundtracks'
      and 'search' not in CALLS and used() == before and 'no YouTube search used' in note, (found, note, CALLS))
found2, _ = ys.suggest(app, '/films/Batman (1989)', 'Batman (1989)', 'Movies')
check('…and remembered like other results', [f['video_id'] for f in found2] == [vid(0)] and 'search' not in CALLS)

CALLS.clear()
found, note = ys.suggest(app, '/films/Batman (1989)', 'Batman (1989)', 'Movies', everywhere=True)
check('"Search all of YouTube": one search, channel result first, duplicates dropped',
      CALLS.count('search') == 1 and [f['video_id'] for f in found] == [vid(0), 'glob0000001'] and used() == before + 1, [f['video_id'] for f in found])

CALLS.clear()
found, _ = ys.suggest(app, '/films/Inception (2010)', 'Inception (2010)', 'Movies')
check('Not on the channel: falls back to the normal YouTube search', CALLS.count('search') == 1 and found and not found[0].get('trusted'), CALLS)

CALLS.clear()
found, _ = ys.suggest(app, '/shows/Batman (1966)', 'Batman (1966)', 'Shows')
check('Shows look on the channel too (user 2026-10-31): match shown, no search spent', 'search' not in CALLS and [f['video_id'] for f in found] == [vid(0)], (CALLS, found))
CALLS.clear()
found, _ = ys.suggest(app, '/shows/Birdgirl (2021)', 'Birdgirl (2021)', 'Shows')
check('…a show not on the channel falls back to the normal search', CALLS.count('search') == 1 and all(not f.get('trusted') for f in found))

saved = json.loads((work / 'config' / ys.CHANNEL_FILE).read_text())
saved['fetched'] = int(time.time()) - 8 * 86400; (work / 'config' / ys.CHANNEL_FILE).write_text(json.dumps(saved)); DOWN['channel'] = True
check('YouTube unreachable after a week: the saved list is still used (up to 30 days)', match('Batman (1989)') == ['Batman Main Theme'])
saved['fetched'] = int(time.time()) - 31 * 86400; (work / 'config' / ys.CHANNEL_FILE).write_text(json.dumps(saved))
check('…but never past 30 days (YouTube policy)', match('Batman (1989)') == [])
DOWN['channel'] = False
check('The saved channel list is private (0600)', oct((work / 'config' / ys.CHANNEL_FILE).stat().st_mode & 0o777) == '0o600')

u = ys.usage(); u.update(used=60, last=0, paused=False); ys._write(ys.USAGE, u)
ok_bg, why_bg = ys.can_search(interactive=False)
ok_me, _ = ys.can_search(interactive=True)
check('Background search stops at 60 of 80: the last 20 searches are kept for your own clicks', not ok_bg and 'kept for you' in why_bg and ok_me, why_bg)
u.update(used=59); ys._write(ys.USAGE, u)
check('…below its share it may still search, spaced over the day (one every 24 min)', ys.can_search(interactive=False)[0] and ys.background_gap(80) == 1440)
check('Reserve for small budgets: 3 → none kept, 1 → background never blocked by the reserve', ys.reserve(3) == 0 and ys.background_share(1) == 1)
u.update(used=0); ys._write(ys.USAGE, u)

mc = __import__('movie_collections')
item = {'id': '77', 'title': 'Batman Collection'}
hits = ys.channel_hits(app, mc._search_name(item), 'k')
html_ = ''.join(mc._sources('plex', item, {'search': True, 'results': hits, 'manual': True}, ''))
check('Collections: "Batman Collection" finds the channel theme, with the ⭐ tag, "Use this" and "Search all of YouTube"',
      [h['video_id'] for h in hits] == [vid(0)] and '⭐ MainThemeSoundtracks' in html_ and 'name="scope" value="all"' in html_ and 'Use this' in html_
      and 'mainthemesoundtracks' in html_)
mixed = ''.join(mc._sources('plex', item, {'search': True, 'results': hits + [{'url': 'https://www.youtube.com/watch?v=glob0000001', 'track': 'x', 'video_id': 'glob0000001'}]}, ''))
check('…no extra button once all of YouTube was searched', 'value="all"' not in mixed)

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
