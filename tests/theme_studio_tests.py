# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline Theme Studio regressions using generated fixture audio, never real media."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from unittest.mock import patch

work = Path(tempfile.mkdtemp())
root, movies = work / 'anime', work / 'movies'
first, second, third = root / 'Fixture title', root / 'New fixture title', root / 'Upload fixture'
for folder in (first, second, third, movies):
    folder.mkdir(parents=True)
fixture = work / 'fixture.mp3'
subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6',
                '-codec:a', 'libmp3lame', '-q:a', '4', str(fixture)], check=True, timeout=30)
original = fixture.read_bytes()
(first / 'theme.mp3').write_bytes(original)
os.environ.update(CONFIG_DIR=str(work / 'config'), TV_ROOT=str(root), MOVIE_ROOT=str(movies),
                  AUTO_DOWNLOAD='false', POSTER_BACKGROUND_ENABLED='false')
sys.path.insert(0, '/src/preludarr')
import anidb
with patch.object(anidb, 'enabled', return_value=False):
    import app
import custom_upload
import theme_history as history
import theme_studio as studio
import theme_review
import tasks

results = []


def check(name, ok):
    results.append(bool(ok))
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


def rejected(call):
    try:
        call()
    except (ValueError, OSError, RuntimeError):
        return True
    return False


app.discover()
app.auto_themes.init(app)
owner = 'fixture-session'
check('Upgrade does not invent installation history for existing audio', not history.rows(app, first))
token = studio.start(app, owner, 'current', str(first))
check('Starting the editor keeps the installed theme unchanged', (first / 'theme.mp3').read_bytes() == original)
check('Editor sessions are isolated by the signed-in session', rejected(lambda: studio._find(app, token, 'other-session')))
for fields in ({'start': 'nan'}, {'end': '0'}, {'start': '100'}, {'fade_in': '5', 'fade_out': '5'}, {'end': '99999'}):
    check('Invalid clip configuration rejected: ' + str(fields), rejected(lambda fields=fields: studio.render(app, owner, token, fields)))
tasks.CONVERSION_LOCK.acquire()
try:
    check('Studio respects the single FFmpeg conversion slot', rejected(lambda: studio.render(app, owner, token, {})))
finally:
    tasks.CONVERSION_LOCK.release()
edited = studio.render(app, owner, token, {'start': '1', 'end': '4', 'fade_in': '.35', 'fade_out': '.35',
                                           'normalize': '1', 'song': 'Fixture song', 'artist': 'Fixture artist'})
preview_file = edited['stage'] / 'preview.mp3'
preview = preview_file.read_bytes()
check('Trimming, fades and normalization produce a three-second playable MP3',
      len(preview) >= 1024 and preview != original and abs(edited['preview_duration'] - 3) < .3
      and edited['options']['normalize'])
check('Waveform is measured from the real audio and changes over the fade',
      len(edited['waveform']) == 96 and max(edited['waveform']) > 0
      and edited['waveform'][0] < max(edited['waveform']) and edited['waveform'][-1] < max(edited['waveform']))
check('A rendered preview has not altered current audio', (first / 'theme.mp3').read_bytes() == original
      and not history.rows(app, first))


class H:
    def __init__(self, path, headers=None):
        self.path, self.headers = path, headers or {}
        self.code, self.body = None, ''
        self.out_headers = {}
        self.wfile = io.BytesIO()
    def send(self, code, body, ctype='text/html'):
        self.code, self.body = code, body
    def send_response(self, code):
        self.code = code
    def send_header(self, key, value):
        self.out_headers[key] = value
    def end_headers(self):
        return None


query = urllib.parse.urlencode({'token': token, 'revision': edited['revision']})
page = H('/theme-studio?' + query)
studio.page(app, page, owner)
check('Studio renders trim/fade controls, waveform and current-vs-preview players',
      page.code == 200 and all(t in page.body for t in ('Trim start', 'Fade out', 'A · Current theme', 'B · Preview', 'studio-wave', 'Install this preview')))
partial = H('/theme-studio/audio?' + query, {'Range': 'bytes=10-99'})
studio.audio(app, partial, owner)
check('Preview supports byte-range audio seeking', partial.code == 206 and partial.wfile.getvalue() == preview[10:100]
      and partial.out_headers['Content-Range'] == f'bytes 10-99/{len(preview)}')
denied = H('/theme-studio/audio?' + query)
studio.audio(app, denied, 'another-session')
check('Preview audio cannot be retrieved from another session', denied.code == 404)
check('A replacement requires explicit confirmation', rejected(lambda: studio.install(app, owner, token, edited['revision'], False)))
check('A stale preview revision cannot install', rejected(lambda: studio.install(app, owner, token, 'old-revision', True)))
with patch.object(app.media_servers, 'after_install') as refresh:
    studio.install(app, owner, token, edited['revision'], True)
    check('Studio publishes exactly the MP3 the owner heard and uses the usual targeted refresh', (first / 'theme.mp3').read_bytes() == preview and refresh.call_args.args[1] == str(first))
check('Confirmation consumes and removes the editor stage', not edited['stage'].exists() and rejected(lambda: studio._find(app, token, owner)))
entries = history.rows(app, first)
check('Successful installation records metadata, actual duration and edit settings',
      len(entries) == 1 and entries[0]['source'].startswith('Theme Studio') and entries[0]['song'] == 'Fixture song'
      and entries[0]['artist'] == 'Fixture artist' and abs(entries[0]['duration'] - 3) < .3
      and json.loads(entries[0]['details'])['start'] == 1 and entries[0]['source_id'] != token)
check('Current file is recognized by the latest recorded history', history.current(app, first) is not None)
recovery = theme_review.batches(app)
check('Only one active theme exists; its previous version is safely in the recycle bin',
      len(list(first.glob('*.mp3'))) == 1 and len(recovery) == 1 and recovery[0]['kind'] == 'replaced')
theme_review.undo(app, recovery[0]['id'])
check('Recycle bin restores the previous bytes and records the restoration',
      (first / 'theme.mp3').read_bytes() == original and len(history.rows(app, first)) == 2
      and history.rows(app, first)[0]['source'].startswith('Recycle bin'))

# Verified provider track editing when no prior theme exists.
track = {'title': 'Opening fixture', 'artist': 'Fixture band', 'slug': 'OP2', 'type': 'OP', 'version': 2, 'video_id': 77}
with patch.object(app, 'resolve_animethemes_track', return_value=track) as verify, \
        patch.object(app, 'download_verified_theme_audio', side_effect=lambda selected, dest: shutil.copyfile(fixture, dest)):
    provider_token = studio.start(app, owner, 'animethemes', str(second), 'fixture', '77')
    check('AnimeThemes Studio verifies the exact track and does not yet install it', verify.call_args.args == ('fixture', '77')
          and not (second / 'theme.mp3').exists())
candidate = studio.render(app, owner, provider_token, {'start': '0', 'end': '3'})
studio.install(app, owner, provider_token, candidate['revision'], False)
meta = history.rows(app, second)[0]
check('Provider song, artist, OP/ED number, variant and exact source ID are saved',
      meta['song'] == 'Opening fixture' and meta['artist'] == 'Fixture band' and meta['track'] == 'OP2'
      and meta['version'] == '2' and meta['source_id'] == 'fixture:77')

# A reviewed custom upload can enter Studio without a new upload, and disabling
# manual installs still prevents a later Studio install.
stage = Path(tempfile.mkdtemp(prefix='themearr-custom-upload-', dir=app.CONFIG))
(stage / 'uploaded.audio').write_bytes(original)
pending = 'fixture-upload-review'
custom_upload._PENDING[pending] = {'expiry': time.monotonic() + 300, 'stage': stage, 'folder': str(third), 'filename': 'Fixture.mp3'}
with patch.object(custom_upload, '_enabled', return_value=True):
    upload_token = studio.start(app, owner, 'upload', pending=pending)
    review = studio.render(app, owner, upload_token, {'start': '0', 'end': '2'})
with patch.object(custom_upload, '_enabled', return_value=False):
    check('Disabling manual installs cannot be bypassed with a prior Studio upload', rejected(lambda: studio.install(app, owner, upload_token, review['revision'], True))
          and not (third / 'theme.mp3').exists())
check('Cancel discards staged audio without installing a theme', studio.cancel(app, owner, upload_token) == str(third) and not review['stage'].exists())

stale = studio.start(app, owner, 'current', str(first))
clip = studio.render(app, owner, stale, {'end': '2'})
# Simulate an ordinary external replacement after the user rendered a preview.
(first / 'theme.mp3').write_bytes(preview)
check('A theme changed by someone else after preview cannot be overwritten', rejected(lambda: studio.install(app, owner, stale, clip['revision'], True))
      and (first / 'theme.mp3').read_bytes() == preview)
studio.cancel(app, owner, stale)
stale = studio.start(app, owner, 'current', str(first))
item = studio._find(app, stale, owner)
with studio._LOCK:
    studio._SESSIONS[stale]['expiry'] = time.monotonic() - 1
check('Expired editor sessions are removed with their temporary audio', rejected(lambda: studio._find(app, stale, owner)) and not item['stage'].exists())
# Move metadata with existing library-root state.
import path_moves
moved = work / 'moved-root'
path_moves.remap(app, root, moved)
check('Installation history follows a moved library root', not history.rows(app, first) and len(history.rows(app, moved / first.name)) == 2)
print(f'{sum(results)}/{len(results)} passed')
sys.exit(0 if all(results) else 1)
