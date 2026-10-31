# SPDX-License-Identifier: GPL-3.0-or-later
import io, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from pathlib import Path
work = Path(tempfile.mkdtemp()); lib = work / 'lib'
for n in ('anime/Romelia War Chronicle', 'anime/Koupen-chan (2025)', 'movies/Bubble (2022)'):
    (lib / n).mkdir(parents=True)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(lib / 'anime'), MOVIE_ROOT=str(lib / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
subprocess.Popen([sys.executable, '/src/tests/fake_arr.py', '18991', '17880'])
for _ in range(100):
    try:
        urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:18991/api/v3/system/status', headers={'X-Api-Key': 'testkey123'}), timeout=1); break
    except Exception:
        time.sleep(0.2)
import app, arr as arr, media_servers as v4
rb = v4._base_url; v4._base_url = lambda r: r if r.startswith('http://127.0.0.1:') else rb(r)
app.discover()
class H:
    def __init__(s, b): b = b.encode(); s.headers = {'Content-Length': str(len(b))}; s.rfile = io.BytesIO(b)
cap = {}
post = lambda f: arr.post(app, H(urllib.parse.urlencode(f)), lambda h, u: cap.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).items()}))
post({'action': 'save', 'kind': 'sonarr', 'name': 'Sonarr Anime', 'url': 'http://127.0.0.1:18991', 'api_key': 'testkey123'})
iid = arr.load(app.CONFIG)['instances'][0]['id']
before = arr.connect_html(app.CONFIG, cap.get('arr_notice', ''))
n = next(i for i, x in enumerate(arr.load(app.CONFIG)['instances'][0]['suggested']) if x['remote'] == '/data/tv/anime')
post({'action': 'preview_suggestion', 'id': iid, 'n': str(n)})
after = arr.connect_html(app.CONFIG, cap['arr_notice'], cap['arr_preview'])
shell = '<!doctype html><html><head><meta charset="utf-8"><style>body{background:#0e1422;color:#e7ecf6;font:15px system-ui;margin:0;padding:24px;max-width:1100px}button,.btn{background:#334362;color:#fff;border:0;border-radius:8px;padding:9px 14px;font-weight:600}.tag{display:inline-block;padding:2px 8px;border-radius:999px;background:#334362;font-size:12px}.tag.warn{background:#7a5a17}.muted{color:#9fb0cc}</style></head><body>'
open('/out/card-suggestion.html', 'w').write(shell + before + '</body></html>')
open('/out/card-preview.html', 'w').write(shell + after + '</body></html>')
print('ok', cap['arr_notice'])
