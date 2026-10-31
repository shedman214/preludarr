# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.1 Permissions tests — run as root against a throwaway fixture (never real media).

Usage: python3 -I tests/permissions_tests.py <v191-dev dir> <empty scratch dir>
"""
import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import time
import types
from pathlib import Path

SRC, WORK = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
sys.path.insert(0, str(SRC))
import permissions as P  # noqa: E402

RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok)))
    print(('PASS ' if ok else 'FAIL ') + name + ((' — ' + detail) if detail and not ok else ''))


# ------------------------------------------------------------------ fixture ----
assert not any(WORK.iterdir()), 'scratch dir must be empty'
config = WORK / 'config'
anime = WORK / 'anime'
movies = WORK / 'movies'
for d in (config, anime, movies):
    d.mkdir()
dbfile = config / 'library.db'
con = sqlite3.connect(dbfile)
con.execute('CREATE TABLE shows(path TEXT PRIMARY KEY, library TEXT, name TEXT, state TEXT, detail TEXT, updated TEXT)')
con.execute('CREATE TABLE events(at TEXT, name TEXT, status TEXT, detail TEXT)')


def show(root, name, theme_mode=None, owner=(0, 0), extra=()):
    folder = root / name
    folder.mkdir()
    for f in extra:
        (folder / f).write_bytes(b'video')
        os.chmod(folder / f, 0o640)
    if theme_mode is not None:
        t = folder / 'theme.mp3'
        t.write_bytes(b'ID3' + b'x' * 2000)
        os.chmod(t, theme_mode)
        os.chown(t, *owner)
    con.execute('INSERT INTO shows VALUES(?,?,?,?,?,?)', (str(folder), 'Anime TV', name, 'installed', '', ''))
    return folder


a = show(anime, 'Root Owned (2009)', 0o644, (0, 0), extra=('ep1.mkv',))
b = show(anime, 'Already Right (2010)', 0o664, (99, 100))
c = show(anime, 'Wrong Mode (2011)', 0o777, (99, 100))
d = show(movies, 'Movie Root (2015)', 0o644, (0, 0))
e = show(anime, 'No Theme (2012)')
# symlinked theme.mp3 pointing at a file outside the library → must never be touched
outside = WORK / 'outside.mp3'
outside.write_bytes(b'x' * 100)
os.chmod(outside, 0o600)
f = anime / 'Symlink Theme (2013)'
f.mkdir()
(f / 'theme.mp3').symlink_to(outside)
con.execute('INSERT INTO shows VALUES(?,?,?,?,?,?)', (str(f), 'Anime TV', 'Symlink Theme', 'installed', '', ''))
# theme.mp3 in an unregistered folder → not touched
g = anime / 'Unregistered (2014)'
g.mkdir()
(g / 'theme.mp3').write_bytes(b'x' * 100)
os.chmod(g / 'theme.mp3', 0o600)
con.commit()


@contextlib.contextmanager
def _db():
    c2 = sqlite3.connect(dbfile)
    c2.row_factory = sqlite3.Row
    try:
        yield c2
        c2.commit()
    finally:
        c2.close()


def _event(name, status, detail):
    with _db() as c2:
        c2.execute('INSERT INTO events VALUES(?,?,?,?)', ('now', name, status, detail))


app = types.SimpleNamespace(CONFIG=config, ROOTS=[('Anime TV', anime), ('Anime Movies', movies)], db=_db, event=_event)


def snapshot():
    out = {}
    for p in sorted(WORK.rglob('*')):
        if p == config or p.name.startswith('library.db') or p == config / P.FILE or 'logs' in p.parts:
            continue
        st = os.lstat(p)
        out[str(p)] = (st.st_mode, st.st_uid, st.st_gid, st.st_ino, st.st_mtime_ns)
    return out


class H:
    def __init__(self, path, body=''):
        self.path = path
        data = body.encode()
        self.headers = {'Content-Length': str(len(data))}
        self.rfile = io.BytesIO(data)


def post(path, body=''):
    seen = {}
    P.post(app, H(path, body), lambda h, url: seen.setdefault('url', url))
    q = urllib_parse.parse_qs(urllib_parse.urlsplit(seen['url']).query)
    return q.get('notice', [''])[0]


import urllib.parse as urllib_parse  # noqa: E402

# ------------------------------------------------------------------- tests ----
s = P.settings(config)
check('defaults 775 / 99 / 100, off', s == {'enabled': False, 'folder_mode': '775', 'user': '99', 'group': '100'}, str(s))
(config / P.FILE).write_text(json.dumps({'enabled': True, 'folder_mode': '770', 'group': ''}))
s = P.settings(config)
check('V1.9.0 settings file (no user) still loads', s['enabled'] and s['folder_mode'] == '770' and s['user'] == '99' and s['group'] == '', str(s))
(config / P.FILE).unlink()
check('umask/file mode table', P.umask('777') == '0000' and P.file_mode('777') == '666' and P.umask('775') == '0002' and P.file_mode('755') == '644')

# off → apply does nothing
before = os.lstat(a / 'theme.mp3')
P.apply_file(config, a / 'theme.mp3')
after = os.lstat(a / 'theme.mp3')
check('off: install leaves file alone', (before.st_mode, before.st_uid) == (after.st_mode, after.st_uid))

# invalid inputs refused, nothing saved
for body, label in (('enabled=1&folder_mode=600&user=99&group=100', 'mode 600'),
                    ('enabled=1&folder_mode=775&user=99999&group=100', 'uid > 65535'),
                    ('enabled=1&folder_mode=775&user=nobody&group=100', 'name nobody'),
                    ('enabled=1&folder_mode=775&user=zzqqxx&group=100', 'unknown name'),
                    ('enabled=1&folder_mode=775&user=99&group=bad%20name', 'bad group')):
    msg = post('/media-management/permissions', body)
    check('rejected: ' + label, msg.startswith('Permissions:') and not (config / P.FILE).exists(), msg)

msg = post('/media-management/permissions', 'enabled=1&folder_mode=775&user=99&group=users')
s = P.settings(config)
check('save resolves group name users → 100', s == {'enabled': True, 'folder_mode': '775', 'user': '99', 'group': '100'} and msg.startswith('Permissions saved'), msg + ' ' + str(s))
check('settings file is 0600', stat.S_IMODE(os.stat(config / P.FILE).st_mode) == 0o600)

# install applies mode + owner
new = anime / 'Fresh Install (2020)'
new.mkdir()
(new / 'theme.mp3').write_bytes(b'x' * 100)
P.apply_file(config, new / 'theme.mp3')
st = os.lstat(new / 'theme.mp3')
check('install → 664 99:100', (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid) == (0o664, 99, 100), oct(st.st_mode))
made = anime / 'Created Folder (2021)'
made.mkdir(mode=0o700)
P.apply_dir(config, made)
st = os.lstat(made)
check('created folder → 775 99:100', (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid) == (0o775, 99, 100), oct(st.st_mode))
P.apply_file(config, f / 'theme.mp3')
check('symlinked theme.mp3 not followed', stat.S_IMODE(os.stat(outside).st_mode) == 0o600 and os.stat(outside).st_uid == 0)
P.apply_dir(config, new / 'theme.mp3')
check('apply_dir on a file does nothing', stat.S_IMODE(os.lstat(new / 'theme.mp3').st_mode) == 0o664)
new_files = {new / 'theme.mp3', new, made}  # created by this test, excluded from the no-change snapshot

# preview is read-only
snap = snapshot()
msg = post('/media-management/permissions-preview')
pv = P._STATE['preview']
paths = sorted(Path(x['path']).parent.name for x in pv['wrong'])
check('preview finds exactly the 3 mismatched registered files', paths == ['Movie Root (2015)', 'Root Owned (2009)', 'Wrong Mode (2011)'], str(paths))
check('preview counts checked files (symlink + unregistered excluded)', pv['checked'] == 4, str(pv['checked']))
check('preview changed nothing', snapshot() == snap)
html = P.settings_html(app, msg)
check('page lists files + confirm form', 'Fix files' in html and 'value="owner"' in html and 'Root Owned (2009)' in html and 'name="confirm"' in html)

# fix without confirm refused
msg = post('/media-management/permissions-fix', '')
check('fix without confirm refused', 'confirm' in msg and snapshot() == snap, msg)

check('preview breakdown: 2 wrong owner, 1 mode only', (pv['owner'], pv['mode_only']) == (2, 1), str((pv['owner'], pv['mode_only'])))
check('fix without scope refused', 'choose what to fix' in post('/media-management/permissions-fix', 'confirm=1') and snapshot() == snap)

# owner/group-only fix: modes stay as they are
msg = post('/media-management/permissions-fix', 'confirm=1&scope=owner')
for _ in range(100):
    if not P.status()['fix']['running']:
        break
    time.sleep(0.05)
fx = P.status()['fix']
check('owner-only fix: 2 changed', fx['changed'] == 2 and fx['errors'] == 0, str(fx))
st_a, st_d, st_c = (os.lstat(x / 'theme.mp3') for x in (a, d, c))
check('owner-only: owners fixed, modes kept', (st_a.st_uid, st_a.st_gid, stat.S_IMODE(st_a.st_mode)) == (99, 100, 0o644)
      and (st_d.st_uid, stat.S_IMODE(st_d.st_mode)) == (99, 0o644) and stat.S_IMODE(st_c.st_mode) == 0o777)
first_log = sorted((config / 'logs').glob('permissions-fix-*.csv'))
check('owner-only CSV says mode unchanged', first_log and 'unchanged' in first_log[0].read_text().splitlines()[1], str(first_log))
time.sleep(1.1)  # new CSV name (per-second timestamp)

# full fix with confirm
msg = post('/media-management/permissions-fix', 'confirm=1&scope=all')
for _ in range(100):
    if not P.status()['fix']['running']:
        break
    time.sleep(0.05)
fx = P.status()['fix']
check('fix finished: 3 changed, 0 errors', fx['changed'] == 3 and fx['errors'] == 0 and not fx['running'], str(fx))
for folder in (a, c, d, b):
    st = os.lstat(folder / 'theme.mp3')
    check('fixed ' + folder.name, (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid) == (0o664, 99, 100), oct(st.st_mode) + ' ' + str(st.st_uid))
after_snap = snapshot()
changed = {k for k in after_snap if after_snap[k] != snap.get(k)}
expected = {str(a / 'theme.mp3'), str(c / 'theme.mp3'), str(d / 'theme.mp3')}
check('only the 3 theme.mp3 files changed (videos, folders, symlink target, unregistered untouched)', changed == expected, str(changed ^ expected))
check('video file untouched', stat.S_IMODE(os.lstat(a / 'ep1.mkv').st_mode) == 0o640)
check('inodes kept (chmod/chown in place, no rewrite)', all(after_snap[k][3] == snap[k][3] for k in expected))
logs = sorted((config / 'logs').glob('permissions-fix-*.csv'))
rows = logs[-1].read_text().splitlines() if logs else []
check('CSV undo record: header + 3 rows with old values', len(logs) == 2 and len(rows) == 4 and ',644,99,100,664,99,100,changed' in ''.join(rows) and ',777,99,100,664,99,100,changed' in ''.join(rows), str(rows))
check('CSV is 0600', logs and stat.S_IMODE(os.stat(logs[0]).st_mode) == 0o600)
with _db() as c2:
    ev = [dict(r) for r in c2.execute("SELECT * FROM events WHERE name='Permissions'")]
check('Activity entries recorded', len(ev) == 2 and '2 theme.mp3' in ev[0]['detail'] and '3 theme.mp3' in ev[1]['detail'], str(ev))

# second preview → nothing left
post('/media-management/permissions-preview')
check('preview after fix: 0 differ', len(P._STATE['preview']['wrong']) == 0)

# fix refused when permissions off
post('/media-management/permissions', 'folder_mode=775&user=99&group=100')
msg = post('/media-management/permissions-fix', 'confirm=1&scope=all')
check('fix refused while Set Permissions is off', 'turn on Set Permissions' in msg, msg)

passed = sum(ok for _, ok in RESULTS)
print('\n%d/%d passed' % (passed, len(RESULTS)))
sys.exit(0 if passed == len(RESULTS) else 1)
