# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared anime identity selection: remembered match, verified ID, then title matching.

The legacy automatic and preview title strategies retain their different
confidence thresholds. Neither is used to silently overrule explicit IDs.
"""
import re
import urllib.error
import urllib.parse

import theme_retry

SITES = {'anidb': 'AniDB', 'anilist': 'AniList', 'mal': 'MyAnimeList'}
_TAG = re.compile(r'\[\s*(anidb|anilist|myanimelist|mal)(?:id)?\s*[-=:]\s*([^\]\s]+)\s*\]|\{\s*(anidb|anilist|myanimelist|mal)(?:id)?\s*[-=:]\s*([^}\s]+)\s*\}', re.I)
_SLUG = re.compile(r'[a-zA-Z0-9_-]{1,120}')
_DETAIL = 'animethemes.animethemeentries.videos.audio,animethemes.song'
_FORMATS = {'tv', 'tv short', 'ova', 'ona', 'special', 'movie'}
MAX_ID = 2147483647  # maximum GraphQL Int


class NoMatch(ValueError):
    """No sufficiently certain title match for automatic work."""


class MatchConflict(NoMatch):
    """Explicit identity evidence is invalid, ambiguous or contradictory."""

def has_explicit(name):
    return bool(_TAG.search(str(name)))


def folder_ids(name):
    """Collect explicit IDs, including duplicate/conflicting tags for the same site."""
    tags = {}
    for square_site, square_id, brace_site, brace_id in _TAG.findall(str(name)):
        site = (square_site or brace_site).lower()
        site = 'mal' if site == 'myanimelist' else site
        value = square_id or brace_id
        if not re.fullmatch(r'[0-9]{1,10}', value) or not 0 < int(value) <= MAX_ID:
            raise MatchConflict(f'Invalid {SITES[site]} ID in folder name.')
        tags.setdefault(site, set()).add(int(value))
    for site, values in tags.items():
        if len(values) != 1:
            raise MatchConflict(f'Conflicting {SITES[site]} IDs in the folder name.')
    return {site: next(iter(values)) for site, values in tags.items()}


def compatible(app, name, library, item):
    """Strong ID evidence must also agree with the known folder year and type."""
    year = app.year(name)
    if year and str(item.get('year')) != str(year):
        return False
    fmt = str(item.get('media_format') or '').casefold()
    return fmt in _FORMATS and (fmt == 'movie') == (library == 'Anime Movies')


def explicit(app, name, library):
    """Return a uniquely verified entry and ID evidence, or None if no ID tag exists."""
    ids = folder_ids(name)
    if not ids:
        return None
    chosen, evidence = [], []
    for site, value in sorted(ids.items()):
        label = SITES[site]
        evidence.append(f'{label} {value}')
        url = 'https://api.animethemes.moe/anime?' + urllib.parse.urlencode(
            {'filter[has]': 'resources', 'filter[site]': label, 'filter[external_id]': value})
        try:
            data = theme_retry.provider_call('AnimeThemes', app.request_json, url)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            raise MatchConflict(f'{label} {value} has no AnimeThemes entry yet.') from error
        matches = {str(item['slug']) for item in data.get('anime') or []
                   if isinstance(item, dict) and _SLUG.fullmatch(str(item.get('slug') or ''))
                   and compatible(app, name, library, item)}
        if len(matches) != 1:
            raise MatchConflict(f'{label} {value} does not identify one AnimeThemes entry with the expected year and library type.')
        chosen.append(next(iter(matches)))
    description = ', '.join(evidence)
    if len(set(chosen)) != 1:
        raise MatchConflict(f'Folder IDs identify different anime: {description}.')
    slug = chosen[0]
    params = urllib.parse.urlencode({'include': _DETAIL})
    url = f'https://api.animethemes.moe/anime/{slug}?{params}'
    try:
        detail = theme_retry.provider_call('AnimeThemes', app.request_json, url).get('anime')
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        raise MatchConflict('The identified AnimeThemes entry no longer exists.') from error
    if not isinstance(detail, dict) or detail.get('slug') != slug or not compatible(app, name, library, detail):
        raise MatchConflict('The identified AnimeThemes entry could not be verified with the expected year and library type.')
    return detail, 'id', description


def resolve(app, path, name, library, *, preview=False):
    """Return (entry, method, evidence). Explicit conflicts always stop automatic matching."""
    import media_servers
    if media_servers.has_override(app.CONFIG, path):
        if not media_servers.override(app.CONFIG, path):
            raise MatchConflict('The saved anime match is invalid. Review it or choose another entry.')
        show = theme_retry.provider_call('AnimeThemes', media_servers.resolve, app.CONFIG, path, name,
                                         {}, lambda *_: None, app.request_json, strict=True)
        if not show:
            raise MatchConflict('Your saved anime match could not be loaded from AnimeThemes right now')
        return show, 'confirmed', 'Your remembered match'
    result = explicit(app, name, library)
    if result:
        return result
    # Browsing is a non-mutating preview. Keep the existing folder-first lookup.
    if preview:
        show = app.anime_theme(name, {'title': {}, 'synonyms': []})
        if show:
            return show, 'folder', 'Folder title'
        try:
            media = app.anilist(name)
        except Exception as error:
            print(f'AniList preview lookup unavailable: {type(error).__name__}', flush=True)
            media = None
        return (app.anime_theme(name, media) if media else None), 'anilist', 'AniList title variants'
    # Automatic installs retain the exact original title and year checks.
    try:
        media = theme_retry.provider_call('AniList', app.anilist, name)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        media = None
    if not media:
        raise NoMatch('AniList did not identify the series')
    yr, ay = app.year(name), media.get('seasonYear')
    if yr and ay and yr != ay:
        raise NoMatch(f'AniList year mismatch: folder {yr}, result {ay}')
    show = theme_retry.provider_call('AnimeThemes', app.anime_theme, name, media)
    if not show:
        raise NoMatch('No exact alternative-title match on AnimeThemes')
    sy = show.get('year')
    if yr and sy and int(sy) != yr:
        raise NoMatch(f'AnimeThemes year mismatch: folder {yr}, result {sy}')
    return show, 'anilist', 'Original Matching (exact title + year)'
