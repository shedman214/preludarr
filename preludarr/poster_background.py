# SPDX-License-Identifier: GPL-3.0-or-later
"""First-install poster defaults and durable missing-poster queue integration."""
import os
import threading
from pathlib import Path

_WAKE = threading.Event()
_WORKER = {'started': False}
_WORKER_LOCK = threading.Lock()
SAVED_KEY = 'background_enabled'   # 1.14.2: the choice saved in the app; overrides the Docker setting


def enabled(app, environ=None):
    """Remember the first-start default; explicit Docker choices always win.

    An upgrade must never silently start bulk processing on an existing install.
    Auto is persisted, so a fresh install stays enabled after its first restart.
    """
    environ = os.environ if environ is None else environ
    app.poster_v2_init()
    with app.db() as conn:
        conn.execute("INSERT OR IGNORE INTO poster_v2_control(key,value) VALUES('background_default',?)",
                     ('0' if app.EXISTING_INSTALL else '1',))
        row = conn.execute("SELECT value FROM poster_v2_control WHERE key='background_default'").fetchone()
        saved = conn.execute("SELECT value FROM poster_v2_control WHERE key=?", (SAVED_KEY,)).fetchone()
    if saved is not None and saved['value'] in ('0', '1'):
        return saved['value'] == '1'
    choice = environ.get('POSTER_BACKGROUND_ENABLED', 'auto').strip().lower()
    if choice in ('true', '1'):
        return True
    if choice not in ('auto', ''):
        return False
    return row['value'] == '1'


def source(app, environ=None):
    """Where the current on/off choice comes from: 'settings', 'docker' or 'default'."""
    environ = os.environ if environ is None else environ
    app.poster_v2_init()
    with app.db() as conn:
        saved = conn.execute("SELECT value FROM poster_v2_control WHERE key=?", (SAVED_KEY,)).fetchone()
    if saved is not None and saved['value'] in ('0', '1'):
        return 'settings'
    if environ.get('POSTER_BACKGROUND_ENABLED', 'auto').strip().lower() not in ('auto', ''):
        return 'docker'
    return 'default'


def set_enabled(app, value):
    """Save the in-app choice and apply it now: on starts the worker if needed, off stops new lookups."""
    app.poster_v2_init()
    with app.db() as conn:
        conn.execute("INSERT INTO poster_v2_control(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SAVED_KEY, '1' if value else '0'))
    app.POSTER_BACKGROUND_ENABLED = bool(value)
    if value:
        ensure_worker(app)
        wake()


def ensure_worker(app):
    """Start the single background poster worker thread once per process."""
    with _WORKER_LOCK:
        if _WORKER['started']:
            return False
        threading.Thread(target=app.background_poster_worker, daemon=True, name='preludarr-posters').start()
        _WORKER['started'] = True
        return True


def describe(app):
    """Plain explanation of the switch, shared by Poster Recovery and the bulk poster review."""
    return ('When background poster processing is on, Preludarr looks up missing posters with your TMDB or TVDB key '
            'and saves them: one lookup every %d seconds and at most %d per hour. Existing posters are never replaced, '
            'and theme music is never installed. Pause stops it for a while and keeps the setting; turning it off '
            'stops it until you turn it on again. Your choice here applies immediately and overrides the Docker setting '
            'POSTER_BACKGROUND_ENABLED.' % (app.POSTER_BACKGROUND_DELAY, app.POSTER_BACKGROUND_LIMIT))


def switch_html(app):
    import html as _html
    on = bool(app.POSTER_BACKGROUND_ENABLED)
    origin = {'settings': 'your saved setting', 'docker': 'the Docker setting POSTER_BACKGROUND_ENABLED',
              'default': 'the default for this install'}[source(app)]
    return ''.join([
        '<div id="poster-background-switch" style="border:1px solid #3e5174;border-radius:10px;padding:12px 14px;margin:12px 0">',
        '<p style="margin:0 0 6px"><strong>Background poster processing: ', 'On' if on else 'Off', '</strong> ',
        '<span class="muted">(from ', _html.escape(origin), ')</span></p>',
        '<p class="muted" style="margin:0 0 8px">', _html.escape(describe(app)), '</p>',
        '' if on else '<p style="margin:0 0 8px">Posters you queue stay waiting until it is on.</p>',
        '<form method="POST" action="/poster-v2-background" style="margin:0">',
        '<input type="hidden" name="enabled" value="', '0' if on else '1', '">',
        '<button type="submit">', 'Turn off background posters' if on else 'Turn on background posters', '</button></form></div>'])


def enqueue_missing(app):
    """Queue never-attempted posters only; retain existing attempts and files.

    This only writes the application's queue. The existing worker performs
    lookups and caching under its normal cooldown, concurrency and rate limits.
    """
    if (not app.POSTER_BACKGROUND_ENABLED or app.poster_v2_paused()
            or not app.poster_connect.has_provider(app.CONFIG)):
        return 0
    roots = {str(root) for _, root in app.ROOTS}
    with app.db() as conn:
        rows = conn.execute('SELECT s.path FROM shows s LEFT JOIN posters p ON p.path=s.path '
                            'WHERE p.path IS NULL ORDER BY s.path').fetchall()
        paths = [(row['path'],) for row in rows
                 if str(Path(row['path']).parent) in roots
                 and not app.poster_file(row['path']).is_file()]
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO posters(path,status,checked,provider) VALUES(?,'queued',0,'')", paths)
        return conn.total_changes - before


def wake():
    _WAKE.set()


def wait(seconds):
    """Wake idle workers after roots or credentials change, never rate-limit waits."""
    _WAKE.wait(seconds)
    _WAKE.clear()
