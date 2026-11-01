# SPDX-License-Identifier: GPL-3.0-or-later
"""Remembered Library filters (1.12.2, user 2026-11-01: "first time install defaults to all statuses/all posters, but if the
user changes it, it stays ... until the user changes it themselves or they fresh install again").

Kept in /config, so every browser sees the same choice and a fresh install (new /config) starts at All / All. Only a filter
the user picks in the Library's filter form (Search or Clear) is remembered; links that open the Library with a filter for
that one view (e.g. the sidebar's Missing Themes) apply it without changing what is remembered.
"""
import json
import os
from pathlib import Path

FILE = 'preludarr_library_filters.json'
STATES = ('all', 'recent', 'missing', 'installed', 'review', 'searching', 'downloading', 'converting')
POSTERS = ('all', 'missing', 'cached', 'failed')
DEFAULT = {'state': 'all', 'poster': 'all'}


def load(config):
    try:
        p = Path(config) / FILE
        data = {} if p.is_symlink() else json.loads(p.read_text())
    except (OSError, ValueError):
        data = {}
    return {'state': data.get('state') if data.get('state') in STATES else 'all',
            'poster': data.get('poster') if data.get('poster') in POSTERS else 'all'}


def save(config, state, poster):
    state = state if state in STATES else 'all'
    poster = poster if poster in POSTERS else 'all'
    if load(config) == {'state': state, 'poster': poster}:
        return
    p = Path(config) / FILE
    tmp = p.with_name('.' + FILE + '.part')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'state': state, 'poster': poster}, f)
    os.replace(tmp, p)


def resolve(config, query):
    """(state, poster) for this Library view. `f=1` marks the Library's own filter form: its values are remembered.
    Without f, a value in the address applies to this view only; a missing value uses the remembered one."""
    saved = load(config)
    state = query.get('state', [''])[0] or saved['state']
    poster = query.get('poster', [''])[0] or saved['poster']
    state = state if state in STATES else 'all'
    poster = poster if poster in POSTERS else 'all'
    if query.get('f', [''])[0] == '1':
        save(config, state, poster)
    return state, poster


# 1.12.2 (user 2026-11-01): Dashboard Activity — "the ability to filter the amount they want to see" (page size), remembered.
ACTIVITY_SIZES = (10, 25, 50, 100, 250)
ACTIVITY_DEFAULT = 25
ACTIVITY_FILE = 'preludarr_dashboard_activity.json'


def activity_size(config, query=None):
    """Events per page on the Dashboard: `asize` from the Dashboard's own form is remembered; otherwise the saved size."""
    p = Path(config) / ACTIVITY_FILE
    try:
        saved = int(({} if p.is_symlink() else json.loads(p.read_text())).get('size'))
    except (OSError, ValueError, TypeError):
        saved = ACTIVITY_DEFAULT
    saved = saved if saved in ACTIVITY_SIZES else ACTIVITY_DEFAULT
    raw = (query or {}).get('asize', [''])[0]
    if raw.isdigit() and int(raw) in ACTIVITY_SIZES and int(raw) != saved:
        tmp = p.with_name('.' + ACTIVITY_FILE + '.part')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'size': int(raw)}, f)
        os.replace(tmp, p)
        saved = int(raw)
    return saved
