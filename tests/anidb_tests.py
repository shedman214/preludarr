# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for anidb (V1.7.0). Run inside a Themearr image with --network none:
    python3 anidb_tests.py /path/to/anime-titles.xml.gz
"""
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, '/src/preludarr')
import anidb as a  # noqa: E402

DUMP = Path(sys.argv[1])
results = []


def check(name, ok, detail=''):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail else ''), flush=True)


def wait_idle(seconds=10):
    end = time.time() + seconds
    while (a._DATA['downloading'] or a._DATA['loading']) and time.time() < end:
        time.sleep(0.1)


clean = lambda n: __import__('re').sub(r'\s*\(\d{4}\)\s*$', '', n).strip()  # noqa: E731

# ---- download rule: at most one attempt per 24 h, even after failures ----
calls = []
real_urlopen = urllib.request.urlopen


def fake_urlopen(req, timeout=None):
    calls.append(getattr(req, 'full_url', req))
    if fake_urlopen.fail:
        raise OSError('offline')
    return io.BytesIO(DUMP.read_bytes())


fake_urlopen.fail = True
urllib.request.urlopen = fake_urlopen
cfg = Path(tempfile.mkdtemp())
a.ensure(cfg); wait_idle()
check('No title list yet → one download attempt', len(calls) == 1)
check('Failed attempt is reported, nothing half-written', 'download failed' in a._DATA['error'] and not (cfg / 'anidb/anime-titles.xml.gz').exists()
      and not (cfg / 'anidb/anime-titles.xml.gz.part').exists(), a._DATA['error'])
fake_urlopen.fail = False
a.ensure(cfg); wait_idle()
check('A failed attempt still counts: no second attempt within 24 h', len(calls) == 1)
st = json.loads((cfg / 'anidb/state.json').read_text()); st['last_attempt'] -= 25 * 3600
(cfg / 'anidb/state.json').write_text(json.dumps(st))
a.ensure(cfg); wait_idle(30)
check('After 24 h: downloaded, verified and loaded', len(calls) == 2 and a.status(cfg)['available'] and a.status(cfg)['anime'] > 10000,
      f"{a.status(cfg)['anime']} anime")
a.ensure(cfg); wait_idle()
check('Fresh copy: no further download', len(calls) == 2)
# a broken download never replaces the working copy
st = json.loads((cfg / 'anidb/state.json').read_text()); st['last_attempt'] -= 25 * 3600
(cfg / 'anidb/state.json').write_text(json.dumps(st)); os.utime(cfg / 'anidb/anime-titles.xml.gz', (0, 0)); a._DATA['mtime'] = 0
good = (cfg / 'anidb/anime-titles.xml.gz').read_bytes()
urllib.request.urlopen = lambda req, timeout=None: io.BytesIO(gzip.compress(b'<animetitles></animetitles>'))
a.ensure(cfg); wait_idle(30)
check('A broken/incomplete list is rejected; the last good copy is kept', (cfg / 'anidb/anime-titles.xml.gz').read_bytes() == good
      and a.status(cfg)['available'], a._DATA['error'])
urllib.request.urlopen = real_urlopen

# ---- title lookup ----
c = a.candidates('Concrete Revolutio Superhuman Phantasmagoria')
check('Regression case: folder title = AniDB official English title of AniDB 11343', c and c[0]['aid'] == 11343 and c[0]['exact'], c[:1])
check('Japanese title finds the same anime', any(x['aid'] == 11343 and x['exact'] for x in a.candidates('コンクリート・レボルティオ 超人幻想')))
check('Romaji main title finds it too', any(x['aid'] == 11343 for x in a.candidates('Concrete Revolutio Choujin Gensou')))
check('Nonsense title finds nothing', a.candidates('Zzyzx Imaginary Fixture Title') == [])

# ---- identification rules (fake AnimeThemes) ----
AT = {11343: [{'slug': 'concrete_revolutio_choujin_gensou', 'name': 'Concrete Revolutio: Choujin Gensou', 'year': 2015, 'media_format': 'TV'}]}


def fake_request_json(url, data=None):
    aid = int(url.rsplit('=', 1)[1])
    return {'anime': AT.get(aid, [])}


r, notes = a.identify('Concrete Revolutio Superhuman Phantasmagoria (2015)', 2015, False, fake_request_json, clean)
check('Exact title + same year + TV folder → High confidence', r and r[0]['confidence'] == 'High' and r[0]['slug'] == 'concrete_revolutio_choujin_gensou', r[:1] and r[0]['why'])
r, notes = a.identify('Concrete Revolutio Superhuman Phantasmagoria (2016)', 2016, False, fake_request_json, clean)
check('Year contradiction → suggestion dropped, explained in details', not r and any('skipped' in n for n in notes), notes[:1])
r, _ = a.identify('Concrete Revolutio Superhuman Phantasmagoria (2015)', 2015, True, fake_request_json, clean)
check('TV entry for a Movies-library folder → never High', r and r[0]['confidence'] != 'High', r[:1] and r[0]['confidence'])
r, _ = a.identify('Concrete Revolutio Superhuman Phantasmagoria', None, False, fake_request_json, clean)
check('No year on the folder → at most Medium', r and r[0]['confidence'] == 'Medium')
AT.clear()
r, notes = a.identify('Concrete Revolutio Superhuman Phantasmagoria (2015)', 2015, False, fake_request_json, clean)
check('AniDB ID without an AnimeThemes entry → no suggestion, explained', not r and any('no AnimeThemes entry' in n for n in notes))
r, notes = a.identify('Concrete Revolutio Superhuman Phantasmagoria (2015)', 2015, False, lambda u, d=None: (_ for _ in ()).throw(OSError('down')), clean)
check('AnimeThemes unavailable → no crash, explained', not r and any('could not be checked' in n for n in notes))

# ---- settings ----
s = a.settings(cfg)
check('Default settings: enabled, no client', s == {'enabled': True, 'client': '', 'clientver': ''})
a._save_settings(cfg, {'enabled': False, 'client': 'themearr', 'clientver': '1'})
mode = oct((cfg / 'anidb/settings.json').stat().st_mode & 0o777)
check('Settings saved privately (0600) and read back', a.settings(cfg)['client'] == 'themearr' and mode == '0o600', mode)
check('Switch off → Theme Browser section disappears', not a.enabled(cfg))
page = a.settings_html(cfg)
check('Connect card shows instructions and links', all(x in page for x in ('Add client', 'registration', 'HTTP API rules', 'No account or key needed')))
check('Connect card never echoes field values into inputs (placeholders only)', 'value="themearr"' not in page)

print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
