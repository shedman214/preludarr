# SPDX-License-Identifier: GPL-3.0-or-later
"""1.12.2 (user): the Library remembers the filters the user picks; a fresh install starts at All statuses / All posters.
Run in a Preludarr image, --network none."""
import json
import os
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
sys.path.insert(0, '/src/preludarr')
import library_prefs as lp  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


q = lambda **kw: {k: [v] for k, v in kw.items()}  # noqa: E731
check('Fresh install: All statuses / All posters', lp.resolve(work, q(view='library')) == ('all', 'all') and not (work / lp.FILE).exists())
check('Picking filters in the Library form (f=1) is remembered', lp.resolve(work, q(view='library', state='recent', poster='missing', f='1')) == ('recent', 'missing')
      and lp.load(work) == {'state': 'recent', 'poster': 'missing'})
check('…and used the next time the Library opens without filters', lp.resolve(work, q(view='library')) == ('recent', 'missing'))
check('A link with a filter (e.g. sidebar Missing Themes) applies to that view only', lp.resolve(work, q(view='library', state='missing')) == ('missing', 'missing')
      and lp.load(work) == {'state': 'recent', 'poster': 'missing'})
check('Clear (All / All, f=1) is remembered too', lp.resolve(work, q(view='library', state='all', poster='all', f='1')) == ('all', 'all')
      and lp.resolve(work, q(view='library')) == ('all', 'all'))
check('Unknown values fall back to All, and are never saved', lp.resolve(work, q(view='library', state='evil', poster='../x', f='1')) == ('all', 'all')
      and lp.load(work) == {'state': 'all', 'poster': 'all'})
(work / lp.FILE).write_text('{broken')
check('A damaged settings file means All / All (no crash)', lp.load(work) == {'state': 'all', 'poster': 'all'})
lp.save(work, 'installed', 'cached')
check('Saved privately (0600)', oct((work / lp.FILE).stat().st_mode & 0o777) == '0o600' and json.loads((work / lp.FILE).read_text())['state'] == 'installed')

src = Path('/src/preludarr/app.py').read_text()
check('The Library form marks its own submits (f=1) and Clear resets to All / All',
      '<input type="hidden" name="f" value="1">' in src and "'state':'all','poster':'all','f':'1'" in src and "__import__('library_prefs').resolve(CONFIG,query)" in src)
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
