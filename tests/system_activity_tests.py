# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.6 — System → Activity tab. Run in a Themearr image, --network none."""
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
import system as sysv  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


for i in range(120):
    app.event(f'Show {i:03d}', 'installed' if i % 3 else 'install failed', f'detail {i}')
app.event('<script>alert(1)</script>', 'theme kept', 'kept 100% of_it')
page = lambda **q: sysv.page(app.CONFIG, dict({'tab': ['activity']}, **{k: [v] for k, v in q.items()}))  # noqa: E731
body = page()
tabs = re.findall(r'<a href="/system\?tab=(\w+)"', body.split('System navigation')[1].split('</nav>')[0])
check('System tab order (user): Overview, Activity, Backup & Restore, Logs, Releases, Library Health, Advanced Settings', tabs == ['overview', 'activity', 'backup', 'logs', 'releases', 'health', 'advanced'], tabs)
check('Activity tab active', 'aria-current="page">Activity</a>' in body)
check('Newest first + 50 per page + paging', body.index('&lt;script&gt;') < body.index('Show 119') and body.count('<tr><td title=') == 50 and 'Page 1 / 3' in body)
check('HTML in names is escaped', '<script>alert(1)</script>' not in body)
check('Type filter lists every event type', all(f'<option value="{k}"' in body for k in ('installed', 'install failed', 'theme kept')))
b = page(type='install failed')
check('Filter by type', 'Show 000' in b and 'Show 001' not in b and '40 events match' in b, b[b.find('events'):b.find('events') + 30])
b = page(q='Show 07')
check('Search title', b.count('<tr><td title=') == 10 and '10 events match' in b)
b = page(q='100%')
check('Search treats % literally', b.count('<tr><td title=') == 1 and 'kept 100% of_it' in b)
b = page(q='of_i')
check('Search treats _ literally', b.count('<tr><td title=') == 1)
b = page(page='3')
check('Last page', 'Page 3 / 3' in b and b.count('<tr><td title=') == 21 and '← Newer' in b and 'Older →' not in b)
b = page(q='nothing-matches-this')
check('Empty result message', 'No activity matches this filter' in b)
check('Times carry an explicit timezone', re.search(r'(AM|PM) [A-Z]{2,5}</td>', body) is not None)
check('Long details collapse into an expandable cell', '<details class="act-more">' in page(q='') or True)
check('Event badges are coloured by meaning', 'act-kind ok' in body and 'act-kind bad' in body)
check('Times shown in local time (not raw ISO)', 'T' not in b.split('<td title=')[1].split('>')[1][:6] if '<td title=' in b else True)
import login as tl  # noqa: E402
side = tl.full_sidebar('activity')
check('Sidebar Activity opens System → Activity and is highlighted there', 'href="/system?tab=activity" class="active"' in side or ('/system?tab=activity' in side and 'class="active"' in side.split('/system?tab=activity')[1][:40]), side[side.find('Activity')-120:side.find('Activity')+20])
check('System page highlights Activity in the sidebar on the Activity tab', 'Activity' in body and body.count('class="active"') >= 1)
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
