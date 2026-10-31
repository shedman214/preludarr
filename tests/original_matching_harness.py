# SPDX-License-Identifier: GPL-3.0-or-later
"""Original-matching regression harness (Themearr V1.7.0 work).

Runs the UNMODIFIED matching code of whichever Themearr source tree is mounted at /src:
  * automatic path  - app.process() (the Download / Retry button and scheduled scans)
  * browser path    - the real GET /themes page rendered by app.Handler

Network access goes through app.request_json, which is replaced by a recorder (record mode)
or a strict replayer (replay mode: any request that was not recorded fails the run).
The final audio download (urllib.request.urlopen) is replaced by a sentinel exception, so no
audio is ever downloaded and nothing is written outside the throw-away /tmp test folders.

Usage (inside a Themearr image):  python3 harness.py record|replay FIXTURES TITLES OUT
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

MODE, FIXTURES, TITLES, OUT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
assert MODE in ('record', 'replay')

work = Path(tempfile.mkdtemp(prefix='themearr-regress-'))
os.environ['CONFIG_DIR'] = str(work / 'config')
os.environ['TV_ROOT'] = str(work / 'lib' / 'anime')
os.environ['MOVIE_ROOT'] = str(work / 'lib' / 'movies')
for _root in ('anime', 'movies'):  # 1.12: a new install only registers root folders that exist
    (work / 'lib' / _root).mkdir(parents=True, exist_ok=True)
os.environ['AUTO_DOWNLOAD'] = '0'
os.environ['POSTER_BACKGROUND_ENABLED'] = '0'
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402  (import has no side effects beyond creating CONFIG_DIR)

recorded = json.loads(FIXTURES.read_text()) if FIXTURES.exists() else {}
unrecorded = []
_last = [0.0]


class RecordedError(Exception):
    pass


class DownloadReached(Exception):
    pass


def fake_request_json(url, data=None):
    key = json.dumps([url, data], sort_keys=True, ensure_ascii=False)
    if key not in recorded:
        if MODE == 'replay':
            unrecorded.append(url)
            raise RecordedError('UNRECORDED REQUEST ' + url)
        wait = 1.1 - (time.monotonic() - _last[0])  # stay well under AniList / AnimeThemes rate limits
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        try:
            recorded[key] = {'ok': real_request_json(url, data)}
        except Exception as error:  # record failures too, so replays fail the same way
            recorded[key] = {'error': str(error)}
    item = recorded[key]
    if 'error' in item:
        raise RecordedError(item['error'])
    return json.loads(json.dumps(item['ok']))  # fresh copy every call


def no_download(request, *args, **kwargs):
    url = request.full_url if hasattr(request, 'full_url') else str(request)
    raise DownloadReached('DOWNLOAD_REACHED ' + url)


_real_urlopen = urllib.request.urlopen


def real_request_json(url, data=None):
    """Same request as app.request_json, but on the unpatched urlopen (record mode only)."""
    headers = {'User-Agent': 'Themearr/0.1 (personal media manager)', 'Accept': 'application/json'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with _real_urlopen(req, timeout=18) as r:
        return json.load(r)


app.request_json = fake_request_json
urllib.request.urlopen = no_download
if hasattr(app, 'open_approved'):  # 1.12: theme audio downloads go through app.open_approved (redirect host check)
    def approved_no_download(request, hosts, timeout):
        if not app._approved(request.full_url, hosts):
            raise ValueError('Download address is not from an approved provider')
        return no_download(request)
    app.open_approved = approved_no_download

saves, events = [], []
real_save = app.save
app.save = lambda path, library, name, state, detail='': (saves.append([state, detail]), real_save(path, library, name, state, detail))[1]
app.event = lambda name, status, detail: events.append([status, detail])
app.auth.get = lambda handler: True  # render pages without a session

titles = json.loads(TITLES.read_text())
roots = {label: root for label, root in app.ROOTS}
for t in titles:
    folder = roots[t['library']] / Path(t['path']).name
    folder.mkdir(parents=True, exist_ok=True)
    t['test_path'] = str(folder)
    real_save(folder, t['library'], folder.name, 'missing', '')

server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]


def browser(path):
    url = f'http://127.0.0.1:{port}/themes?' + urllib.parse.urlencode({'path': path})
    with urllib.request.build_opener().open(url, timeout=120) as response:  # opener avoids the patched urlopen
        page = response.read().decode('utf-8')
    # V1.7.3+: the theme list loads after the page (like a browser, follow the placeholder's address).
    m = re.search(r'data-match-url="([^"]+)"', page)
    if m:
        import html as _html
        with urllib.request.build_opener().open(f'http://127.0.0.1:{port}' + _html.unescape(m.group(1)), timeout=120) as response:
            page += response.read().decode('utf-8')
    return {
        'matched_slugs': sorted(set(re.findall(r'name="anime_slug" value="([^"]*)"', page))),
        'video_ids': re.findall(r'name="video_id" value="([^"]*)"', page),
        'groups': re.findall(r'<summary><strong>([^<]*)</strong>', page),
        'alternatives': re.findall(r'theme_slug=([A-Za-z0-9_-]+)', page),
        'messages': [m for m in ('No matching AnimeThemes entry found', 'Theme lookup failed',
                                 'No audio themes available', 'Manual search failed') if m in page],
    }


results = {}
for n, t in enumerate(titles, 1):
    saves.clear(); events.clear()
    app.STATUS.update(running=False)
    outcome = app.process(t['test_path'])
    auto = {'returned': outcome, 'saves': [list(s) for s in saves], 'events': [list(e) for e in events]}
    results[t['path']] = {'name': t['name'], 'category': t['category'], 'auto': auto, 'browser': browser(t['test_path'])}
    print(f'{n:3}/{len(titles)} {t["name"][:60]:60} auto={outcome[:60]}', flush=True)
    if MODE == 'record':
        FIXTURES.write_text(json.dumps(recorded, ensure_ascii=False, sort_keys=True))

server.shutdown()
OUT.write_text(json.dumps({'results': results, 'unrecorded_requests': unrecorded}, ensure_ascii=False, indent=1, sort_keys=True))
print(f'DONE mode={MODE} titles={len(results)} recorded_requests={len(recorded)} unrecorded={len(unrecorded)}')
