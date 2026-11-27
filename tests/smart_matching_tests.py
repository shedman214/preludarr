# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline Smart Matching V6 tests using disposable anime folders and provider fixtures."""
import contextlib
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

work = Path(tempfile.mkdtemp())
root, movies = work / 'anime', work / 'movies'
root.mkdir()
movies.mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(root), MOVIE_ROOT=str(movies),
                  AUTO_DOWNLOAD='false', POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import app
import smart_matching as matching
import animethemes_graphql as gql
import theme_queue

results = []


def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def track(kind, n, video):
    return {'type': kind, 'slug': f'{kind}{n}', 'sequence': n,
            'song': {'title': f'{kind} Song {n}'}, 'animethemeentries': [
                {'id': 1, 'videos': [{'id': video, 'audio': {'link': f'https://a.animethemes.moe/fixture{video}.ogg'}}]}]}


entry = {'slug': 'fixture_anime', 'name': 'Provider Romaji Title', 'year': 2022,
         'media_format': 'TV', 'animethemes': [track('OP', 1, 12), track('ED', 1, 13)]}
other = dict(entry, slug='other_anime', name='Different Anime')
movie = dict(entry, slug='fixture_movie', media_format='Movie')
entry_by_slug = {e['slug']: e for e in (entry, other, movie)}
by_id = {('AniDB', '100'): [entry], ('AniList', '200'): [entry],
         ('MyAnimeList', '300'): [entry], ('AniList', '201'): [other],
         ('AniDB', '101'): [entry, other],
         ('MyAnimeList', '301'): [movie]}
calls = []


def provider(url, data=None, fresh=False):
    calls.append(url)
    u = urllib.parse.urlsplit(url)
    if u.hostname != 'api.animethemes.moe':
        raise AssertionError('ID matching must not use the title provider')
    if u.path == '/anime':
        args = urllib.parse.parse_qs(u.query)
        return {'anime': by_id.get((args['filter[site]'][0], args['filter[external_id]'][0]), [])}
    return {'anime': entry_by_slug.get(u.path.rsplit('/', 1)[-1])}


def conflict(name, library='Anime TV'):
    try:
        matching.resolve(app, str(root / name), name, library)
    except matching.MatchConflict as error:
        return str(error)
    return ''


name = 'Local Title (2022) [anidbid-100] {AniList-200} [mal-300]'
folder = root / name
folder.mkdir()
conflicting = root / 'Conflicting (2022) [anidb-100] [anilist-201]'
conflicting.mkdir()
app.discover()
app.auto_themes.init(app)
check('Supported tags parse consistently and are stripped before extracting the year',
      matching.folder_ids(name) == {'anidb': 100, 'anilist': 200, 'mal': 300}
      and app.clean(name) == 'Local Title' and app.year(name) == 2022)
check('Repeated identical IDs are benign; MyAnimeList is a supported alias',
      matching.folder_ids('[mal-300] {myanimelistid=300} [anilist:200]') == {'mal': 300, 'anilist': 200})
check('TVDB/TMDB/IMDb are not treated as AnimeThemes IDs',
      matching.folder_ids('Title (2022) [tvdbid-5] [tmdb-12] {imdb-tt12345}') == {})
for provider_name, enum in (('AniDB', 'ANIDB'), ('AniList', 'ANILIST'), ('MyAnimeList', 'MAL')):
    url = 'https://api.animethemes.moe/anime?' + urllib.parse.urlencode({'filter[site]': provider_name, 'filter[external_id]': 100})
    query, variables, convert = gql.plan(url)
    check(f'{provider_name} uses the actual supported GraphQL resource enum',
          f'site: {enum}' in query and variables == {'id': [100]}
          and convert({'findAnimeByExternalSite': [{'slug': 'fixture', 'format': 'TV'}]})['anime'][0]['slug'] == 'fixture')
check('Unsupported resource and out-of-range IDs are not planned',
      gql.plan('https://api.animethemes.moe/anime?filter%5Bsite%5D=TMDB&filter%5Bexternal_id%5D=100') is None
      and gql.plan('https://api.animethemes.moe/anime?filter%5Bsite%5D=AniDB&filter%5Bexternal_id%5D=2147483648') is None)

with patch.object(app, 'request_json', side_effect=provider), \
     patch.object(app, 'anilist', side_effect=AssertionError('No legacy lookup for ID-tagged titles')), \
     patch.object(app, 'anime_theme', side_effect=AssertionError('No title guess when IDs are explicit')):
    show, method, evidence = matching.resolve(app, str(folder), name, 'Anime TV')
    check('A different local title identifies the same anime by three agreeing IDs',
          show == entry and method == 'id' and all(x in evidence for x in ('AniDB 100', 'AniList 200', 'MyAnimeList 300')))
    check('Only ID lookups and one detail request were needed', len(calls) == 4)
    preview = matching.resolve(app, str(folder), name, 'Anime TV', preview=True)
    check('Preview and automatic matching use the same explicit identity', preview[0]['slug'] == show['slug'])
    ev = app._match_evidence(str(folder), {'name': name, 'library': 'Anime TV'}, show, method, evidence)
    check('Evidence reports verified identity, not a fictional title similarity',
          'Verified ID' in ev and 'AniDB 100' in ev and 'rather than spelling' in ev)
    server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def browser(path, fragment=True):
        query = {'path': str(path)}
        if fragment:
            query['fragment'] = 'match'
        url = f'http://127.0.0.1:{server.server_address[1]}/themes?' + urllib.parse.urlencode(query)
        with urllib.request.build_opener().open(url, timeout=15) as response:
            return response.read().decode('utf-8')
    try:
        with patch.object(app.auth, 'get', return_value=True):
            page = browser(folder)
            conflict_page = browser(conflicting)
            title_page = browser(folder, fragment=False)
        check('Real Theme Browser shows the verified entry and separate profile control',
              'Verified ID' in page and 'name="video_id" value="12"' in page and 'id="title-theme-profile"' in title_page)
        check('Conflicting explicit identities show manual-review guidance and no guessed track',
              'different anime' in conflict_page and 'Manual matching' in conflict_page and 'name="video_id"' not in conflict_page)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    state, _, proposal, _ = theme_queue.propose(app, {'path': str(folder), 'name': name, 'library': 'Anime TV'})
    check('Queue proposes the same verified ID and video', state == 'proposed' and proposal['anime_slug'] == 'fixture_anime'
          and proposal['video_id'] == 12 and 'Verified folder ID' in proposal['how'])
    suggestion = app.unmapped.suggest(app, str(folder))
    check('Match / Import offers the verified identity without remembering or installing it',
          len(suggestion['candidates']) == 1 and suggestion['candidates'][0]['source'] == 'Verified folder ID'
          and not app.media_servers.has_override(app.CONFIG, str(folder)) and not (folder / 'theme.mp3').exists())
    check('Conflicting tags for one provider stop', 'Conflicting' in conflict('Local (2022) [anidb-100] {anidb-101}'))
    check('Different provider IDs identifying different entries stop', 'different anime' in conflict('Local (2022) [anidb-100] [anilist-201]'))
    check('Wrong year or movie/series type stops', 'expected year and library type' in conflict('Local (2021) [anidb-100]')
          and 'expected year and library type' in conflict('Local (2022) [mal-301]'))
    check('Ambiguous or missing provider ID stops', 'does not identify one' in conflict('Local (2022) [anidb-101]')
          and 'does not identify one' in conflict('Local (2022) [anidb-999]'))
    check('Invalid and out-of-range numeric IDs stop', 'Invalid' in conflict('Local (2022) [anilist-abc]')
          and 'Invalid' in conflict('Local (2022) [mal-2147483648]'))
    identified = matching.resolve(app, str(movies / 'Local (2022) [mal-301]'), 'Local (2022) [mal-301]', 'Anime Movies')
    check('Matching movie ID selects the movie, not a TV entry', identified[0]['slug'] == 'fixture_movie')
    calls.clear()
    with patch.object(app, 'library_health') as health, \
         patch.object(app, 'resolve_animethemes_track', return_value={'video_id': 12}) as verified, \
         patch.object(app, 'stage_verified_theme_audio', return_value=contextlib.nullcontext(work / 'fixture.mp3')), \
         patch.object(app, 'manual_install_mp3') as install:
        outcome = app.process(str(folder))
        check('Automatic ID match installs only the exact verified opening without replacing',
              outcome == 'Downloaded: OP Song 1' and verified.call_args.args == ('fixture_anime', '12')
              and install.call_args.kwargs['replace'] is False and health.preflight.called)
    with app.media_servers._LOCK:
        app.media_servers._write(app.CONFIG, 'matches', {str(folder): {'slug': 'other_anime', 'name': 'Confirmed'}})
    calls.clear()
    saved = matching.resolve(app, str(folder), name, 'Anime TV')
    check('Remembered user choice takes precedence over folder IDs', saved[0]['slug'] == 'other_anime' and saved[1] == 'confirmed'
          and len(calls) == 1)
    with app.media_servers._LOCK:
        app.media_servers._write(app.CONFIG, 'matches', {})

with patch.object(app, 'request_json', side_effect=urllib.error.HTTPError('https://api.animethemes.moe/anime', 429, 'Wait', {'Retry-After': '120'}, None)):
    try:
        matching.resolve(app, str(folder), name, 'Anime TV')
    except app.theme_retry.RetryLater as error:
        check('Explicit ID provider rate-limit is classified as a retry', error.retry_after == 120)
    else:
        check('Explicit ID provider rate-limit is classified as a retry', False)

plain = 'Old Title (2022) [tvdb-10]'
with patch.object(app, 'anilist', return_value={'seasonYear': 2022, 'title': {'english': 'Old Title'}}) as title, \
     patch.object(app, 'anime_theme', return_value=entry) as lookup:
    check('Unsupported tags retain the original automatic title matching route',
          matching.resolve(app, str(root / plain), plain, 'Anime TV')[1] == 'anilist' and title.called and lookup.called)
print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
