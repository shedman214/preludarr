# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline tests for Unmapped Folders + Match / Import (V1.7.3). Run in a Themearr image with --network none."""
import hashlib
import io
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

work = Path(tempfile.mkdtemp())
lib = work / 'lib'
for n in ('Has Theme (2001)', 'No Theme (2015)', 'Not Scanned Yet (2020)'):
    (lib / 'anime' / n).mkdir(parents=True)
(lib / 'movies').mkdir(parents=True)
(lib / 'anime/Has Theme (2001)/theme.mp3').write_bytes(b'ID3' + b'\0' * 2000)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(lib / 'anime'), MOVIE_ROOT=str(lib / 'movies'),
                  AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import unmapped as um  # noqa: E402
import media_servers as v4  # noqa: E402

results = []
def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail else ''), flush=True)

def snapshot():
    out = {}
    for root, dirs, files in os.walk(lib):
        for n in dirs + files:
            p = Path(root) / n
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'dir'
    return out

ANIME = {'concrete_revolutio_choujin_gensou': {'slug': 'concrete_revolutio_choujin_gensou', 'name': 'Concrete Revolutio: Choujin Gensou', 'year': 2015,
                                               'media_format': 'TV', 'animethemes': []}}
def fake_request_json(url, data=None, fresh=False):
    slug = url.split('/anime/', 1)[1].split('?', 1)[0] if '/anime/' in url else ''
    if slug in ANIME:
        return {'anime': ANIME[slug]}
    raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
app.request_json = fake_request_json
import urllib.error  # noqa: E402

app.discover()
# simulate: "Not Scanned Yet" appeared after the last scan
with app.db() as c:
    c.execute('DELETE FROM shows WHERE name=?', ('Not Scanned Yet (2020)',))
before = snapshot()
names = [f['name'] for f in um.unmapped(app, lib / 'anime')]
check('Unmapped = no theme and no confirmed match (incl. not scanned yet); theme folders are mapped', names == ['No Theme (2015)', 'Not Scanned Yet (2020)'], names)

try:
    um.match(app, str(lib / 'anime/No Theme (2015)'), 'no_such_entry'); ok = False
except (ValueError, OSError):
    ok = True
check('Match refuses an entry AnimeThemes cannot confirm (nothing saved)', ok and not v4.has_override(app.CONFIG, str(lib / 'anime/No Theme (2015)')))
try:
    um.match(app, '/etc', 'concrete_revolutio_choujin_gensou'); ok = False
except ValueError:
    ok = True
check('Match refuses folders outside root folders', ok)

name, imported = um.match(app, str(lib / 'anime/Not Scanned Yet (2020)'), 'concrete_revolutio_choujin_gensou')
row = app.db().execute('select state,library from shows where name=?', ('Not Scanned Yet (2020)',)).fetchone()
check('Match / Import registers a not-yet-scanned folder (records only) and remembers the entry',
      imported and row and row[0] == 'missing' and row[1] == 'Anime TV' and v4.override(app.CONFIG, str(lib / 'anime/Not Scanned Yet (2020)'))['slug'] == 'concrete_revolutio_choujin_gensou')
um.match(app, str(lib / 'anime/No Theme (2015)'), 'concrete_revolutio_choujin_gensou')
check('After matching, both folders are mapped', um.unmapped(app, lib / 'anime') == [])
check('Matching wrote an Activity entry', 'Matched to AnimeThemes Concrete Revolutio' in ' '.join(r[0] for r in app.db().execute('select detail from events')))
check('NOTHING on disk changed by Match / Import', snapshot() == before)

# automatic install uses the remembered entry, never the title search
app.anilist = lambda name: (_ for _ in ()).throw(AssertionError('title search must not run for a matched folder'))
used = []
real_resolve = v4.resolve
v4.resolve = lambda *a, **k: (used.append(a[1]), real_resolve(*a, **k))[1]
msg = app.process(str(lib / 'anime/No Theme (2015)'))
check('Automatic install for a matched folder uses the remembered entry (no AniList title search)',
      used == [str(lib / 'anime/No Theme (2015)')] and 'No OP audio available' in msg, msg)
v4.resolve = real_resolve
check('Still nothing written to disk (fixture entry has no audio)', snapshot() == before)

# suggestions: Original Matching first
app.anilist = lambda name: {'seasonYear': 2015, 'title': {}, 'synonyms': []}
app.anime_theme = lambda name, media: ANIME['concrete_revolutio_choujin_gensou']
v4._write(app.CONFIG, 'matches', {})
d = um.suggest(app, str(lib / 'anime/No Theme (2015)'))
check('Suggestions: Original Matching result first, with poster art address and explanation',
      d['candidates'] and d['candidates'][0]['source'] == 'Original Matching' and d['candidates'][0]['art'].startswith('/anime-art?') and d['candidates'][0]['confidence'] == 'High', d['candidates'][:1])
app.anime_theme = lambda name, media: dict(ANIME['concrete_revolutio_choujin_gensou'], year=2009)
d = um.suggest(app, str(lib / 'anime/No Theme (2015)'))
check('Suggestions: a year contradiction is not offered', all(c['source'] != 'Original Matching' for c in d['candidates']))
print(f'{sum(results)}/{len(results)} passed'); sys.exit(0 if all(results) else 1)
