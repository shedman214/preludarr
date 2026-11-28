# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only Plex/Jellyfin/Emby discovery, conservative links and post-install refresh."""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from unittest.mock import patch

work = Path(tempfile.mkdtemp())
anime, movie = work / 'anime', work / 'movies'
names = [anime / n for n in ('Exact', 'IDs [tvdbid-123]', 'Conflict [tvdbid-456]', 'Duplicate A [tvdbid-789]', 'Duplicate B [tvdbid-789]')]
film = movie / 'Film [tmdbid-123]'
for f in names + [film]:
    f.mkdir(parents=True)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(anime), MOVIE_ROOT=str(movie),
                  AUTO_DOWNLOAD='false', POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import app
import media_mapping as mm
import media_servers as ms

results = []


def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


app.discover()
app.auto_themes.init(app)
exact, by_id, conflict, dup_a, dup_b = (str(p) for p in names)
plex = {'kind': 'plex', 'url': 'http://192.168.1.10:32400', 'api_key': 'fixture-' + 'token',
        'enabled': True, 'mappings': {exact: '999'}, 'library_mapping': {'libraries': ['1', '2'],
        'paths': [{'remote': '/server/tv', 'local': str(anime)}, {'remote': '/server/films', 'local': str(movie)}],
        'automatic': True}}
jelly = {'kind': 'jellyfin', 'url': 'http://192.168.1.11:8096', 'api_key': 'fixture-' + 'jf',
         'enabled': False, 'mappings': {}, 'library_mapping': {'libraries': ['lib-a'], 'paths': [{'remote': 'G:\\TV', 'local': str(anime)}], 'automatic': False}}
emby = {'kind': 'emby', 'url': 'http://192.168.1.12:8096', 'api_key': 'fixture-' + 'emby',
        'mapping_refresh': True, 'mappings': {exact: 'emby-item'}}
plex_extra = {**plex, 'url': 'http://192.168.1.13:32400', 'mappings': {exact: '444'}, 'mapping_refresh': False}
ms._write(app.CONFIG, 'servers', {'plex': plex, 'jellyfin': jelly, 'emby-2': emby, 'plex-2': plex_extra})
check('Independent multiple server connections are recognized', set(mm.connections(app.CONFIG)) == {'plex', 'jellyfin', 'emby-2', 'plex-2'}
      and not mm.refresh_enabled('plex-2', plex_extra) and mm.refresh_enabled('emby-2', emby))

p_rows = [
    {'ratingKey': '11', 'type': 'show', 'Location': [{'path': '/server/tv/Exact'}]},
    {'ratingKey': '12', 'type': 'show', 'Guid': [{'id': 'tvdb://123'}]},
    {'ratingKey': '13', 'type': 'show', 'Location': [{'path': '/server/tv/Conflict [tvdbid-456]'}], 'Guid': [{'id': 'tvdb://999'}]},
    {'ratingKey': '14', 'type': 'show', 'Guid': [{'id': 'tvdb://789'}]},
]
film_data = {'ratingKey': '21', 'type': 'movie', 'Media': [{'Part': [{'file': '/server/films/Film [tmdbid-123]/Film.mkv'}]}],
             'Guid': [{'id': 'tmdb://123'}]}
calls = []


def fake(server, method, path, **kw):
    calls.append((server['kind'], method, path))
    assert method == 'GET'
    parsed = urllib.parse.urlsplit(path)
    q = urllib.parse.parse_qs(parsed.query)
    if server['kind'] == 'plex':
        if parsed.path == '/library/sections':
            return json.dumps({'MediaContainer': {'Directory': [
                {'key': '1', 'title': 'Anime', 'type': 'show', 'Location': [{'path': '/server/tv'}]},
                {'key': '2', 'title': 'Films', 'type': 'movie', 'Location': [{'path': '/server/films'}]},
                {'key': '3', 'title': 'Music', 'type': 'artist'}]}}).encode()
        rows = p_rows if parsed.path == '/library/sections/1/all' else [film_data]
        start = int(q['X-Plex-Container-Start'][0])
        return json.dumps({'MediaContainer': {'Metadata': rows[start:start+2], 'totalSize': len(rows)}}).encode()
    if parsed.path.endswith('/Library/VirtualFolders'):
        return json.dumps([{'Name': 'Anime', 'ItemId': 'lib-a', 'CollectionType': 'tvshows', 'Locations': ['G:\\TV']}]).encode()
    rows = [{'Id': 'jf-show', 'Type': 'Series', 'Path': 'G:\\TV\\Exact'},
            {'Id': 'jf-id', 'Type': 'Series', 'ProviderIds': {'Tvdb': '123'}}]
    start = int(q['StartIndex'][0])
    return json.dumps({'Items': rows[start:start+1], 'TotalRecordCount': len(rows)}).encode()


with patch.object(ms, '_call', side_effect=fake), patch.object(mm, 'PAGE_SIZE', 2):
    found = mm.discover(plex)
    check('Plex library discovery lists only movie and TV libraries', {x['id'] for x in found} == {'1', '2'} and found[0]['paths'] == ['/server/tv'])
    result = mm.rebuild(app, 'plex')
    check('Plex mappings cover exact folders and verified IDs', '3 of 6' in result and mm.lookup(app.CONFIG, 'plex', plex, exact) == '11'
          and mm.lookup(app.CONFIG, 'plex', plex, by_id) == '12' and mm.lookup(app.CONFIG, 'plex', plex, str(film)) == '21')
    check('Conflicting or ambiguous provider evidence is never linked', mm.lookup(app.CONFIG, 'plex', plex, conflict) is None
          and mm.lookup(app.CONFIG, 'plex', plex, dup_a) is None and mm.lookup(app.CONFIG, 'plex', plex, dup_b) is None)
    check('Coverage is owned by that exact connection', mm.coverage(app.CONFIG, 'plex', plex) == 3
          and mm.lookup(app.CONFIG, 'plex', {**plex, 'url': 'http://192.168.1.19:32400'}, exact) is None)
    check('Jellyfin discovery and paged Windows path mapping work', mm.discover(jelly)[0]['paths'] == ['G:\\TV']
          and '2 of 5' in mm.rebuild(app, 'jellyfin') and mm.lookup(app.CONFIG, 'jellyfin', jelly, exact) == 'jf-show'
          and mm.lookup(app.CONFIG, 'jellyfin', jelly, by_id) == 'jf-id')
    check('All mapping operations are read-only', all(x[1] == 'GET' for x in calls))
xml = b'<MediaContainer size="1"><Directory key="7" title="TV" type="show"><Location path="/tv"/></Directory></MediaContainer>'
with patch.object(ms, '_call', return_value=xml):
    check('Plex XML discovery supported as well as JSON', mm.discover(plex) == [{'id': '7', 'name': 'TV', 'type': 'tv', 'paths': ['/tv']}])
with patch.object(ms, '_call', return_value=b'<!DOCTYPE MediaContainer><MediaContainer/>'):
    failed = False
    try:
        mm.discover(plex)
    except ValueError:
        failed = True
    check('Unexpected XML document declarations rejected', failed)
with patch.object(ms, '_call', return_value=b'{"MediaContainer":{"Directory":[]}}'):
    failed = False
    try:
        mm.rebuild(app, 'plex')
    except ValueError:
        failed = True
    check('Incomplete mapping does not replace the last valid snapshot', failed and mm.lookup(app.CONFIG, 'plex', plex, by_id) == '12')

refreshed = []
finished = threading.Event()


def refresh(server, item):
    refreshed.append((server['kind'], item))
    if len(refreshed) == 2:
        finished.set()


with patch.object(ms, '_refresh_one', side_effect=refresh):
    ms.after_install(app.CONFIG, exact)
    finished.wait(3)
    check('Manual mapping wins and only individually opted-in instances refresh', set(refreshed) == {('plex', '999'), ('emby', 'emby-item')})
    refreshed.clear()
    finished.clear()
    ms.after_install(app.CONFIG, by_id)
    for _ in range(40):
        if refreshed:
            break
        time.sleep(.05)
    check('Automatic mapping enables a targeted refresh, not a library scan', refreshed == [('plex', '12')])
with app.db() as conn:
    conn.execute('INSERT INTO media_ids(path,tvdb,source) VALUES(?,?,?)', (by_id, 321, 'sonarr'))
check('Changed local provider identity invalidates a cached automatic link', mm.lookup(app.CONFIG, 'plex', plex, by_id) is None)
with app.db() as conn:
    conn.execute('UPDATE media_server_links SET checked=0')
check('Expired automatic links cannot refresh', mm.lookup(app.CONFIG, 'plex', plex, exact) is None)
print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
