# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only Plex/Jellyfin/Emby discovery, conservative links and post-install refresh."""
import io
import json
import os
import sys
import tempfile
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
import anidb
with patch.object(anidb, 'enabled', return_value=False):  # no AniDB title-list download
    import app
import media_mapping as mm
import arr
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
with app.db() as conn:
    before = [tuple(r) for r in conn.execute("SELECT * FROM media_server_links WHERE server_key='plex' ORDER BY path")]
    report = tuple(conn.execute("SELECT * FROM media_mapping_report WHERE server_key='plex'").fetchone())


def broken_second_page(server, method, path, **kw):
    if 'X-Plex-Container-Start=2' in path:
        raise OSError('connection reset')
    return fake(server, method, path, **kw)


for label, reply in (('a network failure', broken_second_page),
                     ('a page without its items', lambda s, m, p, **kw: b'{"MediaContainer":{"size":1}}' if 'X-Plex-Container-Start=2' in p else fake(s, m, p, **kw))):
    failed = False
    with patch.object(ms, '_call', side_effect=reply), patch.object(mm, 'PAGE_SIZE', 2):
        try:
            mm.rebuild(app, 'plex')
        except (OSError, ValueError):
            failed = True
    with app.db() as conn:
        after = [tuple(r) for r in conn.execute("SELECT * FROM media_server_links WHERE server_key='plex' ORDER BY path")]
        same_report = tuple(conn.execute("SELECT * FROM media_mapping_report WHERE server_key='plex'").fetchone()) == report
    check('After the first page, ' + label + ' keeps the last valid snapshot', failed and after == before and same_report)
with patch.object(ms, '_call', return_value=b'{"MediaContainer":{"size":0}}'):
    check('A genuinely empty Plex library page is accepted', list(mm.read_items(plex, {'id': '1', 'type': 'tv'}, lambda: None)) == [])
check('A trailing space in a server folder name is kept', mm._remote_path('/server/tv/Show ') == '/server/tv/Show '
      and mm._remote_path('/server/tv/Show') == '/server/tv/Show' and mm._remote_path('g:\\TV\\Show') == 'G:\\TV\\Show')
local_paths = {exact: {'type': 'tv', 'ids': {}, 'conflict': False}}
space_item = {'id': '99', 'type': 'tv', 'ids': {}, 'paths': ['/server/tv/Exact '], 'library': '1', 'conflict': False}
check('A real trailing space cannot link the similarly named folder',
      not mm.match_items(local_paths, [space_item], plex['library_mapping']['paths'])[0]
      and mm.match_items(local_paths, [{**space_item, 'paths': ['/server/tv/Exact']}], plex['library_mapping']['paths'])[0].get(exact, (None,))[0] == '99'
      and arr.to_local({'mappings': jelly['library_mapping']['paths']}, 'G:\\TV\\Exact ') == exact + ' ')
gone = {**plex, 'library_mapping': {**plex['library_mapping'], 'paths': [{'remote': '/server/tv', 'local': str(work / 'removed-root')}]}}
check('Mappings that only point at removed roots map nothing', mm._local_titles(app, gone, [{'type': 'tv'}]) == {}
      and len(mm._local_titles(app, {**plex, 'library_mapping': {'libraries': ['1']}}, [{'type': 'tv'}])) == 5)


class Form:
    def __init__(self, text):
        data = text.encode()
        self.headers, self.rfile = {'Content-Length': str(len(data))}, io.BytesIO(data)


def submit(text):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(mm.post(app, Form(text), lambda handler, url: url)).query)['notice'][0]


ms._write(app.CONFIG, 'servers', {**ms._read(app.CONFIG, 'servers'),
                                  'plex': {**plex, 'discovered_libraries': found}})
too_many = '\n'.join(f'/server/tv{n} => {anime}' for n in range(21))
before_save = ms._read(app.CONFIG, 'servers')['plex']['library_mapping']
check('More path mappings than can be saved are refused, not cut short',
      'could not finish' in submit(urllib.parse.urlencode({'server': 'plex', 'action': 'configure', 'library': '1', 'paths': too_many}))
      and ms._read(app.CONFIG, 'servers')['plex']['library_mapping'] == before_save)
ms._write(app.CONFIG, 'servers', {**ms._read(app.CONFIG, 'servers'), 'plex': plex})

refreshed = []


class Inline:
    """Runs the refresh worker before after_install returns, so no thread outlives the patch."""
    def __init__(self, target, **kw):
        self.target = target

    def start(self):
        self.target()


with patch.object(ms, '_refresh_one', side_effect=lambda server, item: refreshed.append((server['kind'], item))), \
        patch.object(ms.threading, 'Thread', Inline):
    ms._RECENT.clear()
    ms.after_install(app.CONFIG, exact)
    check('Manual mapping wins and only individually opted-in instances refresh', sorted(refreshed) == [('emby', 'emby-item'), ('plex', '999')])
    refreshed.clear()
    ms.after_install(app.CONFIG, by_id)
    check('Automatic mapping enables a targeted refresh, not a library scan', refreshed == [('plex', '12')])
    refreshed.clear()
    ms._RECENT.clear()
    live_connections = mm.connections
    off = lambda config: {k: ({**v, 'enabled': False} if k == 'plex' else v) for k, v in live_connections(config).items()}
    with patch.object(mm, 'connections', side_effect=[live_connections(app.CONFIG), off(app.CONFIG)]):
        ms.after_install(app.CONFIG, by_id)  # refresh switched off before the worker ran
    ms.after_install(app.CONFIG, by_id)
    check('A refresh skipped before sending does not block the next install', refreshed == [('plex', '12')])
    # A delayed worker that skips a request may only release its own reservation.
    pending = []
    class Delayed:
        def __init__(self, target, **kw):
            pending.append(target)
        def start(self):
            return None
    ms._RECENT.clear()
    with patch.object(ms.threading, 'Thread', Delayed):
        ms.after_install(app.CONFIG, by_id)
    reservation = next(iter(ms._RECENT))
    newer = ms._RECENT[reservation] + 121
    ms._RECENT[reservation] = newer
    with patch.object(mm, 'connections', return_value=off(app.CONFIG)):
        pending[0]()
    check('A skipped old worker cannot remove a newer refresh reservation', ms._RECENT.get(reservation) == newer)
    overview = ms.connect_overview(app.CONFIG)
    check('Connect shows manual and automatic mapping counts separately', '1 manual · 3 automatic mappings' in overview)
with app.db() as conn:
    conn.execute('INSERT INTO media_ids(path,tvdb,source) VALUES(?,?,?)', (by_id, 321, 'sonarr'))
check('Changed local provider identity invalidates a cached automatic link', mm.lookup(app.CONFIG, 'plex', plex, by_id) is None)
with app.db() as conn:
    conn.execute('UPDATE media_server_links SET checked=0')
check('Expired automatic links cannot refresh', mm.lookup(app.CONFIG, 'plex', plex, exact) is None)
print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
