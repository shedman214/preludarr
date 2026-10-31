# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixes from the 1.12 pre-release fresh-install test (#15). Run in a Preludarr image, --network none."""
import os
import re
import sys
import tempfile
from pathlib import Path

work = Path(tempfile.mkdtemp())
(work / 'tv').mkdir(); (work / 'films').mkdir()
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(work / 'none-a'), MOVIE_ROOT=str(work / 'none-b'), AUTO_DOWNLOAD='0', POSTER_BACKGROUND_ENABLED='0')
os.environ.pop('THEMEARR_MANUAL_INSTALL_ENABLED', None); os.environ.pop('PRELUDARR_MANUAL_INSTALL_ENABLED', None)
sys.path.insert(0, '/src/preludarr')
import app  # noqa: E402
sys.modules['app'] = app
results = []


def check(name, ok, detail=''):
    results.append(bool(ok)); print(('PASS ' if ok else 'FAIL ') + name + (f'  — {detail}' if detail and not ok else ''), flush=True)


src = Path('/src/preludarr/app.py').read_text()
block = src[src.index("if self.path=='/confirm-theme':"):src.index("fingerprint=take_theme_confirmation(token")]
check('Theme Browser AnimeThemes installs are not blocked when manual installs are off (default)', "getenv('THEMEARR_MANUAL_INSTALL_ENABLED'" not in block)
check('…uploads and YouTube links still follow the manual-install setting', all("getenv('THEMEARR_MANUAL_INSTALL_ENABLED', '0') == '1'" in Path('/src/preludarr/' + f).read_text().replace('"', "'")
                                                                              for f in ('custom_upload.py', 'youtube_manual.py')))

ob = __import__('onboarding')
check('New install (no root folders): the dashboard shows "Getting started" with the next steps', app.ROOTS == [] and 'Getting started' in ob.box(app)
      and 'href="/media-management"' in ob.box(app) and 'href="/download-clients?tab=sources"' in ob.box(app))
rf = __import__('root_folders')
rf._save(app, [{'path': str(work / 'films'), 'type': 'Movies', 'name': 'Movies'}]); rf.load(app)
check('Movies root but no free theme source on: a short tip instead', 'Tip:' in ob.box(app) and 'Getting started' not in ob.box(app))
(Path(app.CONFIG) / __import__('sources').FILE).write_text('{"plex": true, "themerrdb": true}')
check('…and nothing once a source is on', ob.box(app) == '')

lg = Path('/src/preludarr/login.py').read_text()
check('Setup and sign-in cards fit a phone screen (padding counted in the width)', lg.count('main{box-sizing:border-box;width:min(') == 2)
check('Pages declare a dark colour scheme unless Appearance already set one (dark scrollbars)', 'content="dark"></head>' in lg)
check('No old version names in the Automatic Themes text', 'V1.7.3' not in Path('/src/preludarr/auto_themes.py').read_text().split('def ', 1)[1])
check('Empty base-image folders (cdrom, floppy, usb) are not in /media', not any(Path('/media', d).exists() for d in ('cdrom', 'floppy', 'usb')))
import xml.etree.ElementTree as ET  # noqa: E402
repo = ET.parse('/src/templates/my-preludarr.xml').getroot().find('Repository').text
wf = Path('/src/.github/workflows/docker.yml').read_text()
check('Unraid template pulls a tag that exists: :latest (stable releases) and beta follows every release',
      repo == 'ghcr.io/shedman214/preludarr:latest' and "type=raw,value=latest,enable=${{ steps.version.outputs.prerelease == 'false' }}" in wf
      and 'type=raw,value=beta\n' in wf, repo)
tpl = ET.parse('/src/templates/my-preludarr.xml').getroot()
check('Template for Community Applications: TemplateURL points at itself, beta branch, licence, screenshots; ca_profile.xml has a Profile',
      tpl.find('TemplateURL').text.endswith('/main/templates/my-preludarr.xml') and tpl.find('Branch/Tag').text == 'beta'
      and tpl.find('License').text and len(tpl.findall('Screenshot')) >= 3 and (ET.parse('/src/ca_profile.xml').getroot().find('Profile').text or '').strip())
print(f'\n{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
