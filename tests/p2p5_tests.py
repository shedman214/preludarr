# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.11 — P2 match evidence + retry, P5 poster ranking. Run in a Themearr image, --network none."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import poster_manual as pm  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


# ------------------------------------------------------------------ P2: why this match ----
row = {'name': 'Bubble (2022)', 'library': 'Anime Movies'}
show = {'name': 'Bubble', 'year': 2022, 'slug': 'bubble', 'id': 4321}
ev = app._match_evidence('/movies/Bubble (2022)', row, show, 'folder')
check('Evidence: provider, entry, slug link, id, folder', all(x in ev for x in ('AnimeThemes.moe', 'Bubble (2022)', 'href="https://animethemes.moe/anime/bubble"', 'id 4321', 'Your folder')), ev)
check('Evidence: exact title + same year', 'Exact title — your folder' in ev and 'Same year: 2022' in ev and 'Exact title · same year' in ev)
check('Evidence: how it was found (folder search)', 'Searched AnimeThemes with your folder title' in ev)
ev = app._match_evidence('/x', {'name': 'Bubble Movie (2022)', 'library': 'Anime Movies'}, show, 'anilist')
check('Evidence: AniList route + alternative title explained', 'titles from AniList' in ev and 'alternative title or a spelling at least 96% identical' in ev)
ev = app._match_evidence('/x', {'name': 'Bubble (2021)', 'library': 'Anime Movies'}, show, 'confirmed')
check('Evidence: confirmed match with a different year says so', 'You confirmed this AnimeThemes entry' in ev and 'Year differs: folder 2021, entry 2022 — accepted because you confirmed it' in ev)
ev = app._match_evidence('/x', {'name': 'Bubble', 'library': 'Anime Movies'}, show, 'folder')
check('Evidence: folder without a year', 'Your folder name has no year' in ev and 'no folder year' in ev)
ev = app._match_evidence('/x', {'name': '<b>x</b> (2022)', 'library': 'Anime TV'}, {'name': '<i>y</i>', 'year': 2022, 'slug': 'a"b'}, 'folder')
check('Evidence: names and slug are escaped', '<b>x</b>' not in ev and '<i>y</i>' not in ev and 'a"b' not in ev)
with app.db() as c:
    c.execute("INSERT OR REPLACE INTO shows(path,library,name,state,detail,updated) VALUES('/r','Anime TV','Yuuki Yuuna (2014)','review','Review: AniList year mismatch: folder 2014, result 2017','x')")
    c.execute('INSERT INTO media_ids(path,tvdb,tmdb,imdb,source) VALUES(?,?,?,?,?)', ('/r', 278626, 61663, 'tt3784472', 'sonarr'))
ev = app._match_evidence('/r', {'name': 'Yuuki Yuuna (2014)', 'library': 'Anime TV'}, {'name': 'Yuuki Yuuna', 'year': 2014, 'slug': 'yuyuyu'}, 'folder')
check('Evidence: IDs from Sonarr + why it needed review', 'TVDB 278626' in ev and 'TMDB 61663' in ev and 'IMDB tt3784472' in ev and '(from Sonarr)' in ev
      and 'Last automatic attempt' in ev and 'year mismatch' in ev, ev)
check('Evidence markup does not look like a theme group to the matching harness', '<summary><strong>' not in ev and 'theme_slug=' not in ev and 'anime_slug' not in ev)

src = Path('/src/preludarr/app.py').read_text()
check('Loader: reloadable slot + Try again on network/HTTP errors', 'id="tp-match-slot"' in src and 'b.textContent="Try again"' in src and 'data-tp-retry' in src)
check('Server-side lookup failure offers Try again', src.count('<button type="button" data-tp-retry>Try again</button>') == 3)  # 1.11: + Shows/Movies suggestions

# ------------------------------------------------------------------ P5: poster ranking ----
name = 'Bubble (2022)'
cands = [{'provider': 'TVDB', 'title': 'Bubbles', 'year': '2022', 'poster': 'https://x/0'},
         {'provider': 'TMDB', 'title': 'Bubble', 'year': '2022', 'poster': 'https://x/1'},
         {'provider': 'TMDB', 'title': 'Bubble', 'year': '2021', 'poster': 'https://x/2'},
         {'provider': 'TVDB', 'title': 'Bubble', 'year': '2022', 'poster': 'https://x/3'},
         {'provider': 'TMDB', 'title': 'Something Else', 'year': '2010', 'poster': 'https://x/4'},
         {'provider': 'TMDB', 'title': 'バブル', 'original': 'Bubble', 'year': '2022', 'poster': 'https://x/5'}]
tiers = [pm.rank(app, name, c)[0] for c in cands]
check('Tiers: exact+year best, near/similar close, unrelated weak, original title counts', tiers == [1, 0, 1, 0, 2, 0], tiers)
check('Labels say why', pm.rank(app, name, cands[2])[2] == ['Exact title', 'year 2021 (folder 2022)'] and pm.rank(app, name, cands[0])[2][1] == 'same year')
h = pm._grid('/p', cands, 'search', 'tok', True, app, name)
order = re.findall(r'name="index" value="(\d+)"', h)
check('Every candidate still shown once, each with its own original index', sorted(order) == ['0', '1', '2', '3', '4', '5'] and len(order) == 6, order)
check('Best matches first, then close, weak folded away', h.index('Best matches') < h.index('Close matches') < h.index('<details class="pm-weak">') and order[-1] == '4')
check('Same title + year from TMDB and TVDB grouped as alternate artwork', '2 artworks: TMDB, TVDB' in h)
check('Provider and year visible on every card', h.count('class="pm-src"') == 6 and 'TVDB · 2022' in h)
check('Replace checkbox and approval form kept', h.count('name="replace"') == 6 and h.count('action="/poster-choose"') == 6)
only_weak = pm._grid('/p', [cands[4]], 'search', 'tok', False, app, name)
check('Only weak results → shown openly (not hidden behind an expander)', 'pm-weak' not in only_weak and 'Weak matches' in only_weak)
check('No results → empty (caller shows its own message)', pm._grid('/p', [], 'search', 'tok', False, app, name) == '')

# popup payload: sorted, indexes unchanged
with app.db() as c:
    c.execute("INSERT OR REPLACE INTO shows(path,library,name,state,detail,updated) VALUES('/b','Anime Movies','Bubble (2022)','missing','','x')")
app.record_poster_attempt('/b', 'review', 'test', cands)
pay = pm.candidates_payload(app, '/b')
idx = [c['index'] for c in pay['candidates']]
check('Popup: best first, each keeps its index', idx[:3] == [1, 3, 5] and sorted(idx) == list(range(6)) and idx[-1] == 4, idx)
check('Popup: plain-language reason', pay['candidates'][0]['why'].startswith('Best match · Exact title · same year') and 'order' not in pay['candidates'][0], pay['candidates'][0])
check('Popup payload is JSON-safe', json.loads(json.dumps(pay))['name'] == 'Bubble (2022)')

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
