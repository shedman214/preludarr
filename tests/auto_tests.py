# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for Automatic Themes + Recently added (V1.7.3). Run in a Themearr image with --network none."""
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

work = Path(tempfile.mkdtemp())
lib = work / 'lib'
for n in ('Old Missing A (2001)', 'Old Missing B (2002)', 'Old Installed (2003)'):
    (lib / 'anime' / n).mkdir(parents=True)
(lib / 'movies').mkdir(parents=True)
(lib / 'anime/Old Installed (2003)/theme.mp3').write_bytes(b'ID3' + b'\0' * 2000)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(lib / 'anime'), MOVIE_ROOT=str(lib / 'movies'),
                  AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import auto_themes as auto  # noqa: E402

results = []
def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail else ''), flush=True)

calls = []
app.process = lambda path: (calls.append(Path(path).name), 'recorded')[1]   # no network, no downloads
def run(download):
    calls.clear(); app.job(download=download)
    for _ in range(100):
        if not app.STATUS['running']: break
        time.sleep(0.05)
    return list(calls)

app.discover()                     # library as it was before V1.7.3
auto.init(app)
check('Default follows Docker setting: AUTO_DOWNLOAD=0 → Off', auto.settings()['mode'] == 'off')
base = {r['path']: (r['added'], r['baseline']) for r in app.db().execute('select * from show_added')}
check('Existing titles become the baseline (no guessed dates)', len(base) == 3 and all(v == (0, 1) for v in base.values()))
check('Off: automatic run installs nothing', run(True) == [])
recent_sql, p = auto.recent_clause()
check('Recently added is empty right after upgrade', app.db().execute('select count(*) from shows where ' + recent_sql, p).fetchone()[0] == 0)

class H:  # minimal form-post handler
    def __init__(self, body): self.headers = {'Content-Length': str(len(body))}; self.rfile = io.BytesIO(body.encode())
redirect = lambda h, url: url  # noqa: E731
bad = auto.post(app, H('mode=all&per_scan=0&interval_hours=12&recent_days=14'), redirect)
check('Invalid per-scan value rejected', 'Not+saved' in bad or 'Not saved' in bad, bad[:90])
auto.post(app, H('mode=new&per_scan=2&interval_hours=6&recent_days=7'), redirect)
s = auto.settings()
check('Saved: New titles only, 2 per scan, every 6 h, 7 days', (s['mode'], s['per_scan'], s['interval_hours'], s['recent_days']) == ('new', 2, 6, 7) and s['enabled_at'] > 0)
check('Settings file is private (0600)', oct((work / 'config' / auto.FILE).stat().st_mode & 0o777) == '0o600')
check('New titles only: the old missing titles are NOT touched', run(True) == [])
time.sleep(1.1)
for n in ('New One (2026)', 'New Two (2026)', 'New Three (2026)'):
    (lib / 'anime' / n).mkdir(); time.sleep(1.1); app.discover()
done = run(True)
check('New titles only: newest new titles first, limited to 2 per scan', done == ['New Three (2026)', 'New Two (2026)'], done)
rows = app.db().execute('select name from shows where ' + auto.recent_clause()[0] + ' order by name', auto.recent_clause()[1]).fetchall()
check('Recently added lists exactly the 3 new folders', [r[0] for r in rows] == ['New One (2026)', 'New Three (2026)', 'New Two (2026)'])
auto.post(app, H('mode=all&per_scan=10&interval_hours=6&recent_days=7'), redirect)
done = run(True)
check('All missing: new first, then the older missing titles; never the one with a theme', done[:3] == ['New Three (2026)', 'New Two (2026)', 'New One (2026)']
      and set(done[3:]) == {'Old Missing A (2001)', 'Old Missing B (2002)'} and 'Old Installed (2003)' not in done, done)
check('Turning New→All kept the original switch-on time', auto.settings()['enabled_at'] == s['enabled_at'])
check('Manual scan button never installs (download=False)', run(False) == [])
auto.post(app, H('mode=off&per_scan=10&interval_hours=6&recent_days=7'), redirect)
check('Off again: nothing installed', run(True) == [])
check('Scheduler interval follows the setting (6 h)', auto.interval_seconds() == 6 * 3600)
check('Summary text for the dashboard', auto.summary() == 'Automatic themes: Off · every 6 h · up to 10 per scan', auto.summary())
check('Existing theme.mp3 untouched', (lib / 'anime/Old Installed (2003)/theme.mp3').read_bytes() == b'ID3' + b'\0' * 2000)
print(f'{sum(results)}/{len(results)} passed'); sys.exit(0 if all(results) else 1)
