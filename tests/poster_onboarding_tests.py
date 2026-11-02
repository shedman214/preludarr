# SPDX-License-Identifier: GPL-3.0-or-later
"""Real discovery and background worker integration, with providers replaced offline."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
work = Path(tempfile.mkdtemp())
config = work/'config'; config.mkdir()
root = work/'library'; root.mkdir()
os.environ.update(CONFIG_DIR=str(config), TV_ROOT=str(root), MOVIE_ROOT=str(work/'absent'),
                  POSTER_BACKGROUND_ENABLED='auto', AUTO_DOWNLOAD='false')
import anidb
import app_logs
with patch.object(anidb, 'enabled', return_value=False), patch.object(app_logs, 'install', return_value=False):
    import app
import poster_background as bg

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)

app.POSTER_BACKGROUND_ENABLED = bg.enabled(app)
app.auto_themes.init(app)
for i in range(25):
    (root/('Title %02d' % i)).mkdir()
with patch.object(app.poster_connect, 'has_provider', return_value=False):
    check('Fresh scan discovers library without credentials', app.discover() == 25)
    with app.db() as conn:
        check('No poster jobs before credentials', conn.execute('SELECT COUNT(*) FROM posters').fetchone()[0] == 0)
check('Discovery signals idle worker', bg._WAKE.is_set())

class StopWorker(BaseException):
    pass
with patch.object(app.poster_connect, 'has_provider', return_value=False), patch.object(bg, 'wait', side_effect=StopWorker) as idle:
    try:
        app.background_poster_worker()
    except StopWorker:
        pass
    check('Worker idles without a provider', idle.call_args.args == (300,))

fetched = []
sleeps = []
def fetch(path, library, name):
    fetched.append((path, library, name))
    app.poster_file(path).write_bytes(b'offline-test-poster')
    with app.db() as conn:
        conn.execute("UPDATE posters SET status='found',checked=1000000 WHERE path=?", (path,))
    with app.POSTER_LOCK:
        app.POSTER_BUSY.discard(path)
def sleep(seconds):
    sleeps.append(seconds)
    if seconds == 60:
        raise StopWorker
with patch.object(app.poster_connect, 'has_provider', return_value=True), patch.object(app, 'fetch_poster', side_effect=fetch), patch.object(app.time, 'time', return_value=1000000), patch.object(app.time, 'sleep', side_effect=sleep):
    try:
        app.background_poster_worker()
    except StopWorker:
        pass
check('Adding provider after scan automatically processes posters', bool(fetched))
check('Hourly limit is enforced', len(fetched) == app.POSTER_BACKGROUND_LIMIT == 20)
check('Normal delay remains between attempts', sleeps[:-1] == [app.POSTER_BACKGROUND_DELAY]*20 and sleeps[-1] == 60)
with app.db() as conn:
    check('Remaining work stays durably queued', conn.execute("SELECT COUNT(*) FROM posters WHERE status='queued'").fetchone()[0] == 5)
check('Media folders and theme files untouched', not any(p.is_file() for p in root.rglob('*')))
app.POSTER_BACKGROUND_ENABLED = False
check('Poster recovery reports background disabled honestly', 'Background processing off' in app.poster_v2_panel())
check('Polling status preserves disabled state', app.poster_v2_snapshot()['enabled'] is False)
print('%s/%s passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
