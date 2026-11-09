# SPDX-License-Identifier: GPL-3.0-or-later
"""1.15 faster background posters: a TVDB "slow down" (HTTP 429) pauses TVDB through the shared rate
limiter, like every other provider call. Offline; the provider is replaced."""
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
work = Path(tempfile.mkdtemp())
config = work/'config'; config.mkdir()
root = work/'library'; root.mkdir()
os.environ.update(CONFIG_DIR=str(config), TV_ROOT=str(root), MOVIE_ROOT=str(work/'absent'), AUTO_DOWNLOAD='false')
import anidb
import app_logs
with patch.object(anidb, 'enabled', return_value=False), patch.object(app_logs, 'install', return_value=False):
    import app

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)

show = root/'Some Show'
show.mkdir()
app.discover()
backoffs = []
def too_many(*args, **kwargs):
    raise urllib.error.HTTPError('https://api4.thetvdb.com/v4/search', 429, 'Too Many Requests', {'Retry-After': '42'}, io.BytesIO(b''))
with patch.object(app.poster_connect, 'effective_values', return_value=('', 'k', '', 'tvdb')), \
        patch.object(app, 'request_json', return_value={'data': {'token': 't'}}), \
        patch.object(app, 'poster_titles', return_value=['Some Show']), \
        patch.object(app.urllib.request, 'urlopen', side_effect=too_many), \
        patch.object(app.ratelimit, 'wait'), patch.object(app.ratelimit, 'backoff', side_effect=lambda s, a: backoffs.append((s, a))):
    try:
        app.fetch_poster(str(show), 'TV', 'Some Show')
    except Exception as exc:
        print('fetch_poster raised', type(exc).__name__)
check('TVDB 429 pauses TVDB via the rate limiter (Retry-After kept)', backoffs == [('tvdb', '42')])
check('No poster file written on a 429', not app.poster_file(str(show)).is_file())

import email.utils as eu
import time as _t
rl = app.ratelimit
check('Retry-After in seconds', rl.retry_after_seconds('42') == 42.0)
date = eu.formatdate(_t.time() + 90, usegmt=True)
check('Retry-After as an HTTP-date', 80 <= rl.retry_after_seconds(date) <= 91)
check('Unreadable or missing Retry-After falls back to 30 s',
      rl.retry_after_seconds('soon') == rl.retry_after_seconds(None) == 30.0)
rl._PAUSED.pop('tvdb', None)
rl.backoff('tvdb', date)
check('A date-form Retry-After still pauses TVDB', rl._PAUSED.get('tvdb', 0) > _t.monotonic() + 60)
print('%s/%s passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
