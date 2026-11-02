# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual-install defaults, live opt-in, rendering and server guards; no network."""
import html
import io
import json
import os
import sys
import tempfile
import textwrap
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'preludarr'))
import manual_settings as settings
import custom_upload
import youtube_manual

work = Path(tempfile.mkdtemp())
os.environ['CONFIG_DIR'] = str(work)
for key in ('THEMEARR_MANUAL_INSTALL_ENABLED', 'SPLICARR_MANUAL_INSTALL_ENABLED', 'PRELUDARR_MANUAL_INSTALL_ENABLED'):
    os.environ.pop(key, None)
results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)

check('Fresh installs remain disabled', not settings.enabled())
os.environ['PRELUDARR_MANUAL_INSTALL_ENABLED'] = '1'
check('Existing environment opt-in remains enabled', settings.enabled())
os.environ['THEMEARR_MANUAL_INSTALL_ENABLED'] = '0'
check('Legacy environment keeps its documented precedence', not settings.enabled())
os.environ.pop('THEMEARR_MANUAL_INSTALL_ENABLED')
settings.save(False)
check('Saved off overrides environment on for both backends', not custom_upload._enabled() and not youtube_manual.enabled())
settings.save(True)
check('Saved on enables both backends immediately', custom_upload._enabled() and youtube_manual.enabled())
check('Saved choice is a JSON boolean', json.loads((work/settings.FILE).read_text()) == {'enabled': True})
check('Settings switch reflects saved on', 'name="enabled" value="1" checked' in settings.panel())

class H:
    def __init__(self, body):
        self.headers = Message()
        self.headers['Content-Length'] = str(len(body))
        self.headers['Content-Type'] = 'application/x-www-form-urlencoded'
        self.rfile = io.BytesIO(body)
        self.connection = SimpleNamespace(settimeout=lambda _: None)
        self.sent = None
    def send(self, code, body, ctype):
        self.sent = (code, body, ctype)

redirects = []
redirect = lambda handler, url: redirects.append(url)
h = H(b'save=1')
settings.post(h, redirect)
check('Unchecked form saves off and redirects to the setting', not settings.enabled() and redirects[-1] == settings.URL)
h = H(b'save=1&enabled=1')
settings.post(h, redirect)
check('Checked form saves on without restart', settings.enabled())
for body in (b'save=1&enabled=1&enabled=0', b'save=1&enabled=oops', b'enabled=1', b'save=1&other=1'):
    h = H(body)
    settings.post(h, redirect)
    check('Malformed form is rejected without changing saved choice', h.sent[0] == 400 and settings.enabled())

# Execute the real Theme Browser secondary-tools rendering block in isolation.
source = (Path(__file__).resolve().parents[1]/'preludarr/app.py').read_text()
block = source[source.index('    extras=[]'):source.index('    # V1.7.3 faster first visits')]
code = compile(textwrap.dedent(block), '<theme-browser-tools>', 'exec')
def render(enabled):
    settings.save(enabled)
    namespace = dict(fragment_mode=False, query={}, row={'library':'Shows'},
        kinds=SimpleNamespace(is_regular=lambda _: True),
        poster_manual=SimpleNamespace(panel=lambda *args: '<p>Poster controls</p>'),
        sys=sys, custom_upload=custom_upload, __name__=__name__, selected='/test/A & B', manual_query='', manual_slug='',
        esc=html.escape, remember=[])
    exec(code, namespace)
    return ''.join(namespace['extras'])
off = render(False)
check('Disabled tools explain how to enable in Settings', off.count('Manual installs are disabled.') == 2 and 'Allow manual installs' in off and 'cookies=1#manual-installs' in off)
check('Disabled tools contain no upload or YouTube submission form', '/review-youtube-theme' not in off and '/review-custom-upload' not in off and 'name="audio"' not in off and 'name="youtube_url"' not in off)
check('Disabled summaries reveal their state before expanding', off.count('Manual installs disabled — enable in Settings') == 2)
on = render(True)
check('Enabled tools retain both review forms and escape title path', '/review-youtube-theme' in on and '/review-custom-upload' in on and '/test/A &amp; B' in on)
settings.save(False)
h = H(b'save=1')
youtube_manual.review(h, work, [], None, None)
check('Direct YouTube requests still reject disabled installs', h.sent[:2] == (403, 'Manual installations disabled'))
h = H(b'save=1')
youtube_manual.confirm(h, work, [], None, None, None)
check('Disabling also blocks pending YouTube confirmations', h.sent[0] == 403)
h = H(b'save=1')
custom_upload.review(h, work, [], None, None, '')
check('Direct custom-upload requests still reject disabled installs', h.sent[0] == 403)
check('Settings form stays behind the existing authenticated POST gate', source.index('if not auth.post(self):return') < source.index("if self.path=='/download-clients/manual-installs':"))
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
