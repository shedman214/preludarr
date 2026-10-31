# SPDX-License-Identifier: GPL-3.0-or-later
"""1.12 review finding: replacing a theme keeps the previous theme.mp3 in the recycle bin (Undo restores it). Run in a Preludarr image, --network none."""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
anime = work / 'anime'
title = anime / 'Replace Test (2020)'
title.mkdir(parents=True); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(anime), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import theme_review as tr  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


def tone(path, hz):
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', f'sine=frequency={hz}:duration=3', '-codec:a', 'libmp3lame', str(path)], check=True)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


app.save(str(title), 'Anime TV', title.name, 'installed')
tone(title / 'theme.mp3', 300)
old = sha(title / 'theme.mp3')
new_src = work / 'new.mp3'; tone(new_src, 900)

app.manual_install_mp3(title, new_src, replace=True)
entries = tr.batches(app)
check('Replacing a theme puts the previous theme.mp3 in the recycle bin first', len(entries) == 1 and entries[0].get('kind') == 'replaced'
      and entries[0]['state'] == 'done' and entries[0]['items'][0]['sha256'] == old and sha(title / 'theme.mp3') != old, entries)
stored = Path(app.CONFIG) / tr.TRASH / entries[0]['id'] / entries[0]['items'][0]['stored']
check('…the saved copy is identical to the old theme and private (0600)', sha(stored) == old and oct(stored.stat().st_mode & 0o777) == '0o600')
msg = tr.undo(app, entries[0]['id'])
check('Undo puts the previous theme back and empties that entry', sha(title / 'theme.mp3') == old and not tr.batches(app), msg)

fresh = anime / 'Fresh Title (2021)'; fresh.mkdir(); app.save(str(fresh), 'Anime TV', fresh.name, 'missing')
app.manual_install_mp3(fresh, new_src, replace=False)
check('A first install (no old theme) adds nothing to the recycle bin', (fresh / 'theme.mp3').is_file() and not tr.batches(app))

real = tr.save_replaced
def broken(*a, **k):
    raise OSError('disk full')
tr.save_replaced = broken
try:
    app.manual_install_mp3(title, new_src, replace=True)
    refused = False
except OSError:
    refused = True
tr.save_replaced = real
check('If the copy cannot be made, nothing is replaced (the old theme stays)', refused and sha(title / 'theme.mp3') == old)

app.manual_install_mp3(title, new_src, replace=True, backup=False)
check('backup=False (the extra-files review keeps its own copy) adds no second entry', not tr.batches(app))

class H:
    path = '/library-health/trash'; sent = None
    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)
app.manual_install_mp3(title, new_src, replace=True)
h = H(); tr.trash_page(app, h)
check('The recycle bin page explains a replaced theme', 'previous <code>theme.mp3</code> (replaced by a new theme)' in h.sent[1] and 'the new theme' in h.sent[1])
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
