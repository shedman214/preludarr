# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.12 — polling stops when signed out, bulk-scope wording, tablet/phone drawer sidebar. Run in a Themearr image, --network none."""
import os
import re
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import login as tl  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


src = {n: Path('/src/preludarr/' + n).read_text() for n in ('app.py', 'poster_filters.py', 'library_health.py', 'permissions.py')}
OUT = 'r.status===401||(r.redirected&&new URL(r.url).pathname'

# ---------------------------------------------------------------- 1. polling ----
app = src['app.py']
check('Dashboard job poll: pauses while the tab is hidden', 'async function poll(){if(document.hidden)return;' in app)
check('Dashboard job poll: stops + explains when signed out', OUT in app and 'clearInterval(pollTimer)' in app and 'You were signed out' in app)
check('Dashboard job poll: resumes when the tab is shown again', 'visibilitychange' in app)
check('Poster recovery counters: pause when hidden, stop when signed out', 'if(document.hidden)return;const r=await fetch("/poster-v2-status"' in app and 'clearInterval(v2Timer)' in app)
pf = src['poster_filters.py']
check('Poster queue: signed-out detected once, history polling stops', "signedOut=true" in pf and 'if(signedOut){clearInterval(historyPolling);return}' in pf)
check('Library Health progress stops when signed out', 'clearInterval(lhT)' in src['library_health.py'])
check('Permissions fix progress stops when signed out', 'clearInterval(pmT)' in src['permissions.py'])
check('No poll loop left without a stop (all intervals stored)', not re.search(r'[;}]setInterval\((poll|refresh|tick),', app + src['permissions.py']))

# ---------------------------------------------------------------- 2. wording ----
check('Bulk scope said once, above the actions', app.count('Bulk actions apply to <strong>all ') == 1 and 'every page, not only the ones shown below' in app)
check('Poster button names the scope', "' missing poster',('' if poster_preview_count==1 else 's'),' (all pages)</button>" in app)
check('Queue checkbox: clear label, still required, same field', '<input type="checkbox" name="confirm" value="1" required> Confirm all ' in app)
check('Queue help: tick, then Queue; nothing installs until approved', 'Tick <em>Confirm all' in app and 'Nothing is installed until you approve it' in app)  # V1.9.14 shorter help

# ---------------------------------------------------------------- 3. drawer ----
side = tl.full_sidebar('general')
check('Sidebar still ends with </aside> (settings_shell relies on it)', side.endswith('</aside>'))
check('Menu button inside the sidebar, labelled, controls it, starts closed', 'id="tm-sidebar"' in side and 'class="tm-menu-btn" aria-controls="tm-sidebar" aria-expanded="false">☰ Menu</button>' in side)
check('Drawer script: toggle, Escape, backdrop, resize, focus', all(x in side for x in ('aria-expanded', '"Escape"', '.tm-scrim', '"resize"', 'f.focus()')))
check('Drawer only below 1100px; hidden drawer links are not tabbable', '@media(max-width:1100px){' in side and 'visibility:hidden' in side and 'aside.tm-full.tm-open{left:0;visibility:visible' in side)
check('Desktop: menu button hidden', '.tm-menu-btn,.tm-scrim{display:none}' in side)
check('Fixed sidebar pages get full width on tablets', '.tm-fixed~.themearr-content{margin-left:0!important}' in side)
check('Respects reduced motion', 'prefers-reduced-motion' in side)
check('No inline event handlers (System pages allow only nonce scripts)', not re.search(r'\son(click|keydown)=', side))

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
