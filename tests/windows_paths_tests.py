# SPDX-License-Identifier: GPL-3.0-or-later
"""1.12.1, GitHub issue #1: Sonarr/Radarr running on Windows report paths like G:\\TV\\Show; path mappings must accept and
translate them. Run in a Preludarr image, --network none."""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, '/src/preludarr')
import arr  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


work = Path(tempfile.mkdtemp())
media, anime = work / 'mnt' / 'media', work / 'anime'
for d in (media / 'TV' / 'Breaking Bad (2008)', media / 'TV' / 'Friends (1994)', anime / 'Bleach (2004)'):
    d.mkdir(parents=True)
app = types.SimpleNamespace(ROOTS=[('Shows', media / 'TV'), ('Anime TV', anime), ('Shows', media)])

maps = arr._parse_mappings(app, f'G:\\ => {media}\nG:\\Anime => {anime}')
check('The issue: "G:\\ => /mnt/media" is accepted (Sonarr/Radarr on Windows)', [m['remote'] for m in maps] == ['G:\\', 'G:\\Anime'], maps)
check('UNC shares and forward slashes are accepted too', arr._parse_mappings(app, f'\\\\NAS\\media => {media}')[0]['remote'] == '\\\\NAS\\media'
      and arr._parse_mappings(app, f'g:/TV/ => {media / "TV"}')[0]['remote'] == 'G:\\TV')
for bad, why in (('TV => /x', 'relative Sonarr/Radarr path'), (f'G:\\ => mnt/media', 'relative Preludarr path'), ('G:\\ => G:\\TV', 'Windows path on the Preludarr side')):
    try:
        arr._parse_mappings(app, bad); ok = False
    except ValueError:
        ok = True
    check('Still refused: ' + why, ok)

inst = {'mappings': maps + [{'remote': '/data/tv', 'local': str(media / 'TV')}]}
check('Windows paths translate to the Preludarr folder (any case, either slash)',
      arr.to_local(inst, 'G:\\TV\\Breaking Bad (2008)') == str(media / 'TV' / 'Breaking Bad (2008)')
      and arr.to_local(inst, 'g:/tv/Friends (1994)') == str(media / 'tv' / 'Friends (1994)'))
check('The longest mapping wins (G:\\Anime before G:\\)', arr.to_local(inst, 'G:\\Anime\\Bleach (2004)') == str(anime / 'Bleach (2004)'))
check('Other drives and look-alike prefixes are not mapped', arr.to_local(inst, 'H:\\TV\\X') is None and arr.to_local(inst, '/data/tvx/Show') is None)
check('".." cannot climb above the mapped drive', arr.to_local(inst, 'G:\\TV\\..\\..\\..\\etc') == str(media / 'etc'))
check('Linux paths behave as before', arr.to_local(inst, '/data/tv/Friends (1994)') == str(media / 'TV' / 'Friends (1994)'))

items = [{'path': 'G:\\TV\\Breaking Bad (2008)'}, {'path': 'G:\\TV\\Friends (1994)'}, {'path': 'G:\\TV\\Not Here (2001)'}]
sugg = arr.suggest_mappings(app, {}, items)
check('Suggestions work for Windows paths (G:\\TV → the TV root, 2 of 3 found)',
      sugg and sugg[0]['remote'] == 'G:\\TV' and sugg[0]['local'] == str(media / 'TV') and sugg[0]['hits'] == 2, sugg)
form = arr._form(app, None)
check('The mapping form shows a Windows example', 'G:\\TV =&gt; /media/tv_shows' in form or 'G:\\TV => /media/tv_shows' in form)
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
