# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.13 — automatic backups (interval + retention). Run in a Themearr image, --network none."""
import io
import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import auto_backup as ab  # noqa: E402
import system as sysv  # noqa: E402
import ui as ui  # noqa: E402
sys.modules['app'] = app
cfg = app.CONFIG
app.discover()  # creates library.db, as a running Themearr always has
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


d = ab.load(cfg)
check('Defaults like Sonarr: on, every 7 days, keep 28', (d['enabled'], d['interval_days'], d['retention_days']) == (True, 7, 28) and d['auto'] == [])
for bad in ((True, 0, 28), (True, 31, 40), (True, 7, 0), (True, 7, 400), (True, 10, 5), (True, 'x', 7)):
    try:
        ab.validate(*bad); ok = False
    except ValueError:
        ok = True
    check('Rejects ' + repr(bad[1:]), ok)
check('Due on first run, not right after a backup', ab.due(d) and not ab.due(dict(d, last_auto=time.time())))
check('Not due when off', not ab.due(dict(d, enabled=False)))

name = ab.run_once(app)
d = ab.load(cfg)
check('Automatic backup created and recorded', name and name in d['auto'] and (Path(cfg) / 'system_backups' / name).is_file() and d['last_error'] == '', d)
check('Settings file private (0600)', oct((Path(cfg) / ab.FILE).stat().st_mode & 0o777) == '0o600')
check('Nothing new while not due', ab.run_once(app) == '')
manual = sysv.create_backup(cfg)
old = time.time() - 40 * 86400
for n in (name, manual):
    os.utime(Path(cfg) / 'system_backups' / n, (old, old))
removed = ab.cleanup(cfg, sysv)
check('Retention removes old AUTOMATIC backups only', removed == [name] and not (Path(cfg) / 'system_backups' / name).exists()
      and (Path(cfg) / 'system_backups' / manual).exists(), removed)
check('Removed backup dropped from the automatic list', name not in ab.load(cfg)['auto'])

# limit reached → skipped, error shown, retry later (not every check)
real = sysv.create_backup
sysv.create_backup = lambda c: (_ for _ in ()).throw(ValueError('Backup limit (20) reached.'))
t0 = time.time()
check('Failure returns no name', ab.run_once(app, force=True) == '')
d = ab.load(cfg)
check('Failure remembered and retried in ~6 h', 'limit (20)' in d['last_error'] and 5 * 3600 < (ab.next_run(d) - t0) < 7 * 3600, d)
with app.db() as c:
    ev = c.execute("SELECT status,detail FROM events WHERE name='Automatic backup' ORDER BY id DESC LIMIT 1").fetchone()
check('Failure written to Activity', ev and ev['status'] == 'error' and 'limit' in ev['detail'])
sysv.create_backup = real
name2 = ab.run_once(app, force=True)
check('Next success clears the error', name2 and ab.load(cfg)['last_error'] == '')

# settings form through the real POST handler
class H:
    def __init__(self, path, fields):
        b = urllib.parse.urlencode(fields).encode()
        self.path = path; self.headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': str(len(b))}
        self.rfile = io.BytesIO(b); self.code = None; self.hdrs = []; self.wfile = io.BytesIO(); self.sent = None
    def send_response(self, c): self.code = c
    def send_header(self, k, v): self.hdrs.append((k, v))
    def end_headers(self): pass


h = H('/system/backup/schedule', {'csrf': sysv._new_token(), 'enabled': '1', 'interval_days': '3', 'retention_days': '14'})
sysv.post_route(h, cfg)
loc = dict(h.hdrs).get('Location', '')
d = ab.load(cfg)
check('Save via form: values stored, back to Backup tab with a message', h.code == 303 and 'tab=backup' in loc and 'every 3 days' in urllib.parse.unquote(loc)
      and (d['interval_days'], d['retention_days']) == (3, 14), (h.code, loc))
h = H('/system/backup/schedule', {'csrf': 'bogus', 'interval_days': '3', 'retention_days': '14'})
sysv.post_route(h, cfg)
check('Bad token refused, nothing changed', ab.load(cfg)['enabled'] is True)
h = H('/system/backup/schedule', {'csrf': sysv._new_token(), 'interval_days': '5', 'retention_days': '20'})
sysv.post_route(h, cfg)
check('Unticked box turns automatic backups off', ab.load(cfg)['enabled'] is False and ab.next_run(ab.load(cfg)) is None)

page = sysv._backup_page(cfg)
check('Backup page: Automatic backups card with form, folder, status', all(x in page for x in ('id="auto-backups"', 'action="/system/backup/schedule"', 'name="csrf"',
      '/config/system_backups', 'Automatic backups kept', 'Manual backups are never deleted automatically')))
check('Archive list marks Manual and Automatic', 'bk-kind auto">Automatic' in page and '>Manual</span>' in page)
check('Overview card mentions the schedule', 'Automatic backups off' in sysv._cards(cfg) or 'Automatic every' in sysv._cards(cfg))

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
