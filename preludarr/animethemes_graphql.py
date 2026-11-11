# SPDX-License-Identifier: GPL-3.0-or-later
"""AnimeThemes GraphQL (1.16.0): the same answers Preludarr read from the old JSON:API, from the API the
AnimeThemes website now uses.

AnimeThemes deprecated its JSON:API (api.animethemes.moe) and it started answering every request with
HTTP 503, while its website (GraphQL at graphql.animethemes.moe) and the audio/video servers kept
working. Every caller still asks for an api.animethemes.moe URL; plan(url) turns the four shapes
Preludarr uses into a GraphQL query, and the answer is converted back into the JSON:API shape the
callers already read. Matching rules, install checks and the audio hosts do not change.

  /anime?q=…&page[size]=N[&include=animesynonyms]          -> search(search:, first:)
  /anime?filter[site]=AniDB&filter[external_id]=N          -> findAnimeByExternalSite(site: ANIDB)
  /anime/{slug}?include=animethemes.…videos.audio,…song    -> anime(slug:) with themes, songs, files
  /anime/{slug}?include=images                             -> anime(slug:) with cover images
Any other URL returns None and is asked of the JSON:API as before.
"""
import re
import urllib.parse

ENDPOINT = 'https://graphql.animethemes.moe/'
_SLUG = re.compile(r'\A[a-z0-9_]{1,192}\Z')
MAX_SEARCH = 40

# the JSON:API's display values, which callers compare against
_FORMATS = {'TV': 'TV', 'TV_SHORT': 'TV Short', 'OVA': 'OVA', 'MOVIE': 'Movie', 'SPECIAL': 'Special', 'ONA': 'ONA'}
_FACETS = {'SMALL_COVER': 'Small Cover', 'LARGE_COVER': 'Large Cover', 'GRILL': 'Grill',
           'DOCUMENT': 'Document', 'AVATAR': 'Avatar', 'BANNER': 'Banner'}

_BASE = 'id slug year title { romaji english } format seasonLocalized'
_SYNONYMS = ' synonyms { text }'
_IMAGES = ' images { nodes { facet link } }'
_THEMES = (' animethemes { id type sequence slug'
           ' song { title { romaji native } performances { artist { name { main } } } }'
           ' animethemeentries { id version episodes nsfw spoiler'
           ' videos { nodes { id link basename resolution nc audio { id link basename } } } } }')


def _anime(a, synonyms=False, images=False, themes=False):
    """One GraphQL Anime as the JSON:API returned it."""
    if not isinstance(a, dict):
        return None
    title = a.get('title') or {}
    name = title.get('romaji') or a.get('slug') or ''
    out = {'id': a.get('id'), 'slug': a.get('slug'), 'name': name, 'year': a.get('year'),
           'season': a.get('seasonLocalized'), 'media_format': _FORMATS.get(a.get('format') or '', a.get('format') or '')}
    if synonyms:
        texts = [s.get('text') for s in a.get('synonyms') or [] if isinstance(s, dict) and s.get('text')]
        # the JSON:API listed the English title among the synonyms; GraphQL gives it separately
        english = title.get('english')
        if english and english.casefold() != name.casefold() and english not in texts:
            texts.append(english)
        out['animesynonyms'] = [{'text': t} for t in texts]
    if images:
        out['images'] = [{'facet': _FACETS.get(i.get('facet') or '', i.get('facet') or ''), 'link': i.get('link')}
                         for i in (a.get('images') or {}).get('nodes') or [] if isinstance(i, dict)]
    if themes:
        out['animethemes'] = [_theme(t) for t in a.get('animethemes') or [] if isinstance(t, dict)]
    return out


def _theme(t):
    song = t.get('song') or {}
    song_title = song.get('title') or {}
    artists, seen = [], set()
    for p in song.get('performances') or []:
        artist_name = (((p or {}).get('artist') or {}).get('name') or {}).get('main')
        if artist_name and artist_name not in seen:
            seen.add(artist_name)
            artists.append({'name': artist_name})
    return {
        'id': t.get('id'), 'type': t.get('type'), 'sequence': t.get('sequence'), 'slug': t.get('slug'),
        'song': {'title': song_title.get('romaji') or song_title.get('native'), 'artists': artists} if song else None,
        'animethemeentries': [{
            'id': e.get('id'), 'version': e.get('version'), 'episodes': e.get('episodes'),
            'nsfw': e.get('nsfw'), 'spoiler': e.get('spoiler'),
            'videos': [{
                'id': v.get('id'), 'link': v.get('link'), 'basename': v.get('basename'), 'filename': v.get('basename'),
                'resolution': v.get('resolution'), 'nc': v.get('nc'),
                'audio': {'id': (v.get('audio') or {}).get('id'), 'link': (v.get('audio') or {}).get('link'),
                          'basename': (v.get('audio') or {}).get('basename'),
                          'filename': (v.get('audio') or {}).get('basename')} if v.get('audio') else None,
            } for v in (e.get('videos') or {}).get('nodes') or [] if isinstance(v, dict)],
        } for e in t.get('animethemeentries') or [] if isinstance(e, dict)],
    }


def plan(url):
    """(query, variables, convert) for an api.animethemes.moe URL Preludarr uses, or None."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != 'api.animethemes.moe':
        return None
    query = urllib.parse.parse_qs(parts.query)
    first = lambda key: (query.get(key) or [''])[0]
    path = parts.path.rstrip('/')
    include = {x.strip() for x in first('include').split(',') if x.strip()}
    if path == '/anime':
        if first('filter[site]').casefold() == 'anidb' and first('filter[external_id]').isdigit():
            gql = ('query($id: [Int!]) { findAnimeByExternalSite(site: ANIDB, id: $id) { ' + _BASE + ' } }')
            return gql, {'id': [int(first('filter[external_id]'))]}, \
                lambda d: {'anime': [x for x in (_anime(a) for a in (d or {}).get('findAnimeByExternalSite') or []) if x]}
        text = first('q')
        if text and include <= {'animesynonyms'}:
            try:
                size = max(1, min(MAX_SEARCH, int(first('page[size]') or 20)))
            except ValueError:
                size = 20
            syn = 'animesynonyms' in include
            gql = ('query($q: String!, $n: Int) { search(search: $q, first: $n) { anime { ' + _BASE
                   + (_SYNONYMS if syn else '') + ' } } }')
            return gql, {'q': text, 'n': size}, \
                lambda d: {'anime': [x for x in (_anime(a, synonyms=syn) for a in ((d or {}).get('search') or {}).get('anime') or []) if x]}
        return None
    if path.startswith('/anime/'):
        slug = urllib.parse.unquote(path[len('/anime/'):])
        if not _SLUG.fullmatch(slug):
            return None
        if include == {'images'}:
            gql = 'query($s: String!) { anime(slug: $s) { ' + _BASE + _IMAGES + ' } }'
            return gql, {'s': slug}, lambda d: {'anime': _anime((d or {}).get('anime'), images=True)}
        if include and include <= {'animethemes.animethemeentries.videos.audio', 'animethemes.song', 'animethemes',
                                   'animethemes.animethemeentries', 'animethemes.animethemeentries.videos'}:
            gql = 'query($s: String!) { anime(slug: $s) { ' + _BASE + _SYNONYMS + _THEMES + ' } }'
            return gql, {'s': slug}, lambda d: {'anime': _anime((d or {}).get('anime'), synonyms=True, themes=True)}
    return None
