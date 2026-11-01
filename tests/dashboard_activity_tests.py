# SPDX-License-Identifier: GPL-3.0-or-later
"""1.12.2 (user): Activity on the Dashboard with a remembered page size; System → Activity unchanged.
Run in a Preludarr image, --network none."""
import os
import re
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
for d in ('anime', 'movies'):
    (work / d).mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import system  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


with app.db() as c:
    for i in range(60):
        c.execute('INSERT INTO events(at,name,status,detail) VALUES(?,?,?,?)',
                  (f'2026-11-01T10:{i // 60:02d}:{i % 60:02d}Z', f'Show {i:02d}', 'install failed' if i % 10 == 0 else 'installed', f'detail {i}'))
q = lambda **kw: {k: [v] for k, v in kw.items()}  # noqa: E731
rows = lambda html: len(re.findall(r'<tr><td title="Recorded', html))  # noqa: E731

dash = system._activity_page(app, {}, 'dashboard')
check('Dashboard Activity: 25 events per page by default, newest first', rows(dash) == 25 and dash.index('Show 59') < dash.index('Show 58') and 'id="dash-activity"' in dash)
check('…with a "Show N per page" choice (10/25/50/100/250) and a link to the full history', all(f'<option value="{n}"' in dash for n in (10, 25, 50, 100, 250))
      and 'name="asize"' in dash and 'href="/system?tab=activity"' in dash)
d10 = system._activity_page(app, q(asize='10'), 'dashboard')
check('Choosing 10 shows 10 and is remembered for the next visit', rows(d10) == 10 and rows(system._activity_page(app, {}, 'dashboard')) == 10)
check('Other sizes work (100 → all 60)', rows(system._activity_page(app, q(asize='100'), 'dashboard')) == 60)
check('An invalid size is ignored (keeps the remembered 100)', rows(system._activity_page(app, q(asize='7'), 'dashboard')) == 60)
system._activity_page(app, q(asize='10'), 'dashboard')
f = system._activity_page(app, q(atype='install failed'), 'dashboard')
check('Dashboard filters use their own names (aq, atype, apage) so they never clash with the Library', rows(f) == 6 and 'name="aq"' in f and 'name="atype"' in f
      and 'name="q"' not in f.split('id="dash-activity"', 1)[1])
p2 = system._activity_page(app, q(apage='2'), 'dashboard')
check('Paging stays on the Dashboard', rows(p2) == 10 and 'Show 49' in p2 and 'href="/?apage=3#dash-activity"' in p2.replace('&amp;', '&'))
sysp = system._activity_page(app, q(asize='10'), 'system')
check('System → Activity is unchanged: 50 per page, its own form, no page-size control', rows(sysp) == 50 and 'name="asize"' not in sysp and 'action="/system"' in sysp)
src = Path('/src/preludarr/app.py').read_text()
check('The Dashboard (and only the Dashboard) shows it', "if view=='':parts.append(system._activity_page(sys.modules[__name__],query,'dashboard'))" in src)
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
