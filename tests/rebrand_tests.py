# SPDX-License-Identifier: GPL-3.0-or-later
"""Brand tests: Preludarr (1.12; formerly Splicarr 1.10–1.11, originally Themearr). Branding, compatibility with existing installs
of either older name, and no stray old branding in visible text. Run in a Preludarr image, --network none."""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'anime').mkdir(); (work / 'movies').mkdir(); (work / 'config').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'anime'), MOVIE_ROOT=str(work / 'movies'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
sys.path.insert(0, '/src/preludarr')
import general_settings as g  # noqa: E402
import login as tl  # noqa: E402
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


# ------------------------------------------------------------------ branding ----
side, login = tl.brand_html(), tl.brand_html('login')
check('Sidebar + login use the Preludarr logo with the tagline as alt text', all('preludarr-logo.png' in x and 'alt="Preludarr — Theme your library"' in x for x in (side, login)))
check('Logo keeps its shape (720×245, height auto)', 'width="720" height="245"' in side and 'height:auto' in side)
icons = tl.icon_tags()
check('Favicon / apple-touch icons = Preludarr emblem', all('preludarr-emblem-' + n + '.png' in icons for n in ('16', '32', '48', '180')) and 'splicarr-emblem' not in icons)
man = json.loads(tl.manifest_json())
check('App manifest: name Preludarr, emblem icons', man['name'] == 'Preludarr' and all('preludarr-emblem' in i['src'] for i in man['icons']))
dims = {}
for n in ('preludarr-logo.png', 'preludarr-emblem-512.png', 'preludarr-emblem-32.png'):
    dims[n] = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,pix_fmt', '-of', 'csv=p=0', '/src/preludarr/static/' + n],
                             capture_output=True, text=True).stdout.strip()
check('Assets: logo 720×245 and emblems square, all with transparency', dims == {'preludarr-logo.png': '720,245,rgba', 'preludarr-emblem-512.png': '512,512,rgba', 'preludarr-emblem-32.png': '32,32,rgba'}, dims)
check('New assets allow-listed; older Splicarr/Themearr files still served for cached pages',
      all(n in tl.STATIC_FILES for n in ('preludarr-logo.png', 'preludarr-emblem-512.png', 'splicarr-logo.png', 'splicarr-emblem-32.png', 'theme-forge-logo.png')))

# ------------------------------------------------------------------ instance name ----
check('New install: default name Preludarr', g.settings()['instance_name'] == 'Preludarr' and tl.instance_name() == 'Preludarr')
cfg = work / 'config' / g.FILE
for saved, want in (('Themearr', 'Preludarr'), ('Splicarr', 'Preludarr'), ('Themearr DEMO', 'Themearr DEMO'), ('Home Themes', 'Home Themes')):
    cfg.write_text(json.dumps({'instance_name': saved}))
    check(f'Saved name "{saved}" → shown as "{want}"', g.settings()['instance_name'] == want, g.settings()['instance_name'])
page = tl.add_icons('<!doctype html><html><head><title>Settings - Preludarr</title></head><body>x</body></html>')
check('Custom instance name still replaces the app name in the tab', '<title>Settings - Home Themes</title>' in page, page[:120])
cfg.unlink()
check('…and the default leaves "Preludarr" in the tab', '<title>Settings - Preludarr</title>' in tl.add_icons('<!doctype html><html><head><title>Settings - Preludarr</title></head><body></body></html>'))

# ------------------------------------------------------------------ settings aliases (new process) ----
probe = ("import os,sys;sys.path.insert(0,'/src/preludarr');import app;"
         "print(os.environ.get('THEMEARR_EXTRA_ALLOWED_HOSTS'),os.environ.get('THEMEARR_PUBLIC_URL'),os.environ.get('THEMEARR_LIBRARY_MOUNTS'))")
env = dict(os.environ, PRELUDARR_EXTRA_ALLOWED_HOSTS='a.lan:1', PRELUDARR_PUBLIC_URL='https://new.example.org', THEMEARR_PUBLIC_URL='https://old.example.org',
           SPLICARR_LIBRARY_MOUNTS='/m1')
out = subprocess.run([sys.executable, '-B', '-c', probe], env=env, capture_output=True, text=True, cwd=str(work)).stdout.strip().splitlines()[-1:]
check('PRELUDARR_* and SPLICARR_* settings work; an existing THEMEARR_* value wins', out == ['a.lan:1 https://old.example.org /m1'], out)

# ------------------------------------------------------------------ compatibility ----
src = {p.name: p.read_text() for p in Path('/src/preludarr').glob('*.py')}
check('API answer: app Preludarr, formerly Splicarr and Themearr', "payload={'app':'Preludarr','formerly':'Splicarr, Themearr'" in src['app.py'])
check('Sign-in cookie name unchanged (no forced sign-out)', 'themearr_session=' in src['login.py'])
check('Settings files keep their names (existing settings load)', "FILE = 'themearr_general.json'" in src['general_settings.py'] and "KEY_FILE = 'themearr_api_key'" in src['general_settings.py'])
check('Backup format unchanged (old backups still recognised)', "SCHEMA = 'themearr.system-backup.v1'" in src['restore.py'] and 'tsbackup-' in src['restore.py'])
check('Release history keeps the real old names', 'previously named **Splicarr**' in Path('/src/CHANGELOG.md').read_text() and 'Rebrand' in Path('/src/CHANGELOG.md').read_text())
check('User-Agent is Preludarr everywhere', 'Themearr/' not in ''.join(src.values()) and 'Splicarr/1.' not in ''.join(src.values())
      and src['app.py'].count("'Preludarr/1.12 (self-hosted theme manager)'") >= 4)
check('Update check looks at shedman214/preludarr (old env names still honoured)', "'shedman214/preludarr'" in src['updates.py'] and 'SPLICARR_UPDATE_CHECK' in src['updates.py'])

# ------------------------------------------------------------------ no stray branding in visible text ----
import ast  # noqa: E402
ALLOWED = {"'Themearr'", "'Splicarr'", "'Theme Forge branding'", "'Splicarr, Themearr'"}  # compatibility values / history (documented)
stray = []
for name, text in src.items():
    if name in ('themearr_system_restore_v155.py',):  # host restore engine: targets the existing container name "Themearr"
        continue
    lines = text.splitlines()
    hist = set()
    if name == 'release_notes.py' and 'RELEASES = (' in text:
        a = next(i for i, l in enumerate(lines) if l.startswith('RELEASES = ('))
        b = next(i for i in range(a, len(lines)) if lines[i].startswith(')'))
        hist = set(range(a + 1, b + 2))
    docstrings = {n.body[0].lineno for n in ast.walk(ast.parse(text))
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], 'value', None), ast.Constant) and isinstance(n.body[0].value.value, str)}
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type != tokenize.STRING or t.start[0] in docstrings or t.start[0] in hist or t.string in ALLOWED:
            continue
        body = re.sub(r'/\*.*?\*/', '', t.string, flags=re.S).replace('X-Themearr-', '').replace('X-Splicarr-', '')  # CSS comments + internal header names
        if 'Themearr' in body or 'Theme Forge' in body or 'Splicarr' in body:
            stray.append(f'{name}:L{t.start[0]}')
check('No old branding left in visible strings (only the documented legacy names)', not stray, stray)

print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
