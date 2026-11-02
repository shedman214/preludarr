# SPDX-License-Identifier: GPL-3.0-or-later
"""First-install poster defaults and durable missing-poster queue integration."""
import os
import threading
from pathlib import Path

_WAKE = threading.Event()


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
    choice = environ.get('POSTER_BACKGROUND_ENABLED', 'auto').strip().lower()
    if choice in ('true', '1'):
        return True
    if choice not in ('auto', ''):
        return False
    return row['value'] == '1'


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
