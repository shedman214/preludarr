# SPDX-License-Identifier: GPL-3.0-or-later
"""Background poster processing switch (1.14.2): saved in-app choice, live start/stop, guidance; no external requests."""
import contextlib
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import poster_background as bg
import poster_filters

results = []


def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)


with tempfile.TemporaryDirectory() as temp:
    work = Path(temp)
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

    started = threading.Event()
    runs = []

    def worker():
        runs.append(1)
        started.set()

    app = SimpleNamespace(CONFIG=work, EXISTING_INSTALL=True, POSTER_BACKGROUND_ENABLED=False,
                          POSTER_BACKGROUND_DELAY=180, POSTER_BACKGROUND_LIMIT=20, background_poster_worker=worker)
    app.db = db

    def init():
        with db() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS poster_v2_control(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    app.poster_v2_init = init

    off_env = {'POSTER_BACKGROUND_ENABLED': 'false'}
    check('Docker false keeps an existing install off', not bg.enabled(app, off_env) and bg.source(app, off_env) == 'docker')
    check('Without a Docker value, an existing install uses its stored default (off)', not bg.enabled(app, {}) and bg.source(app, {}) == 'default')

    bg.set_enabled(app, True)
    check('Turning it on in the app applies immediately', app.POSTER_BACKGROUND_ENABLED is True)
    check('Turning it on starts the worker without a restart', started.wait(2) and runs == [1])
    check('The saved choice overrides Docker false', bg.enabled(app, off_env) and bg.source(app, off_env) == 'settings')
    bg.set_enabled(app, True)
    check('The worker is only ever started once', runs == [1] and not bg.ensure_worker(app))

    bg.set_enabled(app, False)
    check('Turning it off applies immediately and is remembered', app.POSTER_BACKGROUND_ENABLED is False
          and not bg.enabled(app, {'POSTER_BACKGROUND_ENABLED': 'true'}) and bg.source(app, {}) == 'settings')

    html_off = bg.switch_html(app)
    check('Poster Recovery shows the switch, its state and where it comes from',
          'Background poster processing: Off' in html_off and 'your saved setting' in html_off
          and 'action="/poster-v2-background"' in html_off and 'name="enabled" value="1"' in html_off
          and 'Turn on background posters' in html_off)
    check('It explains what it does, pause versus off, and that it overrides Docker',
          all(x in html_off for x in ('every 180 seconds', 'at most 20 per hour', 'never replaced', 'theme music is never installed',
                                      'Pause', 'turning it off', 'POSTER_BACKGROUND_ENABLED')))
    check('When off, it says queued posters wait', 'Posters you queue stay waiting until it is on' in html_off)
    bg.set_enabled(app, True)
    html_on = bg.switch_html(app)
    check('When on, the button turns it off', 'Background poster processing: On' in html_on and 'name="enabled" value="0"' in html_on
          and 'Turn off background posters' in html_on and 'stay waiting' not in html_on)

# ---- bulk review warns before queuing work that will not run ----
check('The bulk review knows where to send users', 'Activity → Poster recovery' in poster_filters.OFF_NOTE)
src = (Path(__file__).resolve().parents[1] / 'preludarr' / 'poster_filters.py').read_text()
check('The review reply carries the warning for the dialog and the plain page',
      '"background_off": off' in src and 'data.background_off' in src and "html.escape(OFF_NOTE)" in src)
app_src = (Path(__file__).resolve().parents[1] / 'preludarr' / 'app.py').read_text()
check('The app passes the live state to the review and exposes the switch route',
      'background_on=lambda:POSTER_BACKGROUND_ENABLED' in app_src and "self.path=='/poster-v2-background'" in app_src
      and 'poster_background.switch_html(sys.modules[__name__])' in app_src)
check('The worker re-checks the switch every round', 'if not POSTER_BACKGROUND_ENABLED:' in app_src
      and 'poster_background.ensure_worker(sys.modules[__name__])' in app_src)

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
