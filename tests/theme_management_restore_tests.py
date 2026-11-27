# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the actual confirm/remove and confirm/restore flows on a disposable library."""
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
root = work / 'anime'
title = root / 'Recovery Test (2026)'
title.mkdir(parents=True)
(work / 'movies').mkdir()
original = b'ID3' + b'a' * 4096
(title / 'theme.mp3').write_bytes(original)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(root),
                  MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='false',
                  POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import app
import theme_management as tm

checks = []


def check(name, ok):
    checks.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


class Handler:
    def __init__(self, path=''):
        self.path, self.code, self.body, self.location = path, None, '', ''

    def send(self, code, body, *args):
        self.code, self.body = code, body


def review(operation, archive=''):
    h = Handler('/review-' + operation + '-theme?' + urllib.parse.urlencode(
        {'path': str(title), 'archive': archive}))
    tm.review_action(h, app.ROOTS, app.db, '', app.open_safe_theme_directory,
                     app.verify_theme_directory_identity, 'test-session', operation)
    token = re.search(r'name="token" value="([^"]+)"', h.body)
    assert h.code == 200 and token, h.body
    return {'path': [str(title)], 'token': [token.group(1)], 'archive': [archive]}


def confirm(operation, form):
    h = Handler()
    tm.perform_action(h, app.ROOTS, app.db, app.open_safe_theme_directory,
                      app.verify_theme_directory_identity, app._manual_install_lock,
                      'test-session', form, operation,
                      lambda handler, location: setattr(handler, 'location', location), app.event)
    return h


def state():
    with app.db() as c:
        return c.execute('SELECT state FROM shows WHERE path=?', (str(title),)).fetchone()[0]


app.discover()
removed = confirm('remove', review('remove'))
archives = tm._tm_archives(title)
check('Remove moves theme into recovery and updates the library',
      'result=removed' in removed.location and not (title / 'theme.mp3').exists()
      and len(archives) == 1 and state() == 'missing')
archive = archives[0]
restored = confirm('restore', review('restore', archive))
check('Restore finishes successfully and restores the exact original bytes',
      'result=restored' in restored.location and (title / 'theme.mp3').read_bytes() == original)
check('Restore removes the used recovery entry and updates the library',
      not tm._tm_archives(title) and state() == 'installed')

# A theme created between review and confirm must not be replaced.
confirm('remove', review('remove'))
archive = tm._tm_archives(title)[0]
form = review('restore', archive)
replacement = b'ID3' + b'b' * 4096
(title / 'theme.mp3').write_bytes(replacement)
refused = confirm('restore', form)
check('Restore leaves a newly installed theme and the recovery copy untouched',
      refused.code == 409 and (title / 'theme.mp3').read_bytes() == replacement
      and archive in tm._tm_archives(title))

print(f'{sum(checks)}/{len(checks)} passed')
sys.exit(0 if all(checks) else 1)
