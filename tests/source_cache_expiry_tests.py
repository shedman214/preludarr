# SPDX-License-Identifier: GPL-3.0-or-later
"""1.16.3: provider outages, settings changes, and legacy negative caches."""
import json
import os
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

work = Path(tempfile.mkdtemp())
root = work / 'shows'
one, two = root / 'One (2020)', root / 'Two (2020)'
one.mkdir(parents=True)
two.mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), AUTO_DOWNLOAD='false',
                  POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import app
import sources
import youtube_search

app.ROOTS[:] = [('Shows', root)]
app.discover()
app.auto_themes.init(app)
sources.save_settings(app.CONFIG, {'plex': True, 'themerrdb': True, 'tmdb_search': True})
checks = []


def check(name, ok):
    checks.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def lookup(folder=one, **kw):
    return sources.cached_find(app, str(folder), folder.name, 'Shows', **kw)


def meta(folder=one):
    with app.db() as c:
        return c.execute('SELECT * FROM theme_suggestion_meta WHERE path=?', (str(folder),)).fetchone()


def expire(folder=one):
    with app.db() as c:
        c.execute('UPDATE theme_suggestion_meta SET expires=? WHERE path=?', (int(time.time())-1, str(folder)))


with app.db() as c:
    c.execute('INSERT INTO theme_suggestions VALUES(?,?,?,?)',
              (str(one), '[]', json.dumps(['ThemerrDB: no entry for this title.']), int(time.time())))
proposal = {'source': 'plex', 'label': 'Plex TV Themes', 'url': 'https://tvthemes.plexapp.com/12345.mp3',
            'track': 'Theme', 'note': 'Exact ID'}
with patch.object(sources, 'find', return_value=([], ['ThemerrDB: unavailable right now.'])) as fake:
    lookup()
    check('An old negative result without cache metadata is not trusted after upgrade', fake.call_count == 1)
    check('A provider outage gets a short cooldown, not a week',
          0 < meta()['expires'] - time.time() <= sources.WAIT_TTL)
    lookup()
    check('Repeated page visits do not hammer an unavailable provider', fake.call_count == 1)
with patch.object(sources, 'find', return_value=([proposal], [])) as fake:
    expire()
    check('When the cooldown expires, a provider is checked again and can recover', lookup()[0] == [proposal] and fake.call_count == 1)
    check('A successful lookup retains the week-long cache',
          meta()['expires'] - time.time() > 6 * 86400)
    lookup()
    check('Successful suggestions are cached', fake.call_count == 1)
    signature = meta()['signature']
    sources.save_settings(app.CONFIG, {'plex': True, 'themerrdb': False, 'tmdb_search': True})
    lookup()
    check('Changing enabled sources invalidates the cached suggestion', fake.call_count == 2 and meta()['signature'] != signature)
    with patch.object(app.poster_connect, 'effective_values', return_value=('new-provider-key', '', '', 'tmdb')):
        lookup()
        check('Changing poster-provider credentials invalidates title-source lookups', fake.call_count == 3)
        with app.db() as c:
            check('Raw provider keys are not stored in cache metadata',
                  not any('new-provider-key' in str(tuple(r)) for r in c.execute('SELECT * FROM theme_suggestion_meta')))

with patch.object(sources, 'find', return_value=([], ['No matching theme'])) as fake:
    lookup(folder=two)
    check('A genuine negative result has its own finite TTL', 0 < meta(two)['expires'] - time.time() <= sources.NEGATIVE_TTL)
    lookup()  # return to the unchanged configuration after the temporary credential test
    expire(two)
    check('Background search can revisit expired suggestions', youtube_search._next_title(app)['path'] == str(two))

with patch.object(youtube_search, 'settings', return_value={'enabled': False, 'api_key': '', 'budget': 80, 'background': False}), \
     patch.object(youtube_search, '_cached', side_effect=AssertionError('disabled sources must not use cached results')):
    check('Disabled YouTube search shows no stale cached suggestions', youtube_search.suggest(app, str(one), one.name, 'Shows') == ([], ''))

check('Suggestion key is stable but changes with the selected track',
      sources.proposal_key(proposal) == sources.proposal_key(dict(reversed(list(proposal.items()))))
      and sources.proposal_key(proposal) != sources.proposal_key(dict(proposal, url='https://tvthemes.plexapp.com/54321.mp3')))

import ratelimit
with patch.object(ratelimit, 'wait'), patch.object(ratelimit, 'backoff') as backoff, \
     patch.object(sources.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError(
         sources.THEMERR.format(kind='movies', id=123), 429, 'Slow down', {'Retry-After': '120'}, None)):
    try:
        sources._get(sources.THEMERR.format(kind='movies', id=123), 1000)
    except urllib.error.HTTPError as error:
        check('ThemerrDB HTTP error is propagated', error.code == 429)
    else:
        check('ThemerrDB HTTP error is propagated', False)
    check('ThemerrDB honours provider rate-limit backoff', bool(backoff.call_args) and backoff.call_args.args == ('themerrdb', '120'))

print(f'{sum(checks)}/{len(checks)} passed')
sys.exit(0 if all(checks) else 1)
