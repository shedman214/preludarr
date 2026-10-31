# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.8 — Connect / General / Download Clients tabs, floating box removed, Library Search markup preserved. Run in a Themearr image, --network none."""
import io
import os
import re
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0',
                  THEMEARR_WRITE_AUTH='aa' * 16 + ':' + 'bb' * 32)
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import media_servers as v4  # noqa: E402
import login as tl  # noqa: E402
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


q = lambda **kw: {k: [v] for k, v in kw.items()}  # noqa: E731
check('Connect: Status is the default tab', v4.connect_tab({}) == 'status')
check('Connect: explicit ?tab= wins', all(v4.connect_tab(q(tab=t)) == t for t in ('status', 'media', 'posters', 'arr', 'anidb')))
check('Connect: unknown tab falls back to Status', v4.connect_tab(q(tab='x')) == 'status')
check('Connect: back on the tab that just saved', (v4.connect_tab(q(arr_notice='x')), v4.connect_tab(q(poster_notice='x')), v4.connect_tab(q(anidb_notice='x')), v4.connect_tab(q(configure='plex'))) == ('arr', 'posters', 'anidb', 'media'))
nav = v4.connect_tabs('arr')
check('Connect: tab order Status, Media servers, Poster providers, Sonarr & Radarr, AniDB', re.findall(r'tab=(\w+)', nav) == ['status', 'media', 'posters', 'arr', 'anidb'])
check('Connect: only the chosen section is shown', 'main .cx-sec{display:none}' in nav and 'main #cx-sec-arr{display:block}' in nav and 'aria-current="page">Sonarr &amp; Radarr<' in nav)


class H:
    def __init__(self, path):
        self.path = path; self.headers = {'Host': '192.168.1.20:8788'}; self.client_address = ('10.0.0.9', 1); self.sent = None

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)


# Connect page assembled end-to-end (no network: no media servers configured, so the page's auto-check has nothing to test)
import poster_connect as pc  # noqa: E402
h = H('/media-servers?tab=posters')
pc.render_connect_page(h, app.CONFIG, v4.settings_page, app.ROOTS, app.db)
body = h.sent[1]
secs = re.findall(r'class="cx-sec" id="cx-sec-(\w+)"', body)
check('Connect page: one section per tab, Status first', secs == ['status', 'media', 'posters', 'arr', 'anidb'], secs)
check('Connect page: Posters tab selected', 'main #cx-sec-posters{display:block}' in body)
check('Connect page: Media servers section closes before Posters starts (not nested)', '</section><div class="cx-sec" id="cx-sec-posters">' in body)

# Download Clients tabs
import download_clients as dc  # noqa: E402
for t in ('engines', 'sources'):
    h = H('/download-clients?tab=' + t); dc.page(app, h)
    check('Download Clients: ' + t + ' tab shows only its section', 'main #dc-sec-' + t + '{display:block}' in h.sent[1] and h.sent[1].count('class="dc-sec"') == 3)  # V1.9.14: engines, sources, rate limits

# floating Settings/System/Log out box removed (also for the fixed sidebar variant used by the Theme Browser)
auth = tl.Auth(); auth.active = lambda h: True
page = '<!doctype html><html><head></head><body>' + tl.full_sidebar('themes', True) + '<main>x</main></body></html>'
out = auth.decorate(H('/themes'), page)
check('No floating box on pages with the fixed full sidebar', 'tm-float' not in out and 'position:fixed;top:10px;right:12px' not in out)
out = auth.decorate(H('/whatever'), '<!doctype html><html><head></head><body><main>no sidebar</main></body></html>')
check('No floating box anywhere any more', 'tm-float' not in out)

# Library Search panel: same forms, fields, values and actions as before
src = open('/src/preludarr/app.py').read()
i = src.index('V1.9.8 (user 2026-10-27): Library Search restyled'); seg = src[i:src.index('parts.append(poster_filters.modal_script())', i)]
for needle in ('method="GET" action="/"', 'name="view" value="library"', 'name="q"', 'name="library"', 'name="state"', 'name="poster"', 'class="ls-btn ls-quiet">Clear',  # 1.11: Clear keeps the Library tab (dynamic href)
           
               'method="POST" action="/poster-filtered-review"', 'method="POST" action="/queue/add-filtered"', 'name="confirm" value="1" required', 'id="themearr-theme-queue-link"',
               "Shows the list first; nothing is queued until you confirm. Existing rate limits apply.",  # V1.9.12 wording
               "Nothing is installed until you approve it in the Theme Queue."):  # V1.9.14 shorter help
    check('Library Search keeps ' + needle[:60], needle in seg)
check('Library Search: Search is the primary button, bulk actions are outlined', 'class="ls-btn ls-primary">Search' in seg and seg.count('ls-btn ls-outline') == 2)
css = re.sub(r'/\*.*?\*/', '', app.LIBRARY_SEARCH_CSS.replace('<style>', '').replace('</style>', ''), flags=re.S)
sels = [sel.strip() for block in re.findall(r'([^{}]+)\{', css) for sel in block.split(',') if sel.strip() and not sel.strip().startswith('@container')]
check('Library Search styles are scoped to the panel (every selector starts with .ls-panel)', sels and all(x.startswith('.ls-panel') for x in sels), [x for x in sels if not x.startswith('.ls-panel')])
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
