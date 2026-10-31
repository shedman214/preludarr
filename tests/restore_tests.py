# SPDX-License-Identifier: GPL-3.0-or-later
"""Preludarr 1.12 task 6: restore on restart, portable Releases page, About credits, portable root defaults. Run in a Preludarr image, --network none."""
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
cfg = work / 'config'
os.environ.update(CONFIG_DIR=str(cfg), TV_ROOT=str(work / 'no-anime'), MOVIE_ROOT=str(work / 'no-movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import restore as R  # noqa: E402
import system as S  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


src = Path('/src')
# ------------------------------------------------------------------ portable defaults ----
check('New install without the old /anime and /movies folders starts with no root folders', app.ROOTS == []
      and json.loads((cfg / 'themearr_root_folders.json').read_text())['roots'] == [])
check('/media is a library volume by default', Path('/media') in __import__('root_folders').mounts())
check('No host restore engine in the app any more', not (src / 'preludarr' / 'themearr_system_restore_v155.py').exists() and not list(src.glob('**/*restore_v155*')))
texts = ''.join(p.read_text() for p in (src / 'preludarr').glob('*.py'))
check('No server-specific text left in the app', not any(x in texts for x in ('disk2', 'host watcher', 'host helper')))


def marker(value):
    with sqlite3.connect(cfg / 'library.db') as c:
        c.execute('CREATE TABLE IF NOT EXISTS t_marker(v TEXT)'); c.execute('DELETE FROM t_marker'); c.execute('INSERT INTO t_marker VALUES (?)', (value,))


def read_marker():
    with sqlite3.connect(cfg / 'library.db') as c:
        return c.execute('SELECT v FROM t_marker').fetchone()[0]


# ------------------------------------------------------------------ backup → change → schedule → restart ----
marker('at-backup'); (cfg / 'themearr_general.json').write_text('{"instance_name": "At backup"}')
name = S.create_backup(cfg)
info = R.verify_archive(cfg / 'system_backups' / name)
check('A new backup verifies (checksums + SQLite) and reports its SHA256', info['files'] >= 2 and len(info['sha256']) == 64)
marker('after-backup'); (cfg / 'themearr_general.json').write_text('{"instance_name": "Changed later"}')
(cfg / 'library.db-wal').write_bytes(b'')
try:
    R.schedule(cfg, name, '0' * 64)
    ok = False
except ValueError:
    ok = True
check('Scheduling refuses a checksum that does not match the verified file', ok and R.pending(cfg) is None)
R.schedule(cfg, name, info['sha256'])
check('Scheduling writes a private request and changes nothing yet', R.pending(cfg)['archive'] == name and read_marker() == 'after-backup'
      and oct((cfg / R.PENDING).stat().st_mode & 0o777) == '0o600')
page = S._backup_page(cfg)
check('Backup page says "Restart the container" and offers Cancel', 'Restart the Preludarr container' in page and '/system/backup/cancel' in page)
R.cancel(cfg)
check('Cancel removes the request; nothing restored', R.pending(cfg) is None and R.apply_pending(cfg) is None and read_marker() == 'after-backup')
R.schedule(cfg, name, info['sha256'])
log = io.StringIO()
res = R.apply_pending(cfg, log=lambda *a, **k: log.write(' '.join(a)))
check('On restart: database and settings come back from the backup', res['ok'] and read_marker() == 'at-backup'
      and json.loads((cfg / 'themearr_general.json').read_text())['instance_name'] == 'At backup', res)
safety = cfg / 'system_backups' / res['safety']
with tarfile.open(safety) as t:
    names = t.getnames()
    old_general = json.loads(t.extractfile('themearr_general.json').read())
check('A safety copy of the replaced data was saved first (0600)', 'library.db' in names and old_general['instance_name'] == 'Changed later'
      and oct(safety.stat().st_mode & 0o777) == '0o600')
check('Stale database journal removed; request cleared; result saved; logged', not (cfg / 'library.db-wal').exists() and R.pending(cfg) is None
      and R.last_result(cfg)['ok'] and 'Restored' in log.getvalue())
check('Backup page shows the result', 'Last restore' in S._backup_page(cfg) and '✓' in S._backup_page(cfg))

# ------------------------------------------------------------------ failures ----
marker('before-tamper')
R.schedule(cfg, name, info['sha256'])
with zipfile.ZipFile(cfg / 'system_backups' / name, 'a') as z:
    z.writestr('config/extra.json', '{}')
res = R.apply_pending(cfg, log=lambda *a, **k: None)
check('A backup changed after confirming is refused and nothing changes', not res['ok'] and 'nothing was changed' in res['message']
      and read_marker() == 'before-tamper' and R.pending(cfg) is None, res)

marker('before-bad-restore'); (cfg / 'themearr_general.json').write_text('{"instance_name": "Keep me"}')
name2 = S.create_backup(cfg); info2 = R.verify_archive(cfg / 'system_backups' / name2)
marker('current'); (cfg / 'themearr_general.json').write_text('{"instance_name": "Current"}')
R.schedule(cfg, name2, info2['sha256'])
real_sha = R._sha
R._sha = lambda p: '0' * 64 if Path(p).parent == cfg else real_sha(p)   # the check after the swap fails
res = R.apply_pending(cfg, log=lambda *a, **k: None)
R._sha = real_sha
check('A failure after files were swapped puts the previous data back automatically', not res['ok'] and 'put back' in res['message']
      and read_marker() == 'current' and json.loads((cfg / 'themearr_general.json').read_text())['instance_name'] == 'Current', res)
check('…and the failure is shown on the Backup page', '✗' in S._backup_page(cfg))
check('No staging folders left behind', not [p for p in cfg.iterdir() if p.name.startswith('.preludarr-restore-') or p.name.startswith('.rollback-')])
check('app.py runs the restore before importing anything that opens data',
      (src / 'preludarr' / 'app.py').read_text().index("apply_pending(") < (src / 'preludarr' / 'app.py').read_text().index('import custom_upload'))

# ------------------------------------------------------------------ Releases + About ----
page = S._body('releases', cfg, {})
check('Releases: current version, what\'s new from CHANGELOG, update/rollback steps', 'Current release' in page and 'What’s new' in page
      and '1.11.9' in page and 'Updating and going back' in page and 'id="updates"' in page)
check('Releases: no image tags or host audit panels', 'splicarr:1.11' not in page and '-local' not in page and 'audit' not in page.lower())
ov = S._body('overview', cfg, {})
check('Overview: About with GPL-3.0-or-later and the TMDB attribution', 'GPL-3.0-or-later' in ov
      and 'This product uses the TMDB API but is not endorsed or certified by TMDB.' in ov)


class RH:
    def __init__(self):
        self.status = None; self.h = {}

    def send_response(self, c):
        self.status = c

    def send_header(self, k, v):
        self.h[k] = v

    def end_headers(self):
        pass


h = RH(); __import__('release_notes').route(h, cfg)
check('Old /release-manager link opens System → Releases', h.status == 303 and h.h['Location'] == '/system?tab=releases')

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
