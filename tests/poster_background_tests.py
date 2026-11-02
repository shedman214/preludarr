# SPDX-License-Identifier: GPL-3.0-or-later
"""First-install defaults, persistent queue and protections; no external requests."""
import hashlib
import contextlib
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import poster_background as bg

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)

with tempfile.TemporaryDirectory() as temp:
    work = Path(temp)
    root = work / 'library'; root.mkdir()
    cache = work / 'cache'; cache.mkdir()
    database = work / 'queue.db'
    @contextlib.contextmanager
    def db():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()
    app = SimpleNamespace(CONFIG=work, EXISTING_INSTALL=False, ROOTS=[('Movies', root)],
                          POSTER_BACKGROUND_ENABLED=True, provider=False, paused=False)
    app.db = db
    def init():
        with db() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS poster_v2_control(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    app.poster_v2_init = init
    app.poster_v2_paused = lambda: app.paused
    app.poster_connect = SimpleNamespace(has_provider=lambda config: app.provider)
    app.poster_file = lambda path: cache / (hashlib.sha256(path.encode()).hexdigest() + '.jpg')
    check('Fresh install enables automatic posters', bg.enabled(app, {}))
    app.EXISTING_INSTALL = True
    check('Default survives first restart', bg.enabled(app, {}))
    check('Explicit false or 0 disables', not bg.enabled(app, {'POSTER_BACKGROUND_ENABLED':'false'}) and not bg.enabled(app, {'POSTER_BACKGROUND_ENABLED':'0'}))
    check('Explicit true or 1 enables', bg.enabled(app, {'POSTER_BACKGROUND_ENABLED':'true'}) and bg.enabled(app, {'POSTER_BACKGROUND_ENABLED':'1'}))
    check('Auto retains first-install choice', bg.enabled(app, {'POSTER_BACKGROUND_ENABLED':'auto'}))
    check('Unknown setting fails closed', not bg.enabled(app, {'POSTER_BACKGROUND_ENABLED':'invalid'}))
    with db() as conn:
        conn.execute('DELETE FROM poster_v2_control')
    check('Existing install stays off by default', not bg.enabled(app, {}))
    app.EXISTING_INSTALL = False
    check('Existing choice is not overwritten', not bg.enabled(app, {}))
    with db() as conn:
        conn.execute('CREATE TABLE shows(path TEXT PRIMARY KEY, library TEXT,name TEXT)')
        conn.execute('CREATE TABLE posters(path TEXT PRIMARY KEY,status TEXT,checked INTEGER,provider TEXT)')
        for name in ('Missing', 'Cached', 'Failed', 'Queued'):
            folder = root / name; folder.mkdir()
            conn.execute('INSERT INTO shows VALUES(?,?,?)', (str(folder),'Movies',name))
        conn.execute('INSERT INTO shows VALUES(?,?,?)', (str(work / 'removed-root' / 'Title'),'Movies','Stale'))
        conn.execute('INSERT INTO posters VALUES(?,?,?,?)', (str(root/'Failed'),'error',12345,'TMDB'))
        conn.execute('INSERT INTO posters VALUES(?,?,?,?)', (str(root/'Queued'),'queued',0,''))
    app.poster_file(str(root/'Cached')).write_bytes(b'existing-poster')
    check('Roots alone do not queue before credentials', bg.enqueue_missing(app) == 0)
    app.provider = True
    app.paused = True
    check('Paused recovery does not autoqueue', bg.enqueue_missing(app) == 0)
    app.paused = False
    app.POSTER_BACKGROUND_ENABLED = False
    check('Explicitly disabled processing does not queue', bg.enqueue_missing(app) == 0)
    app.POSTER_BACKGROUND_ENABLED = True
    check('Credentials after roots queue missing posters', bg.enqueue_missing(app) == 1)
    check('Repeated scans do not duplicate or reset jobs', bg.enqueue_missing(app) == 0)
    with db() as conn:
        rows = {row['path']:dict(row) for row in conn.execute('SELECT * FROM posters')}
    check('Durable queued status and immediate eligibility', rows[str(root/'Missing')]['status'] == 'queued' and rows[str(root/'Missing')]['checked'] == 0)
    check('Previous failure cooldown retained', rows[str(root/'Failed')]['checked'] == 12345 and rows[str(root/'Failed')]['status'] == 'error')
    check('Cached poster unchanged, not queued', app.poster_file(str(root/'Cached')).read_bytes() == b'existing-poster' and str(root/'Cached') not in rows)
    check('Removed roots are not queued', str(work/'removed-root'/'Title') not in rows)
    added = root/'AfterCredentials'; added.mkdir()
    with db() as conn:
        conn.execute('INSERT INTO shows VALUES(?,?,?)', (str(added),'Movies',added.name))
    check('Roots discovered after credentials also queue', bg.enqueue_missing(app) == 1)
    check('No theme music installed or media files changed', not any(p.is_file() for p in root.rglob('*')))
    bg.wake()
    bg.wait(0)
    check('Idle wake signal is consumed', not bg._WAKE.is_set())
print('%s/%s passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
