# SPDX-License-Identifier: GPL-3.0-or-later
"""V1.9.8 — shared Settings/System design layer, System Overview/Logs/Backup. Run in a Themearr image, --network none."""
import io
import os
import re
import sys
import tempfile
import time
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
import ui as ui  # noqa: E402
import system as sysv  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


labels = {'Save Changes': 'tm-primary', '＋ Add Root Folder': 'tm-primary', 'Create backup now': 'tm-primary', 'Run health scan': 'tm-primary',
          'Scan': 'tm-secondary', 'Edit': 'tm-secondary', 'Test': 'tm-secondary', 'Configure': 'tm-secondary', 'Turn on': 'tm-secondary',
          '✕': 'tm-danger', 'Remove': 'tm-danger', 'Disconnect': 'tm-danger', '🔄 Regenerate': 'tm-danger', 'Empty recycle bin now': 'tm-danger', 'Removed files': 'tm-secondary'}
check('Button hierarchy by label', all(ui.classify(k) == v for k, v in labels.items()), [(k, ui.classify(k)) for k, v in labels.items() if ui.classify(k) != v])
check('value="remove" is always destructive', ui.classify('Go', 'name="action" value="remove"') == 'tm-danger')
html_in = '<html><head><title>x</title></head><body><main><button type="submit">Save</button><script>a="<button class=\\"z\\">Remove</button>"</script></main></body></html>'
out = ui.apply(html_in, '/settings')
check('CSS injected once, last in <head>', out.count('id="tm-ui"') == 1 and out.index('id="tm-ui"') < out.index('</head>') and ui.apply(out, '/settings') == out)
check('Buttons classified, <script> content untouched', 'class="tm-btn tm-primary">Save' in out and 'a="<button class=\\"z\\">Remove</button>"' in out)
check('Not applied outside Settings/System (dashboard unchanged)', ui.apply(html_in, '/') == html_in and ui.apply(html_in, '/themes') == html_in)
check('Applied on all Settings/System paths', all('tm-ui' in ui.apply(html_in, p) for p in ('/settings', '/media-management', '/media-servers', '/download-clients', '/system', '/library-health/trash')))
stamp = ui.when(time.time())
check('Timestamps carry an explicit timezone', re.search(r'(AM|PM) [A-Z+\-0-9]{2,6}$', stamp) is not None, stamp)
check('Unknown time handled', ui.when('garbage') == 'unknown time')


class H:
    def __init__(self, path):
        self.path = path; self.headers = {}; self.code = None; self.hdrs = []; self.wfile = io.BytesIO()

    def send_response(self, c):
        self.code = c

    def send_header(self, k, v):
        self.hdrs.append((k, v))

    def end_headers(self):
        pass


h = H('/system?tab=health')
sysv._send(h, 200, '<html><head></head><body><main><script>x=1</script><button>Run health scan</button></main></body></html>')
body = h.wfile.getvalue().decode()
csp = dict(h.hdrs)['Content-Security-Policy']
nonce = re.search(r"'nonce-([^']+)'", csp)
check('System scripts run only with this page’s nonce (fixes blocked Library Health progress)', nonce and f'<script nonce="{nonce.group(1)}">' in body and "default-src 'none'" in csp)
check('Nonce changes per response', (lambda h2: (sysv._send(h2, 200, '<html><head></head><body></body></html>'), re.search(r"'nonce-([^']+)'", dict(h2.hdrs)['Content-Security-Policy']).group(1))[1])(H('/system')) != nonce.group(1))

page = lambda **q: sysv.page(app.CONFIG, dict({'tab': [q.pop('tab', 'overview')]}, **{k: (v if isinstance(v, list) else [v]) for k, v in q.items()}))  # noqa: E731
ov = page()
check('Overview: Version, Database, Backups, Library Health, Activity cards linked to tabs', all(x in ov for x in ('href="/system?tab=releases"', 'href="/system?tab=backup"', 'href="/system?tab=health"', 'href="/system?tab=activity"')))
check('Overview: labels current / unavailable, nothing invented', 'tm-label current' in ov and 'Not scanned yet' in ov and 'tm-label unavailable' in ov)
check('Overview: no backups shown honestly', 'None yet' in ov)

logdir = work / 'config' / 'logs'; logdir.mkdir(parents=True, exist_ok=True)
(logdir / 'themearr.log').write_text('\n'.join([
    '2026-10-27 01:00:00 INFO 10.0.0.9 - - [27/Oct/2026 01:00:00] "GET /api/status HTTP/1.1" 200 -',
    '2026-10-27 01:00:01 INFO Scan finished',
    '2026-10-27 01:00:02 WARNING 10.0.0.9 - - [27/Oct/2026 01:00:02] "POST /login HTTP/1.1" 401 -',
    '2026-10-27 01:00:03 ERROR Something broke']) + '\n')
lg = page(tab='logs')
check('Logs: routine successful requests hidden by default', '/api/status' not in lg and 'Scan finished' in lg and '1 hidden by filters' in lg)
check('Logs: showing routine requests on demand', '/api/status' in page(tab='logs', routine=['show']))
check('Logs: unticking the box (hidden "show" only) shows them', '/api/status' in page(tab='logs', routine=['show']) and '/api/status' not in page(tab='logs', routine=['show', 'hide']))
check('Logs: errors only', 'Something broke' in page(tab='logs', level='error') and 'Scan finished' not in page(tab='logs', level='error'))
check('Logs: empty state message', 'No log lines match these filters' in page(tab='logs', q='zzz-nothing'))
check('Logs: auto-refresh is off by default and visibly switchable', 'Auto-refresh: <strong>Off' in lg and 'http-equiv="refresh"' not in lg and 'http-equiv="refresh" content="30"' in page(tab='logs', auto='1'))
bk = page(tab='backup')
check('Backup: created date first, verify vs restore explained, credential notice visible, details collapsed',
      '<th>Created</th>' in bk and 'only checks the file (read-only)' in bk and 'keep downloads private' in bk and '<details class="tm-help"><summary>How restore works' in bk)
adv = page(tab='advanced')
check('Advanced: only real links, no placeholder promise', 'will appear here as they are added' not in adv and '/settings?section=logging' in adv)
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
