# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings → Appearance (1.12): schemes, OLED, themes, readable contrast everywhere, default untouched. Run in a Preludarr image, --network none."""
import io
import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ['CONFIG_DIR'] = str(work)
sys.path.insert(0, '/src/preludarr')
import appearance as ap  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


PAGE = ('<html><head><style>body{background:#101c30;color:#eef4ff}.panel{background:#16243b;border:1px solid #3b4b6b}.muted{color:#9fb0cc}'
        'a{color:#9fd0ff}button{background:#4f8cff;color:#fff}.ok{background:#2f8f5b;color:#fff}.bad{background:#4a2430;color:#e07a86}'
        'input{background:#101c30;color:#fff}</style></head><body><p style="color:#cfe0f8">x</p><script>var c="#4f8cff";</script></body></html>')

check('Defaults: Dark scheme, Blue theme, OLED off', ap.settings() == {'scheme': 'dark', 'oled': False, 'theme': 'blue'})
check('Dark + Blue (the original look) is sent completely untouched', ap.apply(PAGE) == PAGE)
saved = ap.save({'scheme': 'evil', 'oled': 'yes', 'theme': '../../x'})
check('Invalid choices fall back to the defaults', saved == {'scheme': 'dark', 'oled': True, 'theme': 'blue'})
check('Settings are saved privately (0600)', oct((work / ap.FILE).stat().st_mode & 0o777) == '0o600')

PAIRS = [('background:#101c30;color:#eef4ff', 'body text'), ('background:#16243b;color:#9fb0cc', 'muted text on a panel'),
         ('color:#9fd0ff', 'link on the page'), ('color:#9fb0cc', 'muted text on the page'), ('background:#4f8cff;color:#fff', 'primary button'),
         ('background:#2f8f5b;color:#fff', 'green badge'), ('background:#4a2430;color:#e07a86', 'red notice'), ('background:#101c30;color:#fff', 'input text'),
         ('background:#2a4a7a;color:#ffffff', 'active tab'), ('background:#4a3a1a;color:#ffd36b', 'amber badge')]
DECL = re.compile(r'(background|color):(#[0-9a-f]{6})')
worst = []
for theme in list(ap.STANDARD) + list(ap.IMMERSIVE):
    for mode, oled in (('dark', False), ('dark', True), ('light', False)):
        if theme in ap.IMMERSIVE and mode == 'light':
            continue
        for css, what in PAIRS:
            out = dict(DECL.findall(ap._block(css, mode, oled, theme)))
            bg = out.get('background') or ap.map_colour(ap.PAGE_BG, mode, oled, theme)
            ratio = ap.contrast(out['color'], bg)
            if ratio < 4.5:
                worst.append(f'{theme}/{mode}{"+oled" if oled else ""}: {what} {ratio:.2f}')
check('Every theme × Dark/Light/OLED keeps all common text readable (WCAG AA 4.5:1)', not worst, worst[:8])

ap.save({'scheme': 'light', 'oled': False, 'theme': 'blue'})
light = ap.apply(PAGE)
check('Light: the dark background becomes light and the body text dark', ap._lum(ap._rgb(re.search(r'body\{background:(#[0-9a-f]{6})', light).group(1))[0]) > 0.7
      and 'data-pl-scheme="light"' in light and '<meta name="color-scheme" content="light">' in light)
check('Colours in inline styles and scripts are themed too', 'color:#cfe0f8' not in light and '"#4f8cff"' not in light)
check('A page is never themed twice', ap.apply(light) == light)
ap.save({'scheme': 'dark', 'oled': True, 'theme': 'blue'})
oled = ap.apply(PAGE)
check('OLED: the page background is (nearly) black', ap._lum(ap._rgb(re.search(r'body\{background:(#[0-9a-f]{6})', oled).group(1))[0]) < 0.005)
ap.save({'scheme': 'auto', 'oled': False, 'theme': 'purple'})
check('Auto on a light device (pl_cs=light) → light page; on a dark device → dark page; the device check script is included',
      'data-pl-scheme="light"' in ap.apply(PAGE, prefers='light') and 'data-pl-scheme="dark"' in ap.apply(PAGE, prefers='dark') and 'prefers-color-scheme: light' in ap.apply(PAGE))
ap.save({'scheme': 'auto', 'oled': False, 'theme': 'blue'})
check('Auto + Blue on a dark device: the original look', ap.apply(PAGE, prefers='dark').count('#101c30') >= 1)
ap.save({'scheme': 'dark', 'oled': False, 'theme': 'terminal'})
term = ap.apply(PAGE)
check('Immersive theme: its own palette and font', 'monospace' in term and 'data-pl-theme="terminal"' in term and '#101c30' not in term)
for k in ap.STANDARD:
    acc = ap.map_colour('#4f8cff', 'dark', False, k)
    check('Accent swatch ' + k + ' is its own colour', (k == 'blue') == (acc == '#4f8cff') if k != 'slate' else acc != '#4f8cff')


class H:
    def __init__(self, path, body=''):
        b = body.encode(); self.path = path; self.headers = {'Content-Length': str(len(b))}; self.rfile = io.BytesIO(b); self.sent = None; self.loc = None

    def send(self, code, body, ctype='text/html'):
        self.sent = (code, body)


class App:
    pass


import types  # noqa: E402
sys.modules['media_servers'] = types.SimpleNamespace(settings_shell=lambda t, a: '<html><head></head><body><aside>')
h = H('/settings/appearance'); ap.page(App, h)
pg = h.sent[1]
check('The page offers Light / Dark / Auto, OLED, 6 standard and 8 immersive themes, and a live preview', all(x in pg for x in ('value="light"', 'value="dark"', 'value="auto"', 'name="oled"', 'id="pl-preview"'))
      and sum(pg.count('value="' + k + '"') for k in ap.STANDARD) == 6 and sum(pg.count('value="' + k + '"') for k in ap.IMMERSIVE) == 8)
data = re.search(r'atob\("([^"]+)"\)', pg).group(1)
import base64  # noqa: E402
check('Preview colours for every combination are prepared (and kept out of the page transform)', len(json.loads(base64.b64decode(data))) == 2 * 2 * 14)
h = H('/settings/appearance', urllib.parse.urlencode({'scheme': 'light', 'oled': '1', 'theme': 'rose'}))
ap.post(App, h, lambda hh, loc: setattr(hh, 'loc', urllib.parse.unquote_plus(loc)))
check('Saving shows what was saved', ap.settings() == {'scheme': 'light', 'oled': True, 'theme': 'rose'} and 'Saved: Light · OLED · Rose theme.' in h.loc, h.loc)
ap.save({'scheme': 'dark', 'oled': False, 'theme': 'synthwave'})
themed = ap.apply(pg)
check('Theme swatches keep their true colours on a themed page', ap.map_colour('#4f8cff', 'dark', False, 'blue') in themed and ap.map_colour('#4f8cff', 'dark', False, 'green') in themed)
check('Dark blues (e.g. status boxes) follow immersive themes too', ap.map_colour('#17305a', 'dark', False, 'terminal') != '#17305a'
      and abs(__import__('colorsys').rgb_to_hls(*ap._rgb(ap.map_colour('#17305a', 'dark', False, 'terminal'))[0])[0] * 360 - 130) < 25)
src = Path('/src/preludarr/login.py').read_text()
check('Every page goes through the theme step, and Settings has an Appearance entry', "__import__('appearance').apply(" in src and "'/settings/appearance','Appearance'" in src)
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
