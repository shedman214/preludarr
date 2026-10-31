#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rewrite requirements.txt with every published sha256 of each pinned version (from PyPI), for pip --require-hashes.

Usage: python3 tools/lock_requirements.py   (edit the versions in PINS first)
"""
import json
import urllib.request
from pathlib import Path

PINS = [('yt-dlp[default]', 'yt-dlp', '2026.8.19'), ('yt-dlp-ejs', None, '0.8.0'), ('brotli', None, '1.2.1'), ('certifi', None, '2026.10.23'),
        ('charset-normalizer', None, '3.5.2'), ('idna', None, '3.20'), ('mutagen', None, '1.48.1'), ('pycryptodomex', None, '3.24.1'),
        ('requests', None, '2.34.2'), ('urllib3', None, '2.8.0'), ('websockets', None, '17.3')]

out = ['# SPDX-License-Identifier: GPL-3.0-or-later',
       '# Python packages in the Preludarr image (yt-dlp and its dependencies), pinned with every published file hash.',
       '# Installed with: pip install --require-hashes --no-deps -r requirements.txt',
       '# Update: change the versions in tools/lock_requirements.py and run it (regenerates every hash from PyPI).', '']
for spec, name, ver in PINS:
    name = name or spec
    data = json.load(urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{ver}/json', timeout=30))
    hashes = sorted({u['digests']['sha256'] for u in data['urls']})
    if not hashes:
        raise SystemExit(f'No files for {name} {ver}')
    out.append(f'{spec}=={ver} \\')
    out += [f'    --hash=sha256:{h}' + (' \\' if i < len(hashes) - 1 else '') for i, h in enumerate(hashes)]
(Path(__file__).resolve().parent.parent / 'docker' / 'requirements.txt').write_text('\n'.join(out) + '\n')
print('docker/requirements.txt updated')
