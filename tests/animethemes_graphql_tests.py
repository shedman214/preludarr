# SPDX-License-Identifier: GPL-3.0-or-later
"""1.16.0: AnimeThemes requests go to its GraphQL API and come back in the JSON:API shape callers read;
the deprecated JSON:API is only a fallback. Offline: GraphQL answers are canned."""
import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import animethemes
import animethemes_graphql as g

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)

API = 'https://api.animethemes.moe'
DETAIL = API + '/anime/example_show?include=animethemes.animethemeentries.videos.audio%2Canimethemes.song'

# --- plan: which requests have a GraphQL equivalent ---------------------------------------------
check('Search with synonyms is planned', g.plan(API + '/anime?q=Example&page%5Bsize%5D=40&include=animesynonyms') is not None)
check('Plain search is planned', g.plan(API + '/anime?q=Example&page%5Bsize%5D=20') is not None)
check('AniDB lookup is planned', g.plan(API + '/anime?filter%5Bhas%5D=resources&filter%5Bsite%5D=AniDB&filter%5Bexternal_id%5D=16063') is not None)
check('Detail with themes is planned', g.plan(DETAIL) is not None)
check('Images are planned', g.plan(API + '/anime/example_show?include=images') is not None)
check('Unknown requests stay on the JSON:API', g.plan(API + '/video?filter[x]=1') is None and g.plan(API + '/anime/Bad Slug!?include=images') is None)
check('Other hosts are never planned', g.plan('https://example.com/anime?q=x') is None)
q, v, _ = g.plan(API + '/anime?q=Example&page%5Bsize%5D=999')
check('Search size is capped at 40', v['n'] == 40 and v['q'] == 'Example')

# --- conversion: GraphQL data -> JSON:API shape ------------------------------------------------
gql_anime = {'id': 7, 'slug': 'example_show', 'year': 2022, 'title': {'romaji': 'Example Show', 'english': 'EXAMPLE'},
             'format': 'TV_SHORT', 'seasonLocalized': 'Fall', 'synonyms': [{'text': 'Ex Show'}],
             'images': {'nodes': [{'facet': 'LARGE_COVER', 'link': 'https://i/x.jpg'}]},
             'animethemes': [{'id': 1, 'type': 'OP', 'sequence': None, 'slug': 'OP1',
                              'song': {'title': {'romaji': 'Song', 'native': None},
                                       'performances': [{'artist': {'name': {'main': 'Band'}}},
                                                        {'artist': {'name': {'main': 'Band'}}}]},
                              'animethemeentries': [{'id': 9, 'version': 1, 'episodes': '1-3', 'nsfw': False, 'spoiler': False,
                                                     'videos': {'nodes': [{'id': 5, 'link': 'https://v.animethemes.moe/X.webm',
                                                                           'basename': 'X.webm', 'resolution': 720, 'nc': True,
                                                                           'audio': {'id': 3, 'link': 'https://a.animethemes.moe/X.ogg',
                                                                                     'basename': 'X.ogg'}}]}}]}]}
_, _, convert = g.plan(DETAIL)
a = convert({'anime': gql_anime})['anime']
check('Name, year, format and season match the JSON:API',
      (a['name'], a['year'], a['media_format'], a['season']) == ('Example Show', 2022, 'TV Short', 'Fall'))
check('English title is listed among synonyms', [s['text'] for s in a['animesynonyms']] == ['Ex Show', 'EXAMPLE'])
t = a['animethemes'][0]
check('Theme, song title and de-duplicated artists', (t['type'], t['slug'], t['song']['title'], t['song']['artists']) == ('OP', 'OP1', 'Song', [{'name': 'Band'}]))
vid = t['animethemeentries'][0]['videos'][0]
check('Video and audio links survive', vid['link'].endswith('X.webm') and vid['audio']['link'] == 'https://a.animethemes.moe/X.ogg')
_, _, conv_img = g.plan(API + '/anime/example_show?include=images')
check('Image facets use the JSON:API names', conv_img({'anime': gql_anime})['anime']['images'] == [{'facet': 'Large Cover', 'link': 'https://i/x.jpg'}])
_, _, conv_search = g.plan(API + '/anime?q=x&include=animesynonyms')
check('Search results convert', [x['slug'] for x in conv_search({'search': {'anime': [gql_anime]}})['anime']] == ['example_show'])
check('A missing anime converts to None', convert({'anime': None})['anime'] is None)

# --- get_json: GraphQL first, JSON:API as fallback ----------------------------------------------
class Answer(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
calls = []
def graphql_ok(req, timeout=0):
    calls.append(req.full_url)
    assert req.full_url == g.ENDPOINT and req.get_method() == 'POST'
    return Answer(json.dumps({'data': {'anime': gql_anime}}).encode())
with patch.object(animethemes.urllib.request, 'urlopen', side_effect=graphql_ok), patch.object(animethemes, '_pace'):
    got = animethemes.get_json(DETAIL, {'User-Agent': 'Preludarr'}, fresh=True)
check('get_json answers from GraphQL', got['anime']['slug'] == 'example_show' and calls == [g.ENDPOINT])
check('GraphQL success marks AnimeThemes online', animethemes.health()['state'] == 'ok')

calls.clear()
def graphql_down_jsonapi_ok(req, timeout=0):
    calls.append(req.full_url)
    if req.full_url == g.ENDPOINT:
        raise urllib.error.HTTPError(g.ENDPOINT, 503, 'Service Unavailable', {}, io.BytesIO(b''))
    return Answer(json.dumps({'anime': {'slug': 'from_jsonapi'}}).encode())
with patch.object(animethemes.urllib.request, 'urlopen', side_effect=graphql_down_jsonapi_ok), \
        patch.object(animethemes, '_pace'), patch.object(animethemes.time, 'sleep'):
    got = animethemes.get_json(DETAIL, {'User-Agent': 'Preludarr'}, fresh=True)
check('If GraphQL fails, the JSON:API is asked', got['anime']['slug'] == 'from_jsonapi' and calls[-1] == DETAIL)

calls.clear()
def refused(req, timeout=0):
    calls.append(req.full_url)
    if req.full_url == g.ENDPOINT:
        return Answer(json.dumps({'errors': [{'message': 'Cannot query field'}]}).encode())
    return Answer(json.dumps({'anime': {'slug': 'fallback'}}).encode())
with patch.object(animethemes.urllib.request, 'urlopen', side_effect=refused), patch.object(animethemes, '_pace'):
    got = animethemes.get_json(DETAIL, {'User-Agent': 'Preludarr'}, fresh=True)
check('A refused GraphQL query falls back too', got['anime']['slug'] == 'fallback')
print('%s/%s passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
