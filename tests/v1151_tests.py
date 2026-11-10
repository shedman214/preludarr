# SPDX-License-Identifier: GPL-3.0-or-later
"""1.15.1: the main log is preludarr.log (themearr.log and its rotated copies are renamed on start),
and System → Overview shows AnimeThemes' status from Preludarr's own requests. Offline."""
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))

results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name)

# --- log rename ---------------------------------------------------------------------------------
import app_logs
work = Path(tempfile.mkdtemp())
logs = work / 'logs'; logs.mkdir()
(logs / 'themearr.log').write_text('old main\n')
(logs / 'themearr.log.1').write_text('old rotated\n')
(logs / 'preludarr.log.2').write_text('already new\n')
(logs / 'themearr.log.2').write_text('older copy\n')
app_logs._rename_old_logs(logs)
check('themearr.log becomes preludarr.log', (logs / 'preludarr.log').read_text() == 'old main\n')
check('Rotated copies are renamed too', (logs / 'preludarr.log.1').read_text() == 'old rotated\n')
check('An existing preludarr.log copy is never overwritten',
      (logs / 'preludarr.log.2').read_text() == 'already new\n' and (logs / 'themearr.log.2').exists())
check('Log viewer accepts the new name only', app_logs.permitted('preludarr.log') and app_logs.permitted('preludarr.log.3')
      and not app_logs.permitted('themearr.log') and app_logs.permitted('poster.log'))
check('Viewer defaults to preludarr.log', app_logs.MAIN_LOG == 'preludarr.log')

# --- AnimeThemes status --------------------------------------------------------------------------
import animethemes as at
check('Unknown before any request', at.health()['state'] == 'unknown')

def fail_503(*args, **kwargs):
    raise urllib.error.HTTPError('https://api.animethemes.moe/anime', 503, 'Service Unavailable', {}, io.BytesIO(b''))
with patch.object(at.urllib.request, 'urlopen', side_effect=fail_503), patch.object(at.time, 'sleep'), patch.object(at, '_pace'):
    try:
        at.get_json('https://api.animethemes.moe/anime?x=1', {}, fresh=True)
    except urllib.error.HTTPError:
        pass
h = at.health()
check('A 503 marks AnimeThemes down with a time and reason', h['state'] == 'down' and h['since'] > 0 and '503' in h['detail'])

class Answer(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
with patch.object(at.urllib.request, 'urlopen', return_value=Answer(b'{"anime": []}')), patch.object(at, '_pace'):
    at.get_json('https://api.animethemes.moe/anime?x=2', {}, fresh=True)
h = at.health()
check('A good answer clears the outage', h['state'] == 'ok' and h['since'] == 0.0 and h['last_ok'] > 0)

# Overview card (system._cards) explains the state in plain words
at._STATE.update(down_since_wall=1.0e9, down_detail='AnimeThemes server error (HTTP 503).')
import system
config = work / 'config'; config.mkdir()
with patch.object(system, '_about_html', return_value=''):
    try:
        html = system._cards(str(config))
    except Exception as exc:
        html = 'error ' + repr(exc)
check('Overview shows AnimeThemes down and what it means',
      'AnimeThemes' in html and 'Down' in html and 'HTTP 503' in html and 'wait and retry on their own' in html
      and 'not affected' in html)
print('%s/%s passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
