# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.4 — review extra theme files + theme trash. Run in a Themearr image, --network none:
python3 /src/tests/theme_review_tests.py
"""
import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
anime, movies = work / 'lib/anime', work / 'lib/movies'
for d in (anime, movies):
    d.mkdir(parents=True)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(anime), MOVIE_ROOT=str(movies), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')


def tone(path, freq, secs=3, fmt=None):
    args = ['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', f'sine=frequency={freq}:duration={secs}']
    if path.suffix == '.mp3':
        args += ['-codec:a', 'libmp3lame', '-q:a', '6', '-metadata', f'title=Song {freq}', '-metadata', 'artist=Test']
    subprocess.run(args + [str(path)], check=True)
    return path


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def mk(name):
    d = anime / name
    d.mkdir()
    return d


bleach = mk('Bleach (2004)')
tone(bleach / 'theme.mp3', 440); tone(bleach / 'Theme.mp3', 880); (bleach / 'ep1.mkv').write_bytes(b'video')
os.chmod(bleach / 'Theme.mp3', 0o640); os.chown(bleach / 'Theme.mp3', 0, 0)
os.chmod(bleach / 'theme.mp3', 0o666); os.chown(bleach / 'theme.mp3', 99, 100)
A, B = sha(bleach / 'theme.mp3'), sha(bleach / 'Theme.mp3')
flacdir = mk('Flac Extra (2010)')
tone(flacdir / 'theme.mp3', 500); tone(flacdir / 'theme.flac', 600)
nested = mk('Nested (2017)')
tone(nested / 'theme.mp3', 300); (nested / 'Season 01').mkdir(); tone(nested / 'Season 01/theme.mp3', 700); (nested / 'Season 01/ep1.mkv').write_bytes(b'v')
N_TOP, N_SUB = sha(nested / 'theme.mp3'), sha(nested / 'Season 01/theme.mp3')
nested_only = mk('Only Season (2018)')
(nested_only / 'Season 01').mkdir(); tone(nested_only / 'Season 01/theme.mp3', 750)
conv = mk('Only Flac (2019)')
tone(conv / 'theme.flac', 650)
F = sha(conv / 'theme.flac')
linked = mk('Linked (2020)')
tone(linked / 'theme.mp3', 400); outside = work / 'outside.mp3'; tone(outside, 410); (linked / 'Theme.mp3').symlink_to(outside)
unreg = anime / 'Not Registered (2021)'

import app  # noqa: E402
import theme_review as tr  # noqa: E402
import library_health as lh  # noqa: E402

app.discover(); app.auto_themes.init(app)
unreg.mkdir(); tone(unreg / 'theme.mp3', 420); tone(unreg / 'Theme.mp3', 430)  # created after discover → not registered
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


class H:
    def __init__(self, path, body='', headers=None):
        b = body.encode(); self.path = path; self.headers = dict({'Content-Length': str(len(b))}, **(headers or {})); self.rfile = io.BytesIO(b)
        self.wfile = io.BytesIO(); self.sent = None; self.code = None; self.hdrs = {}

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.hdrs[k] = v

    def end_headers(self):
        pass


LAST = {}


def redirect(h, url):
    LAST['url'] = url
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return (q.get('notice') or q.get('recycle_notice') or [''])[0]


def post(route, fields):
    return tr.post(app, H(route, urllib.parse.urlencode(fields)), redirect)


def batch_ids():
    return [m['id'] for m in tr.batches(app)]


lh.scan(app, check_servers=False)

# ---------------------------------------------------------------- candidates + page ----
folder, items = tr.candidates(app, str(bleach))
check('Bleach: both files listed, theme.mp3 first + marked as the one Plex plays', [i['rel'] for i in items] == ['theme.mp3', 'Theme.mp3'] and items[0]['current'])
check('Song name + length read from the files', items[1]['title'] == 'Song 880' and 2.5 < (items[1]['duration'] or 0) < 3.5, items[1])
check('Default keeper = current theme.mp3', tr.default_keep(items) == 'theme.mp3')
_, items = tr.candidates(app, str(nested))
check('Season-folder theme.mp3 included', [i['rel'] for i in items] == ['theme.mp3', 'Season 01/theme.mp3'] and items[1]['nested'])
_, items = tr.candidates(app, str(linked))
check('Symlinked Theme.mp3 is never offered', [i['rel'] for i in items] == ['theme.mp3'])
for bad, label in ((str(unreg), 'unregistered folder'), ('/etc', 'folder outside the roots'), (str(anime / '..' / 'anime' / 'Bleach (2004)'), 'path with ..')):
    try:
        tr.candidates(app, bad); ok = False
    except ValueError:
        ok = True
    check('Refused: ' + label, ok)
h = H('/library-health/review?' + urllib.parse.urlencode({'path': str(bleach)})); tr.review_page(app, h)
body = h.sent[1]
check('Review page shows the recycle bin setting + where to change it (V1.9.5)', 'kept for 30 days' in body and 'No emptying schedule is set' in body
      and '/settings?section=recycle' in body)
check('Review page: players, radios, confirm, leave-as-is', body.count('<audio') == 2 and body.count('name="keep"') == 2 and 'name="confirm"' in body
      and 'Leave everything as it is' in body and 'value="theme.mp3" checked' in body)

# ---------------------------------------------------------------- audio route ----
h = H('/library-health/audio?' + urllib.parse.urlencode({'path': str(bleach), 'file': 'Theme.mp3'}), headers={'Range': 'bytes=0-99'}); tr.audio(app, h)
check('Audio: Range request → 206, 100 bytes, audio/mpeg', h.code == 206 and len(h.wfile.getvalue()) == 100 and h.hdrs['Content-Type'] == 'audio/mpeg'
      and h.wfile.getvalue() == (bleach / 'Theme.mp3').read_bytes()[:100])
for f in ('../Flac Extra (2010)/theme.flac', 'ep1.mkv', '/etc/passwd'):
    h = H('/library-health/audio?' + urllib.parse.urlencode({'path': str(bleach), 'file': f})); tr.audio(app, h)
    check('Audio refuses ' + f, h.sent and h.sent[0] == 404)

# ---------------------------------------------------------------- keep: rename ----
check('Keep without the confirm tick is refused', 'Tick the box' in post('/library-health/keep', {'path': str(bleach), 'keep': 'Theme.mp3'}))
(work / 'config' / 'themearr_permissions.json').write_text(json.dumps({'enabled': True, 'folder_mode': '775', 'user': '99', 'group': '100'}))
r = post('/library-health/keep', {'path': str(bleach), 'keep': 'Theme.mp3', 'confirm': '1'})
(work / 'config' / 'themearr_permissions.json').unlink()
check('Kept file gets the Permissions settings (664 99:100)', (oct(os.stat(bleach / 'theme.mp3').st_mode & 0o777), os.stat(bleach / 'theme.mp3').st_uid) == ('0o664', 99))
check('Keep Theme.mp3 → message', r.startswith('Kept Theme.mp3 as theme.mp3'), r)
files = sorted(p.name for p in bleach.iterdir())
check('Bleach now: theme.mp3 = the chosen song, extra gone, video untouched', files == ['ep1.mkv', 'theme.mp3'] and sha(bleach / 'theme.mp3') == B, files)
m = tr.batches(app)[0]
check('Trash holds the old theme.mp3, verified', [i['rel'] for i in m['items']] == ['theme.mp3'] and m['items'][0]['sha256'] == A and m['state'] == 'done')
check('Trash is private (0700 folder, 0600 files)', oct((work / 'config/theme-trash').stat().st_mode & 0o777) == '0o700'
      and all(oct(p.stat().st_mode & 0o777) == '0o600' for p in (work / 'config/theme-trash' / m['id']).iterdir()))
with app.db() as c:
    ev = c.execute("SELECT name,status,detail FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
check('Message says recycle bin', 'recycle bin' in r and 'trash' not in r, r)
check('Activity: theme kept', ev['status'] == 'theme kept' and 'Theme.mp3' in ev['detail'], dict(ev))
check('Library Health finding for Bleach cleared', not lh.title_findings(app.CONFIG, str(bleach)))

# undo
r = post('/library-health/trash', {'action': 'undo', 'id': m['id']})
st = os.stat(bleach / 'theme.mp3')
check('Undo: both files back, exactly as before (content, mode, owner)', r.startswith('Undone') and sha(bleach / 'theme.mp3') == A and sha(bleach / 'Theme.mp3') == B
      and oct(st.st_mode & 0o777) == '0o666' and (st.st_uid, st.st_gid) == (99, 100) and m['id'] not in batch_ids(), r)
kst = os.stat(bleach / 'Theme.mp3')
check('Undo also gives the kept file back its original mode + owner', (oct(kst.st_mode & 0o777), kst.st_uid, kst.st_gid) == ('0o640', 0, 0), (oct(kst.st_mode), kst.st_uid))
# undo refused when theme.mp3 changed afterwards
post('/library-health/keep', {'path': str(bleach), 'keep': 'Theme.mp3', 'confirm': '1'})
bid = batch_ids()[0]
tone(bleach / 'theme.mp3', 999)
r = post('/library-health/trash', {'action': 'undo', 'id': bid})
check('Undo refused when theme.mp3 changed after the review (files stay in trash)', r.startswith('✗') and 'changed after the review' in r and bid in batch_ids(), r)

# ---------------------------------------------------------------- keep current, extra flac ----
ino = os.stat(flacdir / 'theme.mp3').st_ino
r = post('/library-health/keep', {'path': str(flacdir), 'keep': 'theme.mp3', 'confirm': '1'})
check('Keep current theme.mp3: flac moved to trash, theme.mp3 untouched (same inode)', sorted(p.name for p in flacdir.iterdir()) == ['theme.mp3']
      and os.stat(flacdir / 'theme.mp3').st_ino == ino, r)

# ---------------------------------------------------------------- nested ----
r = post('/library-health/keep', {'path': str(nested), 'keep': 'Season 01/theme.mp3', 'confirm': '1'})
check('Keep the Season-folder theme: it becomes theme.mp3, Season folder keeps its video', sha(nested / 'theme.mp3') == N_SUB
      and sorted(p.name for p in (nested / 'Season 01').iterdir()) == ['ep1.mkv'], r)
bid = batch_ids()[0]
post('/library-health/trash', {'action': 'undo', 'id': bid})
check('Undo nested: both back in place', sha(nested / 'theme.mp3') == N_TOP and sha(nested / 'Season 01/theme.mp3') == N_SUB)
r = post('/library-health/keep', {'path': str(nested_only), 'keep': 'Season 01/theme.mp3', 'confirm': '1'})
with app.db() as c:
    stt = c.execute('SELECT state FROM shows WHERE path=?', (str(nested_only),)).fetchone()['state']
check('Folder with only a Season theme: moved up as theme.mp3, list says installed', (nested_only / 'theme.mp3').is_file()
      and not (nested_only / 'Season 01/theme.mp3').exists() and stt == 'installed', (r, stt))

# ---------------------------------------------------------------- convert ----
r = post('/library-health/keep', {'path': str(conv), 'keep': 'theme.flac', 'confirm': '1'})
probe = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=format_name', '-of', 'json', str(conv / 'theme.mp3')], capture_output=True).stdout or b'{}')
check('Keep a FLAC: converted to a real MP3 theme.mp3, FLAC to trash', 'converted' in r and sorted(p.name for p in conv.iterdir()) == ['theme.mp3']
      and 'mp3' in probe.get('format', {}).get('format_name', ''), (r, probe))
bid = batch_ids()[0]
r = post('/library-health/trash', {'action': 'undo', 'id': bid})
check('Undo convert: converted theme.mp3 removed, FLAC restored', sorted(p.name for p in conv.iterdir()) == ['theme.flac'] and sha(conv / 'theme.flac') == F, r)

# ---------------------------------------------------------------- safety ----
r = post('/library-health/keep', {'path': str(linked), 'keep': 'Theme.mp3', 'confirm': '1'})
check('Keeping a symlink is impossible (not in the list)', r.startswith('✗') and (linked / 'Theme.mp3').is_symlink() and outside.exists(), r)
app._manual_install_lock.acquire()
r = post('/library-health/keep', {'path': str(flacdir), 'keep': 'theme.mp3', 'confirm': '1'})
app._manual_install_lock.release()
check('Refused while another install runs', 'Nothing to do' in r or 'Another install' in r, r)

# ---------------------------------------------------------------- cron ----
def cron_ok(text):
    try:
        tr.parse_cron(text); return True
    except ValueError:
        return False


check('Cron accepts common forms', all(cron_ok(c) for c in ('0 3 * * 0', '30 4 1 * *', '0 */12 * * *', '15 2 * * mon-fri', '0 0 1 jan *', '5/15 * * * *', '0 3 * * 7', '')))
check('Cron rejects bad input with a reason', not any(cron_ok(c) for c in ('0 3 * *', '60 3 * * *', '0 25 * * *', 'abc * * * *', '0 3 * * 8', '0 3 32 * *', '0 3 * jan-mon *')))
spec = tr.parse_cron('5/15 * * * *')
check('Step from a start value: 5/15 → 5,20,35,50', sorted(spec['min']) == [5, 20, 35, 50], sorted(spec['min']))
nxt = tr.next_run(tr.parse_cron('0 3 * * 0'), datetime.datetime(2026, 10, 26, 12, 0))  # Monday
check('next_run: “0 3 * * 0” from Mon 26 Oct → Sun 1 Nov 03:00', nxt == datetime.datetime(2026, 11, 1, 3, 0), nxt)
check('7 also means Sunday', tr.next_run(tr.parse_cron('0 3 * * 7'), datetime.datetime(2026, 10, 26, 12, 0)) == nxt)
both = tr.parse_cron('0 0 13 * 5')
check('Both day fields set → either matches (classic cron)', tr.cron_matches(both, datetime.datetime(2026, 11, 13, 0, 0)) and tr.cron_matches(both, datetime.datetime(2026, 11, 6, 0, 0))
      and not tr.cron_matches(both, datetime.datetime(2026, 11, 7, 0, 0)))

# ---------------------------------------------------------------- settings + schedule ----
r = post('/library-health/trash', {'action': 'settings', 'days': '0', 'cron': ''})
check('Days 0 refused', r.startswith('✗'), r)
r = post('/library-health/trash', {'action': 'settings', 'days': '7', 'cron': '61 * * * *'})
check('Bad cron refused with the reason', r.startswith('✗') and 'minute' in r, r)
r = post('/library-health/trash', {'action': 'settings', 'days': '7', 'cron': '0  3 * *   0'})
check('Settings saved (cron whitespace tidied, next run shown)', tr.settings(app.CONFIG) == {'days': 7, 'cron': '0 3 * * 0'} and 'next run' in r, r)
check('Saving settings returns to Settings → General → Recycle bin', LAST['url'].startswith('/settings?section=recycle'), LAST['url'])
h = H('/library-health/trash'); tr.trash_page(app, h)
check('Recycle bin page: entries with Undo/Delete, Empty now, current settings + link to Settings', 'Undo' in h.sent[1] and 'Empty recycle bin now' in h.sent[1]
      and 'kept for 7 days' in h.sent[1] and '“0 3 * * 0”' in h.sent[1] and '/settings?section=recycle' in h.sent[1] and 'name="days"' not in h.sent[1])
panel = tr.settings_panel(app.CONFIG, 'Recycle bin saved: keep files 7 days.')
check('Settings panel: days + cron fields, next emptying, notice', 'id="settings-recycle-bin"' in panel and 'name="days"' in panel and 'value="7"' in panel
      and 'value="0 3 * * 0"' in panel and 'Next scheduled emptying' in panel and 'Recycle bin saved' in panel)
h = H('/library-health/review?' + urllib.parse.urlencode({'path': str(nested)})); tr.review_page(app, h)
check('Review page follows the saved setting (7 days + schedule)', 'kept for 7 days' in h.sent[1] and 'schedule “0 3 * * 0”' in h.sent[1])
# age limit
ids = batch_ids()
old = work / 'config/theme-trash' / ids[-1] / 'manifest.json'
d = json.loads(old.read_text()); d['at'] = int(time.time()) - 8 * 86400; old.write_text(json.dumps(d))
tr._SCHED['last_purge'] = 0
tr.tick(app, datetime.datetime(2026, 10, 27, 12, 0))
check('Age limit: entry older than 7 days deleted, newer kept', ids[-1] not in batch_ids() and len(batch_ids()) == len(ids) - 1)
# cron run at a matching minute empties everything, only once per minute
tr.tick(app, datetime.datetime(2026, 11, 1, 3, 0))
check('Cron minute: whole trash emptied', batch_ids() == [])
post('/library-health/keep', {'path': str(nested), 'keep': 'Season 01/theme.mp3', 'confirm': '1'})
tr.tick(app, datetime.datetime(2026, 11, 1, 3, 0))
check('…and not again in the same minute', len(batch_ids()) == 1)
r = post('/library-health/trash', {'action': 'delete', 'id': batch_ids()[0]})
check('Delete now removes one entry', r.startswith('Deleted permanently') and batch_ids() == [], r)
r = post('/library-health/trash', {'action': 'empty'})
check('Empty now needs the confirm tick', r.startswith('✗'))
r = post('/library-health/trash', {'action': 'undo', 'id': '../../etc'})
check('Bad trash id refused', r.startswith('✗'))

# V1.9.10: crontab.guru help under the schedule field
g = tr._guru_help('30 4 1 * *')
check('crontab.guru help: explained, deep link with the schedule, new tab, no tracking', 'href="https://crontab.guru/#30_4_1_*_*"' in g and 'target="_blank"' in g
      and 'rel="noopener noreferrer"' in g and 'plain English' in g and 'does not contact it' in g, g)
check('crontab.guru help: no schedule → plain link', 'href="https://crontab.guru/"' in tr._guru_help('') and 'filled in' not in tr._guru_help(''))
check('crontab.guru help: odd input never reaches the link', 'href="https://crontab.guru/"' in tr._guru_help('"><script>') and '<script>' not in tr._guru_help('"><script>'))

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
