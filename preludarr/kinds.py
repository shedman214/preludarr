# SPDX-License-Identifier: GPL-3.0-or-later
"""Root-folder types (Preludarr 1.11, user 2026-10-29: "we should have anime, anime movies, movies and shows. that way splicarr
knows which download source to use for the folders").

Every title stores its root folder's type in shows.library, so the type alone decides the sources:
  Anime TV                          AnimeThemes (+ AniDB titles) -> Plex TV Themes -> ThemerrDB tv     (unchanged)
  Anime Movies                      AnimeThemes -> ThemerrDB movies                                    (unchanged)
  Shows                             Plex TV Themes -> ThemerrDB tv -> optional YouTube search (preview only)
  Movies                            ThemerrDB movies (TMDB, else IMDb) -> optional YouTube search (preview only)
The stored values are unchanged for existing installs (no database migration).
"""
TYPES = ('Anime TV', 'Anime Movies', 'Shows', 'Movies')
LABELS = {'Anime TV': 'Anime TV', 'Anime Movies': 'Anime Movies', 'Shows': 'Shows', 'Movies': 'Movies'}
ICONS = {'Anime TV': '🍜', 'Anime Movies': '🍥', 'Shows': '📡', 'Movies': '🎞️'}  # user 2026-10-30: anime types get anime emojis
# Library tab key, stored type (order = tab order). User 2026-10-29: one sidebar entry "Library" (Collections under it) and
# inside the Library page a tab per type: Anime TV | Anime Movies | Shows | Movies.
NAV = (('series', 'Anime TV'), ('movies', 'Anime Movies'), ('shows', 'Shows'), ('films', 'Movies'))


def valid(kind):
    return kind in TYPES


def label(kind):
    return LABELS.get(kind, str(kind or ''))


def is_movie(kind):
    return kind in ('Anime Movies', 'Movies')


def is_anime(kind):
    return kind in ('Anime TV', 'Anime Movies')


def is_regular(kind):
    return kind in ('Shows', 'Movies')


def present(roots):
    """Types that have at least one root folder, in sidebar order."""
    have = {kind for kind, _ in roots}
    return [kind for kind in TYPES if kind in have]
